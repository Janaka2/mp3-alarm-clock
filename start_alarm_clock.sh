#!/bin/bash
# Linux launcher (mark executable; on most desktops double-click -> "Run in terminal").
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3) || { echo "Install python3 and python3-tk first"; exit 1; }
if ! .venv/bin/python -c 'import pygame.mixer, sounddevice' >/dev/null 2>&1; then
  rm -rf .venv
  "$PY" -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python alarm_clock.py
