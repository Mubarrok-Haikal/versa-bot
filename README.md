# versa-bot

A desktop app that submits your assignment to **HEBAT UNAIR** (the university's Moodle) the moment it
opens.

Log in, open a matkul, pick the assignment, add your file and press **Start waiting**. When the
assignment opens, the bot uploads and submits it for you, usually within a second.

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

When it's done, the log shows `Submitted for grading at HH:MM:SS WIB`. It's still worth opening the
assignment on HEBAT to see your file there.

## How it works

| When | What versa-bot does |
|---|---|
| You press Start | Compares your PC's clock with HEBAT's, so the bot aims at HEBAT's time, not your PC's |
| While waiting | Sleeps, and stops Windows from going to sleep |
| 2 minutes before | Logs in again for a fresh session, re-checks the clock and uploads your file ahead of time |
| 3 seconds before | Checks whether the submission form is open every 0.2 seconds, with up to 4 checks in flight so a slow reply doesn't hold up the next one |
| 5 seconds after | If it still hasn't opened, checks every 0.5 seconds |
| When it opens | Attaches the uploaded file, saves, presses "Submit assignment", then confirms HEBAT recorded it |
| 2 minutes after | If it still hasn't opened, checks every 3 seconds instead |
| 30 minutes after | If it still hasn't opened, stops and tells you, so you can submit by hand |

If HEBAT is slow to respond while the bot waits, the bot logs it and keeps going. It only stops on
errors that actually prevent the submission.

## FAQ

**Will it get logged out if it waits for days?**
No problem. It logs in again 2 minutes before the opening, and again automatically if HEBAT ever sends
it to the login page. Your HEBAT login in the browser is not affected.

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
The fresh login before the opening will fail. Stop the bot, log in again, and start it again.

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
| `requirements.txt` | Python dependencies |

## Disclaimer

versa-bot is an unofficial, student-made tool. It is not affiliated with or endorsed by Universitas
Airlangga. It submits only the files you give it, to assignments on your own account, the same way you
would by hand. You are responsible for what you submit and for checking that it arrived.

## License

MIT. See [LICENSE](LICENSE).
