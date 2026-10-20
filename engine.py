"""HEBAT UNAIR (Moodle) client and the auto-submit runner. No UI code here; versa_bot.py drives it."""
import html
import os
import queue
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("HEBAT_BASE", "https://hebat.elearning.unair.ac.id").rstrip("/")
WIB = timezone(timedelta(hours=7))
FAST_POLL, SLOW_POLL, FAST_WINDOW = 0.5, 3.0, 120  # seconds between checks, and how long to keep checking fast
BURST_GAP, BURST_UNTIL, BURST_MAX = 0.2, 5, 4  # until 5s after the target: a check every 0.2s, up to 4 at once
MAX_WAIT = 30 * 60  # stop polling this long after the target time

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "agu": 8,
          "agt": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "nop": 11, "dec": 12, "des": 12}
DATE_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4}),?\s+(\d{1,2})[:.](\d{2})\s*([AaPp][Mm])?")
DATE_MDY = re.compile(r"([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+(\d{1,2})[:.](\d{2})\s*([AaPp][Mm])?")
DAY_ONLY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4})")
OPEN_WORDS = ("open", "buka", "mulai")
DUE_WORDS = ("due", "tenggat", "batas", "jatuh tempo", "tutup", "close")
# Restrict access notes: "Available from <strong>27 October 2026, 7:00 AM</strong>", "It is before ...", Indonesian too.
LOCK_FROM_WORDS = ("from", "after", "dari", "setelah", "sejak", "mulai")
LOCK_UNTIL_WORDS = ("until", "before", "sampai", "hingga", "sebelum")
STATE_CLASS = re.compile(r"^submissionstatus(submitted|draft|new|reopened)$")
STATE_LABELS = {"submitted": "Submitted", "draft": "Draft (not submitted)", "reopened": "Reopened", "new": "No attempt"}


class HebatError(Exception):
    """Something the user should read, as is."""


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
    """A date as Moodle prints it, English or Indonesian, read as WIB. None if there isn't one."""
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
    """(unlocks, locks) from a Restrict access note, either one None if the note doesn't give it."""
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
    """Moodle forms carry a hidden <input name="action">; find the form by its value."""
    marker = page.find("input", {"name": "action", "value": action})
    return marker.find_parent("form") if marker else None


def form_data(form):
    """What a browser would send for this form if you just clicked its main button."""
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
    """'submitted', 'draft', 'new', 'reopened', or None when the page has no status table (no attempt yet,
    or not a status page at all). Read from CSS classes, so it works whatever language HEBAT is in."""
    el = page.find(class_=STATE_CLASS)
    if el:
        for c in el.get("class", []):
            if m := STATE_CLASS.match(c):
                return m.group(1)
    return None


def keep_awake(on):
    """Stop Windows from sleeping while the bot waits. Applies to the calling thread until turned off."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


class Hebat:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.s = requests.Session()
        self.sesskey = None
        self.user_id = None
        self.logged_in_at = 0.0
        self.login_lock = threading.Lock()

    def login(self):
        with self.login_lock:
            return self._login()

    def relogin(self):
        started = time.time()
        with self.login_lock:
            if self.logged_in_at < started:  # skip if another thread logged in while we waited
                self._login()

    def _login(self):
        self.s.cookies.clear()
        page = parse(self.s.get(BASE + "/login/index.php", timeout=20).text)
        token = page.find("input", {"name": "logintoken"})
        r = self.s.post(BASE + "/login/index.php", timeout=30, data={
            "username": self.username, "password": self.password,
            "logintoken": token["value"] if token else "", "anchor": "",
        })
        user = re.search(r'"userId":(\d+)', r.text)
        sesskey = re.search(r'"sesskey":"([^"]+)"', r.text)
        if not user or user.group(1) == "0" or not sesskey:
            raise HebatError("Login failed. Check your NIM and password (same as UNAIR Satu).")
        self.sesskey, self.user_id = sesskey.group(1), int(user.group(1))
        self.logged_in_at = time.time()
        return self.user_id

    def get(self, path, timeout=20):
        r = self.s.get(BASE + path, timeout=timeout)
        if "/login/index.php" in r.url:  # session expired: log in again and retry once
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
        if isinstance(res, dict):  # the whole call was refused, usually an expired session
            if retry and res.get("errorcode") in ("invalidsesskey", "requireloginerror", "servicerequireslogin"):
                self.relogin()
                return self.ajax(method, args, retry=False)
            raise HebatError(res.get("error") or "HEBAT refused the request.")
        if res[0].get("error"):
            raise HebatError(res[0].get("exception", {}).get("message") or "HEBAT refused the request.")
        return res[0]["data"]

    def clock_offset(self):
        """HEBAT's clock minus this PC's clock, in seconds (about ±0.5s accurate). None if HEBAT didn't answer."""
        samples = []
        for _ in range(3):
            t0 = time.time()
            try:
                r = self.s.head(BASE + "/", timeout=5)
            except requests.RequestException:
                continue
            t1 = time.time()
            if "Date" in r.headers:
                server = parsedate_to_datetime(r.headers["Date"]).timestamp() + 0.5  # header drops the fraction
                samples.append(server - (t0 + t1) / 2)
        return sum(samples) / len(samples) if samples else None

    def courses(self):
        """Your matkul: in-progress ones first, all of them if HEBAT marks none as in progress."""
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
        """Fallback for older Moodle: the 'Course profiles' links on your profile page."""
        found = {}
        for a in parse(self.get("/user/profile.php").text).select("a[href]"):
            href = a["href"]
            m = re.search(r"[?&]course=(\d+)", href) if "/user/view.php" in href else re.search(r"/course/view\.php\?id=(\d+)", href)
            name = a.get_text(" ", strip=True)
            if m and int(m.group(1)) != 1 and name:
                found.setdefault(int(m.group(1)), name)
        return [{"id": cid, "name": name, "category": ""} for cid, name in found.items()]

    def assignments(self, course_id):
        """Every assignment on the course page. Read from there, not the assignment index, because the index
        leaves out ones locked by Restrict access, and lecturers lock each week's task until it opens."""
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
        """What the course page says about an assignment you can't open yet. HEBAT won't show its own page,
        so the opening time is the 'Available from' date."""
        unlocks, locks = lock_dates(box) if box else (None, None)
        now = datetime.now(WIB)
        if unlocks and unlocks > now:
            availability = "upcoming"
        elif locks and locks <= now:
            availability = "closed"
        else:
            availability = "locked"  # some other condition, like a group or finishing another activity
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
        """Id of the "Upload a file" repository, read from any page that has a file picker."""
        page_html = page_html or self.get("/user/files.php").text
        m = re.search(r'"id":"?(\d+)"?,"name":"(?:[^"\\]|\\.)*","type":"upload"', page_html)
        return int(m.group(1)) if m else None

    def upload_draft(self, paths, repo_id):
        """Upload files into a fresh private draft area. Returns its itemid, which the form then claims."""
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
        """The submission form, or None while HEBAT won't show it yet."""
        url = f"{BASE}/mod/assign/view.php?id={cmid}&action=editsubmission"
        r = self.s.get(url, timeout=15, allow_redirects=False)
        if r.is_redirect and "/login/index.php" in r.headers.get("Location", ""):
            self.relogin()
            r = self.s.get(url, timeout=15, allow_redirects=False)
        if r.is_redirect:  # still locked: HEBAT bounces you to the course page, no need to download it
            return None, r
        if "savesubmission" not in r.text:  # not open yet; skip parsing the whole page
            return None, r
        return find_form(parse(r.text), "savesubmission"), r

    def save(self, cmid, form, draft_itemid, online_text, log):
        """Fill in and save the submission form. Returns the submission state HEBAT shows afterwards."""
        data = form_data(form)
        takes_files, takes_text = "files_filemanager" in data, "onlinetext_editor[text]" in data
        if draft_itemid is not None:
            if takes_files:
                data["files_filemanager"] = str(draft_itemid)
            else:
                log("This assignment doesn't take files, so they're left out.")
        if online_text:
            if takes_text:
                data["onlinetext_editor[text]"] = "".join(
                    f"<p>{html.escape(line)}</p>" for line in online_text.splitlines() if line.strip())
            else:
                log("This assignment doesn't take online text, so it's left out.")
        if not ((draft_itemid is not None and takes_files) or (online_text and takes_text)):
            raise HebatError("This assignment doesn't accept what you gave the bot (files or online text). Submit by hand!")
        if form.find(attrs={"name": "submissionstatement"}):
            data["submissionstatement"] = "1"

        page = parse(self.post("/mod/assign/view.php", data=data).text)
        if find_form(page, "savesubmission"):  # Moodle shows the form again when it rejects it
            errors = [e.get_text(" ", strip=True) for e in page.select(".invalid-feedback, .alert-danger, .error")]
            raise HebatError("HEBAT rejected the submission: " + ("; ".join(filter(None, errors)) or "no reason shown"))
        state = submission_state(page) or self.assignment_info(cmid)["state"]
        if state not in ("draft", "submitted"):
            raise HebatError(f"HEBAT didn't record the submission (status: {STATE_LABELS.get(state, 'no attempt')}). "
                             "Check HEBAT now and submit by hand!")
        return state

    def finalize(self, cmid):
        """The 'Submit assignment' step, for assignments that keep submissions as drafts until you confirm."""
        conf = find_form(parse(self.get(f"/mod/assign/view.php?id={cmid}&action=submit").text), "confirmsubmit")
        if not conf:
            raise HebatError("Saved as DRAFT, but the 'Submit assignment' step wasn't found. Submit it by hand!")
        data = form_data(conf)
        if conf.find(attrs={"name": "submissionstatement"}):
            data["submissionstatement"] = "1"
        self.post("/mod/assign/view.php", data=data)
        if self.assignment_info(cmid)["state"] != "submitted":
            raise HebatError("Saved as DRAFT, but 'Submit assignment' didn't go through. Submit it by hand!")


class Runner(threading.Thread):
    """Waits for the target time, then submits the moment HEBAT shows the submission form.

    job: cmid, name, course, at (aware datetime, HEBAT's clock), files (list of Path), online_text, finalize
    """

    def __init__(self, bot, job, log):
        super().__init__(daemon=True)
        self.bot, self.job, self.log = bot, job, log
        self.stop_flag = threading.Event()
        self.state = "starting"  # starting, waiting, polling, submitting, done, failed, stopped
        self.go = None  # this PC's timestamp for job["at"], after correcting for clock drift
        self.result = None
        self.last_hiccup = 0.0

    def stop(self):
        self.stop_flag.set()

    def pause(self, seconds):
        if self.stop_flag.wait(max(seconds, 0)):
            raise Stopped

    def sleep_until(self, ts):
        # Short naps against the wall clock, so the PC sleeping and waking can't make us late.
        while (left := ts - time.time()) > 0:
            self.pause(min(left, 20))

    def run(self):
        keep_awake(True)
        try:
            self.work()
        except Stopped:
            self.state, self.result = "stopped", "Stopped before anything was submitted."
            self.log(self.result)
        except (HebatError, requests.RequestException) as e:
            self.state, self.result = "failed", str(e) if isinstance(e, HebatError) else f"Network error: {e}"
            self.log(f"FAILED: {self.result}")
        except Exception as e:  # never die silently while the user thinks the bot is on it
            self.state, self.result = "failed", f"Unexpected error: {e!r}. Submit by hand!"
            self.log(f"FAILED: {self.result}")
        finally:
            keep_awake(False)

    def soft(self, what, fn):
        """A step the submission doesn't depend on. HEBAT being slow here shouldn't end the run."""
        try:
            return fn()
        except requests.RequestException as e:
            self.log(f"{what}: HEBAT didn't answer ({e.__class__.__name__}). Carrying on.")
            return None

    def check_clock(self):
        offset = self.bot.clock_offset()
        if offset is None:
            self.log("Couldn't check your clock (HEBAT is slow right now). Assuming it matches.")
            return
        self.go = self.job["at"].timestamp() - offset
        self.log(f"Your clock vs HEBAT: {offset:+.1f}s" + (" (compensating)" if abs(offset) >= 1 else ""))

    def fresh_session(self):
        """Log in again close to the time (sessions expire after a few hours), retrying while HEBAT is slow.
        If it never answers, the polling below logs in by itself once HEBAT is back."""
        while time.time() < self.go - 15:
            try:
                self.bot.login()
                self.log("Logged in again for a fresh session.")
                return
            except requests.RequestException as e:
                self.log(f"Logging in again: HEBAT didn't answer ({e.__class__.__name__}). Retrying.")
                self.pause(5)

    def work(self):
        job, cmid = self.job, self.job["cmid"]
        self.go = job["at"].timestamp()
        immediate = self.go <= time.time() + 5  # "Submit now": nothing to aim at, so skip the clock and the early upload
        if not immediate:
            self.check_clock()
        before = self.soft("Reading the current status", lambda: self.bot.assignment_info(cmid)["state"])
        self.state = "waiting"
        if self.go - time.time() > 5:
            self.log(f"Target: {fmt(job['at'])} WIB, in {human(self.go - time.time())}. Keep this app open.")
        if self.go - time.time() > 150:
            self.sleep_until(self.go - 120)
            self.fresh_session()
            self.check_clock()  # the PC's clock can drift over days of waiting

        draft = None
        if job["files"] and not immediate:
            try:
                repo = self.bot.upload_repo_id()
                if repo:
                    draft = self.bot.upload_draft(job["files"], repo)
                    self.log(f"Uploaded {len(job['files'])} file(s) ahead of time, ready to attach.")
            except (HebatError, requests.RequestException) as e:
                self.log(f"Early upload didn't work ({e}); will upload the moment it opens instead.")

        self.sleep_until(self.go - 3)
        self.state = "polling"
        self.log("Checking HEBAT for the submission form...")
        form, page_html = self.wait_for_form()
        self.state = "submitting"
        self.log("Got the submission form." if immediate else f"OPEN! ({time.time() - self.go:+.2f}s from the target time)")
        if job["files"] and draft is None:
            repo = self.bot.upload_repo_id(page_html)
            if not repo:
                raise HebatError("Can't find HEBAT's file upload on the submission page. Submit by hand, quick!")
            draft = self.bot.upload_draft(job["files"], repo)

        state = self.save_with_retries(form, draft, before)
        self.log("Saved." if state == "draft" else "Saved and submitted.")
        if state == "draft" and job["finalize"]:
            self.bot.finalize(cmid)
            state = "submitted"
            self.log("Submitted for grading.")
        self.state = "done"
        self.result = (f"Submitted for grading at {datetime.now(WIB):%H:%M:%S} WIB." if state == "submitted" else
                       "Saved as a DRAFT (you turned off 'Submit assignment'). Submit it on HEBAT when ready.")
        self.log(self.result)

    def wait_for_form(self):
        """Ask HEBAT for the submission form until it shows up. Around the target time the checks overlap: a new
        one every 0.2s without waiting for the last answer, so the opening is seen about one page load after it
        happens instead of up to two."""
        hits = queue.Queue()  # (form, page_html), or an error the main loop should raise
        lock, in_flight = threading.Lock(), [0]

        def check():
            try:
                form, r = self.bot.edit_form(self.job["cmid"])
                if form:
                    hits.put((form, r.text))
            except requests.RequestException as e:
                self.hiccup(e)
            except Exception as e:  # e.g. logging in again failed: that ends the run
                hits.put(e)
            finally:
                with lock:
                    in_flight[0] -= 1

        pool = ThreadPoolExecutor(BURST_MAX)
        deadline, slow_logged = self.go + MAX_WAIT, False
        try:
            while True:
                if self.stop_flag.is_set():
                    raise Stopped
                now = time.time()
                if now > deadline:
                    raise HebatError("Gave up: the submission form didn't appear within 30 minutes of the target time.")
                if now >= self.go + FAST_WINDOW and not slow_logged:
                    slow_logged = True
                    self.log("Still not open after 2 minutes. Checking every 3 seconds now.")
                burst = now < self.go + BURST_UNTIL
                with lock:
                    send = in_flight[0] < (BURST_MAX if burst else 1)
                    if send:
                        in_flight[0] += 1
                if send:
                    pool.submit(check)
                gap = BURST_GAP if burst else FAST_POLL if now < self.go + FAST_WINDOW else SLOW_POLL
                try:
                    got = hits.get(timeout=gap)  # returns the moment a check finds the form
                except queue.Empty:
                    continue
                if isinstance(got, Exception):
                    raise got
                return got
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # don't wait for checks still out

    def hiccup(self, e):
        now = time.time()
        if now - self.last_hiccup > 5:  # checks overlap, so one outage would otherwise log many times
            self.last_hiccup = now
            self.log(f"Network hiccup ({e.__class__.__name__}), retrying")

    def save_with_retries(self, form, draft, before):
        cmid = self.job["cmid"]
        for attempt in range(1, 4):
            try:
                return self.bot.save(cmid, form, draft, self.job["online_text"], self.log)
            except requests.RequestException as e:
                self.log(f"Network error while saving ({e.__class__.__name__}), attempt {attempt} of 3.")
            time.sleep(1)
            try:  # the save may have gone through even though the answer got lost
                state = self.bot.assignment_info(cmid)["state"]
                if state == "submitted" or (state == "draft" and before != "draft"):
                    return state
                form, _ = self.bot.edit_form(cmid)
            except requests.RequestException:
                continue
            if not form:
                raise HebatError("The submission form disappeared (did the assignment close?). Check HEBAT now!")
        raise HebatError("Saving failed 3 times because of network errors. Submit by hand on HEBAT now!")
