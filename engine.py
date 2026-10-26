"""HEBAT Moodle client and scheduled submission runner."""
import html
import math
import os
import queue
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from forms import FastForm, extract_form
from runtime import AimClock, PrecisionWindow, new_session as make_session, wall_time

BASE = os.environ.get("HEBAT_BASE", "https://hebat.elearning.unair.ac.id").rstrip("/")
WIB = timezone(timedelta(hours=7))
FAST_POLL, SLOW_POLL, FAST_WINDOW = 0.5, 3.0, 120  # poll intervals and fast-poll duration, in seconds
BURST_FROM, BURST_UNTIL, BURST_GAP = 3, 5, 0.25  # burst window and interval, in seconds
PEAK_FROM, PEAK_UNTIL, PEAK_GAP = 0.4, 0.1, 0.1
LOGINS = 4  # website sessions; reduced to 2 when mobile saves are available
WS_GAP, WS_MAX = 0.025, 16  # peak mobile rate: 40/s, up to 16 concurrent requests
SAVE_CUTOFF = 0.1  # mobile burst cutoff relative to target, in seconds
PREPARE_FROM = 300
EARLY_UPLOAD_MAX = 3 * 86400
SCOUT_EVERY = 4  # load the website form every fourth check outside the peak
CHECK_TIMEOUT = (3, 8)  # connect and read timeouts, in seconds
CLOCK_PROBE = "/pix/i/loading.gif"  # static endpoint for connection warmup and HTTP Date fallback
MAX_WAIT = 30 * 60  # polling deadline relative to target

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "agu": 8,
          "agt": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "nop": 11, "dec": 12, "des": 12}
DATE_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4}),?\s+(\d{1,2})[:.](\d{2})\s*([AaPp][Mm])?")
DATE_MDY = re.compile(r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+(\d{1,2})[:.](\d{2})\s*([AaPp][Mm])?")
DAY_ONLY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4})")
OPEN_WORDS = ("open", "buka", "mulai")
DUE_WORDS = ("due", "tenggat", "batas", "jatuh tempo", "tutup", "close")
# English and Indonesian Restrict access date labels.
LOCK_FROM_WORDS = ("from", "after", "dari", "setelah", "sejak", "mulai")
LOCK_UNTIL_WORDS = ("until", "before", "sampai", "hingga", "sebelum")
STATE_CLASS = re.compile(r"^submissionstatus(submitted|draft|new|reopened)$")
STATE_LABELS = {"submitted": "Submitted", "draft": "Draft (not submitted)", "reopened": "Reopened", "new": "No attempt"}


class HebatError(Exception):
    """Error message displayed directly in the UI."""


class WsError(HebatError):
    """Mobile API error with a Moodle error code."""
    def __init__(self, code, message):
        super().__init__(message or code)
        self.code = code


class Stopped(Exception):
    pass


def parse(text):
    return BeautifulSoup(text, "html.parser")


def fmt(dt):
    return dt.astimezone(WIB).strftime("%a %d %b %Y, %H:%M")


def human(seconds):
    s = int(max(seconds, 0))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m" if h else f"{m}m {s % 60}s"


def parse_date(text):
    """Parse English or Indonesian Moodle dates in WIB."""
    if m := DATE_DMY.search(text):
        day, mon, year, hh, mm, ampm = m.groups()
    elif m := DATE_MDY.search(text):
        mon, day, year, hh, mm, ampm = m.groups()
    else:
        return None
    month = MONTHS.get(mon[:3].lower())
    hour = int(hh) % 12 + (12 if ampm.lower() == "pm" else 0) if ampm else int(hh)
    try:
        return datetime(int(year), month, int(day), hour, int(mm), tzinfo=WIB) if month else None
    except ValueError:
        return None


def lock_dates(box):
    """Read opening and closing dates from a Restrict access notice."""
    unlocks = locks = None
    for m in re.finditer(r"([^<>]{0,60})<strong>(.*?)</strong>", str(box), re.S):
        words, text = m.group(1).lower(), html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        when = parse_date(text)
        if not when and (d := DAY_ONLY.search(text)) and (month := MONTHS.get(d.group(2)[:3].lower())):
            try:
                when = datetime(int(d.group(3)), month, int(d.group(1)), tzinfo=WIB)
            except ValueError:
                continue
            if "end of" in words or "akhir" in words:
                when += timedelta(days=1)
        if not when:
            continue
        if any(w in words for w in LOCK_UNTIL_WORDS):
            locks = locks or when
        elif any(w in words for w in LOCK_FROM_WORDS):
            unlocks = unlocks or when
    return unlocks, locks


def find_form(page, action):
    """Find a Moodle form by its hidden action field."""
    marker = page.find("input", {"name": "action", "value": action})
    return marker.find_parent("form") if marker else None


def find_form_fast(text, action):
    return extract_form(text, action)


def accepted(r):
    """Check for a redirect to the assignment page after a successful form POST."""
    if r.is_redirect:
        return "/mod/assign/view.php" in r.headers.get("Location", "")
    return bool(re.search(r'http-equiv="refresh"[^>]*/mod/assign/view\.php', r.text))  # redirect page, debug mode


def landed(state, before):
    """Check whether the submission state changed after a save."""
    return state == "submitted" or (state == "draft" and before != "draft")


def flatten(out, value, key=""):
    """Encode nested parameters using Moodle REST bracket notation."""
    if not isinstance(value, (dict, list)):
        out[key] = value
        return out
    for k, v in (value.items() if isinstance(value, dict) else enumerate(value)):
        flatten(out, v, f"{key}[{k}]" if key else str(k))
    return out


def text_html(online_text):
    return "".join(f"<p>{html.escape(line)}</p>" for line in online_text.splitlines() if line.strip())


def form_data(form):
    """Collect successful controls for the primary form action."""
    if isinstance(form, FastForm):
        return form.data.copy()
    data = {}
    for el in form.find_all(["input", "textarea", "select", "button"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "textarea":
            data[name] = el.text
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            if opt is not None:
                data[name] = opt.get("value", opt.text)
        else:
            kind = (el.get("type") or ("submit" if el.name == "button" else "text")).lower()
            if kind == "submit":
                if name == "submitbutton":
                    data[name] = el.get("value", "")
                continue
            if kind in ("button", "image", "reset", "file"):
                continue
            if kind in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            data[name] = el.get("value", "")
    return data


def submission_state(page):
    """Read submitted, draft, new, or reopened from the status CSS class; otherwise return None."""
    el = page.find(class_=STATE_CLASS)
    if el:
        for c in el.get("class", []):
            if m := STATE_CLASS.match(c):
                return m.group(1)
    return None


def overloaded(r, *args, **kwargs):
    """Raise transport errors for HTTP failures and proxy challenge pages."""
    if r.status_code >= 400:
        raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
    if 'text/html' in r.headers.get('Content-Type', '').lower() and re.search(
            r'cf-chl-|<title>\s*(?:Access Denied|Just a moment|Too Many Requests)|id=[\"\']captcha', r.text, re.I):
        raise requests.HTTPError("HEBAT returned a blocking/challenge page", response=r)


def keep_awake(on):
    """Toggle the Windows sleep inhibitor for the calling thread."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


def new_session(capacity=18):
    return make_session(BASE, overloaded, capacity)


class Hebat:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.s = new_session()
        self.ws_s = new_session(WS_MAX + 2)
        self.token = None
        self.sesskey = None
        self.user_id = None
        self.logged_in_at = 0.0
        self.login_lock = threading.Lock()
        self.ws_functions = set()
        self.closed_notices = set()
        self.clock_source = None
        self.rtt = 0.15
        self.closed_until = 0.0

    def login(self):
        with self.login_lock:
            return self._login()

    def relogin(self):
        started = wall_time()
        with self.login_lock:
            if self.logged_in_at < started:  # Another thread may have refreshed the session while waiting for the lock.
                self._login()

    def alive(self):
        """Check session validity; Moodle may expire older concurrent logins."""
        try:
            self.ajax("core_session_touch", {}, retry=False)
            return True
        except (HebatError, requests.RequestException):
            return False

    def _login(self):
        candidate = new_session()
        try:
            page = parse(candidate.get(BASE + "/login/index.php", timeout=20).text)
            token = page.find("input", {"name": "logintoken"})
            r = candidate.post(BASE + "/login/index.php", timeout=30, data={
                "username": self.username, "password": self.password,
                "logintoken": token["value"] if token else "", "anchor": "",
            })
            user = re.search(r'"userId"\s*:\s*(\d+)', r.text)
            sesskey = re.search(r'"sesskey"\s*:\s*"([^"]+)"', r.text)
            if not user or user.group(1) == "0" or not sesskey:
                raise HebatError("Login failed. Check your NIM and password (same as UNAIR Satu).")
        except Exception:
            candidate.close()
            raise
        old, self.s = self.s, candidate
        self.sesskey, self.user_id = sesskey.group(1), int(user.group(1))
        self.logged_in_at = wall_time()
        old.close()
        return self.user_id

    def get(self, path, timeout=20):
        r = self.s.get(BASE + path, timeout=timeout)
        if "/login/index.php" in r.url:  # Refresh an expired session and retry once.
            self.relogin()
            r = self.s.get(BASE + path, timeout=timeout)
        return r

    def post(self, path, timeout=60, **kw):
        return self.s.post(BASE + path, timeout=timeout, **kw)

    def ajax(self, method, args, retry=True):
        r = self.s.post(BASE + "/lib/ajax/service.php", params={"sesskey": self.sesskey, "info": method},
                        json=[{"index": 0, "methodname": method, "args": args}], timeout=20)
        try:
            res = r.json()
        except ValueError:
            raise HebatError(f"HEBAT gave an unexpected answer (HTTP {r.status_code}).")
        if isinstance(res, dict):  # Top-level API error, usually an expired session.
            if retry and res.get("errorcode") in ("invalidsesskey", "requireloginerror", "servicerequireslogin"):
                self.relogin()
                return self.ajax(method, args, retry=False)
            raise HebatError(res.get("error") or "HEBAT refused the request.")
        if res[0].get("error"):
            raise HebatError(res[0].get("exception", {}).get("message") or "HEBAT refused the request.")
        return res[0]["data"]

    def clock_offset(self):
        """Read the PHP clock through the calendar API, falling back to HTTP Date."""
        if self.token and 'core_calendar_get_calendar_upcoming_view' in self.ws_functions:
            try:
                def php_time():
                    res = self.ws('core_calendar_get_calendar_upcoming_view', timeout=(1, 1.5))
                    return int(res['date']['timestamp'])
                offset = self.measure_clock(php_time)
                if offset is not None:
                    self.clock_source = 'Moodle/PHP'
                    return offset
            except (HebatError, requests.RequestException, KeyError, TypeError, ValueError):
                pass
        with new_session() as session:
            def header_time():
                try:
                    r = session.head(BASE + CLOCK_PROBE, timeout=(1, 1.5))
                except requests.HTTPError as e:  # An error page (e.g. 404) still carries the server's Date.
                    if e.response is None or 'Date' not in e.response.headers:
                        raise
                    r = e.response
                return parsedate_to_datetime(r.headers['Date']).timestamp()
            try:
                offset = self.measure_clock(header_time)
            except (requests.RequestException, KeyError, TypeError, ValueError):
                return None
        self.clock_source = 'HTTP Date (fallback; proxy clock may differ)'
        return offset

    def measure_clock(self, read):
        """Estimate clock offset from whole-second samples, preferring second transitions."""
        last, flips, rough = None, [], []
        end = time.perf_counter() + 3
        while time.perf_counter() < end and len(flips) < 2 and len(rough) < 30:
            t0 = wall_time()
            start = time.perf_counter()
            sec = read()
            elapsed = time.perf_counter() - start
            mid = t0 + elapsed / 2
            self.rtt = elapsed
            rough.append((elapsed, sec + 0.5 - mid))
            if last and sec - last[1] == 1 and mid - last[0] < 0.3:  # Consecutive samples bracket a second boundary.
                flips.append(sec - (last[0] + mid) / 2)
            last = (mid, sec)
            time.sleep(0.025)
        if flips:
            return sum(flips) / len(flips)
        return min(rough)[1] if rough else None

    def ws_login(self):
        """Get a mobile API token, or return None if the service is unavailable."""
        try:
            res = self.ws_s.post(BASE + "/login/token.php", timeout=20, data={
                "username": self.username, "password": self.password, "service": "moodle_mobile_app"}).json()
        except (requests.RequestException, ValueError):
            return None
        if isinstance(res, dict) and res.get('token'):
            self.token = res['token']
        return self.token

    def prepare_ws(self, function, **params):
        data = flatten({"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}, params)
        return self.ws_s.prepare_request(requests.Request('POST', BASE + '/webservice/rest/server.php', data=data))

    def send_ws(self, prepared, timeout=CHECK_TIMEOUT):
        r = self.ws_s.send(prepared, timeout=timeout, allow_redirects=False)
        if r.is_redirect:
            raise requests.HTTPError('Mobile API redirected instead of answering', response=r)
        try:
            res = r.json()
        except ValueError:
            raise requests.HTTPError(f"HEBAT returned a non-JSON answer (HTTP {r.status_code})", response=r)
        if isinstance(res, dict) and ("exception" in res or 'errorcode' in res):
            if res.get('errorcode') in ('ratelimitexceeded', 'toomanyrequests', 'sitemaintenance', 'servermaintenance'):
                raise requests.HTTPError('HEBAT is rate-limited or unavailable', response=r)
            raise WsError(res.get("errorcode"), res.get("message"))
        return res

    def ws(self, function, timeout=CHECK_TIMEOUT, **params):
        """Call the mobile REST API without taking a website session lock."""
        return self.send_ws(self.prepare_ws(function, **params), timeout)

    def ws_assignment(self, cmid, course_id):
        """Load the assignment instance ID, draft setting, and available submission rules."""
        if not (self.token or self.ws_login()):
            return None
        site = self.ws("core_webservice_get_site_info")
        self.ws_functions = {f['name'] for f in site.get('functions', [])}
        if "mod_assign_save_submission" not in self.ws_functions:
            return None
        info = None
        for section in self.ws("core_course_get_contents", courseid=course_id,
                               options=[{"name": "cmid", "value": cmid}]):
            for m in section.get("modules", []):
                if m.get("id") == cmid:
                    info = {"id": m["instance"], "drafts": None}
        if info:  # Restricted assignments may be omitted from the metadata response.
            for course in self.ws("mod_assign_get_assignments", courseids=[course_id]).get("courses", []):
                for a in course.get("assignments", []):
                    if a.get("cmid") == cmid:
                        info.update(drafts=bool(int(a.get("submissiondrafts", 0))), rules=a,
                                    usermaxuploadfilesize=site.get('usermaxuploadfilesize'))
            if info['drafts'] is not False and 'mod_assign_submit_for_grading' not in self.ws_functions:
                info['save_only'] = True
            # Older Moodle emits this localized notice under a generic warning code.
            try:
                strings = self.ws('core_get_strings', strings=[{'stringid': 'duedatereached', 'component': 'assign'}])
                self.closed_notices = {s['string'] for s in strings}
            except (HebatError, requests.RequestException, KeyError, TypeError):
                self.closed_notices = {'The due date for this assignment has now passed'}
        return info

    def prepare_save(self, assign_id, draft_itemid, online_text):
        """Prepare the mobile save request before polling starts."""
        plugindata = {}
        if draft_itemid is not None:
            plugindata["files_filemanager"] = draft_itemid
        if online_text:
            plugindata["onlinetext_editor"] = {"text": text_html(online_text), "format": 1,
                                               "itemid": random.randint(100_000_000, 999_999_999)}
        return self.prepare_ws('mod_assign_save_submission', assignmentid=assign_id, plugindata=plugindata)

    def save_reply(self, res, closed_until=0, checked_at=None):
        if res == []:
            return True
        if isinstance(res, list) and res and all(isinstance(w, dict) for w in res):
            def closed(w):
                code = w.get('warningcode')
                return code == 'submissionnotopen' or (code == 'couldnotsavesubmission'
                    and html.unescape(w.get('item', '')) in self.closed_notices
                    and (wall_time() if checked_at is None else checked_at) < closed_until)
            if all(closed(w) for w in res):
                return False
            detail = '; '.join(f"{w.get('warningcode', 'warning')}: {html.unescape(w.get('item') or w.get('message') or '')}"
                               for w in res)
            raise WsError('saverefused', 'HEBAT refused the save: ' + detail)
        raise WsError('badanswer', 'HEBAT returned an unexpected save result.')

    def ws_save(self, assign_id, draft_itemid, online_text, closed_until=0):
        request = self.prepare_save(assign_id, draft_itemid, online_text)
        checked_at = wall_time()
        return self.save_reply(self.send_ws(request), closed_until, checked_at)

    def ws_submit(self, assign_id):
        """Submit a saved attempt for grading through the mobile API."""
        res = self.ws("mod_assign_submit_for_grading", assignmentid=assign_id, acceptsubmissionstatement=1)
        if res != []:
            raise WsError('submitrefused', f"HEBAT refused 'Submit assignment': {res}")
        return True

    def ws_status(self, assign_id, team=False):
        res = self.ws('mod_assign_get_submission_status', assignid=assign_id)
        if not isinstance(res, dict) or res.get('warnings') or 'lastattempt' not in res:
            raise WsError('statusunavailable', 'HEBAT did not return an assignment status.')
        attempt = res['lastattempt']
        sub = attempt.get('teamsubmission' if team else 'submission') or {}
        return {'state': sub.get('status'), 'modified': sub.get('timemodified'),
                'timelimit': attempt.get('timelimit', 0)}

    def courses(self):
        """List in-progress courses, falling back to all enrolled courses."""
        for classification in ("inprogress", "all"):
            try:
                found = self.ajax("core_course_get_enrolled_courses_by_timeline_classification", {
                    "offset": 0, "limit": 0, "classification": classification, "sort": "fullname"})["courses"]
            except HebatError:
                break
            if found:
                return [{"id": c["id"], "name": parse(c.get("fullname") or c.get("shortname") or "").get_text(),
                         "category": c.get("coursecategory") or ""} for c in found]
        return self.courses_from_profile()

    def courses_from_profile(self):
        """Read course links from the profile page on older Moodle versions."""
        found = {}
        for a in parse(self.get("/user/profile.php").text).select("a[href]"):
            href = a["href"]
            m = re.search(r"[?&]course=(\d+)", href) if "/user/view.php" in href else re.search(r"/course/view\.php\?id=(\d+)", href)
            name = a.get_text(" ", strip=True)
            if m and int(m.group(1)) != 1 and name:
                found.setdefault(int(m.group(1)), name)
        return [{"id": cid, "name": name, "category": ""} for cid, name in found.items()]

    def assignments(self, course_id):
        """Read assignments from the course page, including restricted activities omitted by the index."""
        page = parse(self.get(f"/course/view.php?id={course_id}").text)
        names, locked = {}, {}
        for li in page.select("li.activity.modtype_assign"):
            m = re.search(r"\d+", li.get("data-id") or li.get("id") or "")
            if not m:
                continue
            cmid = int(m.group())
            card = li.select_one("[data-activityname]")
            label = li.select_one(".instancename")
            if label:
                for hidden in label.select(".accesshide"):
                    hidden.extract()
            names[cmid] = (card and card["data-activityname"]) or (label and label.get_text(" ", strip=True)) or f"Assignment {cmid}"
            if not li.select_one('a[href*="/mod/assign/view.php"]'):
                locked[cmid] = li.select_one(".availabilityinfo")
        if not names:  # course formats that don't list activities on the main page
            for a in parse(self.get(f"/mod/assign/index.php?id={course_id}").text).select("a[href]"):
                if m := re.search(r"(?:^|/mod/assign/)view\.php\?id=(\d+)", a["href"]):
                    names.setdefault(int(m.group(1)), a.get_text(" ", strip=True))

        def info(cmid):
            if cmid in locked:
                return {**self.locked_info(cmid, locked[cmid]), "name": names[cmid]}
            try:
                return {**self.assignment_info(cmid), "name": names[cmid]}
            except requests.RequestException as e:
                return {"cmid": cmid, "name": names[cmid], "error": f"Couldn't load ({e.__class__.__name__})"}

        with ThreadPoolExecutor(4) as pool:
            return list(pool.map(info, names))

    @staticmethod
    def locked_info(cmid, box):
        """Build assignment metadata from the course restriction notice."""
        unlocks, locks = lock_dates(box) if box else (None, None)
        now = datetime.now(WIB)
        if unlocks and unlocks > now:
            availability = "upcoming"
        elif locks and locks <= now:
            availability = "closed"
        else:
            availability = "locked"  # Non-date restriction, such as group membership or activity completion.
        return {
            "cmid": cmid, "title": "",
            "opens_ts": unlocks.timestamp() if unlocks else None, "opens": fmt(unlocks) if unlocks else None,
            "due_ts": None, "due": None,
            "availability": availability, "state": None, "state_label": "Locked",
            "lock_note": box.get_text(" ", strip=True) if box else "Locked",
        }

    def assignment_info(self, cmid):
        page = parse(self.get(f"/mod/assign/view.php?id={cmid}").text)
        lines = []
        region = page.select_one('[data-region="activity-dates"]')
        if region:
            lines += [d.get_text(" ", strip=True) for d in region.find_all("div") if not d.find("div")]
        for tr in page.select("table tr"):  # older Moodle puts the due date in the status table
            th, td = tr.find("th"), tr.find("td")
            if th and td:
                lines.append(f"{th.get_text(' ', strip=True)}: {td.get_text(' ', strip=True)}")
        opens = due = None
        for line in lines:
            label = line.split(":", 1)[0].lower()
            when = parse_date(line)
            if when and not opens and any(w in label for w in OPEN_WORDS):
                opens = when
            elif when and not due and any(w in label for w in DUE_WORDS):
                due = when
        if not opens and (m := re.search(r"(?:submissions from|pengumpulan dari)(.{0,80})", page.get_text(" "), re.I)):
            opens = parse_date(m.group(1))

        open_now = find_form(page, "editsubmission") is not None
        state = submission_state(page)
        if open_now:
            availability = "open"
        elif opens and opens > datetime.now(WIB):
            availability = "upcoming"
        else:
            availability = "closed"
        return {
            "cmid": cmid,
            "title": page.title.get_text(strip=True) if page.title else "",
            "opens_ts": opens.timestamp() if opens else None, "opens": fmt(opens) if opens else None,
            "due_ts": due.timestamp() if due else None, "due": fmt(due) if due else None,
            "availability": availability, "state": state, "state_label": STATE_LABELS.get(state, "No attempt"),
        }

    def upload_repo_id(self, page_html=None):
        """Find the upload repository ID in a page with a file picker."""
        page_html = page_html or self.get("/user/files.php").text
        m = re.search(r'"id":"?(\d+)"?,"name":"(?:[^"\\]|\\.)*","type":"upload"', page_html)
        return int(m.group(1)) if m else None

    def upload_draft(self, paths, repo_id):
        """Upload files to a new private draft area and return its item ID."""
        itemid = random.randint(100_000_000, 999_999_999)
        for p in paths:
            with open(p, "rb") as fh:
                r = self.post("/repository/repository_ajax.php?action=upload", data={
                    "sesskey": self.sesskey, "repo_id": repo_id, "itemid": itemid,
                    "savepath": "/", "title": p.name,
                }, files={"repo_upload_file": (p.name, fh)})
            try:
                res = r.json()
            except ValueError:
                raise HebatError(f"Upload of {p.name} failed: HTTP {r.status_code}")
            if "error" in res:
                raise HebatError(f"Upload of {p.name} failed: {res['error']}")
        return itemid

    def edit_form(self, cmid):
        """Load the submission form, or return None while access is restricted."""
        url = f"{BASE}/mod/assign/view.php?id={cmid}&action=editsubmission"
        checked_at = wall_time()
        r = self.s.get(url, timeout=CHECK_TIMEOUT, allow_redirects=False)
        if r.is_redirect and "/login/index.php" in r.headers.get("Location", ""):
            self.relogin()
            r = self.s.get(url, timeout=CHECK_TIMEOUT, allow_redirects=False)
        form = find_form_fast(r.text, "savesubmission")
        if not form:
            self.check_web_refusal(r, checked_at)
        return form, r

    def check_web_refusal(self, r, checked_at=None):
        """Reject validation errors, login pages, and unsupported timed attempts."""
        if r.is_redirect:
            path = urlparse(r.headers.get('Location', '')).path
            if path.endswith('/course/view.php') or path.endswith('/mod/assign/view.php'):
                return
            raise HebatError('HEBAT redirected the submission to an unexpected page. Check your login/access.')
        if re.search(r'(?:name=[\"\']startsubmission|action=startsubmission|mod_assign_timelimit)', r.text, re.I):
            raise HebatError('This is a timed assignment. Start and submit the attempt on HEBAT manually.')
        errors = re.findall(r'<[^>]+class=[\"\'][^\"\']*(?:alert-danger|invalid-feedback|errormessage)[^\"\']*[\"\'][^>]*>(.*?)</',
                            r.text, re.I | re.S)
        errors = [html.unescape(re.sub('<[^>]+>', '', e)).strip() for e in errors]
        if any(errors):
            raise HebatError('HEBAT refused the submission: ' + '; '.join(filter(None, errors)))
        if '/login/index.php' in r.url or re.search(r'name=[\"\']logintoken[\"\']', r.text):
            raise HebatError('HEBAT requires a fresh login; the submission was not accepted.')
        if re.search(r'submissionnotopen|notavailable|availabilityinfo|submissions from|Available from|pengumpulan dari', r.text, re.I):
            return
        if (wall_time() if checked_at is None else checked_at) < self.closed_until and re.search(r'submissionstatus|activity-dates', r.text, re.I):
            return
        raise HebatError('HEBAT did not offer or accept the submission form, and did not report that it is not open yet. Check HEBAT now.')

    def prepare_blind_save(self, cmid, draft_itemid, online_text, first_only=False):
        """Prepare a website save with an optional stale lastmodified guard against repeat saves."""
        data = {"id": cmid, "userid": self.user_id, "action": "savesubmission", "sesskey": self.sesskey,
                "_qf__mod_assign_submission_form": "1", "submissionstatement": "1", "submitbutton": "Save changes"}
        if first_only:
            data["lastmodified"] = 1
        if draft_itemid is not None:
            data["files_filemanager"] = draft_itemid
        if online_text:
            data.update({"onlinetext_editor[text]": text_html(online_text), "onlinetext_editor[format]": "1",
                         "onlinetext_editor[itemid]": random.randint(100_000_000, 999_999_999)})
        return self.s.prepare_request(requests.Request('POST', BASE + '/mod/assign/view.php', data=data))

    def blind_save(self, cmid, draft_itemid, online_text, first_only=False, prepared=None):
        """Return (saved, form, response), preserving the form when Moodle requests additional fields."""
        prepared = prepared or self.prepare_blind_save(cmid, draft_itemid, online_text, first_only)
        checked_at = wall_time()
        r = self.s.send(prepared, timeout=CHECK_TIMEOUT, allow_redirects=False)
        if accepted(r):
            return True, None, r
        if r.is_redirect and "/login/index.php" in r.headers.get("Location", ""):
            self.relogin()
        form = find_form_fast(r.text, "savesubmission")
        if not form:
            self.check_web_refusal(r, checked_at)
        return False, form, r

    def save(self, cmid, form, draft_itemid, online_text, log, before=None):
        """Save the form and raise an error if Moodle rejects the submission."""
        data = form_data(form)
        takes_files, takes_text = "files_filemanager" in data, "onlinetext_editor[text]" in data
        if draft_itemid is not None:
            if takes_files:
                data["files_filemanager"] = str(draft_itemid)
            else:
                raise HebatError("This assignment doesn't accept files. Nothing was saved.")
        if online_text:
            if takes_text:
                data["onlinetext_editor[text]"] = text_html(online_text)
            else:
                raise HebatError("This assignment doesn't accept online text. Nothing was saved.")
        if not ((draft_itemid is not None and takes_files) or (online_text and takes_text)):
            raise HebatError("This assignment doesn't accept what you gave the bot (files or online text). Submit by hand!")
        if ('submissionstatement' in form.controls if isinstance(form, FastForm)
                else form.find(attrs={"name": "submissionstatement"})):
            data["submissionstatement"] = "1"

        r = self.post("/mod/assign/view.php", timeout=(3, 20), data=data, allow_redirects=False)  # skip the next page
        if accepted(r):
            return
        page = parse(r.text)
        if find_form(page, "savesubmission"):  # Moodle shows the form again when it rejects it
            # A concurrent save can invalidate this form.
            if landed(self.assignment_info(cmid)["state"], before):
                return
            errors = [e.get_text(" ", strip=True) for e in page.select(".invalid-feedback, .alert-danger, .error")]
            raise HebatError("HEBAT rejected the submission: " + ("; ".join(filter(None, errors)) or "no reason shown"))
        state = submission_state(page) or self.assignment_info(cmid)["state"]
        if state not in ("draft", "submitted"):
            raise HebatError(f"HEBAT didn't record the submission (status: {STATE_LABELS.get(state, 'no attempt')}). "
                             "Check HEBAT now and submit by hand!")

    def submit_for_grading(self, cmid):
        """Post the grading confirmation without loading its form first."""
        return accepted(self.post("/mod/assign/view.php", timeout=CHECK_TIMEOUT, allow_redirects=False, data={
            "id": cmid, "action": "confirmsubmit", "sesskey": self.sesskey,
            "_qf__mod_assign_confirm_submission_form": "1", "submissionstatement": "1", "submitbutton": "Continue"}))

    def finalize(self, cmid):
        """Load and submit the grading confirmation form for a saved draft."""
        conf = find_form(parse(self.get(f"/mod/assign/view.php?id={cmid}&action=submit").text), "confirmsubmit")
        if not conf:
            raise HebatError("Saved as DRAFT, but the 'Submit assignment' step wasn't found. Submit it by hand!")
        data = form_data(conf)
        if conf.find(attrs={"name": "submissionstatement"}):
            data["submissionstatement"] = "1"
        self.post("/mod/assign/view.php", data=data, allow_redirects=False)
        if self.assignment_info(cmid)["state"] != "submitted":
            raise HebatError("Saved as DRAFT, but 'Submit assignment' didn't go through. Submit it by hand!")


class Runner(threading.Thread):
    """Schedule a submission and confirm its final state.

    job contains cmid, name, course, course_id, at (WIB datetime), files, online_text, and finalize."""

    def __init__(self, bot, job, log):
        super().__init__(daemon=True)
        self.bot, self.job, self.log = bot, job, log
        self.stop_flag = threading.Event()
        self.state = "starting"  # starting, waiting, polling, submitting, done, failed, stopped
        self.go = None  # Target timestamp adjusted to the local clock.
        self.result = None
        self.last_hiccup = 0.0
        self.bots = [bot]  # One website session per concurrent check.
        self.ws_info = None  # Mobile API assignment metadata.
        self.ws_limit = 6
        self.before_status = None
        self.saved_ack = threading.Event()
        self.submit_ack = threading.Event()
        self.stop_sends = threading.Event()
        self.save_lock = threading.Lock()
        self.save_request = self.submit_request = None
        self.web_submit_requests = {}
        self.web_save_requests = {}
        self.closed_until = 0
        self.aim = None
        self.offset = 0.0

    def stop(self):
        self.stop_flag.set()

    def pause(self, seconds):
        if self.stop_flag.wait(max(seconds, 0)):
            raise Stopped

    def sleep_until(self, ts):
        # Recheck wall time periodically to account for suspend/resume.
        while (left := ts - wall_time()) > 0:
            self.pause(min(left, 20))

    def run(self):
        keep_awake(True)
        try:
            self.work()
        except Stopped:
            self.state, self.result = "stopped", "Stopped. Any request already sent may still have been saved; check HEBAT."
            self.log(self.result)
        except (HebatError, requests.RequestException) as e:
            self.state, self.result = "failed", str(e) if isinstance(e, HebatError) else f"Network error: {e}"
            self.log(f"FAILED: {self.result}")
        except Exception as e:  # Report unexpected failures to the UI.
            self.state, self.result = "failed", f"Unexpected error: {e!r}. Submit by hand!"
            self.log(f"FAILED: {self.result}")
        finally:
            keep_awake(False)

    def soft(self, what, fn):
        """Run an optional setup step, tolerating network failures."""
        try:
            return fn()
        except requests.RequestException as e:
            self.log(f"{what}: HEBAT didn't answer ({e.__class__.__name__}). Carrying on.")
            return None

    def check_clock(self):
        offset = self.bot.clock_offset()
        if offset is None:
            self.log("Couldn't re-check the clock; keeping the previous correction (or the PC clock if none).")
            return
        self.offset = offset
        self.go = self.job["at"].timestamp() - offset
        self.log(f"Your clock vs HEBAT: {offset:+.3f}s via {self.bot.clock_source} (compensating)")

    def fresh_session(self):
        """Refresh the login before the opening, retaining the old session on failure."""
        while wall_time() < self.go - 60:
            try:
                self.bot.login()
                self.log("Logged in again for a fresh session.")
                return
            except requests.RequestException as e:
                self.log(f"Logging in again: HEBAT didn't answer ({e.__class__.__name__}). Retrying.")
                self.pause(5)
            except HebatError as e:
                self.log(f"Fresh login failed ({e}); keeping the previous working session.")
                return

    def more_logins(self, count):
        """Create independent website sessions to avoid Moodle session-lock contention.

        Drop sessions that Moodle immediately expires due to its concurrent-login limit."""
        def login():
            bot = Hebat(self.bot.username, self.bot.password)
            bot.login()
            return bot

        pool = ThreadPoolExecutor(count)
        done, _ = wait([pool.submit(login) for _ in range(count - 1)], timeout=max(self.go - 25 - wall_time(), 0))
        bots = [self.bot] + [f.result() for f in done if not f.exception()]
        checks = {pool.submit(b.alive): b for b in bots}
        done, _ = wait(checks, timeout=max(self.go - 15 - wall_time(), 0))
        pool.shutdown(wait=False, cancel_futures=True)  # Exclude sessions that missed the setup deadline.
        self.bots = [checks[f] for f in done if f.result()] or [self.bot]
        self.log(f"Ready with {len(self.bots)} separate logins for checking.")

    def setup_ws(self):
        """Detect mobile API support for this assignment."""
        course_id = self.job.get("course_id")
        try:
            self.ws_info = course_id and self.bot.ws_assignment(self.job["cmid"], course_id) or None
        except (HebatError, requests.RequestException):
            self.ws_info = None
        if self.ws_info and self.ws_info.get('rules'):
            self.log('HEBAT offers the mobile-app route: using it for quicker, lighter checks.')
        elif self.ws_info:
            self.log('Assignment rules are still hidden. Using website saves until HEBAT exposes the form.')
        else:
            self.log("HEBAT's mobile-app route isn't available here, so the bot uses the website.")

    def status(self, bot=None):
        if self.ws_info and 'mod_assign_get_submission_status' in self.bot.ws_functions:
            try:
                rules = self.ws_info.get('rules', {})
                return self.bot.ws_status(self.ws_info['id'], bool(int(rules.get('teamsubmission', 0))))
            except (WsError, requests.RequestException):
                pass
        return (bot or self.bot).assignment_info(self.job['cmid'])

    def preflight(self):
        files, online = self.job['files'], self.job['online_text']
        names = [p.name.casefold() for p in files]
        if len(names) != len(set(names)):
            raise HebatError('Two files have the same name; rename one before starting.')
        for path in files:
            if not path.is_file() or path.stat().st_size == 0:
                raise HebatError(f'File is missing or empty: {path.name}')
        rules = (self.ws_info or {}).get('rules')
        if not rules:
            opens = (self.before_status or {}).get('opens_ts')
            self.bot.closed_until = float(opens) - self.offset if opens else 0
            try:
                form, _ = self.bot.edit_form(self.job['cmid'])
            except requests.RequestException as e:
                # Before opening, HEBAT may refuse the form page outright (HTTP 404); polling retries it.
                self.log(f"Couldn't load the submission form yet ({e}); HEBAT will check it when saving.")
                form = None
            if form:
                data = form_data(form)
                if files and 'files_filemanager' not in data:
                    raise HebatError('This assignment does not accept files.')
                if online and 'onlinetext_editor[text]' not in data:
                    raise HebatError('This assignment does not accept online text.')
            self.log('Warning: HEBAT has not exposed all assignment limits yet. File type/size/count and timer rules could not all be checked.')
            return
        if int(rules.get('timelimit', 0)):
            raise HebatError('This is a timed assignment. Start and submit the timed attempt on HEBAT manually.')
        configs = {}
        for c in rules.get('configs', []):
            if c.get('subtype') == 'assignsubmission':
                configs.setdefault(c['plugin'], {})[c['name']] = c['value']
        if files and ('file' not in configs or str(configs['file'].get('enabled', '1')) == '0'):
            raise HebatError('This assignment does not accept files.')
        if online and ('onlinetext' not in configs or str(configs['onlinetext'].get('enabled', '1')) == '0'):
            raise HebatError('This assignment does not accept online text.')
        config = configs.get('file', {})
        maxfiles = int(config.get('maxfilesubmissions', -1))
        if files and maxfiles >= 0 and len(files) > maxfiles:
            raise HebatError(f'This assignment allows {maxfiles} file(s); you selected {len(files)}.')
        limits = [int(v) for v in (config.get('maxsubmissionsizebytes'), self.ws_info.get('usermaxuploadfilesize')) if v and int(v) > 0]
        for path in files:
            if limits and path.stat().st_size > min(limits):
                raise HebatError(f'{path.name} exceeds the {min(limits):,}-byte upload limit.')
        types = config.get('filetypeslist', '')
        if files and types and types != '*':
            allowed = set(re.split(r'[\s,;]+', types.lower())) - {''}
            if any(not t.startswith('.') for t in allowed):
                try:
                    browser = self.bot.ajax('core_form_get_filetypes_browser_data', {'onlytypes': types})
                    def extensions(value):
                        if isinstance(value, dict):
                            if str(value.get('key', '')).startswith('.'):
                                yield value['key'].lower()
                            if 'ext' in value and 'types' not in value:
                                yield '.' + value['ext'].lower().lstrip('.')
                            for item in value.values():
                                yield from extensions(item)
                        elif isinstance(value, list):
                            for item in value:
                                yield from extensions(item)
                    expanded = set(extensions(browser))
                    if expanded:
                        allowed = expanded
                    else:
                        raise HebatError('No file extensions returned')
                except (HebatError, requests.RequestException):
                    self.log(f'Warning: could not expand HEBAT file type groups ({types}); HEBAT must validate these on save.')
                    allowed = None
            if allowed:
                for path in files:
                    if not any(path.name.lower().endswith(ext) for ext in allowed):
                        raise HebatError(f'{path.name} has a refused file type. Allowed: {types}')
        text_config = configs.get('onlinetext', {})
        if online and str(text_config.get('wordlimitenabled', '0')) == '1':
            limit = int(text_config.get('wordlimit', 0))
            if len(online.split()) > limit:
                raise HebatError(f'Online text exceeds the assignment limit of {limit} words.')
        self.log('Assignment preflight passed: submission types, file limits and timed-attempt rules checked.')

    def prepare_requests(self, draft):
        opens = (self.before_status or {}).get('opens_ts')
        self.closed_until = float(opens) - self.offset if opens else 0
        if self.ws_info:
            self.save_request = self.bot.prepare_save(self.ws_info['id'], draft, self.job['online_text'])
            self.submit_request = self.bot.prepare_ws('mod_assign_submit_for_grading',
                assignmentid=self.ws_info['id'], acceptsubmissionstatement=1)
            opens = self.ws_info.get('rules', {}).get('allowsubmissionsfromdate')
            if opens:
                self.closed_until = float(opens) - self.offset
        for bot in self.bots:
            bot.closed_until = self.closed_until
            self.web_save_requests[bot] = bot.prepare_blind_save(self.job['cmid'], draft, self.job['online_text'],
                first_only=self.before_status is not None and self.before_status.get('state') in (None, 'new'))
            self.web_submit_requests[bot] = bot.s.prepare_request(requests.Request('POST', BASE + '/mod/assign/view.php', data={
                'id': self.job['cmid'], 'action': 'confirmsubmit', 'sesskey': bot.sesskey,
                '_qf__mod_assign_confirm_submission_form': '1', 'submissionstatement': '1', 'submitbutton': 'Continue'}))
        self.ws_limit = min(WS_MAX, max(6, math.ceil(self.bot.rtt / 0.04) + 2))

    def quick_submit(self, bot):
        if not self.job['finalize'] or (self.ws_info or {}).get('drafts') is False:
            return
        if self.submit_request is not None and not (self.ws_info or {}).get('save_only'):
            res = self.bot.send_ws(self.submit_request)
            if res != []:
                raise WsError('submitrefused', f"HEBAT refused 'Submit assignment': {res}")
            self.submit_ack.set()
        elif bot in self.web_submit_requests:
            if accepted(bot.s.send(self.web_submit_requests[bot], timeout=CHECK_TIMEOUT, allow_redirects=False)):
                self.submit_ack.set()
        elif bot.submit_for_grading(self.job['cmid']):
            self.submit_ack.set()

    def warm(self):
        """Warm connections in background threads without delaying saves."""
        sessions = [b.s for b in self.bots] + ([self.bot.ws_s] * self.ws_limit if self.ws_info else [])
        def touch(session):
            try:
                session.head(BASE + CLOCK_PROBE, timeout=(0.5, 0.5))
            except requests.RequestException:
                pass
        for session in sessions:
            threading.Thread(target=touch, args=(session,), daemon=True, name='connection-warm').start()

    def early_upload(self):
        if not self.job['files']:
            return None
        try:
            repo = self.bot.upload_repo_id()
            if repo:
                draft = self.bot.upload_draft(self.job['files'], repo)
                self.log(f"Uploaded {len(self.job['files'])} file(s) ahead of time, ready to attach.")
                return draft
        except requests.RequestException as e:
            self.log(f'Early upload did not answer ({type(e).__name__}); will retry before saving.')
        return None

    def work(self):
        job, cmid = self.job, self.job['cmid']
        self.go = job['at'].timestamp()
        immediate = self.go <= wall_time() + 5
        self.setup_ws()
        if not immediate:
            self.check_clock()
        self.before_status = self.soft('Reading current status', self.status)
        self.preflight()
        if self.before_status and self.before_status.get('timelimit'):
            raise HebatError('This is a timed assignment. Start and submit the attempt on HEBAT manually.')
        self.state = 'waiting'
        draft = self.early_upload() if self.go - wall_time() < EARLY_UPLOAD_MAX else None
        if self.go - wall_time() > 5:
            self.log(f"Target: {fmt(job['at'])} WIB, in {human(self.go - wall_time())}. Keep this app open.")
        if self.go - wall_time() > PREPARE_FROM:
            self.sleep_until(self.go - PREPARE_FROM)
            self.fresh_session()
            self.setup_ws()
            self.check_clock()
            self.before_status = self.soft('Refreshing current status', self.status)
            self.preflight()
            if self.before_status and self.before_status.get('timelimit'):
                raise HebatError('This is a timed assignment. Start and submit the attempt on HEBAT manually.')
        if job['files'] and draft is None:
            draft = self.early_upload()
        if not immediate and self.go - wall_time() > 30:
            self.more_logins(2 if self.ws_info else LOGINS)
        if not immediate and self.go - wall_time() > 10:
            self.sleep_until(self.go - 10)
            self.check_clock()
        self.prepare_requests(draft)
        blind = draft is not None or not job['files']
        if not immediate:
            self.sleep_until(self.go - BURST_FROM - 2)
            self.warm()
        self.sleep_until(self.go - BURST_FROM)
        self.aim = AimClock(self.go)
        self.state = 'polling'
        self.log(f'Checking HEBAT; up to {self.ws_limit} mobile requests in flight during the short peak.'
                 if self.ws_info else 'Checking HEBAT for the submission form...')
        before = (self.before_status or {}).get('state')
        bot, form, page_html = self.wait_for_form(blind, draft,
            first_only=self.before_status is not None and before in (None, 'new'))
        self.state = 'submitting'
        if form is not None:
            if job['files'] and draft is None:
                repo = bot.upload_repo_id(page_html)
                if not repo:
                    raise HebatError("Can't find HEBAT's file upload. Submit by hand now.")
                draft = bot.upload_draft(job['files'], repo)
            self.save_with_retries(bot, form, draft, before)
            self.saved_ack.set()
        if not self.submit_ack.is_set() and job['finalize'] and (self.ws_info or {}).get('drafts') is not False:
            self.press_submit(bot, immediate)
        # Log after the submit attempt.
        self.log(f"Saved ({self.aim.relative():+.2f}s from the target); checking the recorded status.")
        state = self.final_state(bot)
        if state == 'draft' and job['finalize']:
            bot.finalize(cmid)
            state = self.final_state(bot)
        if state not in ('draft', 'submitted'):
            raise HebatError(f"HEBAT did not confirm the save (status: {STATE_LABELS.get(state, 'no attempt')}). Check HEBAT now.")
        self.state = 'done'
        self.result = (f"Submission confirmed at {datetime.now(WIB):%H:%M:%S} WIB (submitted for grading)."
                       if state == 'submitted' else "Saved as a DRAFT. Submit it on HEBAT when ready.")
        self.log(self.result)

    def press_submit(self, bot, immediate):
        end = time.perf_counter() + 15
        while not self.stop_flag.is_set():
            try:
                self.quick_submit(bot)
                return
            except WsError:
                # Check for a completed submission before falling back to the website.
                if self.final_state(bot) == 'submitted':
                    self.submit_ack.set()
                    return
                bot.finalize(self.job['cmid'])
                self.submit_ack.set()
                return
            except requests.RequestException as e:
                if time.perf_counter() >= end:
                    return  # Final status verification handles an ambiguous submit timeout.
                self.hiccup(e)
                self.pause(0.4)

    def final_state(self, bot):
        for attempt in range(5):
            try:
                return self.status(bot)['state']
            except requests.RequestException as e:
                if attempt == 4:
                    raise HebatError('Could not confirm the final status after sending requests. Check HEBAT; the save may have succeeded.') from e
                self.hiccup(e)
                self.pause(1)

    def recovered_save(self):
        """Check for a newly saved submission after outstanding requests have settled."""
        current = self.status()
        before = self.before_status
        if self.saved_ack.is_set():
            return True
        if before is None or current.get('state') not in ('draft', 'submitted'):
            return False
        changed = (before.get('state') not in ('draft', 'submitted') or
                   (before.get('modified') is not None and current.get('modified') is not None
                    and current['modified'] > before['modified']))
        if changed:
            self.saved_ack.set()
        return changed

    def wait_for_form(self, blind, draft, first_only):
        """Run the opening burst, then drain requests before retrying explicitly refused saves."""
        cmid, text = self.job['cmid'], self.job['online_text']
        events, free = queue.Queue(), list(self.bots)
        out = {'ws': 0, 'web': 0}
        use_ws = blind and bool(self.ws_info and self.ws_info.get('rules'))
        self.aim = self.aim or AimClock(self.go)
        pending_form = winner = fatal = None
        uncertain = False
        next_at, cooldown, sent = -float('inf'), -float('inf'), 0
        slow_logged = warmed_again = False
        request_failures = 0
        pool = ThreadPoolExecutor(len(self.bots) + WS_MAX, thread_name_prefix='hebat-check')

        def check(kind, bot, retry):
            start = time.perf_counter()
            result = None
            try:
                if self.stop_sends.is_set() or self.saved_ack.is_set() or self.stop_flag.is_set():
                    result = ('cancelled',)
                elif kind == 'ws' and not retry and self.aim.relative() >= SAVE_CUTOFF:
                    result = ('cancelled',)
                elif kind == 'ws':
                    checked_at = wall_time()
                    reply = self.bot.send_ws(self.save_request)
                    if self.bot.save_reply(reply, self.closed_until, checked_at):
                        result = ('saved',)
                elif kind == 'save':
                    saved, form, response = bot.blind_save(cmid, draft, text, first_only, self.web_save_requests.get(bot))
                    result = ('saved',) if saved else ('form', form, response.text) if form else None
                else:
                    form, response = bot.edit_form(cmid)
                    result = ('form', form, response.text) if form else None
                if result and result[0] == 'saved':
                    with self.save_lock:
                        first = not self.saved_ack.is_set()
                        self.saved_ack.set()
                        self.stop_sends.set()
                    if first and not self.stop_flag.is_set():
                        # Submit from the accepting worker to avoid another scheduling hop.
                        try:
                            self.quick_submit(bot or self.bot)
                        except (HebatError, requests.RequestException):
                            pass  # Reconcile after draining requests; press_submit handles retries.
            except Exception as exc:
                result = exc
            finally:
                events.put((kind, bot, result, time.perf_counter() - start))

        def send(kind, peak, retry=False):
            if kind == 'ws':
                capacity = self.ws_limit if peak else (1 if retry else 2)
                if peak and self.aim.relative() < 0:
                    capacity -= min(4, self.ws_limit // 2)  # reserve room for the opening second
                if out['ws'] >= capacity:
                    return False
                bot = None
            elif free:
                bot = free.pop(0)
            else:
                return False
            out['ws' if kind == 'ws' else 'web'] += 1
            pool.submit(check, kind, bot, retry)
            return True

        precision = PrecisionWindow()
        precision.__enter__()
        try:
            while True:
                t = self.aim.relative()
                if precision is not None and t >= BURST_UNTIL:
                    precision.__exit__(None, None, None)
                    precision = None
                if not warmed_again and t >= -1.2:
                    warmed_again = True
                    if t < -0.5:
                        self.warm()
                if self.stop_flag.is_set():
                    self.stop_sends.set()
                if t >= MAX_WAIT:
                    fatal = fatal or HebatError('Gave up after 30 minutes without a confirmed submission.')
                    self.stop_sends.set()
                if self.stop_sends.is_set() and sum(out.values()) == 0:
                    if winner:
                        return winner, None, None
                    if pending_form and not (fatal or uncertain or self.stop_flag.is_set()):
                        return pending_form
                    # A timed-out request may still have saved the submission.
                    try:
                        if self.recovered_save():
                            return self.bot, None, None
                    except (HebatError, requests.RequestException) as exc:
                        if fatal:
                            raise fatal from exc
                        raise HebatError('Final status is unavailable after stopping requests. A save may still have succeeded; check HEBAT.') from exc
                    if self.stop_flag.is_set():
                        raise Stopped
                    if fatal:
                        raise fatal
                    if uncertain:
                        raise HebatError('A save response was lost or blocked. Final status did not confirm it; check HEBAT before retrying.')
                if t >= FAST_WINDOW and not slow_logged:
                    slow_logged = True
                    self.log('Still not open after 2 minutes. Checking every 3 seconds now.')
                peak = -PEAK_FROM <= t < PEAK_UNTIL
                gap = ((WS_GAP if t >= -0.1 else 0.05) if use_ws else PEAK_GAP) if peak else (BURST_GAP if t < BURST_UNTIL else
                      FAST_POLL if t < FAST_WINDOW else SLOW_POLL)
                retry = use_ws and t >= SAVE_CUTOFF
                # After cutoff, retry only once all outstanding saves have been refused.
                can_send = not self.stop_sends.is_set() and t >= next_at and t >= cooldown
                if retry or t >= BURST_UNTIL:
                    can_send = can_send and sum(out.values()) == 0
                if can_send:
                    scout = not blind or (not use_ws and not peak and (sent + 1) % SCOUT_EVERY == 0)
                    kind = 'form' if scout else 'ws' if use_ws else 'save'
                    if send(kind, peak, retry):
                        sent += 1
                        # Schedule from the current time to avoid catch-up bursts.
                        next_at = t + (max(gap, FAST_POLL) if retry else gap)
                        if t < -PEAK_FROM:
                            next_at = max(t + (WS_GAP if use_ws else PEAK_GAP), min(next_at, -PEAK_FROM))
                wait_for = min(max(next_at - self.aim.relative(), 0.001), 0.05)
                try:
                    kind, bot, result, latency = events.get(timeout=wait_for)
                except queue.Empty:
                    continue
                out['ws' if kind == 'ws' else 'web'] -= 1
                if bot:
                    free.append(bot)
                if kind == 'ws' and result is None:
                    self.ws_limit = min(WS_MAX, max(self.ws_limit, math.ceil(latency / 0.04) + 2))
                if isinstance(result, requests.RequestException):
                    self.hiccup(result)
                    request_failures += 1
                    cooldown = self.aim.relative() + 1
                    response = getattr(result, 'response', None)
                    if response is not None:
                        value = response.headers.get('Retry-After', '')
                        try:
                            delay = float(value)
                        except ValueError:
                            try:
                                delay = parsedate_to_datetime(value).timestamp() - wall_time()
                            except (TypeError, ValueError):
                                delay = 1
                        cooldown = self.aim.relative() + max(1, delay)
                    if kind in ('ws', 'save'):
                        uncertain = True
                        self.stop_sends.set()
                    elif request_failures >= 3:
                        fatal = HebatError('HEBAT repeatedly blocked or failed the checks. Check the service and try again.')
                        self.stop_sends.set()
                elif isinstance(result, Exception):
                    fatal = result
                    self.stop_sends.set()
                elif result:
                    if result[0] == 'saved':
                        winner = bot or self.bot
                        self.stop_sends.set()
                    elif result[0] == 'form':
                        pending_form = (bot, result[1], result[2])
                        self.stop_sends.set()
        finally:
            # Drain requests before reporting completion.
            self.stop_sends.set()
            pool.shutdown(wait=True, cancel_futures=True)
            if precision is not None:
                precision.__exit__(None, None, None)

    def hiccup(self, e):
        now = wall_time()
        if now - self.last_hiccup > 5:  # Rate-limit duplicate outage messages.
            self.last_hiccup = now
            self.log(f"HEBAT connection problem ({e.__class__.__name__}); checking the outcome before continuing.")

    def save_with_retries(self, bot, form, draft, before):
        cmid = self.job["cmid"]
        for attempt in range(1, 4):
            try:
                return bot.save(cmid, form, draft, self.job["online_text"], self.log, before)
            except requests.RequestException as e:
                self.log(f"Network error while saving ({e.__class__.__name__}), attempt {attempt} of 3.")
            self.pause(1)
            try:  # Check for a successful save before retrying a lost response.
                if self.recovered_save():
                    return
                form, _ = bot.edit_form(cmid)
            except requests.RequestException:
                continue
            if not form:
                raise HebatError("The submission form disappeared (did the assignment close?). Check HEBAT now!")
        raise HebatError("Saving failed 3 times because of network errors. Submit by hand on HEBAT now!")
