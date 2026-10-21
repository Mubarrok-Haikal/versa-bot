"""versa-bot: pick an assignment on HEBAT UNAIR, and the bot submits your file the moment it opens.

    pip install -r requirements.txt
    python versa_bot.py

Opens in its own window if pywebview is installed (pip install pywebview), otherwise in your browser.
Keep it running until the bot is done: closing it stops the bot. Your password is only kept in memory.
"""
import atexit
import json
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

import engine

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "hebat_settings.json"
TOKEN = secrets.token_urlsafe(18)
UPLOADS = Path(tempfile.mkdtemp(prefix="hebat_"))
atexit.register(shutil.rmtree, UPLOADS, ignore_errors=True)
PORT = 0


class App:
    def __init__(self):
        self.bot = None
        self.offset = None
        self.files = {}  # id -> Path
        self.runner = None
        self.logs = []
        self.seq = 0
        self.lock = threading.Lock()

    def log(self, msg):
        t = datetime.now(engine.WIB).strftime("%H:%M:%S.%f")[:-3]
        with self.lock:
            self.seq += 1
            self.logs.append({"seq": self.seq, "t": t, "msg": msg})
            del self.logs[:-1000]
        print(f"[{t}] {msg}", flush=True)

    def busy(self):
        return self.runner is not None and self.runner.is_alive()

    def need_login(self):
        if not self.bot:
            raise engine.HebatError("You're logged out. Log in again.")
        return self.bot

    # --- API routes: each takes the query dict and the JSON body, returns something JSON-able ---

    def state(self, q, body):
        since = int(q.get("since") or 0)
        r = self.runner
        job = None
        if r:
            job = {"state": r.state, "result": r.result, "cmid": r.job["cmid"], "name": r.job["name"],
                   "course": r.job["course"], "target": engine.fmt(r.job["at"]),
                   "go": r.go * 1000 if r.go else None, "files": [p.name for p in r.job["files"]],
                   "finalize": r.job["finalize"], "has_text": bool(r.job["online_text"])}
        with self.lock:
            logs = [entry for entry in self.logs if entry["seq"] > since]
        return {"user": self.bot and {"username": self.bot.username, "id": self.bot.user_id},
                "saved_username": load_settings().get("username", ""), "offset": self.offset,
                "job": job, "logs": logs, "now": time.time() * 1000}

    def login(self, q, body):
        if self.busy():
            raise engine.HebatError("The bot is running. Stop it before logging in again.")
        username, password = (body.get("username") or "").strip(), body.get("password") or ""
        if not username or not password:
            raise engine.HebatError("Enter your NIM and password.")
        bot = engine.Hebat(username, password)
        bot.login()
        self.bot, self.offset = bot, bot.clock_offset()
        save_settings(username=username)
        self.log(f"Logged in as {username}. " + ("Couldn't check your clock yet (HEBAT is slow); the bot checks again when it starts."
                                                 if self.offset is None else f"Your clock vs HEBAT: {self.offset:+.1f}s"))
        return {"ok": True}

    def logout(self, q, body):
        if self.busy():
            raise engine.HebatError("The bot is running. Stop it first.")
        self.bot, self.offset, self.runner = None, None, None
        return {"ok": True}

    def courses(self, q, body):
        return {"courses": self.need_login().courses()}

    def assignments(self, q, body):
        return {"assignments": self.need_login().assignments(int(q["course"]))}

    def add_file(self, q, body, raw, headers):
        name = Path(unquote(headers.get("X-Filename") or "")).name
        name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
        if not name or not raw:
            raise engine.HebatError("That file is empty.")
        fid = uuid.uuid4().hex[:12]
        path = UPLOADS / fid / name
        path.parent.mkdir()
        path.write_bytes(raw)
        self.files[fid] = path
        return {"id": fid, "name": name, "size": len(raw)}

    def remove_file(self, q, body):
        path = self.files.pop(body.get("id"), None)
        if path:
            shutil.rmtree(path.parent, ignore_errors=True)
        return {"ok": True}

    def start(self, q, body):
        bot = self.need_login()
        if self.busy():
            raise engine.HebatError("The bot is already running.")
        files = [self.files[f] for f in body.get("files", []) if f in self.files]
        text = (body.get("online_text") or "").strip()
        if not files and not text:
            raise engine.HebatError("Add a file (or online text) to submit.")
        try:
            at = datetime.fromtimestamp(float(body["at"]), engine.WIB)
            cmid = int(body["cmid"])
            course_id = int(body.get("course_id") or 0)
        except (KeyError, TypeError, ValueError):
            raise engine.HebatError("Pick an assignment and a time first.")
        job = {"cmid": cmid, "name": body.get("name") or f"Assignment {cmid}", "course": body.get("course") or "",
               "course_id": course_id, "at": at, "files": files, "online_text": text,
               "finalize": bool(body.get("finalize", True))}
        self.log(f"Bot started for '{job['name']}'.")
        self.runner = engine.Runner(bot, job, self.log)
        self.runner.start()
        return {"ok": True}

    def stop(self, q, body):
        if self.busy():
            self.runner.stop()
        return {"ok": True}

    def dismiss(self, q, body):
        if not self.busy():
            self.runner = None
        return {"ok": True}


app = App()
ROUTES = {
    ("GET", "/api/state"): app.state,
    ("POST", "/api/login"): app.login,
    ("POST", "/api/logout"): app.logout,
    ("GET", "/api/courses"): app.courses,
    ("GET", "/api/assignments"): app.assignments,
    ("POST", "/api/files/remove"): app.remove_file,
    ("POST", "/api/start"): app.start,
    ("POST", "/api/stop"): app.stop,
    ("POST", "/api/dismiss"): app.dismiss,
}


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    try:
        SETTINGS.write_text(json.dumps({**load_settings(), **values}, indent=2), encoding="utf-8")
    except OSError:
        pass  # remembering the NIM is a convenience, not worth failing a login over


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.reply(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
        self.api("GET", url)

    def do_POST(self):
        self.api("POST", urlparse(self.path))

    def api(self, method, url):
        # Only this app's own page may call the API: other websites can't set the token header,
        # and the Host check stops DNS-rebinding tricks.
        if self.headers.get("X-Token") != TOKEN or self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self.reply_json(403, {"error": "Forbidden"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if (method, url.path) == ("POST", "/api/files"):
                result = app.add_file(query, {}, raw, self.headers)
            elif route := ROUTES.get((method, url.path)):
                result = route(query, json.loads(raw or b"{}"))
            else:
                return self.reply_json(404, {"error": "Not found"})
            self.reply_json(200, result)
        except engine.HebatError as e:
            self.reply_json(400, {"error": str(e)})
        except requests.RequestException as e:
            self.reply_json(502, {"error": f"Couldn't reach HEBAT ({e.__class__.__name__}). Check your internet."})
        except Exception as e:
            self.reply_json(500, {"error": f"Something broke: {e!r}"})

    def reply_json(self, code, obj):
        self.reply(code, json.dumps(obj).encode(), "application/json")

    def reply(self, code, payload, kind):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy bind a port the first is still using; we want the error.
    allow_reuse_address = sys.platform != "win32"


def main():
    global PORT
    try:
        server = Server(("127.0.0.1", 8765), Handler)
    except OSError:  # port taken, maybe by another copy of this app: let the OS pick one
        server = Server(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    PORT = server.server_address[1]
    url = f"http://127.0.0.1:{PORT}/#{TOKEN}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
    except ImportError:
        webview = None
    if webview and "--browser" not in sys.argv:
        webview.create_window("versa-bot", url, width=1240, height=820, min_size=(760, 560), confirm_close=True)
        webview.start()
        return
    print(f"versa-bot is running at {url}\nKeep this window open while the bot works. Press Ctrl+C to quit.")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
