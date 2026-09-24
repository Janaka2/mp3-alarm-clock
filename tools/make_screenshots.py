"""Regenerate the README screenshots (macOS).

Launches the app with a throw-away alarms.json full of sample alarms, captures every state
the user manual shows with `screencapture`, and writes them to docs/screenshots/.
Your real alarms.json, recordings/ and alarmclock.log are never touched.

Run from a terminal that has the Screen Recording permission
(System Settings → Privacy & Security → Screen Recording):

    .venv/bin/python tools/make_screenshots.py

Notes
* "Wake from sleep" is simulated (the pmset call is skipped) so no password prompt
  appears while the shots are taken.  The status pills show the real wording.
* The recording shot uses the default microphone for ~3 seconds; the take is discarded.
* Alarms are rung with volume 0, so nothing is heard.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
sys.path.insert(0, ROOT)

import alarm_clock as ac  # noqa: E402

# Sample data lives under build/ (git-ignored) so the paths shown in the shots look tidy.
TMP = os.path.join(ROOT, "build", "sample")
SOUNDS = os.path.join(TMP, "Alarm sounds")
ac.DATA_FILE = os.path.join(TMP, "alarms.json")
ac.LOG_FILE = os.path.join(TMP, "alarmclock.log")
ac.REC_DIR = os.path.join(TMP, "recordings")

# Pretend the OS accepted the wake request instead of running pmset (no password prompt).
ac.PowerManager._apply_wake = lambda self, old, new: self._set_wake_result(True, new)

MAX_WIDTH = 1600     # px; keeps the repo light


def chime(path: str, seconds: float = 4.0) -> str:
    """A soft synthetic bell so the ring shots play something harmless (at volume 0 anyway)."""
    rate = 22050
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        env = math.exp(-1.2 * t)
        v = env * (math.sin(2 * math.pi * 880 * t) * 0.6 + math.sin(2 * math.pi * 1320 * t) * 0.3)
        frames += struct.pack("<h", int(v * 12000))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))
    return path


def write_data(alarms: list[dict], **settings) -> None:
    s = dict(ac.DEFAULT_SETTINGS, **settings)
    with open(ac.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"alarms": alarms, "settings": s}, f)


def sample_alarms() -> list[dict]:
    os.makedirs(SOUNDS, exist_ok=True)
    morning = chime(os.path.join(SOUNDS, "Morning chimes.wav"))
    voice = chime(os.path.join(SOUNDS, "Take your pills (my voice).wav"))
    talk = chime(os.path.join(SOUNDS, "Evening talk (2 hours).wav"))
    trip = date.today() + timedelta(days=12)
    today = date.today().isoformat()
    return [
        dict(id="a1", label="Wake up", date=today, time="06:30", repeat="daily", sound=morning, output="",
             volume=80, ring_minutes=10, mode="alarm", enabled=True, last_fired=None),
        dict(id="a2", label="Take medicine", date=today, time="21:00", repeat="weekdays", sound=voice, output="",
             volume=60, ring_minutes=5, mode="alarm", enabled=True, last_fired=None),
        dict(id="a3", label="Flight to Colombo", date=trip.isoformat(), time="04:45", repeat="once", sound=morning,
             output="", volume=100, ring_minutes=15, mode="alarm", enabled=False, last_fired=None),
        dict(id="a4", label="Evening talk", date=today, time="19:30", repeat="daily", sound=talk, output="",
             volume=70, ring_minutes=10, mode="play", enabled=True, last_fired=None),
    ]


# ----- capture helpers
def window_rect(win) -> tuple[int, int, int, int]:
    """Screen rectangle of a Tk toplevel including its title bar (points, not pixels)."""
    win.update_idletasks()
    geo = win.geometry()  # WxH+X+Y ; X,Y = outer frame origin on macOS
    _size, x, y = geo.split("+")
    x, y = int(x), int(y)
    title_h = max(0, win.winfo_rooty() - y)
    return x, y, win.winfo_width(), win.winfo_height() + title_h


def widget_rect(w, pad: int = 0) -> tuple[int, int, int, int]:
    w.update_idletasks()
    return w.winfo_rootx() - pad, w.winfo_rooty() - pad, w.winfo_width() + 2 * pad, w.winfo_height() + 2 * pad


def shoot(rect: tuple[int, int, int, int], name: str) -> str:
    x, y, w, h = rect
    path = os.path.join(OUT, name)
    subprocess.run(["screencapture", "-x", "-o", "-R", f"{x},{y},{w},{h}", path], check=True)
    width = subprocess.run(["sips", "-g", "pixelWidth", path], capture_output=True, text=True).stdout
    px = int(width.strip().rsplit(":", 1)[-1])
    if px > MAX_WIDTH:
        subprocess.run(["sips", "--resampleWidth", str(MAX_WIDTH), path], check=True, capture_output=True)
    print(f"  {name:22s} {w}x{h} pt")
    return path


def capture(win, name: str) -> str:
    return shoot(window_rect(win), name)


def capture_widget(w, name: str, pad: int = 0) -> str:
    return shoot(widget_rect(w, pad), name)


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("This helper uses macOS screencapture.")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    if subprocess.run(["screencapture", "-x", os.path.join(TMP, "probe.png")], capture_output=True).returncode:
        sys.exit("Screen Recording permission is missing for this terminal. "
                 "System Settings → Privacy & Security → Screen Recording, then run again.")
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(ac.REC_DIR, exist_ok=True)

    write_data([])                                   # first launch: nothing set yet
    app = ac.App()
    app.geometry("1400x840+80+60")
    app.lift(); app.focus_force()

    header = app.l_next.master.master                # dark header bar
    editor_card = app.l_editor_title.master.master.master
    when_frame = app.time_row.master
    sound_frame = app.l_rec.master
    steps: list = []

    def later(ms, fn):
        steps.append((ms, fn))

    def run_next():
        if not steps:
            return finish()
        ms, fn = steps.pop(0)
        app.after(ms, lambda: (fn(), run_next()))

    # 1. first launch – empty list, default new alarm, neutral status pills
    later(1500, lambda: capture(app, "first-launch.png"))

    # 2. the sample alarms appear; edit "Wake up"
    def load_samples():
        write_data(sample_alarms(), last_sound=os.path.join(SOUNDS, "Morning chimes.wav"), last_volume=80)
        app.store.load()
        app._refresh_list()
        app._apply_power()
        app.tree.selection_set("a1")
    later(200, load_samples)
    later(2500, lambda: capture(app, "main.png"))
    later(100, lambda: capture_widget(header, "header.png"))

    # 3. the editor with a one-time alarm (date row visible), and the whole-file mode
    later(100, lambda: app.tree.selection_set("a3"))
    later(600, lambda: capture_widget(editor_card, "editor.png"))
    later(100, lambda: app.tree.selection_set("a4"))
    later(600, lambda: capture_widget(when_frame, "when-play-whole-file.png", pad=8))
    later(100, lambda: app.tree.selection_set("a1"))

    # 4. more options
    later(300, app._toggle_more)
    later(600, lambda: capture_widget(app.more_card_outer, "more-options.png"))
    later(100, app._toggle_more)

    # 5. the "Where" list, dropped down
    def open_outputs():
        app.tk.call("ttk::combobox::Post", app.cb_output)
    def shoot_outputs():
        pop = app.tk.call("ttk::combobox::PopdownWindow", app.cb_output)
        px = int(app.tk.call("winfo", "rootx", pop)); py = int(app.tk.call("winfo", "rooty", pop))
        pw = int(app.tk.call("winfo", "width", pop)); ph = int(app.tk.call("winfo", "height", pop))
        cx, cy, cw, ch = widget_rect(app.cb_output.master)
        x0, y0 = min(cx, px) - 2, min(cy, py) - 6
        x1, y1 = max(cx + cw, px + pw) + 6, py + ph
        shoot((x0, y0, x1 - x0, y1 - y0), "where-list.png")
        app.tk.call("ttk::combobox::Unpost", app.cb_output)
    later(300, open_outputs)
    later(800, shoot_outputs)

    # 6. recording in progress (3 s, then discarded)
    def start_rec():
        dev = next((d[0] for d in app.devices if d[1] == app.v_mic.get()), None)
        try:
            app.recorder.start(dev, ac.REC_DIR)
        except Exception as e:
            print("  recording shot skipped:", e)
            return
        app.b_rec.config(text="■ Stop")
        app.l_rec.config(text="Recording…", foreground="")
        app._update_meter()
    def stop_rec():
        if app.recorder.recording:
            try:
                app.recorder.stop()
            except Exception:
                pass
        app.b_rec.config(text="🎤  Record my voice")
        app.l_rec.config(text="", foreground="")
        app.meter["value"] = 0
    later(300, start_rec)
    later(3000, lambda: capture_widget(sound_frame, "recording.png", pad=8))
    later(100, stop_rec)

    # 7. header when the password prompt for the OS wake was declined ("Try again" appears)
    def decline():
        ev = app.scheduler.next_event()
        app.power._set_wake_result(False, ev[0] - timedelta(seconds=60), "user declined", declined=True)
        app._tick_indicators()
    later(300, decline)
    later(400, lambda: capture_widget(header, "header-wake-declined.png"))
    later(100, app._retry_wake)

    # 8. ringing: main window with the red STOP bar, the alert itself, then snooze
    def ring_a1():
        a = dict(app.store.get("a1"), volume=0)
        app._ring(a, datetime.now().replace(hour=6, minute=30, second=0))
    def show_alert_again():
        win = app.ring_windows["a1"]
        win.deiconify(); win.lift(); app.update()
    later(300, ring_a1)
    later(1500, lambda: app.ring_windows["a1"].withdraw())   # the window manager needs a moment
    later(600, lambda: capture(app, "main-ringing.png"))
    later(100, show_alert_again)
    later(800, lambda: capture(app.ring_windows["a1"], "ring.png"))
    later(100, lambda: app._snooze(dict(app.store.get("a1"), volume=0)))
    later(1500, lambda: capture_widget(header, "header-snoozed.png"))
    def unsnooze():
        app.scheduler.snoozes.clear()
        app._apply_power(); app._tick_indicators()
    later(100, unsnooze)

    # 9. "play the whole file once" – the calmer Now playing card
    def ring_a4():
        a = dict(app.store.get("a4"), volume=0)
        app._ring(a, datetime.now().replace(hour=19, minute=30, second=0))
    def shoot_now_playing():
        win = app.ring_windows.get("a4")
        if not win:
            print("  now-playing shot skipped: the file had already finished")
            return
        win.lift(); app.update()
        print("  now playing:", win.geometry(), "viewable", win.winfo_viewable(), "audio busy", app.player.is_playing())
        capture(win, "now-playing.png")
    later(300, ring_a4)
    later(1300, shoot_now_playing)
    later(100, lambda: app._dismiss("a4"))

    def finish():
        app.power.set_keep_awake(False)
        app.destroy()

    print("writing to", OUT)
    app.after(1200, run_next)
    app.mainloop()
    shutil.rmtree(TMP, ignore_errors=True)
    os._exit(0)


if __name__ == "__main__":
    main()
