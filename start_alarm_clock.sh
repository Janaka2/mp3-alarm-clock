#!/bin/bash
# Linux launcher (mark executable; on most desktops double-click -> "Run in terminal").
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3) || { echo "Install python3 and python3-tk first"; exit 1; }
if ! .venv/bin/python -c 'import pygame.mixer, sounddevice, yt_dlp, imageio_ffmpeg, certifi' >/dev/null 2>&1; then
  rm -rf .venv
  "$PY" -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || exit 1
  touch .venv/.yt-dlp-updated
fi
# YouTube changes often: refresh the link downloader about once a week (quietly skipped when offline).
if [ -z "$(find .venv -maxdepth 1 -name .yt-dlp-updated -mtime -7 2>/dev/null)" ]; then
  if .venv/bin/python -m pip install -q --upgrade --timeout 10 --retries 1 yt-dlp > .venv/update.log 2>&1 \
     && ! grep -q "Retrying\|connection broken" .venv/update.log; then
    touch .venv/.yt-dlp-updated
  fi
fi
exec .venv/bin/python alarm_clock.py
