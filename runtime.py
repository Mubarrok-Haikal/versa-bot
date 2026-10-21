"""Connection and timing helpers. No network traffic or OS changes at import time."""
import gc
import os
import ssl
import sys
import time
from functools import lru_cache

import requests


@lru_cache(maxsize=1)
def _windows_clock():
    import ctypes
    try:
        read = ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime
    except AttributeError:
        return time.time
    read.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    read.restype = None

    def now():
        value = ctypes.c_ulonglong()
        read(ctypes.byref(value))
        return (value.value - 116444736000000000) / 10_000_000
    return now


def wall_time():
    # Python before 3.13 uses a coarser Windows wall clock; QPC alone cannot fix that anchor.
    return _windows_clock()() if sys.platform == 'win32' else time.time()


@lru_cache(maxsize=4)
def certificate_context(bundle):
    return ssl.create_default_context(cafile=bundle if not os.path.isdir(bundle) else None,
                                      capath=bundle if os.path.isdir(bundle) else None)


class CachedTLSAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, context, **kwargs):
        self.context = context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, ssl_context=self.context, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs.setdefault('ssl_context', self.context)
        return super().proxy_manager_for(proxy, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, options = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            options['ssl_context'] = self.context
        return host, options

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if verify is True and url.lower().startswith('https'):
            # The verified context already contains the CA store; urllib3 must not reload it.
            conn.ca_certs = conn.ca_cert_dir = None


def new_session(base, response_hook, capacity=18):
    session = requests.Session()
    # Resolve environment/Windows proxy settings once, including NO_PROXY for this host.
    # Disabling trust_env also avoids registry and netrc lookups on every prepared send.
    session.proxies.update(requests.utils.get_environ_proxies(base))
    session.trust_env = False
    bundle = os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE') or requests.certs.where()
    session.mount('https://', CachedTLSAdapter(certificate_context(bundle), pool_maxsize=capacity, max_retries=0))
    session.mount('http://', requests.adapters.HTTPAdapter(pool_maxsize=capacity, max_retries=0))
    session.hooks['response'].append(response_hook)
    return session


class PrecisionWindow:
    """Restore process timing and GC settings even when a submission fails or is cancelled."""
    def __enter__(self):
        self.gc_enabled = gc.isenabled()
        self.timer_started = False
        self.throttle = None
        if self.gc_enabled:
            gc.collect()
            gc.disable()
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            self.ctypes = ctypes
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            self.process = kernel.GetCurrentProcess()
            self.timer_started = ctypes.windll.winmm.timeBeginPeriod(1) == 0

            class PowerState(ctypes.Structure):
                _fields_ = [('Version', wintypes.ULONG), ('ControlMask', wintypes.ULONG),
                            ('StateMask', wintypes.ULONG)]

            old = PowerState(1, 0, 0)
            try:
                for fn in (kernel.GetProcessInformation, kernel.SetProcessInformation):
                    fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
                    fn.restype = wintypes.BOOL
                if kernel.GetProcessInformation(self.process, 4, ctypes.byref(old), ctypes.sizeof(old)):
                    # Disable execution-speed throttling and timer-resolution suppression.
                    new = PowerState(1, old.ControlMask | 5, old.StateMask & ~5)
                    if kernel.SetProcessInformation(self.process, 4, ctypes.byref(new), ctypes.sizeof(new)):
                        self.throttle = old
            except AttributeError:  # older Windows
                pass
        return self

    def __exit__(self, *exc):
        if self.throttle is not None:
            self.ctypes.windll.kernel32.SetProcessInformation(
                self.process, 4, self.ctypes.byref(self.throttle), self.ctypes.sizeof(self.throttle))
        if self.timer_started:
            self.ctypes.windll.winmm.timeEndPeriod(1)
        if self.gc_enabled:
            gc.enable()


class AimClock:
    """Wall time for setup; a perf_counter deadline for the short timing-critical window."""
    def __init__(self, target):
        self.target = time.perf_counter() + target - wall_time()

    def relative(self):
        return time.perf_counter() - self.target
