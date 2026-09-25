#!/bin/bash
# macOS double-click launcher.  Creates a private virtual environment next to this
# file on first run, installs the dependencies, then starts the alarm clock.
cd "$(dirname "$0")" || exit 1
export PATH="/Library/Frameworks/Python.framework/Versions/Current/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

find_python() {
  for p in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys,tkinter; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

PY=$(find_python) || {
  osascript -e 'display alert "Python 3 with Tk is required" message "Install Python 3 from https://www.python.org/downloads/mac-osx/ (the python.org installer includes Tk), then double-click this file again." as critical'
  exit 1
}

# (Re)create the environment if it is missing or any of its modules are broken.
if ! ".venv/bin/python" -c 'import pygame.mixer, sounddevice, yt_dlp, imageio_ffmpeg, certifi' >/dev/null 2>&1; then
  echo "First run: setting up (this takes about a minute)…"
  rm -rf .venv
  "$PY" -m venv .venv || exit 1
  ".venv/bin/python" -m pip install --quiet --upgrade pip
  ".venv/bin/python" -m pip install --quiet -r requirements.txt || {
    osascript -e 'display alert "Could not install dependencies" message "Check your internet connection and try again. Details are in the Terminal window." as critical'
    exit 1
  }
  touch .venv/.yt-dlp-updated
fi

# YouTube changes often and old link downloaders stop working: refresh yt-dlp about once a week.
# Fails quietly when offline; the app still starts.
# (pip exits 0 offline when the package is already installed, so also check it really reached the index.)
if [ -z "$(find .venv -maxdepth 1 -name .yt-dlp-updated -mtime -7 2>/dev/null)" ]; then
  if ".venv/bin/python" -m pip install --quiet --upgrade --timeout 10 --retries 1 yt-dlp > .venv/update.log 2>&1 \
     && ! grep -q "Retrying\|connection broken" .venv/update.log; then
    touch .venv/.yt-dlp-updated
  fi
fi
exec ".venv/bin/python" alarm_clock.py
