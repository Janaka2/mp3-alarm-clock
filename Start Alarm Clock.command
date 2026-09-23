#!/bin/bash
# macOS double-click launcher.  Creates a private virtual environment next to this
# file on first run, installs the two dependencies, then starts the alarm clock.
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

# (Re)create the environment if it is missing or its audio modules are broken.
if ! ".venv/bin/python" -c 'import pygame.mixer, sounddevice' >/dev/null 2>&1; then
  echo "First run: setting up (this takes about a minute)…"
  rm -rf .venv
  "$PY" -m venv .venv || exit 1
  ".venv/bin/python" -m pip install --quiet --upgrade pip
  ".venv/bin/python" -m pip install --quiet -r requirements.txt || {
    osascript -e 'display alert "Could not install dependencies" message "Check your internet connection and try again. Details are in the Terminal window." as critical'
    exit 1
  }
fi
exec ".venv/bin/python" alarm_clock.py
