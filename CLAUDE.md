# Alarm Clock – project notes for Claude Code

Single-file desktop alarm clock: `alarm_clock.py` (tkinter GUI, pygame-ce playback, sounddevice recording, yt-dlp + bundled ffmpeg for YouTube / SoundCloud links).
Launch: `bash "Start Alarm Clock.command"` (creates `.venv`, installs `requirements.txt`). Tests: `.venv/bin/python tests/test_logic.py` and `.venv/bin/python tests/test_schedules.py`.

## Skills (load the matching one before editing)
- `alarm-ux` – any change to the App class, Today / Schedules views, ring window, defaults, indicators
- `power-management` – PowerManager, Scheduler, GRACE, sleep/wake behaviour
- `audio-io` – Player, Recorder, LinkFetcher (YouTube / SoundCloud → links/*.mp3), volume, fade, mic permission
- `portable-packaging` – launchers, requirements, BASE_DIR, build_standalone.sh
- `plain-language-errors` – any messagebox / status / log wording
- `alarm-clock-testing` – what to run before saying a change works

## Proactive agents (`.claude/agents/`) – run them without being asked
- after UI / wording changes → `ux-reviewer`
- after PowerManager / Scheduler changes or a "didn't ring after sleep" report → `power-specialist`
- after requirements / launcher / build changes → `packaging-tester`
- after Player / Recorder / LinkFetcher / fade changes or a "no sound" / "link won't fetch" report → `audio-qa`

## Hard rules
- Only five runtime dependencies: `pygame-ce` (not `pygame`: no Python 3.14 wheels, source build lacks the mixer), `sounddevice`, `yt-dlp`, `imageio-ffmpeg` (ships a static ffmpeg wheel per platform; YouTube audio is AAC/Opus and must be converted to MP3) and `certifi` (a fresh python.org Python has no CA bundle until "Install Certificates.command" is run; yt-dlp uses certifi automatically). Every one must have wheels for macOS arm64 + x86_64, Windows x64, Linux x64.
- All data stays next to the app (`alarms.json`, `recordings/`, `links/`, `alarmclock.log`). Never write to the home dir (yt-dlp's cache is disabled for that reason).
- Alarms never stream: a link is downloaded and converted once when the user presses "Get the sound"; at ring time it is an ordinary file path.
- Tk widgets are touched only on the main thread; background threads post to `App.events`.
- Never auto-repeat the admin password prompt; a declined wake shows "NOT registered" + Retry.
- Before reporting "done": compile, run `tests/test_logic.py` and `tests/test_schedules.py`, and launch the GUI once.
