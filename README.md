# versa-bot

A desktop app that submits your assignment to **HEBAT UNAIR** (the university's Moodle) the moment it
opens.

Log in, open a matkul, pick the assignment, add your file and press **Start waiting**. When the
assignment opens, the bot attaches and submits it for you. Files are uploaded ahead of time when possible;
actual timing depends on HEBAT and your connection.

## Features

- **Log in with your HEBAT account.** Use your NIM and password, the same as UNAIR Satu.
- **Browse your matkul**, with a search box.
- **See every assignment in a matkul**, including ones locked with an "Available from ..." date.
  HEBAT's own assignment list hides locked ones, so versa-bot reads the course page instead.
- **Status at a glance:** each assignment is marked *Open now*, *Opens in ...*, *Submitted*, *Closed* or
  *Locked*.
- **Add files** with drag and drop or the file picker. There's a text box for assignments that ask for
  online text.
- **Three ways to time it:**
  - **As soon as it opens.** Uses the opening time HEBAT shows.
  - **At a time I set (WIB).** Any time you choose.
  - **Submit now.** Shown when the assignment is already open.
- **Submit for grading or keep a draft.** By default it also presses "Submit assignment"; turn that off
  to leave the submission as a draft.
- **Start and stop at any time.** A checklist and a countdown show what's ready and how long is left,
  and the bot log records every step.

## Requirements

- Python 3.9 or newer
- `requests` and `beautifulsoup4`
- Optional: `pywebview`, to open the app in its own window instead of a browser tab

## Install

Clone or download this repo, open a terminal in its folder, then run:

```bash
pip install -r requirements.txt
```

To give the app its own window:

```bash
pip install pywebview
```

## Usage

```bash
python versa_bot.py
```

The app opens in its own window if pywebview is installed, otherwise in your browser. To use the
browser even with pywebview installed:

```bash
python versa_bot.py --browser
```

1. Log in with your NIM and password.
2. Open the matkul.
3. Press **Pick** on the assignment.
4. Add your file.
5. Choose when to submit.
6. Press **Start waiting**, or **Submit now** if the assignment is already open.
7. Keep the app open until the sidebar says **Done**.

When it is done, the log confirms the submission status and the time of that confirmation. The time
shown in the log is not a claim about Moodle's recorded modification time. Open the assignment on HEBAT
to inspect the attached file and its recorded timestamp.

## How it works

Before the assignment opens, versa-bot does the slow parts. It checks that HEBAT will accept what you
gave it (file types, sizes, how many files, word limits), uploads your files, and logs in again 5 minutes
before so the session is fresh. It also measures how far your PC's clock is from HEBAT's, so 08:00:00 means
HEBAT's 08:00:00, not your laptop's.

A few seconds before the opening, it starts sending the save itself, many times a second. HEBAT refuses
each one until the assignment opens. When HEBAT accepts one, the bot presses **Submit assignment** straight
away, then reads the status back to confirm it.

If anything is unclear, such as a request that timed out or an error other than "not open yet", the bot
stops sending, works out what actually happened, and tells you. It never resends a save blindly, because a
save that timed out may still have gone through. If it can't tell, it says so; check HEBAT before trying
again.

Timed assignments aren't supported: start and submit those on HEBAT yourself.

Timings, request rates and the reasons behind them are in [For nerds](#for-nerds).

## FAQ

**Will it get logged out if it waits for days?**
It attempts a fresh login 5 minutes before the opening, and again automatically if HEBAT ever sends
it to the login page. Your HEBAT login in the browser is not affected, unless HEBAT limits how many logins
one account can have at once: then the bot's extra logins can log your browser out. Just log in there again.

**Can I close the app while it waits?**
No. Closing the app stops the bot. Minimise it instead.

**Can I close my laptop?**
Only if closing the lid doesn't put it to sleep. The bot stops Windows from sleeping when idle, but not
when the lid closes. Keep it open and plugged in, and watch out for Windows Update restarts. The
anti-sleep feature is Windows-only; on macOS or Linux, turn off sleep in your system settings.

**Where is my password stored?**
Only in the app's memory while it runs. It is never written to disk. Your NIM is remembered in
`hebat_settings.json` so you don't have to retype it; that file is excluded by `.gitignore`.

**Can other people or websites reach the app?**
No. It runs at `127.0.0.1`, so other devices can't connect to it. It also needs a random key that
changes every launch, so websites open in your browser can't send it commands.

**What if I change my UNAIR password while it's waiting?**
The fresh login may fail. The bot retains the previous session and warns you. If that session also expires,
stop the bot, log in with the new password, and start it again.

**An assignment says "Locked" but shows no opening date.**
It's locked by a condition other than a date, such as a group or another activity. Use **At a time I
set** instead.

**Does it work on other Moodle sites?**
It's built for HEBAT. Other Moodle 4.x sites may work if you set `HEBAT_BASE`:

```bash
HEBAT_BASE=https://your.moodle.site python versa_bot.py
```

## For nerds

What happens around the opening, and why. The tuning constants are at the top of `engine.py`.

### Why it's quick

1. **The check is the save.** There's no "load the page, find the form, fill it in, post it" after the
   opening. The final save request is built before the burst and sent as-is; HEBAT refuses it until the
   assignment opens.
2. **No session lock.** When HEBAT offers it, saves go through Moodle's mobile-app API, which doesn't use
   sessions at all, so many requests can be in flight at once. The replies are small JSON instead of full HTML pages.
3. **HEBAT's clock, not yours.** The bot aims at the server's PHP clock, measured to a fraction of a round
   trip.
4. **Dense around zero.** In the 0.2 seconds around the opening, a save leaves every 25 ms, over
   connections that were opened beforehand.
5. **No extra hop to submit.** The worker thread whose save is accepted sends the prepared **Submit
   assignment** request itself, before anything is logged.

### Two ways into Moodle

Moodle locks your session for the whole of each website request. Two requests on the same login cookie
don't run side by side on the server: the second waits for the first. Firing overlapping checks on one
login just builds a queue on HEBAT's side.

| Route | Used for | How it avoids the lock |
|---|---|---|
| Mobile-app API: a token from `/login/token.php` (service `moodle_mobile_app`), then calls to `/webservice/rest/server.php` | Saves, **Submit assignment**, status, the clock | Moodle runs web service requests with no session ("No sessions possible in web services" in `lib/setup.php`), so one token can carry many requests at once |
| Website, with several separate logins | Uploads and course pages, plus saves and submits when the mobile route isn't available for this assignment | Each login has at most one request in flight. 4 logins in total, or 2 when HEBAT offers the mobile route |

The extra website logins are made between 5 minutes and 25 seconds before the opening. Each is checked with
`core_session_touch` (deadline: 15 seconds before), and any that Moodle has already expired, for example
under a concurrent-login limit, is dropped.

### The check is the save

**Mobile route.** The bot calls `mod_assign_save_submission` with your draft file area and/or online text.
While the assignment is closed, Moodle doesn't save; it returns a warning. The code is the generic
`couldnotsavesubmission`, and the text is Moodle's `duedatereached` string, *"The due date for this
assignment has now passed"*, even when the assignment simply hasn't opened yet. So the bot:

- fetches that string in HEBAT's language with `core_get_strings`;
- reads it as "not open yet" only if the request was sent before the opening time (corrected for the clock
  offset). The same text after the opening is a real refusal and ends the run;
- treats an empty warning list as saved.

**Website route.** The bot posts the save form (`action=savesubmission`) without loading it first.
Redirects aren't followed: a redirect to `view.php` is the success signal, so no extra page is loaded. If
there was no attempt before the run, the post carries `lastmodified=1`. Moodle refuses a save when the
stored submission is newer than `lastmodified`, except while it is still new. The first save goes through;
every later one is refused instead of overwriting the recorded time. Outside the peak, every 4th website
check loads the real form instead, as a fallback if the blind post isn't recognised.

**Files** go up beforehand into a private draft area via `repository_ajax.php`, so the save only has to point
at it. If the opening is more than 3 days away, the upload waits until 5 minutes before, because Moodle
deletes draft areas older than 4 days.

**Preflight.** When HEBAT exposes the assignment's rules, the bot checks file/text support, file count,
per-file size (against both the assignment and your account limit), allowed extensions and the online-text
word limit, and refuses timed assignments, all before anything is uploaded. Named type groups such as
"document" are expanded through Moodle's own list (`core_form_get_filetypes_browser_data`). If the rules are
hidden, it logs a warning and leaves validation to HEBAT.

### Knowing HEBAT's clock

Moodle opens an assignment when `allowsubmissionsfromdate <= time()` on the server, in whole seconds. So the
only clock that matters is PHP's. The bot reads it through `core_calendar_get_calendar_upcoming_view`, whose
`date.timestamp` is PHP's `time()`. If that isn't available, it falls back to the HTTP `Date` header of a
static file, which comes from the web server or a proxy, and says so in the log.

Both sources only give whole seconds, so one reading can be up to a second off. `measure_clock` samples
every 25 ms for up to 3 seconds:

- Each sample is stamped with the local time at the middle of its round trip.
- When two samples less than 0.3 s apart show the second ticking over (*n*, then *n*+1), the tick happened
  between them, so the offset is *n*+1 minus the average of their two midpoints. It stops after two ticks
  and averages them.
- If no tick is caught, it uses the fastest sample and assumes the middle of the second.

The result is good to about half of (round trip + 25 ms), assuming the network delay is the same in both
directions. The clock is measured when you press Start, again 5 minutes before, and once more 10 seconds
before.

At 3 seconds before, the target is converted once into a `time.perf_counter()` deadline (`AimClock`). From
then on only that monotonic counter is used, so an NTP correction or a manual clock change during the burst
can't move the aim. Long waits before that wake at least every 20 seconds to re-check the wall clock, which
also covers sleep and resume. On Windows with Python older than 3.13, `time.time()` only ticks every
15.6 ms, so the bot reads `GetSystemTimePreciseAsFileTime` directly.

### Timeline

| When | What versa-bot does |
|---|---|
| You press Start | Reads the rules and current status, runs the preflight, measures the clock. Uploads files now if the opening is less than 3 days away |
| While waiting | Keeps Windows awake when idle. Re-checks the wall clock at least every 20 seconds |
| 5 minutes before | Logs in again on a new session, keeping the old one if that fails. Checks the mobile route, clock, rules and status again, uploads files if not done yet, and makes the extra website logins |
| 10 seconds before | Measures the clock one last time, then builds every save and submit request |
| 5 seconds before | Opens connections in background threads |
| 3 seconds before | Fixes the target to the monotonic clock, enters the Windows timing window, starts sending (next section) |
| 1.2 seconds before | Warms connections a second time |
| First accepted save | Same thread sends **Submit assignment**, then the bot drains outstanding requests and confirms the status |
| 5 seconds after | Leaves the Windows timing window, if the run hasn't already moved on |
| 2 minutes after | If HEBAT still says not open, slows to one check every 3 seconds |
| 30 minutes after | Drains requests, checks status once more, and gives up if it can't confirm a submission |

### The opening burst

Times are relative to the opening on HEBAT's clock.

| Window | Mobile route | Website route |
|---|---|---|
| 3 s to 0.4 s before | A save every 250 ms, at most 2 in flight | A check every 250 ms, one per login |
| 0.4 s to 0.1 s before | A save every 50 ms | A check every 100 ms |
| 0.1 s before to 0.1 s after | A save every 25 ms (40 per second) | A check every 100 ms |
| 0.1 s to 5 s after | One save at a time, at least 0.5 s apart, and only after every earlier save came back "not open" | A check every 250 ms |
| 5 s to 2 min after | Same as above | One check at a time, every 0.5 s |
| After 2 min | Every 3 s | Every 3 s |

**Why start before zero.** What counts is the save that reaches HEBAT just after it opens. With a one-way
network delay of *d*, that's the save sent at about *d* before the opening. Saves leave every 25 ms in the
last 0.1 s and every 50 ms before that, so on a steady connection one of them should land within about 25 to
50 ms of the opening, plus whatever error is left in the clock estimate.

**How many in flight.** The mobile limit starts at `ceil(round trip / 40 ms) + 2`, kept between 6 and 16,
using the round trip from the last clock check. During the burst it rises if refused saves come back slower.
Until the opening, `min(4, limit / 2)` slots are held back, so saves due right at the opening aren't stuck
behind earlier ones that are still waiting for an answer.

**Why stop at 0.1 s after.** Moodle stamps `timemodified` on every accepted save. On an assignment without a
separate submit step, the time HEBAT shows is the last save's, so extra saves landing after the first
accepted one would push it later. After 0.1 s, no new mobile save is launched (a worker that hasn't sent
yet cancels), and the first accepted save cancels everything still queued. Requests already on the wire
can't be recalled, so this narrows the tail but can't guarantee an opening-second timestamp.

**The dispatcher** is a single thread. Workers in a thread pool send requests and put results on a queue;
the dispatcher waits on that queue for at most 50 ms (at least 1 ms) until the next send is due. Each send
is scheduled from the current time, so a late wake-up never fires a backlog of catch-up requests. The first
peak send is pinned to exactly 0.4 s before.

**The winner** takes a lock, marks the save as done, tells the dispatcher to stop sending, and if it was
first, sends the prepared **Submit assignment** request on the same thread. Logging comes after.

### Less work on the hot path

- **Prepared requests.** `Session.prepare_request` builds URL, headers, cookies and form-encoded body before
  the burst. Moodle's REST API wants nested parameters as `plugindata[files_filemanager]=...`; `flatten`
  does that once. During the burst, workers only call `send()` on the same prepared object.
- **Warm connections.** At 5 s and again at 1.2 s before, background threads send `HEAD /pix/i/loading.gif`
  on every website login and on as many mobile connections as the in-flight limit, so the burst reuses open
  keep-alive connections instead of paying for TCP and TLS handshakes.
- **TLS setup once.** The CA bundle is parsed into one `ssl.SSLContext` per bundle path and shared by every
  connection. A small adapter stops urllib3 from loading the bundle into that context again for each new
  connection. Certificates are still verified, and `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` still work.
- **Proxies once.** Proxy settings, including `NO_PROXY`, are resolved when a session is created. Then
  `trust_env` is turned off, so requests stops reading environment variables, the Windows registry and
  `.netrc` on every send.
- **No hidden retries.** `max_retries=0`, so urllib3 never resends a save on its own. Connection pools are
  sized to the concurrency (18 per session).
- **No DOM.** The form check in `forms.py` uses regular expressions on the `<form>` element instead of
  building a BeautifulSoup tree for the whole page.
- **Nonblocking console.** Log lines reach the console through a bounded queue and a separate thread.
  Selecting text in a Windows console blocks writes to it; with the queue, console lines are dropped
  instead of the bot stalling. The in-app log keeps everything.

### Windows timing

From 3 seconds before to 5 seconds after the opening, the bot runs inside `PrecisionWindow`
(`runtime.py`):

- `timeBeginPeriod(1)`. Windows' default timer tick is 15.6 ms, so a short timed wait can overshoot by up to
  a tick. This lowers the tick to 1 ms.
- `SetProcessInformation(ProcessPowerThrottling)`. Opts the process out of EcoQoS execution-speed
  throttling, and out of Windows 11 ignoring timer-resolution requests from minimised or hidden windows.
  This is why minimising the app while it waits is fine.
- `gc.collect()`, then `gc.disable()`. One full garbage collection up front, then no cyclic-GC pause in
  the middle of the burst.
- Everything is restored afterwards, including when the run fails.

For the whole run, `SetThreadExecutionState` keeps Windows from going to sleep when idle.

### When things go wrong

A save isn't idempotent: every accepted save rewrites the submission and its `timemodified`. So the bot
never resends a save just because its answer didn't come back.

- Timeouts, HTTP errors, Moodle rate-limit and maintenance codes, and challenge pages ("Just a moment",
  captchas, "Too Many Requests") count as outages, never as "closed". `Retry-After` is honoured, in seconds
  or as a date.
- After a lost or blocked save answer, the bot stops sending, waits for every request in flight to return,
  and reads the status. It counts as saved only if the submission changed compared with the snapshot taken
  before the run: a new state, or a newer `timemodified`. A submission that was already there doesn't count.
- Any refusal other than "not open yet" (wrong file type, too many files, and so on) ends the run with
  HEBAT's own message.
- Website form checks get a 1-second cooldown after a failure; the third failure ends the run.
- A fresh login is made on a new session and swapped in only if it succeeds, so a failed login keeps the
  working cookie and `sesskey`. Threads that hit an expired session at the same moment share one re-login.

### The local app

`versa_bot.py` is a `ThreadingHTTPServer` on `127.0.0.1:8765` (or a free port if that one is taken) that
serves `ui.html` and a small JSON API. pywebview or your browser just shows that page. Each launch makes a
random token and puts it in the URL fragment (`#...`). Browsers never send the fragment to a server; the
page reads it and sends it back in an `X-Token` header, and requests without it get 403. The server also
checks the `Host` header, which stops DNS-rebinding tricks from other websites. Your password stays in
memory, and the files you add sit in a temporary folder that's deleted when the app exits.

### Numbers

Against the fake Moodle from the tests, which has no network delay and accepts a save that arrives at or
after the opening (Linux, Python 3.13, a few dozen runs; rough figures):

| Fake server response time | First accepted save, after the opening | Saves sent per run |
|---|---|---|
| 50 ms | about 3 to 4 ms typical, worst seen about 16 ms | about 25 |
| 300 ms | about 3 to 4 ms typical, worst seen about 6 ms | about 26 |

That measures only the bot's own scheduling. On the real HEBAT, network delay and clock error come on top,
and Windows timing differs from Linux, so this isn't a prediction of live results.

<details>
<summary>Run it yourself</summary>

Save as `bench.py` in the repo folder and run `python bench.py`:

```python
import statistics, sys
sys.path[:0] = ['.', 'tests']
from test_engine import FakeBot, runner

for latency in (0.05, 0.3):
    firsts, counts = [], []
    for _ in range(15):
        bot = FakeBot(latency=latency)
        r = runner(bot, delta=3.2, drafts=False)
        r.wait_for_form(True, None, True)
        saves = [t for kind, t in bot.calls if kind == 'save']
        firsts.append(min(t for t in saves if t >= 0) * 1000)
        counts.append(len(saves))
    print(f'{latency * 1000:.0f} ms: first accepted save {statistics.median(firsts):.1f} ms median, '
          f'{max(firsts):.1f} ms worst; {min(counts)} to {max(counts)} saves per run')
```

</details>

### Tuning knobs

| Constant in `engine.py` | Default | What it controls |
|---|---|---|
| `BURST_FROM`, `BURST_GAP` | 3 s, 0.25 s | When sending starts, and the gap before the peak |
| `PEAK_FROM`, `PEAK_UNTIL`, `PEAK_GAP` | 0.4 s, 0.1 s, 0.1 s | The peak window around the opening, and the website gap inside it |
| `WS_GAP`, `WS_MAX` | 25 ms, 16 | Mobile gap at the opening, and the most mobile requests in flight |
| `SAVE_CUTOFF` | 0.1 s | No new mobile saves after this |
| `BURST_UNTIL` | 5 s | End of the burst and the Windows timing window |
| `FAST_POLL`, `SLOW_POLL`, `FAST_WINDOW` | 0.5 s, 3 s, 120 s | Polling after the burst |
| `LOGINS` | 4 | Website logins (2 when the mobile route is available) |
| `SCOUT_EVERY` | 4 | Every *n*th website check outside the peak loads the real form |
| `PREPARE_FROM`, `EARLY_UPLOAD_MAX` | 5 min, 3 days | The fresh-login step, and how early files are uploaded |
| `CHECK_TIMEOUT` | 3 s connect, 8 s read | Timeouts for checks and saves |
| `MAX_WAIT` | 30 min | When the bot gives up |

More requests don't mean an earlier save. With saves 25 ms apart, the spacing is already smaller than the
possible clock error unless your round trip to HEBAT is under 25 ms. And every extra request lands on the
same server your classmates are submitting to.

### Sources

Moodle 4.5 code the bot relies on:

- PHP clock: `core_calendar_get_calendar_upcoming_view` returns `date.timestamp`
  ([implementation](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/externallib.php),
  [date export](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/classes/external/calendar_upcoming_exporter.php)).
- Save warnings: `mod_assign_save_submission` turns a closed assignment into `couldnotsavesubmission` with
  the `duedatereached` text
  ([mod/assign/externallib.php](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/externallib.php)).
- Opening check and the `lastmodified` guard: `submissions_open()` and `save_submission()`
  ([mod/assign/locallib.php](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/locallib.php)).
- No sessions for web services
  ([lib/setup.php](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/lib/setup.php)).
- Draft areas older than 4 days are deleted
  ([lib/filestorage/file_storage.php](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/lib/filestorage/file_storage.php)).

## Files

| File | Purpose |
|---|---|
| `versa_bot.py` | Entry point. Starts the app and opens the window |
| `engine.py` | Logs in, reads HEBAT, waits, uploads and submits |
| `ui.html` | The interface |
| `forms.py` | Fast form-control extraction |
| `runtime.py` | TLS/proxy setup and temporary timing settings |
| `tests/test_engine.py` | Offline regression and delayed-response simulations |
| `requirements.txt` | Python dependencies |

## Verification

Run the offline tests without contacting HEBAT or submitting any files:

```bash
python -m unittest discover -s tests -v
```

They exercise delayed responses, cutoff/concurrency behavior, refusal handling, timeout reconciliation,
file rules, login rollback, parsing, clock selection, and timing-setting cleanup. Synthetic timing results
are not a prediction of live HEBAT performance. The Moodle code the bot depends on is listed under
[Sources](#sources).

## Disclaimer

versa-bot is an unofficial, student-made tool. It is not affiliated with or endorsed by Universitas
Airlangga. It submits only the files you give it, to assignments on your own account, the same way you
would by hand. You are responsible for what you submit and for checking that it arrived.

## License

MIT. See [LICENSE](LICENSE).
