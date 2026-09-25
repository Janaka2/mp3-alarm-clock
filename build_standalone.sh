#!/bin/bash
# Builds a self-contained "Alarm Clock.app" (macOS) or Alarm Clock/ folder with an .exe (Windows via Git Bash)
# that runs on machines WITHOUT Python installed.  Output lands in dist/.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
PY=.venv/bin/python; [ -x "$PY" ] || PY=.venv/Scripts/python.exe
"$PY" -m pip install -q -r requirements.txt pyinstaller
# yt-dlp loads its site extractors lazily and imageio-ffmpeg ships the ffmpeg binary as package data,
# so both must be collected whole or "Use a link…" fails inside the frozen app.
"$PY" -m PyInstaller --noconfirm --clean --windowed --name "Alarm Clock" \
  --collect-all yt_dlp --collect-all imageio_ffmpeg \
  --osx-bundle-identifier com.local.alarmclock alarm_clock.py
if [ "$(uname)" = "Darwin" ]; then
  PLIST="dist/Alarm Clock.app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'Needed to record voice alarms.'" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string 'Needed to schedule the system wake and set the volume.'" "$PLIST" 2>/dev/null || true
  codesign --force --deep --sign - "dist/Alarm Clock.app"
  echo "Built: dist/Alarm Clock.app  (copy the .app anywhere; alarms.json and recordings/ are kept next to it)"
else
  echo "Built: dist/Alarm Clock/  (copy the whole folder; run 'Alarm Clock.exe')"
fi
