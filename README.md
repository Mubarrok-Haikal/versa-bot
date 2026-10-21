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

| When | What versa-bot does |
|---|---|
| You press Start | Checks available assignment rules and current submission status. Checks the server clock and uploads files immediately if the target is less than 3 days away |
| While waiting | Stops Windows from sleeping. For targets farther away, defers the upload until preparation |
| 5 minutes before | Refreshes the login, keeping the existing session unless the new login succeeds. Re-checks rules/status and prepares extra website logins |
| 10 seconds before | Re-checks the server clock. Uses PHP's timestamp through Moodle's mobile API when available; explicitly labels the less precise HTTP Date fallback |
| About 5 seconds and 1.2 seconds before | Warms connections in background, without holding up the submission loop |
| 3 seconds before | Begins overlapping save attempts. The mobile route keeps only a few requests in flight outside the peak |
| 0.4 seconds before through 0.1 seconds after | Uses a short, shaped burst, at most 40 save requests/second. Adapts the concurrency ceiling from 6 to 16 using measured latency and reserves slots for the opening second |
| 0.1 seconds after | Stops launching mobile saves and waits for all outstanding answers. Sends another, single save only if every earlier save was explicitly refused because the assignment was not open |
| First accepted save | The accepting worker immediately sends the prepared “Submit assignment” request, where needed, before logging progress |
| Lost/blocked response or other refusal | Stops new saves, drains outstanding requests, and checks status. An ambiguous result is reported for manual inspection; it is not blindly retried |
| 2 minutes after | If HEBAT still explicitly reports not open, reduces checks to every 3 seconds |
| 30 minutes after | Drains requests, checks status once more, and stops if it cannot confirm a submission |

The preflight checks file/text support, file count, per-file size, allowed extensions, online-text word
limits, and timed assignments when HEBAT exposes those rules. It expands named file-type groups using
Moodle's own type list. Hidden rules or unavailable group definitions produce a warning. Timed assignments
require you to start and submit the attempt on HEBAT manually; the bot does not start their timer.
Refusals other than a recognized “not open yet” response stop the run with the server's reason.

Why it is quick:

- **The check is the save.** Once the draft upload is ready, the bot sends the save itself instead of loading
  a form first. The mobile save and submit requests are encoded before the burst.
- **Independent website logins and the mobile API.** Website checks use separate sessions to avoid Moodle's
  per-session lock. The mobile API uses lighter JSON responses, including for status checks.
- **A bounded mobile burst.** In-flight saves can still change Moodle's recorded time, especially on assignments
  with no separate submit step. The cutoff prevents new saves from extending that tail. It cannot cancel
  requests already being processed, and does not guarantee an opening-second timestamp.
- **Less work at the critical moment.** Form controls are extracted without constructing a full page DOM;
  certificate stores and proxy configuration are loaded once per connection setup/session, not per request.
  Normal TLS verification, custom CA bundles, and configured proxies remain supported.
- **Windows timing.** During the short burst, the bot requests 1 ms timer resolution, disables process power
  throttling where supported, and uses a monotonic performance clock for dispatch deadlines. It temporarily
  disables cyclic garbage collection and restores all changed settings afterward, including on errors.
- **Nonblocking console logs.** Console printing happens on a separate daemon thread with a bounded queue.
  Selecting text in a Windows console cannot block the submission or the in-app log.

HTTP errors, rate limits and challenge pages are outages, not evidence that an assignment is closed.
A timed-out save may still complete on the server. The bot checks for a changed submission after draining
requests; it will not report an unchanged, pre-existing submission as a newly recovered save. If status
cannot establish the outcome, inspect HEBAT before trying again.

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
are not a prediction of live HEBAT performance.

The PHP timestamp source is `core_calendar_get_calendar_upcoming_view` → `date.timestamp`:
[Moodle implementation](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/externallib.php)
and [date export](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/classes/external/calendar_upcoming_exporter.php).
The generic save-warning handling follows
[Moodle's assignment API](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/externallib.php).

## Disclaimer

versa-bot is an unofficial, student-made tool. It is not affiliated with or endorsed by Universitas
Airlangga. It submits only the files you give it, to assignments on your own account, the same way you
would by hand. You are responsible for what you submit and for checking that it arrived.

## License

MIT. See [LICENSE](LICENSE).
