---
name: portable-packaging
description: How this tool is started by double-click and moved between machines – the .command/.bat/.sh venv launchers, the PyInstaller build_standalone.sh, the "data next to the app" rule and BASE_DIR resolution. Load before changing startup, file locations, dependencies or the build.
---

# Portability & double-click start

## Layout rule
Everything lives in one folder that can be copied anywhere:
```
Mp3Player/
  alarm_clock.py            single-file app
  requirements.txt          pygame-ce (NOT pygame: 2.6.1 has no Python 3.14 wheels and a source build silently lacks the mixer), sounddevice, yt-dlp, imageio-ffmpeg, certifi (CA bundle for yt-dlp on a fresh python.org install) – nothing else
  Start Alarm Clock.command macOS double-click
  Start Alarm Clock.bat     Windows double-click
  start_alarm_clock.sh      Linux
  build_standalone.sh       PyInstaller → dist/Alarm Clock.app / .exe
  alarms.json               created on first save (alarms + settings)
  recordings/               voice recordings
  links/                    sounds saved from YouTube / SoundCloud links (<extractor>_<id>.mp3), incoming/ holds partial downloads
  alarmclock.log            what happened and when
  .venv/                    created by the launcher on first run (never commit)
```
`BASE_DIR` (`_base_dir()`) resolves to the script folder, or to the folder that *contains* the `.app` / `.exe` when frozen, so data stays beside the app in both modes. Never write to the home directory or a temp dir.

## Launchers
- They create `.venv` on first run and `pip install -r requirements.txt`; afterwards start is instant. Keep them dependency-free shell/batch – no Python code in the launcher.
- The health check imports all five modules (`pygame.mixer, sounddevice, yt_dlp, imageio_ffmpeg, certifi`) so an old `.venv` is rebuilt when a dependency is added.
- Weekly `pip install -U --timeout 10 --retries 1 yt-dlp` guarded by the stamp file `.venv/.yt-dlp-updated` (macOS/Linux: `find -mtime -7`; Windows: `forfiles /D -7`). It must fail silently offline and never block the start for long; the stamp is only touched when pip really reached PyPI (pip exits 0 offline for an already-installed package, so `.venv/update.log` is grepped for `Retrying`).
- macOS launcher searches python.org and Homebrew paths explicitly because Finder gives `.command` files a minimal PATH; it also checks `import tkinter` because Homebrew Python needs `python-tk`.
- Windows uses `pythonw.exe` so no console window stays open.
- Adding a dependency = adding it to `requirements.txt` AND confirming a wheel exists for macOS arm64, macOS x86_64, Windows x64 (no compiler on user machines). Check with `pip download --only-binary=:all: --platform win_amd64 <pkg>`.

## Standalone build
`./build_standalone.sh` → PyInstaller `--windowed --collect-all yt_dlp --collect-all imageio_ffmpeg` (yt-dlp's extractors are lazy imports, imageio-ffmpeg's binary is package data), adds `NSMicrophoneUsageDescription` and ad-hoc codesigns on macOS. A frozen app cannot self-update yt-dlp; rebuild to pick up a newer one. Downloaded unsigned apps get Gatekeeper's "unidentified developer" warning: right-click → Open the first time. Say so in the README; don't try to bypass Gatekeeper.

## Checklist before saying "it's portable"
- Fresh clone, no `.venv`: double-click works and the GUI appears.
- Move the folder somewhere else: alarms and recordings come along, paths in `alarms.json` that point *inside* the folder still resolve (recordings are stored absolute today – if a user reports broken recordings after moving, make them relative to `BASE_DIR`).
- `python3 -m py_compile alarm_clock.py` passes; the headless logic test in [[alarm-clock-testing]] passes.
