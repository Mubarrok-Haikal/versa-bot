"""HEBAT UNAIR (Moodle) client and the auto-submit runner. No UI code here; versa_bot.py drives it."""
import html
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

import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("HEBAT_BASE", "https://hebat.elearning.unair.ac.id").rstrip("/")
WIB = timezone(timedelta(hours=7))
FAST_POLL, SLOW_POLL, FAST_WINDOW = 0.5, 3.0, 120  # seconds between checks, and how long to keep checking fast
BURST_FROM, BURST_UNTIL, BURST_GAP = 3, 5, 0.25  # from 3s before the target to 5s after: checks overlap, 0.25s apart
PEAK_FROM, PEAK_UNTIL, PEAK_GAP = 0.5, 1.5, 0.1  # and from 0.5s before to 1.5s after, 0.1s apart
LOGINS = 4  # website logins, one per overlapping check (2 when the mobile-app route does the saving)
WS_GAP, WS_MAX = 0.05, 6  # mobile-app route: in the peak, a save every 0.05s, up to 6 at once
SCOUT_EVERY = 4  # with blind saves, every 4th check outside the peak loads the form, in case a blind one won't do
CHECK_TIMEOUT = (3, 8)  # connect, read: an overloaded HEBAT shouldn't tie up a login for long
CLOCK_PROBE = "/pix/i/loading.gif"  # a static file: the web server answers it without running Moodle
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


class WsError(HebatError):
    """The mobile-app route refused a call. code is Moodle's errorcode."""
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


def find_form_fast(text, action):
    """find_form on just that <form>, cut out of the page first: parsing a whole Moodle page takes a while, and
    this runs at the moment it counts."""
    i = text.find(f'value="{action}"')
    if i < 0:
        return None
    start, end = text.rfind("<form", 0, i), text.find("</form>", i)
    snippet = text[start:end + 7] if 0 <= start and 0 <= end else text
    return find_form(parse(snippet), action) or find_form(parse(text), action)


def accepted(r):
    """Moodle answers a form it accepted on the assignment page with a redirect back to that page."""
    if r.is_redirect:
        return "/mod/assign/view.php" in r.headers.get("Location", "")
    return bool(re.search(r'http-equiv="refresh"[^>]*/mod/assign/view\.php', r.text))  # redirect page, debug mode


def landed(state, before):
    """Whether the submission state shows a save went through, given the state before the bot started."""
    return state == "submitted" or (state == "draft" and before != "draft")


def flatten(out, value, key=""):
    """Nested parameters the way Moodle's REST server reads them: plugindata[onlinetext_editor][text]=..."""
    if not isinstance(value, (dict, list)):
        out[key] = value
        return out
    for k, v in (value.items() if isinstance(value, dict) else enumerate(value)):
        flatten(out, v, f"{key}[{k}]" if key else str(k))
    return out


def text_html(online_text):
    return "".join(f"<p>{html.escape(line)}</p>" for line in online_text.splitlines() if line.strip())


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


def overloaded(r, *args, **kwargs):
    """A 5xx answer (HEBAT overloaded, or the gateway in front of it) is a network error: retry it, don't read the
    error page as an answer."""
    if r.status_code >= 500:
        raise requests.HTTPError(f"HTTP {r.status_code}", response=r)


def keep_awake(on):
    """Stop Windows from sleeping while the bot waits. Applies to the calling thread until turned off."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))


class Hebat:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.s = requests.Session()
        self.ws_s = requests.Session()  # mobile-app route: no cookies, and many calls at once
        self.ws_s.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=WS_MAX + 2))
        self.ws_s.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=WS_MAX + 2))
        for session in (self.s, self.ws_s):
            session.hooks["response"].append(overloaded)
        self.token = None
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

    def alive(self):
        """Whether this login still works. Some Moodle sites cap how many logins an account can have at once and
        end the oldest."""
        try:
            self.ajax("core_session_touch", {}, retry=False)
            return True
        except (HebatError, requests.RequestException):
            return False

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
        """HEBAT's clock minus this PC's clock, in seconds. None if HEBAT didn't answer. The Date header only has whole
        seconds, so this times the moment it ticks over to the next one, which is accurate to about a round trip
        instead of half a second. Falls back to the plain reading if the round trip is too slow for that."""
        s = requests.Session()
        last, flips, rough, fails = None, [], [], 0
        end = time.time() + 3
        while time.time() < end and len(flips) < 2 and fails < 3:
            t0 = time.time()
            try:
                r = s.head(BASE + CLOCK_PROBE, timeout=(3, 3))
            except requests.RequestException:
                fails += 1
                continue
            mid = (t0 + time.time()) / 2
            if "Date" not in r.headers:
                break
            sec = parsedate_to_datetime(r.headers["Date"]).timestamp()
            rough.append(sec + 0.5 - mid)
            if last and sec - last[1] == 1 and mid - last[0] < 0.3:  # it ticked over between these two
                flips.append(sec - (last[0] + mid) / 2)
            last = (mid, sec)
        s.close()
        if flips:
            return sum(flips) / len(flips)
        return sum(rough) / len(rough) if rough else None

    def ws_login(self):
        """A token for Moodle's mobile-app route, or None if HEBAT doesn't offer it."""
        try:
            res = self.ws_s.post(BASE + "/login/token.php", timeout=20, data={
                "username": self.username, "password": self.password, "service": "moodle_mobile_app"}).json()
        except (requests.RequestException, ValueError):
            return None
        self.token = res.get("token") if isinstance(res, dict) else None
        return self.token

    def ws(self, function, timeout=CHECK_TIMEOUT, **params):
        """Call a function of the mobile-app route. Unlike the website, these don't take Moodle's session lock, so
        any number can run at once, and the answers are a few bytes of JSON instead of whole pages."""
        data = flatten({"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}, params)
        r = self.ws_s.post(BASE + "/webservice/rest/server.php", timeout=timeout, data=data)
        try:
            res = r.json()
        except ValueError:
            raise WsError("badanswer", f"HEBAT gave an unexpected answer (HTTP {r.status_code}).")
        if isinstance(res, dict) and "exception" in res:
            raise WsError(res.get("errorcode"), res.get("message"))
        return res

    def ws_assignment(self, cmid, course_id):
        """What the mobile-app route needs for this assignment: {"id": its instance id (not the cmid), "drafts":
        whether it keeps drafts until "Submit assignment", or None if HEBAT won't say yet}. None if HEBAT doesn't
        offer the route."""
        if not (self.token or self.ws_login()):
            return None
        functions = {f["name"] for f in self.ws("core_webservice_get_site_info").get("functions", [])}
        if not {"mod_assign_save_submission", "mod_assign_submit_for_grading"} <= functions:
            return None
        info = None
        for section in self.ws("core_course_get_contents", courseid=course_id,
                               options=[{"name": "cmid", "value": cmid}]):
            for m in section.get("modules", []):
                if m.get("id") == cmid:
                    info = {"id": m["instance"], "drafts": None}
        if info:  # leaves out assignments Restrict access still hides; drafts stays unknown then
            for course in self.ws("mod_assign_get_assignments", courseids=[course_id]).get("courses", []):
                for a in course.get("assignments", []):
                    if a.get("cmid") == cmid:
                        info["drafts"] = bool(a.get("submissiondrafts"))
        return info

    def ws_save(self, assign_id, draft_itemid, online_text):
        """The blind save over the mobile-app route. True when HEBAT took it, False while the assignment is closed:
        a warning while the date hasn't come, an error while Restrict access still hides it."""
        plugindata = {}
        if draft_itemid is not None:
            plugindata["files_filemanager"] = draft_itemid
        if online_text:
            plugindata["onlinetext_editor"] = {"text": text_html(online_text), "format": 1,
                                               "itemid": random.randint(100_000_000, 999_999_999)}
        try:
            return self.ws("mod_assign_save_submission", assignmentid=assign_id, plugindata=plugindata) == []
        except WsError as e:
            if e.code == "requireloginerror":
                return False
            raise

    def ws_submit(self, assign_id):
        """'Submit assignment' over the mobile-app route. True if HEBAT took it."""
        return self.ws("mod_assign_submit_for_grading", assignmentid=assign_id, acceptsubmissionstatement=1) == []

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
        r = self.s.get(url, timeout=CHECK_TIMEOUT, allow_redirects=False)
        if r.is_redirect and "/login/index.php" in r.headers.get("Location", ""):
            self.relogin()
            r = self.s.get(url, timeout=CHECK_TIMEOUT, allow_redirects=False)
        if r.is_redirect:  # still locked: HEBAT bounces you to the course page, no need to download it
            return None, r
        return find_form_fast(r.text, "savesubmission"), r

    def blind_save(self, cmid, draft_itemid, online_text, first_only=False):
        """Send the submission without loading the form first. The form only ever asks for these fields, and HEBAT
        refuses the save untouched while the assignment is closed. Returns (saved, form, response): form is set
        when HEBAT showed the form instead (open, but it wanted something else).

        first_only (for a submission with no attempt yet): a stale "last modified" makes HEBAT refuse every save
        after the first, so blind saves still on their way can't push the recorded time later."""
        data = {"id": cmid, "userid": self.user_id, "action": "savesubmission", "sesskey": self.sesskey,
                "_qf__mod_assign_submission_form": "1", "submissionstatement": "1", "submitbutton": "Save changes"}
        if first_only:
            data["lastmodified"] = 1
        if draft_itemid is not None:
            data["files_filemanager"] = draft_itemid
        if online_text:
            data.update({"onlinetext_editor[text]": text_html(online_text), "onlinetext_editor[format]": "1",
                         "onlinetext_editor[itemid]": random.randint(100_000_000, 999_999_999)})
        r = self.post("/mod/assign/view.php", timeout=CHECK_TIMEOUT, data=data, allow_redirects=False)
        if accepted(r):
            return True, None, r
        if r.is_redirect and "/login/index.php" in r.headers.get("Location", ""):
            self.relogin()
        return False, find_form_fast(r.text, "savesubmission"), r

    def save(self, cmid, form, draft_itemid, online_text, log, before=None):
        """Fill in and save the submission form. Raises if HEBAT didn't take it."""
        data = form_data(form)
        takes_files, takes_text = "files_filemanager" in data, "onlinetext_editor[text]" in data
        if draft_itemid is not None:
            if takes_files:
                data["files_filemanager"] = str(draft_itemid)
            else:
                log("This assignment doesn't take files, so they're left out.")
        if online_text:
            if takes_text:
                data["onlinetext_editor[text]"] = text_html(online_text)
            else:
                log("This assignment doesn't take online text, so it's left out.")
        if not ((draft_itemid is not None and takes_files) or (online_text and takes_text)):
            raise HebatError("This assignment doesn't accept what you gave the bot (files or online text). Submit by hand!")
        if form.find(attrs={"name": "submissionstatement"}):
            data["submissionstatement"] = "1"

        r = self.post("/mod/assign/view.php", timeout=(3, 20), data=data, allow_redirects=False)  # skip the next page
        if accepted(r):
            return
        page = parse(r.text)
        if find_form(page, "savesubmission"):  # Moodle shows the form again when it rejects it
            # A blind save on another login may have got in first, which makes this form out of date
            if landed(self.assignment_info(cmid)["state"], before):
                return
            errors = [e.get_text(" ", strip=True) for e in page.select(".invalid-feedback, .alert-danger, .error")]
            raise HebatError("HEBAT rejected the submission: " + ("; ".join(filter(None, errors)) or "no reason shown"))
        state = submission_state(page) or self.assignment_info(cmid)["state"]
        if state not in ("draft", "submitted"):
            raise HebatError(f"HEBAT didn't record the submission (status: {STATE_LABELS.get(state, 'no attempt')}). "
                             "Check HEBAT now and submit by hand!")

    def submit_for_grading(self, cmid):
        """The 'Submit assignment' step, sent without loading its page first: that form only ever holds these
        fields. True if HEBAT took it. Harmless on a submission that's already submitted."""
        return accepted(self.post("/mod/assign/view.php", timeout=CHECK_TIMEOUT, allow_redirects=False, data={
            "id": cmid, "action": "confirmsubmit", "sesskey": self.sesskey,
            "_qf__mod_assign_confirm_submission_form": "1", "submissionstatement": "1", "submitbutton": "Continue"}))

    def finalize(self, cmid):
        """The 'Submit assignment' step, for assignments that keep submissions as drafts until you confirm."""
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
    """Waits for the target time, then submits the moment HEBAT shows the submission form.

    job: cmid, name, course, course_id, at (aware datetime, HEBAT's clock), files (list of Path), online_text, finalize
    """

    def __init__(self, bot, job, log):
        super().__init__(daemon=True)
        self.bot, self.job, self.log = bot, job, log
        self.stop_flag = threading.Event()
        self.state = "starting"  # starting, waiting, polling, submitting, done, failed, stopped
        self.go = None  # this PC's timestamp for job["at"], after correcting for clock drift
        self.result = None
        self.last_hiccup = 0.0
        self.bots = [bot]  # one login per overlapping check; see more_logins
        self.ws_info = None  # set when the mobile-app route is used; see setup_ws

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
        self.log(f"Your clock vs HEBAT: {offset:+.2f}s (compensating)")

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

    def more_logins(self, count):
        """Extra logins for the checks around the target time. HEBAT (Moodle) locks a login's session while it
        answers a request, so overlapping checks on one login just wait in line on the server, and the save would
        wait behind them too. Logins that HEBAT ends straight away (a cap on logins per account) are dropped."""
        def login():
            bot = Hebat(self.bot.username, self.bot.password)
            bot.login()
            return bot

        pool = ThreadPoolExecutor(count)
        done, _ = wait([pool.submit(login) for _ in range(count - 1)], timeout=max(self.go - 25 - time.time(), 0))
        bots = [self.bot] + [f.result() for f in done if not f.exception()]
        checks = {pool.submit(b.alive): b for b in bots}
        done, _ = wait(checks, timeout=max(self.go - 15 - time.time(), 0))
        pool.shutdown(wait=False, cancel_futures=True)  # anything still out is too slow to count on
        self.bots = [checks[f] for f in done if f.result()] or [self.bot]
        self.log(f"Ready with {len(self.bots)} separate logins for checking.")

    def setup_ws(self):
        """Use the mobile-app route for the blind saves if HEBAT offers it (see Hebat.ws)."""
        course_id = self.job.get("course_id")
        try:
            self.ws_info = course_id and self.bot.ws_assignment(self.job["cmid"], course_id) or None
        except (HebatError, requests.RequestException):
            self.ws_info = None
        self.log("HEBAT offers the mobile-app route: the bot will use it for quicker, lighter checks." if self.ws_info
                 else "HEBAT's mobile-app route isn't available here, so the bot uses the website.")

    def warm(self):
        """Open a fresh connection on every login just before the checks start: HEBAT drops idle ones after a few
        seconds, and the first check shouldn't spend its time on the handshake."""
        sessions = [b.s for b in self.bots] + ([self.bot.ws_s] * WS_MAX if self.ws_info else [])

        def touch(s):
            try:
                s.head(BASE + CLOCK_PROBE, timeout=(1.5, 1.5))
            except requests.RequestException:
                pass

        with ThreadPoolExecutor(len(sessions)) as pool:
            list(pool.map(touch, sessions))

    def work(self):
        job, cmid = self.job, self.job["cmid"]
        self.go = job["at"].timestamp()
        immediate = self.go <= time.time() + 5  # "Submit now": nothing to aim at, so skip the clock and the early upload
        if not immediate:
            self.check_clock()
        status = self.soft("Reading the current status", lambda: self.bot.assignment_info(cmid))
        before = status and status["state"]
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

        if not immediate and self.go - time.time() > 30:
            self.setup_ws()
            self.more_logins(2 if self.ws_info else LOGINS)
        if not immediate and self.go - time.time() > 45:
            self.sleep_until(self.go - 40)
            self.check_clock()  # once more, right before it counts

        # With the files already up (or only text to send), the checks are the save itself; see wait_for_form.
        blind = not immediate and (draft is not None or not job["files"])
        if not immediate:
            self.sleep_until(self.go - BURST_FROM - 2)
            self.warm()
        self.sleep_until(self.go - BURST_FROM)
        self.state = "polling"
        self.log("Checking HEBAT for the submission form...")
        bot, form, page_html = self.wait_for_form(blind, draft, first_only=status is not None and before in (None, "new"))
        self.state = "submitting"
        if form is None:
            self.log(f"OPEN! Saved straight away ({time.time() - self.go:+.2f}s from the target time).")
        else:
            self.log("Got the submission form." if immediate else f"OPEN! ({time.time() - self.go:+.2f}s from the target time)")
            if job["files"] and draft is None:
                repo = bot.upload_repo_id(page_html)
                if not repo:
                    raise HebatError("Can't find HEBAT's file upload on the submission page. Submit by hand, quick!")
                draft = bot.upload_draft(job["files"], repo)
            self.save_with_retries(bot, form, draft, before)
            self.log(f"Saved ({time.time() - self.go:+.2f}s from the target time)." if not immediate else "Saved.")
        if job["finalize"] and (self.ws_info or {}).get("drafts") is not False:
            self.press_submit(bot, immediate)

        state = self.final_state(bot)
        if state == "draft" and job["finalize"]:
            bot.finalize(cmid)  # the quick way didn't go through: the slow way, which loads the page first
            state = "submitted"
        if state not in ("draft", "submitted"):
            raise HebatError(f"HEBAT didn't record the submission (status: {STATE_LABELS.get(state, 'no attempt')}). "
                             "Check HEBAT now and submit by hand!")
        self.state = "done"
        self.result = (f"Submitted for grading at {datetime.now(WIB):%H:%M:%S} WIB." if state == "submitted" else
                       "Saved as a DRAFT (you turned off 'Submit assignment'). Submit it on HEBAT when ready.")
        self.log(self.result)

    def press_submit(self, bot, immediate):
        """'Submit assignment', sent the moment the save is in and retried quickly while HEBAT is overloaded.
        Only ever after a save went through: HEBAT would happily submit an empty attempt. Repeats are harmless,
        since HEBAT refuses them once it's submitted."""
        end, logins = time.time() + 15, self.logins(bot)
        while True:
            try:
                took = (self.bot.ws_submit(self.ws_info["id"]) if self.ws_info else
                        next(logins).submit_for_grading(self.job["cmid"]))
                if took:
                    self.log("Pressed 'Submit assignment'." if immediate else
                             f"Pressed 'Submit assignment' ({time.time() - self.go:+.2f}s from the target time).")
                return  # not taken: already submitted (no drafts), or something the status check below catches
            except WsError:
                self.ws_info = None  # the website still works
            except requests.RequestException as e:
                if time.time() > end:
                    return
                self.hiccup(e)
                self.pause(0.4)

    def final_state(self, bot):
        """What HEBAT recorded, asked a few times: a network blip here shouldn't turn a submission into FAILED."""
        logins = self.logins(bot)
        for attempt in range(1, 6):
            try:
                return next(logins).assignment_info(self.job["cmid"])["state"]
            except requests.RequestException as e:
                if attempt == 5:
                    raise
                self.hiccup(e)
                self.pause(1)

    def logins(self, first):
        """Logins to try in turn, starting with the one that got in: if HEBAT keeps failing one, use another."""
        order = [first] + [b for b in self.bots if b is not first]
        while True:
            yield from order

    def wait_for_form(self, blind, draft, first_only):
        """Ask HEBAT until the assignment opens. Returns (bot, form, page_html): a website login to carry on with,
        and the form it was shown, or form None when a blind save already went through.

        Around the target time the checks overlap: a new one every 0.25s (0.1s in the peak, right around the
        target) without waiting for the last answer, each on its own login, so the opening is seen about one page
        load after it happens. With `blind`, the checks send the save itself instead of loading the form: HEBAT
        refuses it while the assignment is closed and takes it the moment it opens, a whole page load sooner.
        Those go over the mobile-app route when HEBAT offers it (lighter, and no limit on how many overlap).
        Outside the peak, every few checks still load the form, so a save HEBAT won't take blind falls back to
        the usual way."""
        cmid, text = self.job["cmid"], self.job["online_text"]
        events = queue.Queue()  # (kind, bot, result): every check reports back exactly once
        free = list(self.bots)  # website logins with no check out, so no two requests share one
        out = {"ws": 0, "web": 0}
        ws = blind and self.ws_info is not None

        def check(kind, bot):
            result = None
            try:
                if kind == "ws":
                    result = ("saved",) if self.bot.ws_save(self.ws_info["id"], draft, text) else None
                elif kind == "save":
                    saved, form, r = bot.blind_save(cmid, draft, text, first_only)
                    result = ("saved",) if saved else ("form", form, r.text) if form else None
                else:
                    form, r = bot.edit_form(cmid)
                    result = ("form", form, r.text) if form else None
            except requests.RequestException as e:
                self.hiccup(e)
            except Exception as e:  # handled in the loop: drop that login, or stop using the mobile-app route
                result = e
            events.put((kind, bot, result))

        def send(kind):
            if kind == "ws" and out["ws"] < WS_MAX:
                bot = None
            elif kind != "ws" and free:
                bot = free.pop(0)
            else:
                return False
            out["ws" if kind == "ws" else "web"] += 1
            pool.submit(check, kind, bot)
            return True

        pool = ThreadPoolExecutor(len(self.bots) + WS_MAX)
        deadline, slow_logged, sent, next_at = self.go + MAX_WAIT, False, 0, 0.0
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
                t = now - self.go
                peak, burst = -PEAK_FROM <= t < PEAK_UNTIL, t < BURST_UNTIL
                gap = ((WS_GAP if ws else PEAK_GAP) if peak else BURST_GAP if burst else
                       FAST_POLL if t < FAST_WINDOW else SLOW_POLL)
                wait_for = max(next_at - now, 0.002)
                if now >= next_at and (burst or out["ws"] + out["web"] == 0):  # after the burst, one at a time
                    scout = not blind or (not peak and (sent + 1) % SCOUT_EVERY == 0)
                    kind = "form" if scout else "ws" if ws else "save"
                    if send(kind) or (kind == "form" and ws and send("ws")):  # no login free: save instead
                        sent += 1
                        next_at = now + gap
                    wait_for = gap  # if all were busy, the next answer frees one and wakes the loop
                try:
                    kind, bot, result = events.get(timeout=min(wait_for, gap))
                except queue.Empty:
                    continue
                out["ws" if kind == "ws" else "web"] -= 1
                if isinstance(result, Exception):
                    if kind == "ws":
                        if ws:  # checks already out report the same error; say it once
                            self.log(f"The mobile-app route stopped working ({result}). Using the website instead.")
                        ws = self.ws_info = None
                    else:
                        self.bots = [b for b in self.bots if b is not bot]
                        self.log(f"A login stopped working ({result}). Carrying on with {len(self.bots)}.")
                    if not self.bots and not ws:
                        raise result
                    continue
                if result is None:
                    if bot:
                        free.append(bot)
                    continue
                if result[0] == "saved":
                    return bot or (free[0] if free else self.bot), None, None
                return bot, result[1], result[2]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # don't wait for checks still out

    def hiccup(self, e):
        now = time.time()
        if now - self.last_hiccup > 5:  # checks overlap, so one outage would otherwise log many times
            self.last_hiccup = now
            self.log(f"Network hiccup ({e.__class__.__name__}), retrying")

    def save_with_retries(self, bot, form, draft, before):
        cmid = self.job["cmid"]
        for attempt in range(1, 4):
            try:
                return bot.save(cmid, form, draft, self.job["online_text"], self.log, before)
            except requests.RequestException as e:
                self.log(f"Network error while saving ({e.__class__.__name__}), attempt {attempt} of 3.")
            time.sleep(1)
            try:  # the save may have gone through even though the answer got lost
                if landed(bot.assignment_info(cmid)["state"], before):
                    return
                form, _ = bot.edit_form(cmid)
            except requests.RequestException:
                continue
            if not form:
                raise HebatError("The submission form disappeared (did the assignment close?). Check HEBAT now!")
        raise HebatError("Saving failed 3 times because of network errors. Submit by hand on HEBAT now!")
