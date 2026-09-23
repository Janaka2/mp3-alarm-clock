![visitors](https://visitor-badge.laobi.icu/badge?page_id=mp3-alarm.visitor-badge)
# ⏰ MP3 Alarm Clock

**A tiny desktop alarm clock that plays an MP3 — or your own recorded voice — at a scheduled time, keeps your computer awake, and wakes it from sleep when the alarm is due.**

Single Python file · macOS / Windows / Linux · starts with a double-click · the whole folder is portable.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Why

Phone alarms are fine until you want *that* song, at full volume, through real speakers, at a specific date — or a message in your own voice ("get up, the flight is at nine"). This app does exactly that and nothing else.

## Features

| | |
|---|---|
| 🎵 **Any sound** | MP3, WAV, OGG, FLAC — pick a file, or **record from the microphone** and use the recording as the alarm |
| 📅 **Real scheduling** | Date + time (24 h), repeat **once / every day / weekdays**, unlimited alarms |
| 🔊 **Volume you control** | Per-alarm volume, **fade-in** over N seconds, optional "force the system volume up" when ringing |
| 😴 **Sleep-proof** | Keeps the computer awake while an alarm is armed; optionally registers a **real OS wake** one minute before the alarm (macOS `pmset`, Windows wake timer, Linux `rtcwake`) |
| ⏱ **Catch-up** | If the machine was asleep at alarm time, it rings as soon as it wakes (up to 30 min late); older alarms are reported as *missed*, never silently dropped |
| 🛑 **Big red STOP** | One click, or Enter / Esc. Snooze with one click. Ring timeout so a forgotten alarm doesn't play forever |
| 👀 **Honest status** | Always-visible indicators: *armed + countdown* · *keeping computer awake* · *OS wake registered / NOT registered (+ Retry)* |
| 🧠 **Sensible defaults** | New alarm = next round hour, last sound, last volume. "Add alarm" with zero edits is a valid alarm |
| 📦 **Portable** | Everything (`alarms.json`, `recordings/`, `alarmclock.log`) lives next to the app. Copy the folder anywhere |

## Quick start

### 1. Double-click

| OS | Double-click this | Needs |
|---|---|---|
| macOS | `Start Alarm Clock.command` | [Python 3](https://www.python.org/downloads/mac-osx/) (python.org build, includes Tk) |
| Windows | `Start Alarm Clock.bat` | [Python 3](https://www.python.org/downloads/windows/) with *Add to PATH* ticked |
| Linux | `start_alarm_clock.sh` | `python3 python3-venv python3-tk libportaudio2` |

The first run creates a private `.venv` in the folder and installs the two dependencies (about a minute). Every run after that is instant. If the environment ever breaks, the launcher rebuilds it automatically.

### 2. Or build a standalone app (no Python needed on the target machine)

```bash
./build_standalone.sh          # → dist/Alarm Clock.app  (macOS)  or  dist/Alarm Clock/Alarm Clock.exe  (Windows)
```

Copy the result anywhere; data files are created next to it. An unsigned app downloaded from the internet gets Gatekeeper's warning once — right-click → **Open**.

### 3. Or run it like a developer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python alarm_clock.py
```

## Using it

```
┌ Alarms ──────────────────────────────────────────────────────────────────┐
│ On  Next ring          Repeat    Label        Sound              Vol     │
│ ✔   Thu 24 Sep 07:00   weekdays  Work         wakeup.mp3         80      │
│ ✔   Sat 26 Sep 09:30   once      Flight!      voice_2026-09-…    100     │
├ Alarm details ───────────────────────────────────────────────────────────┤
│ Label [Work        ]   Repeat (•) Once ( ) Every day ( ) Weekdays        │
│ Date  [2026]-[09]-[24] [Today][Tomorrow]   Time [07]:[00] [+1 min][+10]  │
│ Sound [~/Music/wakeup.mp3                 ] [Browse…] [▶ Test] [■ Stop]  │
│ Volume ────────●──────── 80 %         Ring for up to [10] min            │
│ Record voice [Built-in Microphone ▾] [● Record] ▮▮▮▮▯▯▯ 00:04            │
│ [Add alarm] [Clear form]                                                 │
├ Sleep / power ───────────────────────────────────────────────────────────┤
│ [x] Keep computer awake while an alarm is armed                          │
│ [x] Schedule a system wake before the next alarm (asks for password)     │
│ [x] When ringing, set the system output volume to [80] %  Snooze [5] min │
│ ● Armed – next ring in 8h 12m   ● Keeping computer awake   ● OS wake registered for Thu 06:59 │
└──────────────────────────────────────────────────────────────────────────┘
```

1. **Sound** — `Browse…` for a file, or choose a microphone, press `● Record`, speak, press `■ Stop`. The recording is saved to `recordings/` and selected automatically. `▶ Test` plays at the slider volume.
2. **When** — date, time, repeat. `+1 min` is handy to try a sound for real.
3. **Add alarm.** The list shows the next ring time and the status bar counts down.
4. **When it rings** a window pops on top with a big **STOP** and **Snooze**; the main window shows a red STOP bar too; volume fades in.

## How sleep & wake work

The app must be running for alarms to ring (closing it with an alarm armed asks you to confirm). Two safeguards in the *Sleep / power* box:

| | macOS | Windows | Linux |
|---|---|---|---|
| **Keep awake** while armed (no password) | `caffeinate` | `SetThreadExecutionState` | `systemd-inhibit` |
| **Wake from sleep** 1 min before the alarm | `pmset schedule wake` — asks for your password once per change | waitable timer with resume flag — allow *wake timers* in the power plan | `rtcwake` via `pkexec` |

The indicator row always says what is true *right now*. If the password prompt is declined, it says **OS wake NOT registered** and offers **Retry** — it never nags. Laptops with the lid closed may only "dark-wake" and stay silent: keep the lid open or plug in a display.

## Microphone permission (macOS)

The first recording triggers the system permission dialog for the app that launched it (Terminal when started from the `.command`, the app itself when built standalone). If a recording comes out silent, check *System Settings → Privacy & Security → Microphone*.

## Project layout

```
alarm_clock.py              the whole app (core logic + tkinter GUI, ~1 200 lines)
requirements.txt            pygame-ce, sounddevice — that's all
Start Alarm Clock.command   macOS launcher      Start Alarm Clock.bat   Windows launcher
start_alarm_clock.sh        Linux launcher      build_standalone.sh     PyInstaller build
tests/test_logic.py         headless tests for scheduling (next_fire, grace, snooze, missed)
.claude/                    Claude Code skills + proactive review agents used to build this
```

**Why pygame-ce and not pygame?** pygame 2.6 has no wheels for Python 3.14; pip silently builds it from source *without* the audio mixer. pygame-ce ships wheels for every current Python on all three platforms.

## Development

```bash
.venv/bin/python tests/test_logic.py      # scheduling logic
bash "Start Alarm Clock.command"           # GUI smoke test
```

Contributions welcome — the `.claude/skills/*.md` files double as the design notes (UX rules, power-management invariants, error-message style).

## License

MIT — see [LICENSE](LICENSE).
