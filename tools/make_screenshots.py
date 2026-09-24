"""Regenerate the README screenshots (macOS).

Launches the app with a throw-away alarms.json full of sample alarms, captures the main
window and the ring alert with `screencapture`, and writes them to docs/screenshots/.
Your real alarms.json is never touched.  Run from a terminal that has the
Screen Recording permission (System Settings → Privacy & Security → Screen Recording):

    .venv/bin/python tools/make_screenshots.py
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
sys.path.insert(0, ROOT)

import alarm_clock as ac  # noqa: E402

TMP = tempfile.mkdtemp(prefix="alarm-shots-")
ac.DATA_FILE = os.path.join(TMP, "alarms.json")
ac.LOG_FILE = os.path.join(TMP, "alarmclock.log")


def chime(path: str, seconds: float = 3.0) -> str:
    """A soft synthetic bell so the ring shot plays something harmless."""
    rate = 22050
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        env = math.exp(-2.5 * t)
        v = env * (math.sin(2 * math.pi * 880 * t) * 0.6 + math.sin(2 * math.pi * 1320 * t) * 0.3)
        frames += struct.pack("<h", int(v * 12000))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))
    return path


def sample_data() -> None:
    sounds = os.path.join(TMP, "sounds")
    os.makedirs(sounds, exist_ok=True)
    morning = chime(os.path.join(sounds, "Morning chimes.wav"))
    voice = chime(os.path.join(sounds, "Good morning (my voice).wav"))
    trip = date.today() + timedelta(days=12)
    alarms = [
        dict(id="a1", label="Wake up", date=date.today().isoformat(), time="06:30", repeat="daily",
             sound=morning, output="", volume=80, ring_minutes=10, enabled=True, last_fired=""),
        dict(id="a2", label="Take medicine", date=date.today().isoformat(), time="21:00", repeat="weekdays",
             sound=voice, output="", volume=60, ring_minutes=5, enabled=True, last_fired=""),
        dict(id="a3", label="Flight to Colombo", date=trip.isoformat(), time="04:45", repeat="once",
             sound=morning, output="", volume=100, ring_minutes=15, enabled=False, last_fired=""),
    ]
    settings = dict(ac.DEFAULT_SETTINGS, schedule_wake=False, last_sound=morning, last_volume=80)
    with open(ac.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"alarms": alarms, "settings": settings}, f)


def frame_rect(win) -> tuple[int, int, int, int]:
    """Screen rectangle of a Tk toplevel including its title bar (points, not pixels)."""
    win.update_idletasks()
    geo = win.geometry()  # WxH+X+Y ; X,Y = outer frame origin on macOS
    size, x, y = geo.split("+")
    x, y = int(x), int(y)
    title_h = max(0, win.winfo_rooty() - y)
    return x, y, win.winfo_width(), win.winfo_height() + title_h


def capture(win, name: str) -> str:
    x, y, w, h = frame_rect(win)
    path = os.path.join(OUT, name)
    subprocess.run(["screencapture", "-x", "-o", "-R", f"{x},{y},{w},{h}", path], check=True)
    # keep the repo light: resample to at most 1600 px wide
    subprocess.run(["sips", "--resampleWidth", "1600", path], check=True, capture_output=True)
    return path


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("This helper uses macOS screencapture.")
    if subprocess.run(["screencapture", "-x", os.path.join(TMP, "probe.png")], capture_output=True).returncode:
        sys.exit("Screen Recording permission is missing for this terminal. "
                 "System Settings → Privacy & Security → Screen Recording, then run again.")
    os.makedirs(OUT, exist_ok=True)
    sample_data()
    app = ac.App()
    app.geometry("1400x840+80+60")
    app.lift(); app.focus_force()

    def step1():
        app.tree.selection_set("a1")  # loads "Wake up" into the editor
        app.after(1800, step2)

    def step2():
        app._tick_indicators()
        print("main:", capture(app, "main.png"))
        a = dict(app.store.get("a1"), volume=0)  # silent ring for the shot
        app._ring(a, datetime.now().replace(hour=6, minute=30, second=0))
        app.after(1500, step3)

    def step3():
        win = app.ring_windows["a1"]
        print("ring:", capture(win, "ring.png"))
        app._dismiss("a1")
        app.after(300, finish)

    def finish():
        app.power.set_keep_awake(False)
        app.destroy()

    app.after(1200, step1)
    app.mainloop()
    os._exit(0)


if __name__ == "__main__":
    main()
