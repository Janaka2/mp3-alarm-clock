#!/usr/bin/env python3
"""
Alarm Clock
===========
Schedule MP3 (or your own voice recording) alarms with a simple desktop GUI.

* Pick any MP3 / WAV / OGG / FLAC file, or record a voice message from the mic.
* Set date + time, repeat (once / daily / weekdays), volume and ring duration.
* Keeps the computer awake while an alarm is armed, and (optionally) schedules a
  real system wake so the alarm still fires if the machine was put to sleep.
* Everything (alarms.json, recordings/, alarmclock.log) lives next to this file,
  so the whole folder is portable.

Runs on macOS, Windows and Linux.  Requires: pygame, sounddevice (see requirements.txt).
"""
from __future__ import annotations

import atexit
import shutil
import webbrowser
import ctypes
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import uuid
import wave
from array import array
from datetime import date, datetime, timedelta
from datetime import time as dtime

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

APP_NAME = "Alarm Clock"
DEVELOPER = "Janaka Premathilaka"
WEBSITE = "https://janaka.me"          # developer site, linked from the footer
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

GRACE = timedelta(minutes=30)          # fire an alarm that is overdue by at most this much (e.g. after sleep)
WAKE_LEAD_SECONDS = 60                 # wake the machine this many seconds before the alarm
SUPPORTED_AUDIO = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aiff", ".aif")
DATA_VERSION = 2                       # alarms.json schema version (1 = alarms only, 2 = + schedules)
SCHEDULE_GRACE = timedelta(minutes=2)  # a routine message older than this is shown as missed, never played late
OCCURRENCE_KEEP_DAYS = 7               # how long played/missed history of routine messages is kept


# --------------------------------------------------------------------------- paths
def _base_dir() -> str:
    """Folder that holds alarms.json / recordings.  Next to the script, or next to the .app / .exe."""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        marker = ".app/Contents/MacOS"
        if IS_MAC and marker in exe:
            app_bundle = exe[: exe.index(marker) + 4]
            return os.path.dirname(app_bundle)
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
DATA_FILE = os.path.join(BASE_DIR, "alarms.json")
REC_DIR = os.path.join(BASE_DIR, "recordings")
LOG_FILE = os.path.join(BASE_DIR, "alarmclock.log")


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- alarms
DEFAULT_SETTINGS = {
    "keep_awake": True,
    "schedule_wake": True,
    "force_system_volume": IS_MAC,
    "system_volume": 80,
    "snooze_minutes": 5,
    "fade_seconds": 20,          # ramp volume from 0 to the alarm volume over this many seconds
    # remembered from the last alarm you saved, so a new alarm needs no changes to be useful
    "last_sound": "",
    "last_volume": 80,
    "last_repeat": "once",
    "last_output": "",           # "" = system default output device
    "last_mode": "alarm",        # alarm = ring (loop) until stopped | play = play the whole file once
}


def next_round_hour(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def new_alarm(settings: dict | None = None) -> dict:
    s = settings or DEFAULT_SETTINGS
    when = next_round_hour()
    last_sound = s.get("last_sound") or ""
    return {
        "id": uuid.uuid4().hex,
        "label": "Alarm",
        "date": when.date().isoformat(),
        "time": when.strftime("%H:%M"),
        "repeat": s.get("last_repeat", "once"),   # once | daily | weekdays
        "sound": last_sound if os.path.isfile(last_sound) else "",
        "output": s.get("last_output", ""),       # sound output device name, "" = system default
        "volume": int(s.get("last_volume", 80)),
        "mode": s.get("last_mode", "alarm"),      # alarm | play (whole file once, e.g. a 2-hour talk)
        "ring_minutes": 10,
        "enabled": True,
        "last_fired": None,
    }


def alarm_time(a: dict) -> dtime:
    h, m = (int(x) for x in a["time"].split(":"))
    return dtime(h, m)


def next_fire(a: dict, now: datetime | None = None) -> datetime | None:
    """Next moment this alarm should ring (may be slightly in the past if overdue), or None."""
    if not a.get("enabled"):
        return None
    now = now or datetime.now()
    t = alarm_time(a)
    start = date.fromisoformat(a["date"])
    last = datetime.fromisoformat(a["last_fired"]) if a.get("last_fired") else None

    if a["repeat"] == "once":
        dt = datetime.combine(start, t)
        return None if (last and dt <= last) else dt

    first = max(start, (now - GRACE).date())
    for i in range(0, 10):
        cand = datetime.combine(first + timedelta(days=i), t)
        if a["repeat"] == "weekdays" and cand.weekday() >= 5:
            continue
        if last and cand <= last:
            continue
        if cand >= now - GRACE:
            return cand
    return None


class AlarmStore:
    """Thread-safe alarms + schedules + settings persisted to alarms.json (schema DATA_VERSION)."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.alarms: list[dict] = []
        self.schedules: list[dict] = []
        self.exceptions: dict[str, dict] = {}     # "YYYY-MM-DD" → {"schedules": [ids], "events": [ids]}  (skip today)
        self.occurrences: dict[str, dict] = {}    # "sid:eid:YYYY-MM-DD" → {"status", "at", "note"}
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.load_problem = ""                    # plain-language text when the file could not be read
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            log(f"Could not read {self.path}: {e}")
            kept = f"{self.path}.broken-{datetime.now():%Y%m%d-%H%M%S}"
            try:
                shutil.copyfile(self.path, kept)
            except OSError:
                kept = self.path
            self.load_problem = (f"The alarms file could not be read, so the app starts empty.\n\n"
                                 f"The original file was kept as\n{kept}\n"
                                 "If you need the old alarms back, quit, fix or rename that file to alarms.json, and start again.\n\n"
                                 f"Details: {e}")
            return
        version = int(data.get("version", 1) or 1)
        if version < DATA_VERSION:
            backup = f"{self.path}.backup-v{version}"
            if not os.path.exists(backup):
                try:
                    shutil.copyfile(self.path, backup)
                    log(f"upgrading alarms.json from version {version} to {DATA_VERSION}; backup kept at {backup}")
                except OSError as e:
                    log(f"could not back up alarms.json before upgrade: {e}")
        self.alarms = data.get("alarms", [])
        self.settings.update(data.get("settings", {}))
        self.schedules = [self._fix_schedule(x) for x in data.get("schedules", [])]
        self.exceptions = data.get("exceptions", {}) or {}
        self.occurrences = data.get("occurrences", {}) or {}
        for occ in self.occurrences.values():
            if occ.get("status") in ("queued", "playing"):
                occ.update(status="missed", note="the app was closed before it finished")
        self.prune(date.today())

    @staticmethod
    def _fix_schedule(x: dict) -> dict:
        s = dict(new_schedule(), **x)
        s["days"] = sorted({int(d) for d in s.get("days", []) if 0 <= int(d) <= 6})
        s["events"] = [dict(new_event(), **e) for e in s.get("events", [])]
        return s

    def save(self) -> None:
        with self.lock:
            data = {"version": DATA_VERSION, "alarms": self.alarms, "settings": self.settings,
                    "schedules": self.schedules, "exceptions": self.exceptions, "occurrences": self.occurrences}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    # ----- alarms
    def upsert(self, alarm: dict) -> None:
        with self.lock:
            for i, a in enumerate(self.alarms):
                if a["id"] == alarm["id"]:
                    self.alarms[i] = alarm
                    break
            else:
                self.alarms.append(alarm)
            self.save()

    def delete(self, alarm_id: str) -> None:
        with self.lock:
            self.alarms = [a for a in self.alarms if a["id"] != alarm_id]
            self.save()

    def get(self, alarm_id: str) -> dict | None:
        with self.lock:
            return next((a for a in self.alarms if a["id"] == alarm_id), None)

    def next_alarm(self, now: datetime | None = None) -> tuple[datetime, dict] | None:
        now = now or datetime.now()
        with self.lock:
            events = [(nf, a) for a in self.alarms if (nf := next_fire(a, now))]
        return min(events, key=lambda e: e[0]) if events else None

    # ----- schedules
    def get_schedule(self, sid: str) -> dict | None:
        with self.lock:
            return next((x for x in self.schedules if x["id"] == sid), None)

    def upsert_schedule(self, sched: dict) -> None:
        with self.lock:
            for i, x in enumerate(self.schedules):
                if x["id"] == sched["id"]:
                    self.schedules[i] = sched
                    break
            else:
                self.schedules.append(sched)
            self.save()

    def delete_schedule(self, sid: str) -> None:
        """Removes the schedule and its events only.  Audio files are never deleted here."""
        with self.lock:
            self.schedules = [x for x in self.schedules if x["id"] != sid]
            for day in self.exceptions.values():
                day["schedules"] = [x for x in day.get("schedules", []) if x != sid]
            self.save()

    def sound_users(self, path: str) -> int:
        """How many alarms/events refer to this audio file (used before deleting a recording)."""
        target = os.path.normcase(os.path.abspath(resolve_sound(path)))
        n = 0
        with self.lock:
            for a in self.alarms:
                if a.get("sound") and os.path.normcase(os.path.abspath(resolve_sound(a["sound"]))) == target:
                    n += 1
            for x in self.schedules:
                for e in x["events"]:
                    if e.get("sound") and os.path.normcase(os.path.abspath(resolve_sound(e["sound"]))) == target:
                        n += 1
        return n

    # ----- skip today (date exceptions)
    def is_skipped(self, day: date, sid: str, eid: str | None = None) -> bool:
        ex = self.exceptions.get(day.isoformat(), {})
        if sid in ex.get("schedules", []):
            return True
        return eid is not None and eid in ex.get("events", [])

    def set_skip(self, day: date, sid: str | None, eid: str | None, on: bool) -> None:
        """Skip (or un-skip) a whole schedule (eid None) or one event for one date."""
        with self.lock:
            ex = self.exceptions.setdefault(day.isoformat(), {"schedules": [], "events": []})
            kind, ident = ("events", eid) if eid else ("schedules", sid)
            lst = ex.setdefault(kind, [])
            if on and ident not in lst:
                lst.append(ident)
            if not on and ident in lst:
                lst.remove(ident)
            if not ex["schedules"] and not ex["events"]:
                self.exceptions.pop(day.isoformat(), None)
            self.save()

    # ----- occurrence history (prevents replay after restart / sleep / repeated ticks)
    def record(self, key: str, status: str, note: str = "") -> None:
        with self.lock:
            self.occurrences[key] = {"status": status, "at": datetime.now().isoformat(timespec="seconds"), "note": note}
            try:
                self.save()
            except OSError as e:          # keep going: the in-memory record still prevents a replay in this run
                log(f"could not save alarms.json after recording {key} as {status}: {e}")

    def prune(self, today: date) -> None:
        """Drop exceptions for past days and old occurrence history.  Today's skip expires by itself."""
        with self.lock:
            self.exceptions = {d: v for d, v in self.exceptions.items() if d >= (today - timedelta(days=1)).isoformat()}
            cutoff = (today - timedelta(days=OCCURRENCE_KEEP_DAYS)).isoformat()
            self.occurrences = {k: v for k, v in self.occurrences.items() if k.rsplit(":", 1)[-1] >= cutoff}

    def event_due(self, sched: dict, ev: dict, day: date) -> datetime | None:
        """When this event would play on `day`, or None if it is not eligible that day
        (schedule off, day not selected, event off, no sound, skipped, or already handled)."""
        if not sched["enabled"] or day.weekday() not in sched["days"] or not ev["enabled"] or not ev.get("sound"):
            return None
        if self.is_skipped(day, sched["id"], ev["id"]) or occurrence_key(sched["id"], ev["id"], day) in self.occurrences:
            return None
        try:
            return datetime.combine(day, alarm_time(ev))
        except ValueError:
            return None

    def next_announcement(self, now: datetime | None = None) -> tuple[datetime, dict] | None:
        """The next routine message that will actually play (audible, enabled, not skipped, not yet handled)."""
        now = now or datetime.now()
        best: tuple[datetime, dict] | None = None
        with self.lock:
            first = (now - SCHEDULE_GRACE).date()          # a 23:59 message is still valid at 00:01
            for day_offset in range(0, 9):
                day = first + timedelta(days=day_offset)
                for sched in self.schedules:
                    for ev in sched["events"]:
                        due = self.event_due(sched, ev, day)
                        if due is None or due < now - SCHEDULE_GRACE or not local_time_exists(due):
                            continue
                        if best is None or due < best[0]:
                            best = (due, {"id": "event:" + occurrence_key(sched["id"], ev["id"], day), "kind": "event",
                                          "label": f"{ev['label']} · {sched['name']}", "output": sched.get("output", ""),
                                          "schedule": sched, "event": ev})
                if best:
                    return best
        return None

    def next_event(self, now: datetime | None = None) -> tuple[datetime, dict] | None:
        """Next audible thing of any kind: an individual alarm or a routine message."""
        now = now or datetime.now()
        cands = [c for c in (self.next_alarm(now), self.next_announcement(now)) if c]
        return min(cands, key=lambda c: c[0]) if cands else None


# --------------------------------------------------------------------------- schedules (family routines)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS, WEEKEND, EVERY_DAY = [0, 1, 2, 3, 4], [5, 6], [0, 1, 2, 3, 4, 5, 6]


def new_schedule(settings: dict | None = None) -> dict:
    """A schedule is a named group of timed events sharing repeat days, one speaker and one volume."""
    s = settings or DEFAULT_SETTINGS
    return {"id": uuid.uuid4().hex, "name": "", "days": list(WEEKDAYS), "output": s.get("last_output", ""),
            "volume": int(s.get("last_volume", 80)), "enabled": False, "events": []}


def new_event(time_str: str = "07:00") -> dict:
    return {"id": uuid.uuid4().hex, "time": time_str, "label": "", "sound": "", "enabled": True}


def days_label(days) -> str:
    d = sorted(set(int(x) for x in days))
    if d == EVERY_DAY:
        return "Every day"
    if d == WEEKDAYS:
        return "Weekdays"
    if d == WEEKEND:
        return "Weekends"
    if not d:
        return "No days"
    return " ".join(DAY_NAMES[i] for i in d)


def portable_sound(path: str) -> str:
    """Store app-owned recordings relative to the app folder so the whole folder can be moved."""
    if path and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, BASE_DIR)
            if not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        except ValueError:
            pass
    return path


def resolve_sound(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, *path.split("/"))


def local_time_exists(dt: datetime) -> bool:
    """False for a wall-clock time that is skipped when the clocks go forward (a DST gap)."""
    try:
        back = datetime.fromtimestamp(time.mktime(dt.timetuple()))
        return back.replace(microsecond=0) == dt.replace(microsecond=0)
    except (OverflowError, ValueError, OSError):
        return True


def occurrence_key(sid: str, eid: str, day: date) -> str:
    return f"{sid}:{eid}:{day.isoformat()}"


def validate_schedule(sched: dict) -> dict[str, str]:
    """Field name → plain-language problem.  An empty dict means the schedule can be saved."""
    errs: dict[str, str] = {}
    if not sched.get("name", "").strip():
        errs["name"] = "Give the schedule a name, for example “Son — School day”."
    if not sched.get("days"):
        errs["days"] = "Choose at least one day."
    for e in sched.get("events", []):
        try:
            h, m = (int(x) for x in e["time"].split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except (ValueError, AttributeError):
            errs[f"event:{e['id']}"] = "The time needs hours 0–23 and minutes 0–59."
        if not e.get("label", "").strip():
            errs.setdefault(f"event:{e['id']}", "Give the event a name, for example “Breakfast”.")
    return errs


def duplicate_schedule(sched: dict) -> dict:
    """A deep copy with fresh IDs, switched off, sharing the same audio files."""
    d = json.loads(json.dumps(sched))
    d["id"] = uuid.uuid4().hex
    d["name"] = f"{sched['name']} (copy)".strip()
    d["enabled"] = False
    for e in d["events"]:
        e["id"] = uuid.uuid4().hex
    return d


class AnnouncementQueue:
    """Routine messages waiting for the single audio stream, in due-time order.
    The GUI pops one at a time; anything that waited longer than SCHEDULE_GRACE is marked missed, not played."""

    def __init__(self):
        self.pending: list[dict] = []          # {"key", "schedule", "event", "due"}

    def push(self, occ: dict) -> None:
        if any(x["key"] == occ["key"] for x in self.pending):
            return
        self.pending.append(occ)
        self.pending.sort(key=lambda x: (x["due"], x["schedule"]["name"], x["event"]["time"], x["key"]))

    def cancel(self, sid: str | None = None, eid: str | None = None) -> list[dict]:
        """Drop queued messages of one schedule (or one event); returns what was dropped."""
        gone = [x for x in self.pending if (sid is None or x["schedule"]["id"] == sid) and (eid is None or x["event"]["id"] == eid)]
        self.pending = [x for x in self.pending if x not in gone]
        return gone

    def pop_playable(self, now: datetime, store: AlarmStore) -> dict | None:
        """Next message that may still play; late or broken ones are recorded and skipped over."""
        while self.pending:
            occ = self.pending.pop(0)
            if now - occ["due"] > SCHEDULE_GRACE:
                store.record(occ["key"], "missed", "waited too long behind other sounds")
                log(f"routine message '{occ['event']['label']}' missed: {now - occ['due']} late")
                continue
            path = resolve_sound(occ["event"].get("sound", ""))
            if not path or not os.path.isfile(path):
                store.record(occ["key"], "failed", "sound file is missing")
                log(f"routine message '{occ['event']['label']}' failed: sound file missing ({path})")
                continue
            occ["path"] = path
            return occ
        return None

    def __len__(self) -> int:
        return len(self.pending)


# --------------------------------------------------------------------------- audio playback
class Player:
    def __init__(self):
        self.ok = False
        self.error = ""
        self.device: str | None = None      # None = system default output
        try:
            import pygame  # noqa: F401
            pygame.mixer.init()
            self.ok = True
        except Exception as e:  # pragma: no cover - depends on machine
            self.error = str(e)
            log(f"Audio init failed: {e}")

    @staticmethod
    def output_devices() -> list[str]:
        """Names of the sound output devices SDL can see (speakers, headsets, HDMI…)."""
        try:
            from pygame._sdl2 import audio
            return list(audio.get_audio_device_names(False))
        except Exception as e:
            log(f"could not list output devices: {e}")
            return []

    def _ensure_device(self, device: str) -> str:
        """Re-open the mixer on `device` ('' = system default).  Returns a warning if it had to fall back."""
        import pygame
        wanted = device or None
        if wanted == self.device and pygame.mixer.get_init():
            return ""
        pygame.mixer.quit()
        try:
            pygame.mixer.init(devicename=wanted)
            self.device = wanted
            log(f"audio output: {wanted or 'system default'}")
            return ""
        except Exception as e:
            log(f"output device '{wanted}' unavailable ({e}); falling back to system default")
            try:
                pygame.mixer.init()
            except Exception as e2:
                self.ok, self.error = False, str(e2)
                log(f"audio output lost: {e2}")
                raise RuntimeError("No sound output is available right now – plug in speakers or headphones.") from e2
            self.device = None
            return f"Output “{device}” is not available right now, so it is playing on the system default output."

    def play(self, path: str, volume: int, loop: bool = True, device: str = "") -> str:
        """Start playback.  Returns '' or a plain-language warning (e.g. output device fell back)."""
        import pygame
        if not self.ok:
            raise RuntimeError(self.error or "audio not initialised")
        warning = self._ensure_device(device)
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(max(0, min(100, volume)) / 100.0)
        pygame.mixer.music.play(-1 if loop else 0)
        return warning

    def _live(self) -> bool:
        if not self.ok:
            return False
        import pygame
        return bool(pygame.mixer.get_init())

    def set_volume(self, volume: int) -> None:
        if self._live():
            import pygame
            pygame.mixer.music.set_volume(max(0, min(100, volume)) / 100.0)

    def stop(self) -> None:
        if self._live():
            import pygame
            pygame.mixer.music.stop()
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass

    def is_playing(self) -> bool:
        if not self._live():
            return False
        import pygame
        return bool(pygame.mixer.music.get_busy())


class AirPlayPlayer:
    """
    macOS only.  Plays through the Music app so the sound can go to AirPlay speakers
    (HomePod, Apple TV, AirPlay receivers) – the same devices you pick in the Mac's Sound menu.
    Every call talks to Music via osascript, so play()/stop() are run from a worker thread.
    Alarm output values look like "airplay:<device name>".
    """
    PREFIX = "airplay:"

    def __init__(self):
        self._track_id: str | None = None
        self._prev_volume: int | None = None
        self._prev_devices: list[str] = []
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        self.playing = False

    # ----- osascript plumbing
    @staticmethod
    def _q(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _run(script: str, timeout: int = 60) -> str:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            err = r.stderr.strip()
            if "-1743" in err or "Not authorized" in err or "not allowed" in err:
                raise RuntimeError("macOS blocked control of the Music app. Allow it in System Settings → "
                                   "Privacy & Security → Automation (turn on Music for this app), then try again.")
            if "AirPlay device" in err:
                raise RuntimeError("Music could not find or select that AirPlay speaker. "
                                   "Is it switched on and on the same Wi-Fi as this Mac?")
            raise RuntimeError(err or f"osascript exit code {r.returncode}")
        return r.stdout.strip()

    @staticmethod
    def music_running() -> bool:
        try:
            return AirPlayPlayer._run('tell application "System Events" to (name of processes) contains "Music"',
                                      timeout=10) == "true"
        except Exception:
            return False

    def prewarm(self) -> None:
        """Launch Music ahead of an AirPlay alarm so the first command at ring time is fast."""
        try:
            self._run('tell application "Music" to launch', timeout=30)
            log("Music launched ahead of an AirPlay alarm")
        except Exception as e:
            log(f"Music prewarm failed: {e}")

    @classmethod
    def devices(cls, launch: bool = False) -> list[dict]:
        """AirPlay speakers Music can see (the computer itself is skipped).  Empty if Music is not running
        and launch is False, so that opening the alarm clock never opens Music by itself."""
        if not launch and not cls.music_running():
            return []
        out = cls._run('''tell application "Music"
	set out to ""
	repeat with d in AirPlay devices
		set out to out & (name of d) & tab & (kind of d as text) & tab & (available of d) & linefeed
	end repeat
	return out
end tell''', timeout=60)
        devs = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1] != "computer":
                devs.append({"name": parts[0], "kind": parts[1], "available": parts[2] == "true"})
        return devs

    # ----- playback (blocking – call from a worker thread)
    def play(self, path: str, volume: int, device: str, loop: bool = True, fade_seconds: int = 0,
             on_started=None) -> None:
        """Blocking.  With loop=False it returns when the track has ended (or stop() was called)."""
        self._stop_flag.clear()
        script = f'''tell application "Music"
	set prevDevs to ""
	repeat with d in AirPlay devices
		if selected of d then set prevDevs to prevDevs & (name of d) & linefeed
	end repeat
	set prevVol to sound volume
	set current AirPlay devices to {{AirPlay device "{self._q(device)}"}}
	set theTrack to add (POSIX file "{self._q(path)}") to library playlist 1
	set sound volume to {0 if fade_seconds else int(volume)}
	set song repeat to {"one" if loop else "off"}
	play theTrack
	return (persistent ID of theTrack) & tab & prevVol & tab & prevDevs
end tell'''
        out = self._run(script, timeout=120)
        tid, prev_vol, *prev = out.split("\t")
        with self._lock:
            self._track_id = tid
            self._prev_volume = int(prev_vol)
            self._prev_devices = [d for d in "\t".join(prev).splitlines() if d]
            self.playing = True
        log(f"AirPlay: playing {os.path.basename(path)} on '{device}' via Music")
        if on_started:
            on_started()
        if fade_seconds:
            t0 = time.monotonic()
            while not self._stop_flag.is_set():
                frac = min(1.0, (time.monotonic() - t0) / fade_seconds)
                self.set_volume(round(volume * frac))
                if frac >= 1.0:
                    break
                self._stop_flag.wait(1.0)
        if not loop:   # preview or whole-file alarm: wait for the track to end, then tidy up
            while not self._stop_flag.is_set() and self.is_playing():
                self._stop_flag.wait(3.0)   # one osascript per 3 s keeps a 2-hour file cheap
            self.stop()

    def set_volume(self, volume: int) -> None:
        if self.playing:
            try:
                self._run(f'tell application "Music" to set sound volume to {max(0, min(100, int(volume)))}', timeout=15)
            except Exception as e:
                log(f"AirPlay set_volume failed: {e}")

    def is_playing(self) -> bool:
        if not self.playing:
            return False
        try:
            return self._run('tell application "Music" to (player state is playing)', timeout=15) == "true"
        except Exception:
            return False

    def stop(self) -> None:
        """Stop, remove the temporary track from the library, restore Music's volume and speaker selection."""
        self._stop_flag.set()
        with self._lock:
            tid, prev_vol, prev = self._track_id, self._prev_volume, self._prev_devices
            self._track_id, self.playing = None, False
        if tid is None:
            return
        restore = ""
        if prev:
            names = ", ".join(f'"{self._q(n)}"' for n in prev)
            restore = f'''
	repeat with d in AirPlay devices
		try
			set selected of d to ({{{names}}} contains (name of d))
		end try
	end repeat'''
        script = f'''tell application "Music"
	stop
	set song repeat to off
	try
		delete (first track of library playlist 1 whose persistent ID is "{tid}")
	end try
	set sound volume to {prev_vol if prev_vol is not None else 50}{restore}
end tell'''
        try:
            self._run(script, timeout=60)
            log("AirPlay: stopped, Music restored")
        except Exception as e:
            log(f"AirPlay stop/restore failed: {e}")


def output_label(value: str) -> str:
    """Human label for an alarm's stored output value."""
    if not value:
        return "default"
    if value.startswith(AirPlayPlayer.PREFIX):
        return "AirPlay: " + value[len(AirPlayPlayer.PREFIX):]
    return value


def set_system_volume(percent: int) -> None:
    """Best-effort: turn the OS output volume up so the alarm is actually audible."""
    percent = max(0, min(100, int(percent)))
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e", f"set volume output volume {percent}",
                            "-e", "set volume without output muted"], timeout=10, check=False)
        elif IS_LINUX:
            r = subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], check=False)
            if r.returncode != 0:
                subprocess.run(["amixer", "-D", "pulse", "sset", "Master", f"{percent}%", "unmute"], check=False)
        elif IS_WIN:
            # No built-in API without extra packages; nudge volume up with virtual key presses.
            VK_VOLUME_MUTE, VK_VOLUME_UP = 0xAD, 0xAF
            user32 = ctypes.windll.user32
            for _ in range(int(percent / 2)):
                user32.keybd_event(VK_VOLUME_UP, 0, 0, 0)
                user32.keybd_event(VK_VOLUME_UP, 0, 2, 0)
    except Exception as e:
        log(f"set_system_volume failed: {e}")


# --------------------------------------------------------------------------- recording
NORMALIZE_TARGET = 0.9      # peak after normalisation (of full scale)
NORMALIZE_MAX_DB = 40.0     # never boost more than this (pure noise would just get loud)
QUIET_PEAK = 0.003          # below this the take is treated as "nothing recorded"


def normalize_int16(samples: array) -> tuple[array, float, float]:
    """Remove DC offset and peak-normalise 16-bit samples.
    Returns (samples, original_peak_fraction, gain_db).  Quiet microphones (USB headsets, webcams)
    often deliver -30…-45 dBFS; without this the alarm would be barely audible."""
    import math
    n = len(samples)
    if n == 0:
        return samples, 0.0, 0.0
    dc = int(sum(samples) / n)
    peak = max(abs(x - dc) for x in samples)
    if peak == 0:
        return samples, 0.0, 0.0
    gain = max(1.0, min(NORMALIZE_TARGET * 32767 / peak, 10 ** (NORMALIZE_MAX_DB / 20)))   # boost only, never attenuate
    if gain < 1.02 and abs(dc) < 64:      # already loud enough, no meaningful offset: leave untouched
        return samples, peak / 32768, 0.0
    out = array("h", (max(-32768, min(32767, int((x - dc) * gain))) for x in samples))
    return out, peak / 32768, 20 * math.log10(gain)


class Recorder:
    """Records mono 16-bit WAV from a chosen input device using sounddevice, then normalises the level."""

    SAMPLE_RATE = 44100

    def __init__(self):
        self.stream = None
        self.frames: list[bytes] = []
        self.level = 0.0
        self.peak = 0.0            # loudest level seen during the current take (0..1)
        self.started_at = 0.0
        self.path = ""
        self.last_peak = 0.0       # peak of the last take before normalisation
        self.last_gain_db = 0.0    # boost applied to the last take

    @staticmethod
    def input_devices() -> list[tuple[int | None, str]]:
        try:
            import sounddevice as sd
        except Exception as e:
            log(f"sounddevice not available: {e}")
            return []
        try:
            default_name = sd.query_devices(kind="input")["name"]
        except Exception:
            default_name = "none found"
        out = [(None, f"System default microphone  ({default_name})")]
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append((i, d["name"]))
        return out

    def start(self, device: int | None, out_dir: str) -> str:
        import sounddevice as sd
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"voice_{datetime.now():%Y-%m-%d_%H-%M-%S}.wav")
        self.frames = []
        self.level = 0.0
        self.peak = 0.0
        rate = self.SAMPLE_RATE
        if device is not None:
            try:
                rate = int(sd.query_devices(device)["default_samplerate"]) or rate
            except Exception:
                pass
        self.rate = rate

        def cb(indata, frames, t, status):
            data = bytes(indata)
            self.frames.append(data)
            samples = array("h", data)
            self.level = (max(abs(s) for s in samples) / 32768.0) if samples else 0.0
            self.peak = max(self.peak, self.level)

        self.stream = sd.RawInputStream(samplerate=rate, channels=1, dtype="int16",
                                        device=device, callback=cb)
        self.stream.start()
        self.started_at = time.time()
        return self.path

    def stop(self) -> str:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        samples, self.last_peak, self.last_gain_db = normalize_int16(array("h", b"".join(self.frames)))
        with wave.open(self.path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(samples.tobytes())
        self.frames = []
        log(f"recording saved {os.path.basename(self.path)}: peak {self.last_peak*100:.1f}% → boosted {self.last_gain_db:+.0f} dB")
        return self.path

    @property
    def recording(self) -> bool:
        return self.stream is not None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.recording else 0.0


# --------------------------------------------------------------------------- power / sleep handling
class PowerManager:
    """
    Two independent jobs:
      1. keep_awake      – stop the OS from sleeping while any alarm is armed (no admin needed).
      2. schedule_wake   – ask the OS to wake the machine shortly before the next alarm,
                           in case the user puts it to sleep anyway.
                           macOS: `pmset schedule wake` (asks for the admin password via a dialog).
                           Windows: a waitable timer with the resume flag (needs "wake timers" enabled
                                    in the power plan, which is the default on desktops).
                           Linux: `pkexec rtcwake` (asks for the admin password).
    """

    def __init__(self):
        self._awake_proc: subprocess.Popen | None = None
        self._awake = False
        self._wake_target: datetime | None = None
        self._declined_target: datetime | None = None
        # none | registering | registered | declined | failed | unsupported
        self.wake_state = "none"
        self.wake_detail = ""
        self._win_timer = None
        self._win_thread: threading.Thread | None = None
        self._win_stop = threading.Event()
        self.stale_wake: datetime | None = None    # a wake a previous launch registered (macOS); cancelled with the next change
        self.on_registered = None                   # callback(datetime | None) so the app can remember the registered wake
        atexit.register(self.shutdown)

    @property
    def keeping_awake(self) -> bool:
        return self._awake

    @property
    def wake_target(self) -> datetime | None:
        return self._wake_target

    # ----- keep awake
    def set_keep_awake(self, on: bool) -> None:
        if on == self._awake:
            return
        self._awake = on
        try:
            if IS_WIN:
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
            elif on:
                if IS_MAC:
                    cmd = ["caffeinate", "-i", "-s", "-w", str(os.getpid())]
                else:
                    cmd = ["systemd-inhibit", "--what=sleep:idle", f"--who={APP_NAME}",
                           "--why=An alarm is armed", "sleep", "infinity"]
                self._awake_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self._kill_awake()
            log(f"keep awake: {'on' if on else 'off'}")
        except Exception as e:
            self._awake = False
            log(f"keep awake failed: {e}")

    def _kill_awake(self) -> None:
        if self._awake_proc:
            try:
                self._awake_proc.terminate()
            except Exception:
                pass
            self._awake_proc = None

    # ----- scheduled wake
    def set_wake(self, when: datetime | None) -> None:
        """Schedule (or clear) the system wake.  Only acts when the target actually changes."""
        target = None
        if when:
            target = (when - timedelta(seconds=WAKE_LEAD_SECONDS)).replace(microsecond=0)
            if target <= datetime.now():
                target = None  # too close – the machine is awake right now anyway
        if target == self._wake_target:
            return
        if target is None and self._wake_target and self._wake_target <= datetime.now():
            self._wake_target, self.wake_state = None, "none"   # it already fired: nothing to cancel, no prompt
            return
        if target is not None and target == self._declined_target:
            return  # the user declined the password prompt for this exact time; don't nag every second
        if not (IS_MAC or IS_WIN or IS_LINUX):
            self.wake_state, self.wake_detail = "unsupported", "not available on this OS"
            return
        old, self._wake_target = self._wake_target, target
        self.wake_state = "registering" if target else "none"
        threading.Thread(target=self._apply_wake, args=(old, target), daemon=True).start()

    def retry_wake(self, when: datetime | None) -> None:
        """Forget a declined prompt and try to register the wake again."""
        self._declined_target = None
        self._wake_target = None
        self.set_wake(when)

    def _apply_wake(self, old: datetime | None, new: datetime | None) -> None:
        try:
            if IS_MAC:
                self._mac_wake(old, new)
            elif IS_WIN:
                self._win_wake(new)
            elif IS_LINUX:
                self._linux_wake(new)
        except Exception as e:
            self._set_wake_result(False, new, f"{type(e).__name__}: {e}")

    def _set_wake_result(self, ok: bool, new: datetime | None, detail: str = "", declined: bool = False) -> None:
        if new is None:
            self.wake_state, self.wake_detail = "none", ""
            log("system wake cleared" + (f" ({detail})" if detail else ""))
            if self.on_registered:
                self.on_registered(None)
            return
        if ok:
            self.wake_state, self.wake_detail = "registered", ""
            log(f"system wake registered for {new}")
            if self.on_registered:
                self.on_registered(new)
        else:
            self.wake_state = "declined" if declined else "failed"
            self.wake_detail = detail
            self._declined_target = new if declined else None
            self._wake_target = None
            log(f"system wake NOT registered ({self.wake_state}): {detail}")

    def _mac_wake(self, old, new) -> None:
        fmt = "%m/%d/%y %H:%M:%S"
        parts = []
        stale, self.stale_wake = self.stale_wake, None
        if stale and stale != old and stale > datetime.now():
            parts.append(f'pmset schedule cancel wake \\"{stale.strftime(fmt)}\\"')
        if old:
            parts.append(f'pmset schedule cancel wake \\"{old.strftime(fmt)}\\"')
        if new:
            parts.append(f'pmset schedule wake \\"{new.strftime(fmt)}\\"')
        if not parts:
            return
        script = f'do shell script "{" ; ".join(parts)}" with administrator privileges'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
        err = r.stderr.strip()
        declined = "-128" in err or "User canceled" in err or "cancelled" in err.lower()
        self._set_wake_result(r.returncode == 0, new,
                              "password prompt declined" if declined else err, declined=declined)

    def _win_wake(self, new) -> None:
        k32 = ctypes.windll.kernel32
        if self._win_timer is None:
            self._win_timer = k32.CreateWaitableTimerW(None, True, None)
            self._win_thread = threading.Thread(target=self._win_wait_loop, daemon=True)
            self._win_thread.start()
        if new is None:
            k32.CancelWaitableTimer(self._win_timer)
            self._set_wake_result(True, None)
            return
        # absolute FILETIME (UTC, 100 ns since 1601-01-01)
        ft = int((new.timestamp() + 11644473600) * 10_000_000)
        due = ctypes.c_longlong(ft)
        ok = k32.SetWaitableTimer(self._win_timer, ctypes.byref(due), 0, None, None, True)
        self._set_wake_result(bool(ok), new, "" if ok else f"SetWaitableTimer error {ctypes.GetLastError()}")

    def _win_wait_loop(self) -> None:
        k32 = ctypes.windll.kernel32
        while not self._win_stop.is_set():
            if k32.WaitForSingleObject(self._win_timer, 1000) == 0:
                log("wake timer fired")
                time.sleep(2)

    def _linux_wake(self, new) -> None:
        if new is None:
            subprocess.run(["pkexec", "rtcwake", "-m", "disable"], check=False)
            self._set_wake_result(True, None)
            return
        r = subprocess.run(["pkexec", "rtcwake", "-m", "no", "-t", str(int(new.timestamp()))],
                           capture_output=True, text=True)
        declined = r.returncode in (126, 127)
        self._set_wake_result(r.returncode == 0, new,
                              "password prompt declined" if declined else r.stderr.strip(), declined=declined)

    # ----- status / shutdown
    def describe_wake(self) -> str:
        """Plain-language description of whether the OS will really wake the machine."""
        if self.wake_state == "registered" and self._wake_target:
            return f"OS wake registered for {self._wake_target:%a %H:%M}"
        if self.wake_state == "registering":
            return "registering OS wake…"
        if self.wake_state == "declined":
            return "OS wake NOT registered – password prompt declined"
        if self.wake_state == "failed":
            return f"OS wake NOT registered – {self.wake_detail or 'failed'}"
        if self.wake_state == "unsupported":
            return "OS wake not supported here"
        return "no OS wake registered"

    def shutdown(self) -> None:
        self._kill_awake()
        self._win_stop.set()
        if IS_WIN:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass


# --------------------------------------------------------------------------- scheduler
class Scheduler(threading.Thread):
    """Checks once a second whether an alarm is due and posts events to the GUI queue."""

    def __init__(self, store: AlarmStore, events: queue.Queue):
        super().__init__(daemon=True)
        self.store = store
        self.events = events
        self.snoozes: list[tuple[datetime, dict]] = []
        self.ringing: set[str] = set()
        self._last_prune: date | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(datetime.now())
            except Exception as e:
                log(f"scheduler error: {e}")
            self._stop.wait(1.0)

    def tick(self, now: datetime) -> None:
        with self.store.lock:
            for a in list(self.store.alarms):
                if a["id"] in self.ringing:
                    continue
                nf = next_fire(a, now)
                if nf is None or nf > now:
                    continue
                if now - nf > GRACE:
                    a["enabled"] = False
                    log(f"missed alarm '{a['label']}' scheduled {nf} (overdue > grace) – disabled")
                    self.store.save()
                    self.events.put(("missed", a, nf))
                    continue
                a["last_fired"] = nf.isoformat()
                if a["repeat"] == "once":
                    a["enabled"] = False
                self.store.save()
                self.ringing.add(a["id"])
                log(f"alarm due: '{a['label']}' at {nf} (now {now:%H:%M:%S})")
                self.events.put(("ring", a, nf))
            self._tick_schedules(now)
        due = [s for s in self.snoozes if s[0] <= now]
        for s in due:
            self.snoozes.remove(s)
            self.ringing.add(s[1]["id"])
            log(f"snoozed alarm due: '{s[1]['label']}'")
            self.events.put(("ring", s[1], s[0]))

    def _tick_schedules(self, now: datetime) -> None:
        """Routine messages: each (schedule, event, date) plays at most once; late ones become 'missed'."""
        today = now.date()
        if self._last_prune != today:
            self._last_prune = today
            self.store.prune(today)
        days = sorted({(now - SCHEDULE_GRACE).date(), today})    # just after midnight, yesterday's 23:59 still counts
        for day in days:
            for sched in self.store.schedules:
                if not sched["enabled"] or day.weekday() not in sched["days"]:
                    continue
                for ev in sched["events"]:
                    if not ev["enabled"] or not ev.get("sound"):
                        continue
                    key = occurrence_key(sched["id"], ev["id"], day)
                    if key in self.store.occurrences:
                        continue
                    try:
                        due = datetime.combine(day, alarm_time(ev))
                    except ValueError:
                        continue
                    if due > now:
                        continue
                    if self.store.is_skipped(day, sched["id"], ev["id"]):
                        self.store.record(key, "skipped", "skipped for today")
                        self.events.put(("occurrence", key, "skipped"))
                        continue
                    if not local_time_exists(due):
                        self.store.record(key, "missed", "this time did not exist today (clocks went forward)")
                        log(f"routine message '{ev['label']}' ({sched['name']}) missed: {ev['time']} did not exist today")
                        self.events.put(("occurrence", key, "missed"))
                        continue
                    if now - due > SCHEDULE_GRACE:
                        self.store.record(key, "missed", "the computer or the app was not running")
                        log(f"routine message '{ev['label']}' ({sched['name']}) missed: due {due:%H:%M}, now {now:%H:%M:%S}")
                        self.events.put(("occurrence", key, "missed"))
                        continue
                    self.store.record(key, "queued")
                    log(f"routine message due: '{ev['label']}' ({sched['name']}) at {due:%H:%M}")
                    self.events.put(("announce", {"key": key, "schedule": sched, "event": ev, "due": due}, due))

    def snooze(self, alarm: dict, minutes: int) -> datetime:
        when = datetime.now() + timedelta(minutes=minutes)
        self.snoozes.append((when, alarm))
        return when

    def next_event(self) -> tuple[datetime, dict] | None:
        ev = self.store.next_event()
        cands = ([ev] if ev else []) + self.snoozes
        return min(cands, key=lambda e: e[0]) if cands else None

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- GUI
def fmt_delta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class NoteLabel(ttk.Label):
    """A grid-managed label that takes no space while it has nothing to say."""

    def configure(self, cnf=None, **kw):
        r = super().configure(cnf, **kw)
        if "text" in kw:
            (self.grid if kw["text"] else self.grid_remove)()
        return r
    config = configure


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(760, 620)
        self.store = AlarmStore(DATA_FILE)
        self.player = Player()
        self.airplay = AirPlayPlayer() if IS_MAC else None
        self._output_map: dict[str, str] = {}
        self._vol_job: str | None = None
        self._prewarmed: str | None = None
        self.recorder = Recorder()
        self.power = PowerManager()
        remembered = self.store.settings.get("registered_wake") or ""
        if IS_MAC and remembered:
            try:
                self.power.stale_wake = datetime.fromisoformat(remembered)
            except ValueError:
                pass
        self.power.on_registered = self._remember_wake
        self.events: queue.Queue = queue.Queue()
        self.scheduler = Scheduler(self.store, self.events)
        self.editing_id: str | None = None
        self.ring_windows: dict[str, tk.Toplevel] = {}
        self.ring_timeouts: dict[str, str] = {}
        self.once_play: dict[str, dict] = {}       # alarms in "play the whole file once" mode that are playing
        self._local_owner: str | None = None       # alarm currently playing on the pygame stream
        self._fade_job: str | None = None
        self.announcements = AnnouncementQueue()
        self.current_ann: dict | None = None      # routine message playing right now
        self._ann_job: str | None = None
        self.sdraft: dict | None = None           # schedule being edited (unsaved copy)
        self.sdraft_saved = ""                    # JSON of its saved form, to notice unsaved changes
        self.ev_draft: dict | None = None         # event being edited inside the schedule draft
        self.ev_take = ""                         # a recording made for the event but not used yet
        self._rec_owner = ""                      # "event" or "alarm": which editor started the current recording
        self._today_expanded = False
        self._saved_job: str | None = None

        self._build_ui()
        self.bind("<Escape>", lambda e: self._stop_announcement())
        self._load_form(new_alarm(self.store.settings))
        self._refresh_list()
        self.scheduler.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._poll_events)
        self.after(1000, self._tick_status)
        self._apply_power()
        self._tick_indicators()
        self._refresh_schedules()
        self._refresh_today()
        self._show_page("today")
        if self.store.load_problem:
            self.after(200, lambda: messagebox.showerror(APP_NAME, self.store.load_problem))
        if not self.player.ok:
            messagebox.showerror(APP_NAME, "Sound output is not available, so alarms will be silent.\n\n"
                                 "Usually this means no speakers/headphones are connected, or the audio "
                                 "library did not install correctly. Plug in an output device, or delete the "
                                 ".venv folder next to the app and start it again to reinstall.\n\n"
                                 f"Details: {self.player.error}")
        log(f"started; data folder: {BASE_DIR}")

    # ----- UI construction
    PALETTE = dict(bg="#F3F5F9", card="#FFFFFF", line="#E3E7EE", header="#1F2A44", header2="#2E3F66",
                   text="#1E2533", muted="#6B7280", accent="#3A6FF0", accent_dark="#2C56C4",
                   good="#1E8E3E", warn="#C25E00", bad="#C62828", soft="#EEF1F5", soft_hover="#E1E6EE",
                   tint_good="#E3F3E7", tint_warn="#FDEBD3", tint_bad="#FBE3E3", tint_neutral="#E9EDF3",
                   tint_ring="#FFD9D9")

    def _fonts(self) -> None:
        family = "Helvetica Neue" if IS_MAC else ("Segoe UI" if IS_WIN else "DejaVu Sans")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=13)
            except tk.TclError:
                pass
        self.F = dict(base=(family, 13), small=(family, 11), small_u=(family, 11, "underline"), small_b=(family, 11, "bold"), bold=(family, 13, "bold"),
                      title=(family, 17, "bold"), section=(family, 11, "bold"), clock=(family, 46, "bold"),
                      time=(family, 24, "bold"), stop=(family, 20, "bold"), ring_name=(family, 26, "bold"),
                      ring_time=(family, 64, "bold"))

    def _styles(self) -> None:
        P, F = self.PALETTE, self.F
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=P["bg"], foreground=P["text"], font=F["base"], borderwidth=0, focuscolor=P["bg"])
        st.configure("Card.TFrame", background=P["card"])
        st.configure("Card.TLabel", background=P["card"], foreground=P["text"])
        st.configure("Muted.TLabel", background=P["card"], foreground=P["muted"], font=F["small"])
        st.configure("Section.TLabel", background=P["card"], foreground=P["accent"], font=F["section"])
        st.configure("Title.TLabel", background=P["card"], foreground=P["text"], font=F["title"])
        st.configure("TSeparator", background=P["line"])
        st.configure("Accent.TButton", background=P["accent"], foreground="white", padding=(18, 9), font=F["bold"], borderwidth=0)
        st.map("Accent.TButton", background=[("active", P["accent_dark"]), ("disabled", "#B8C6F0")], foreground=[("disabled", "white")])
        st.configure("Soft.TButton", background=P["soft"], foreground=P["text"], padding=(12, 7), borderwidth=0)
        st.map("Soft.TButton", background=[("active", P["soft_hover"])])
        st.configure("Danger.TButton", background=P["tint_bad"], foreground=P["bad"], padding=(12, 7), borderwidth=0)
        st.map("Danger.TButton", background=[("active", "#F6CACA")])
        st.configure("Icon.TButton", background=P["soft"], foreground=P["text"], padding=(7, 5), borderwidth=0)
        st.map("Icon.TButton", background=[("active", P["soft_hover"])])
        st.configure("Stop.TButton", background=P["bad"], foreground="white", font=F["stop"], padding=(28, 16), borderwidth=0)
        st.map("Stop.TButton", background=[("active", "#8E0000")], foreground=[("active", "white")])
        st.configure("Ghost.TButton", background=P["header2"], foreground="white", padding=(18, 10), borderwidth=0)
        st.map("Ghost.TButton", background=[("active", "#3A4E7C")], foreground=[("active", "white")])
        st.configure("Seg.Toolbutton", background=P["soft"], foreground=P["text"], padding=(14, 7), borderwidth=0, font=F["base"])
        st.map("Seg.Toolbutton", background=[("selected", P["accent"]), ("active", P["soft_hover"])],
               foreground=[("selected", "white")])
        st.configure("Card.TCheckbutton", background=P["card"], foreground=P["text"], padding=(0, 4))
        st.map("Card.TCheckbutton", background=[("active", P["card"])])
        st.configure("Treeview", background=P["card"], fieldbackground=P["card"], foreground=P["text"], rowheight=34,
                     borderwidth=0, font=F["base"])
        st.configure("Treeview.Heading", background=P["card"], foreground=P["muted"], font=F["small_b"], relief="flat", padding=(6, 8))
        st.map("Treeview", background=[("selected", "#E3ECFF")], foreground=[("selected", P["text"])])
        st.map("Treeview.Heading", background=[("active", P["card"])])
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground="#FFFFFF", background="#FFFFFF", bordercolor=P["line"], lightcolor=P["line"],
                         darkcolor=P["line"], arrowcolor=P["muted"], padding=6, insertcolor=P["text"])
        st.map("TCombobox", fieldbackground=[("readonly", "#FFFFFF")], background=[("readonly", "#FFFFFF")],
               foreground=[("readonly", P["text"])], selectbackground=[("readonly", "#FFFFFF")],
               selectforeground=[("readonly", P["text"])])
        st.configure("Horizontal.TScale", background=P["card"], troughcolor=P["line"], sliderlength=22, borderwidth=0)
        st.map("Horizontal.TScale", background=[("active", P["card"])])
        st.configure("Meter.Horizontal.TProgressbar", background=P["good"], troughcolor=P["line"], thickness=8, borderwidth=0)
        for w in ("TButton", "Toolbutton", "TCheckbutton", "TRadiobutton", "TCombobox", "TEntry", "TSpinbox"):
            st.configure(w, focuscolor=P["accent"])          # keyboard focus must be visible
        st.configure("Nav.Toolbutton", background=P["bg"], foreground=P["muted"], padding=(16, 6), borderwidth=0, font=F["bold"])
        st.map("Nav.Toolbutton", background=[("selected", P["card"]), ("active", P["soft_hover"])],
               foreground=[("selected", P["accent"])])
        st.configure("Rec.TLabel", background=P["card"], foreground=P["bad"], font=F["bold"])
        st.configure("Good.TLabel", background=P["card"], foreground=P["good"], font=F["small_b"])
        st.configure("Warn.TLabel", background=P["card"], foreground=P["warn"], font=F["small"])
        st.configure("Bad.TLabel", background=P["card"], foreground=P["bad"], font=F["small"])
        self.bind_class("TButton", "<Return>", lambda e: e.widget.invoke())
        self.configure(bg=P["bg"])
        self.option_add("*TCombobox*Listbox.font", F["base"])
        self.option_add("*TCombobox*Listbox.selectBackground", "#E3ECFF")
        self.option_add("*TCombobox*Listbox.selectForeground", P["text"])

    def _card(self, parent, **pack) -> ttk.Frame:
        outer = tk.Frame(parent, bg=self.PALETTE["card"], highlightbackground=self.PALETTE["line"], highlightthickness=1, bd=0)
        outer.pack(**pack)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(20, 16))
        inner.pack(fill="both", expand=True)
        return inner

    def _pill(self, parent) -> tk.Label:
        return tk.Label(parent, text="", font=self.F["small_b"], padx=11, pady=5,
                        bg=self.PALETTE["tint_neutral"], fg=self.PALETTE["muted"])

    def _set_pill(self, pill: tk.Label, text: str, tone: str) -> None:
        P = self.PALETTE
        bg, fg = {"good": (P["tint_good"], P["good"]), "warn": (P["tint_warn"], P["warn"]),
                  "bad": (P["tint_bad"], P["bad"]), "ring": (P["tint_ring"], P["bad"])}.get(tone, (P["tint_neutral"], P["muted"]))
        pill.config(text=text, bg=bg, fg=fg)

    def _build_ui(self) -> None:
        self._fonts()
        self._styles()
        P, F = self.PALETTE, self.F
        self.geometry("1400x840")
        self.minsize(1240, 760)

        # ---- header: title + next alarm + status pills on the left, big clock on the right
        hdr = tk.Frame(self, bg=P["header"], padx=26, pady=20)
        hdr.pack(fill="x")
        left = tk.Frame(hdr, bg=P["header"])
        left.pack(side="left", fill="y")
        tk.Label(left, text="⏰  Alarm Clock", font=F["title"], bg=P["header"], fg="white").pack(anchor="w")
        self.l_next = tk.Label(left, text="", font=F["base"], bg=P["header"], fg="#C7D0E4", justify="left")
        self.l_next.pack(anchor="w", pady=(6, 0))
        pills = tk.Frame(left, bg=P["header"])
        pills.pack(anchor="w", pady=(14, 0))
        self.l_armed = self._pill(pills); self.l_armed.pack(side="left", padx=(0, 8))
        self.l_awake = self._pill(pills); self.l_awake.pack(side="left", padx=(0, 8))
        self.l_wake = self._pill(pills); self.l_wake.pack(side="left", padx=(0, 8))
        self.b_retry_wake = ttk.Button(pills, text="Try again", style="Soft.TButton", command=self._retry_wake)
        self.b_stop_hdr = ttk.Button(pills, text="■  Stop message", style="Danger.TButton", command=self._stop_announcement)
        right = tk.Frame(hdr, bg=P["header"])
        right.pack(side="right")
        self.l_clock = tk.Label(right, text="--:--", font=F["clock"], bg=P["header"], fg="white")
        self.l_clock.pack(anchor="e")
        self.l_date = tk.Label(right, text="", font=F["base"], bg=P["header"], fg="#C7D0E4")
        self.l_date.pack(anchor="e")

        # ---- footer + big STOP bar (the bar is shown only while ringing, see _ring / _dismiss)
        footer = tk.Frame(self, bg=P["bg"], padx=24, pady=6)
        footer.pack(fill="x", side="bottom")
        self.l_status = tk.Label(footer, text="", anchor="w", bg=P["bg"], fg=P["muted"], font=F["small"])
        self.l_status.pack(side="left", fill="x", expand=True)
        tk.Label(footer, text=f"© {datetime.now():%Y} {DEVELOPER}  ·", bg=P["bg"], fg=P["muted"],
                 font=F["small"]).pack(side="left")
        link = tk.Label(footer, text=WEBSITE.removeprefix("https://"), bg=P["bg"], fg=P["accent"],
                 font=F["small"], cursor="hand2")
        link.pack(side="left", padx=(4, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(WEBSITE))
        link.bind("<Enter>", lambda e: link.config(font=F["small_u"]))
        link.bind("<Leave>", lambda e: link.config(font=F["small"]))
        self.b_stop = ttk.Button(self, text="■   STOP ALARM", style="Stop.TButton", command=self._stop_all)

        # ---- navigation: Today and Schedules are the main views; Alarms keeps the classic editor
        nav = tk.Frame(self, bg=P["bg"], padx=22)
        nav.pack(fill="x", pady=(8, 0))
        self.v_page = tk.StringVar(value="today")
        for txt, val in (("Today", "today"), ("Schedules", "schedules"), ("Alarms", "alarms")):
            ttk.Radiobutton(nav, text=txt, value=val, variable=self.v_page, style="Nav.Toolbutton",
                            command=lambda v=val: self._show_page(v)).pack(side="left", padx=(0, 4))
        ttk.Label(nav, text="Schedules play short messages for the family · Alarms are the classic wake-up alarms",
                  foreground=P["muted"], background=P["bg"], font=F["small"]).pack(side="left", padx=16)

        self.body = tk.Frame(self, bg=P["bg"], padx=22, pady=10)
        self.body.pack(fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {n: tk.Frame(self.body, bg=P["bg"]) for n in ("today", "schedules", "alarms")}
        page = self.pages["alarms"]
        right = tk.Frame(page, bg=P["bg"])          # packed first so the editor keeps its natural width
        right.pack(side="right", fill="y")
        left = tk.Frame(page, bg=P["bg"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))

        # ---- left column, card 1: your alarms
        c1 = self._card(left, fill="both", expand=True)
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Your alarms", style="Title.TLabel").pack(side="left")
        ttk.Button(row, text="＋  New alarm", style="Accent.TButton", command=self._new).pack(side="right")
        cols = ("on", "time", "label", "repeat", "next", "sound", "output")
        # requested height is small on purpose: the list expands to fill the column, and a small request
        # leaves room for the More options card below it even at the minimum window size
        self.tree = ttk.Treeview(c1, columns=cols, show="headings", height=3, selectmode="browse")
        heads = {"on": ("", 36, "center"), "time": ("Time", 70, "w"), "label": ("Alarm", 130, "w"),
                 "repeat": ("Repeats", 95, "w"), "next": ("Next ring", 140, "w"), "sound": ("Sound", 105, "w"),
                 "output": ("Plays on", 105, "w")}
        for c in cols:
            self.tree.heading(c, text=heads[c][0], anchor=heads[c][2])
            self.tree.column(c, width=heads[c][1], anchor=heads[c][2], stretch=(c in ("label", "sound", "output")))
        self.tree.tag_configure("off", foreground=P["muted"])
        self.tree.tag_configure("on", foreground=P["text"])
        self.tree.pack(fill="both", expand=True, pady=(12, 6))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._toggle_selected())
        self.l_empty = ttk.Label(c1, text="No alarms yet.  Set one up below in three steps and press Save alarm.",
                                 style="Muted.TLabel")
        self.acts = ttk.Frame(c1, style="Card.TFrame")
        self.acts.pack(fill="x")
        ttk.Button(self.acts, text="Turn on / off", style="Soft.TButton", command=self._toggle_selected).pack(side="left")
        ttk.Button(self.acts, text="Delete", style="Danger.TButton", command=self._delete_selected).pack(side="left", padx=8)
        ttk.Button(self.acts, text="Ring it now (test)", style="Soft.TButton", command=self._ring_selected).pack(side="right")
        ttk.Label(c1, text="Tip: click an alarm to edit it, double-click to turn it on or off.", style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        # ---- right column, card 2: the editor (three steps)
        c2 = self._card(right, fill="both", expand=True)
        row = ttk.Frame(c2, style="Card.TFrame")
        row.pack(fill="x")
        self.l_editor_title = ttk.Label(row, text="New alarm", style="Title.TLabel")
        self.l_editor_title.pack(side="left")
        name = ttk.Frame(c2, style="Card.TFrame")
        name.pack(fill="x", pady=(12, 4))
        ttk.Label(name, text="Name", style="Card.TLabel", width=7).pack(side="left")
        self.v_label = tk.StringVar()
        ttk.Entry(name, textvariable=self.v_label, font=F["base"], width=36).pack(side="left")

        grid = ttk.Frame(c2, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        grid.columnconfigure(1, weight=1)

        # ① When
        ttk.Label(grid, text="①  WHEN", style="Section.TLabel", width=11).grid(row=0, column=0, sticky="nw", pady=(8, 0))
        when = ttk.Frame(grid, style="Card.TFrame")
        when.grid(row=0, column=1, sticky="we")
        self.time_row = ttk.Frame(when, style="Card.TFrame")
        self.time_row.pack(fill="x")
        self.v_hour = tk.StringVar(); self.v_min = tk.StringVar()
        ttk.Spinbox(self.time_row, from_=0, to=23, width=3, textvariable=self.v_hour, format="%02.0f", wrap=True,
                    font=F["time"], justify="center").pack(side="left")
        ttk.Label(self.time_row, text=":", style="Card.TLabel", font=F["time"]).pack(side="left", padx=4)
        ttk.Spinbox(self.time_row, from_=0, to=59, width=3, textvariable=self.v_min, format="%02.0f", wrap=True,
                    font=F["time"], justify="center").pack(side="left")
        self.v_repeat = tk.StringVar(value="once")
        seg = ttk.Frame(self.time_row, style="Card.TFrame")
        seg.pack(side="left", padx=(26, 0))
        for txt, val in (("Just once", "once"), ("Every day", "daily"), ("Weekdays", "weekdays")):
            ttk.Radiobutton(seg, text=txt, value=val, variable=self.v_repeat, style="Seg.Toolbutton").pack(side="left", padx=(0, 3))
        self.v_repeat.trace_add("write", lambda *_: self._on_repeat_change())
        self.date_row = ttk.Frame(when, style="Card.TFrame")
        self.date_row.pack(fill="x", pady=(10, 0))
        ttk.Label(self.date_row, text="on", style="Muted.TLabel").pack(side="left", padx=(2, 8))
        self.v_year = tk.StringVar(); self.v_month = tk.StringVar(); self.v_day = tk.StringVar()
        ttk.Spinbox(self.date_row, from_=2024, to=2100, width=5, textvariable=self.v_year, format="%04.0f").pack(side="left")
        ttk.Label(self.date_row, text="-", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(self.date_row, from_=1, to=12, width=3, textvariable=self.v_month, format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(self.date_row, text="-", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(self.date_row, from_=1, to=31, width=3, textvariable=self.v_day, format="%02.0f", wrap=True).pack(side="left")
        ttk.Button(self.date_row, text="Today", style="Soft.TButton", command=lambda: self._set_date(date.today())).pack(side="left", padx=(12, 4))
        ttk.Button(self.date_row, text="Tomorrow", style="Soft.TButton",
                   command=lambda: self._set_date(date.today() + timedelta(days=1))).pack(side="left")
        quick = ttk.Frame(when, style="Card.TFrame")
        quick.pack(fill="x", pady=(10, 0))
        ttk.Label(quick, text="Try it quickly:", style="Muted.TLabel").pack(side="left", padx=(2, 8))
        ttk.Button(quick, text="In 1 min", style="Soft.TButton",
                   command=lambda: self._set_datetime(datetime.now() + timedelta(minutes=1))).pack(side="left", padx=(0, 4))
        ttk.Button(quick, text="In 10 min", style="Soft.TButton",
                   command=lambda: self._set_datetime(datetime.now() + timedelta(minutes=10))).pack(side="left")
        mode_row = ttk.Frame(when, style="Card.TFrame")
        mode_row.pack(fill="x", pady=(10, 0))
        ttk.Label(mode_row, text="At that time:", style="Muted.TLabel").pack(side="left", padx=(2, 8))
        self.v_mode = tk.StringVar(value="alarm")
        seg = ttk.Frame(mode_row, style="Card.TFrame")
        seg.pack(side="left")
        for txt, val in (("Ring until I stop it", "alarm"), ("Play the whole file once", "play")):
            ttk.Radiobutton(seg, text=txt, value=val, variable=self.v_mode, style="Seg.Toolbutton").pack(side="left", padx=(0, 3))
        self.v_mode.trace_add("write", lambda *_: self._on_mode_change())
        mode_row2 = ttk.Frame(when, style="Card.TFrame")
        mode_row2.pack(fill="x", pady=(6, 0))
        self.ring_len = ttk.Frame(mode_row2, style="Card.TFrame")
        ttk.Label(self.ring_len, text="If nobody stops it, give up after", style="Muted.TLabel").pack(side="left", padx=(2, 6))
        self.v_ring = tk.StringVar(value="10")
        ttk.Spinbox(self.ring_len, from_=1, to=120, width=3, textvariable=self.v_ring).pack(side="left")
        ttk.Label(self.ring_len, text="minutes", style="Muted.TLabel").pack(side="left", padx=4)
        self.l_mode_hint = ttk.Label(mode_row2, style="Muted.TLabel",
                                     text="Plays from start to end and stops by itself – for long talks, sermons or albums.")
        self._on_mode_change()

        ttk.Separator(grid).grid(row=1, column=0, columnspan=2, sticky="we", pady=14)

        # ② Sound
        ttk.Label(grid, text="②  SOUND", style="Section.TLabel").grid(row=2, column=0, sticky="nw", pady=(4, 0))
        snd = ttk.Frame(grid, style="Card.TFrame")
        snd.grid(row=2, column=1, sticky="we")
        self.v_sound = tk.StringVar()
        srow = ttk.Frame(snd, style="Card.TFrame")
        srow.pack(fill="x")
        self.l_sound_name = ttk.Label(srow, text="No sound chosen yet", style="Card.TLabel", font=F["bold"])
        self.l_sound_name.pack(side="left")
        self.l_sound_path = ttk.Label(srow, text="", style="Muted.TLabel")
        self.l_sound_path.pack(side="left", padx=(10, 0))
        self.v_sound.trace_add("write", lambda *_: self._on_sound_change())
        brow = ttk.Frame(snd, style="Card.TFrame")
        brow.pack(fill="x", pady=(8, 0))
        ttk.Button(brow, text="Choose a file…", style="Soft.TButton", command=self._browse).pack(side="left")
        self.b_rec = ttk.Button(brow, text="🎤  Record my voice", style="Soft.TButton", command=self._toggle_record)
        self.b_rec.pack(side="left", padx=8)
        self.b_test = ttk.Button(brow, text="▶  Preview", style="Soft.TButton", command=self._test_play)
        self.b_test.pack(side="left")
        ttk.Button(brow, text="■  Stop", style="Soft.TButton", command=self._stop_all).pack(side="left", padx=8)
        mrow = ttk.Frame(snd, style="Card.TFrame")
        mrow.pack(fill="x", pady=(8, 0))
        ttk.Label(mrow, text="Microphone", style="Muted.TLabel").pack(side="left", padx=(2, 8))
        self.devices = Recorder.input_devices()
        self.v_mic = tk.StringVar(value=self.devices[0][1] if self.devices else "No microphone found")
        self.cb_mic = ttk.Combobox(mrow, textvariable=self.v_mic, state="readonly", width=30,
                                   values=[d[1] for d in self.devices], font=F["small"])
        self.cb_mic.pack(side="left")
        ttk.Button(mrow, text="↻", style="Icon.TButton", command=self._refresh_mics).pack(side="left", padx=(4, 12))
        self.meter = ttk.Progressbar(mrow, length=110, maximum=100, style="Meter.Horizontal.TProgressbar")
        self.meter.pack(side="left")
        self.l_rec = ttk.Label(snd, text="", style="Muted.TLabel")
        self.l_rec.pack(anchor="w", padx=(2, 0), pady=(4, 0))
        vrow = ttk.Frame(snd, style="Card.TFrame")
        vrow.pack(fill="x", pady=(12, 0))
        ttk.Label(vrow, text="Volume", style="Muted.TLabel").pack(side="left", padx=(2, 10))
        ttk.Label(vrow, text="🔈", style="Card.TLabel").pack(side="left")
        self.v_volume = tk.IntVar(value=80)
        ttk.Scale(vrow, from_=0, to=100, orient="horizontal", variable=self.v_volume, length=300,
                  command=lambda v: self._on_volume()).pack(side="left", padx=8)
        ttk.Label(vrow, text="🔊", style="Card.TLabel").pack(side="left")
        self.l_volume = ttk.Label(vrow, text="80 %", style="Card.TLabel", font=F["bold"], width=6)
        self.l_volume.pack(side="left", padx=(10, 0))

        ttk.Separator(grid).grid(row=3, column=0, columnspan=2, sticky="we", pady=14)

        # ③ Where
        ttk.Label(grid, text="③  WHERE", style="Section.TLabel").grid(row=4, column=0, sticky="nw", pady=(6, 0))
        where = ttk.Frame(grid, style="Card.TFrame")
        where.grid(row=4, column=1, sticky="we")
        self.v_output = tk.StringVar(value=self.DEFAULT_OUTPUT)
        wrow = ttk.Frame(where, style="Card.TFrame")
        wrow.pack(fill="x")
        self.cb_output = ttk.Combobox(wrow, textvariable=self.v_output, state="readonly", width=40)
        self.cb_output.pack(side="left")
        self.cb_output.bind("<<ComboboxSelected>>", self._on_output_selected)
        ttk.Button(wrow, text="↻", style="Icon.TButton", command=lambda: self._refresh_outputs(load_airplay=True)).pack(side="left", padx=(4, 0))
        ttk.Label(where, text="Speakers, headset, HDMI" + (", or an AirPlay speaker such as a HomePod" if IS_MAC else ""),
                  style="Muted.TLabel").pack(anchor="w", padx=(2, 0), pady=(4, 0))
        self._refresh_outputs()

        # save row
        save = ttk.Frame(c2, style="Card.TFrame")
        save.pack(fill="x", pady=(18, 0))
        self.b_save = ttk.Button(save, text="Save alarm", style="Accent.TButton", command=self._save)
        self.b_save.pack(side="left")
        ttk.Button(save, text="Cancel", style="Soft.TButton", command=self._new).pack(side="left", padx=8)
        self.l_form_hint = ttk.Label(save, text="", style="Muted.TLabel")
        self.l_form_hint.pack(side="left", padx=12)

        # ---- left column, below the list: more options (collapsed by default)
        more_bar = tk.Frame(left, bg=P["bg"])
        more_bar.pack(fill="x", pady=(12, 0))
        self.b_more = ttk.Button(more_bar, text="▸  More options  (sleep, snooze, fade-in)", style="Soft.TButton", command=self._toggle_more)
        self.b_more.pack(side="left")
        self.more_card_outer = tk.Frame(left, bg=P["card"], highlightbackground=P["line"], highlightthickness=1)
        mc = ttk.Frame(self.more_card_outer, style="Card.TFrame", padding=(20, 12))
        mc.pack(fill="both", expand=True)
        s = self.store.settings
        self.v_keep = tk.BooleanVar(value=s["keep_awake"])
        self.v_wake = tk.BooleanVar(value=s["schedule_wake"])
        self.v_sysvol = tk.BooleanVar(value=s["force_system_volume"])
        self.v_sysvol_level = tk.StringVar(value=str(s["system_volume"]))
        self.v_snooze = tk.StringVar(value=str(s["snooze_minutes"]))
        self.v_fade = tk.StringVar(value=str(s.get("fade_seconds", 20)))
        ttk.Checkbutton(mc, text="Keep my computer awake while an alarm is set", variable=self.v_keep,
                        style="Card.TCheckbutton", command=self._settings_changed).pack(anchor="w")
        ttk.Checkbutton(mc, text="Wake my computer from sleep for alarms", variable=self.v_wake,
                        style="Card.TCheckbutton", command=self._settings_changed).pack(anchor="w")
        ttk.Label(mc, text=("macOS asks for your password once each time the next alarm changes." if IS_MAC else
                            "Asks for your password (uses rtcwake)." if IS_LINUX else
                            "Uses a Windows wake timer; allow wake timers in your power plan."),
                  style="Muted.TLabel").pack(anchor="w", padx=(26, 0))
        r = ttk.Frame(mc, style="Card.TFrame")
        r.pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(r, text="Turn the system volume up to", variable=self.v_sysvol, style="Card.TCheckbutton",
                        command=self._settings_changed).pack(side="left")
        ttk.Spinbox(r, from_=10, to=100, width=3, textvariable=self.v_sysvol_level).pack(side="left", padx=4)
        ttk.Label(r, text="% when an alarm rings", style="Card.TLabel").pack(side="left")
        r2 = ttk.Frame(mc, style="Card.TFrame")
        r2.pack(anchor="w", pady=(10, 4))
        ttk.Label(r2, text="Snooze for", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(r2, from_=1, to=60, width=3, textvariable=self.v_snooze).pack(side="left", padx=4)
        ttk.Label(r2, text="minutes", style="Card.TLabel").pack(side="left")
        ttk.Label(r2, text="Fade the sound in over", style="Card.TLabel").pack(side="left", padx=(28, 0))
        ttk.Spinbox(r2, from_=0, to=120, width=3, textvariable=self.v_fade).pack(side="left", padx=4)
        ttk.Label(r2, text="seconds", style="Card.TLabel").pack(side="left")
        for var in (self.v_sysvol_level, self.v_snooze, self.v_fade):
            var.trace_add("write", lambda *_: self._settings_changed())

        self._build_today()
        self._build_schedules()

    def _show_page(self, name: str) -> None:
        if name != "schedules" and self.sdraft and not self._sched_confirm_discard():
            self.v_page.set("schedules")
            return
        self.v_page.set(name)
        for n, f in self.pages.items():
            if n == name:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        if name == "today":
            self._refresh_today()
        elif name == "schedules":
            self._refresh_schedules()

    # ----- Today page
    def _build_today(self) -> None:
        P, F = self.PALETTE, self.F
        card = self._card(self.pages["today"], fill="both", expand=True)
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        self.l_today_title = ttk.Label(top, text="Today", style="Title.TLabel")
        self.l_today_title.pack(side="left")
        self.b_stop_msg = ttk.Button(top, text="■  Stop message", style="Danger.TButton", command=self._stop_announcement)
        ttk.Label(top, text="Show", style="Muted.TLabel").pack(side="left", padx=(28, 6))
        self.v_today_filter = tk.StringVar(value="All schedules")
        self.cb_today_filter = ttk.Combobox(top, textvariable=self.v_today_filter, state="readonly", width=28, font=F["small"])
        self.cb_today_filter.pack(side="left")
        self.cb_today_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_today())
        self.l_today_next = ttk.Label(card, text="", style="Muted.TLabel")
        self.l_today_next.pack(anchor="w", pady=(8, 0))
        cols = ("time", "activity", "schedule", "sound", "output", "status")
        self.ttree = ttk.Treeview(card, columns=cols, show="headings", height=3, selectmode="browse")
        heads = {"time": ("Time", 70, False), "activity": ("Activity", 200, True), "schedule": ("Schedule", 190, True),
                 "sound": ("Sound", 170, True), "output": ("Speaker", 150, True), "status": ("Status", 150, False)}
        for c in cols:
            self.ttree.heading(c, text=heads[c][0], anchor="w")
            self.ttree.column(c, width=heads[c][1], anchor="w", stretch=heads[c][2])
        self.ttree.tag_configure("earlier", foreground=P["muted"])
        self.ttree.tag_configure("off", foreground=P["muted"])
        self.ttree.tag_configure("playing", background=P["tint_ring"])
        self.ttree.tag_configure("problem", foreground=P["bad"])
        self.ttree.pack(fill="both", expand=True, pady=(12, 6))
        self.ttree.bind("<<TreeviewSelect>>", lambda e: self._today_update_buttons())
        self.ttree.bind("<Button-2>", self._today_menu)
        self.ttree.bind("<Button-3>", self._today_menu)
        self.ttree.bind("<Control-Button-1>", self._today_menu)
        self.today_menu = tk.Menu(self, tearoff=0)
        self.today_menu.add_command(label="Skip today", command=lambda: self._today_skip(True))
        self.today_menu.add_command(label="Undo skip", command=lambda: self._today_skip(False))
        self.today_menu.add_command(label="Preview sound", command=self._today_preview)
        self.today_menu.add_command(label="Stop message", command=self._stop_announcement)
        self.l_today_empty = ttk.Label(card, text="", style="Muted.TLabel", justify="left", wraplength=900)
        self.b_today_create = ttk.Button(card, text="＋  Create your first schedule", style="Accent.TButton",
                                         command=lambda: (self._show_page("schedules"), self._sched_new()))
        acts = ttk.Frame(card, style="Card.TFrame")
        acts.pack(fill="x")
        self.b_today_earlier = ttk.Button(acts, text="▸  Earlier today", style="Soft.TButton", command=self._today_toggle_earlier)
        self.b_today_earlier.pack(side="left")
        self.b_today_skip = ttk.Button(acts, text="Skip today", style="Soft.TButton", command=lambda: self._today_skip(True))
        self.b_today_skip.pack(side="right")
        self.b_today_unskip = ttk.Button(acts, text="Undo skip", style="Soft.TButton", command=lambda: self._today_skip(False))
        self.b_today_preview = ttk.Button(acts, text="▶  Preview sound", style="Soft.TButton", command=self._today_preview)
        self.b_today_preview.pack(side="right", padx=8)
        self.l_today_hint = ttk.Label(card, text="Select a row to preview its sound or skip it for today.  Playing a message only reminds – it never marks the activity as done.",
                                      style="Muted.TLabel")
        self.l_today_hint.pack(anchor="w", pady=(8, 0))

    STATUS_TEXT = {"queued": "Playing soon", "playing": "Playing", "played": "Played", "missed": "Missed", "failed": "Failed",
                   "skipped": "Skipped", "stopped": "Stopped", "interrupted": "Interrupted"}

    def _today_rows(self) -> list[dict]:
        """Everything that happens today, from all schedules plus the individual alarms, with a plain status."""
        now = datetime.now()
        today = now.date()
        rows: list[dict] = []
        for sc in self.store.schedules:
            if not sc["enabled"] or today.weekday() not in sc["days"]:
                continue
            sched_skipped = self.store.is_skipped(today, sc["id"])
            for ev in sc["events"]:
                try:
                    dt = datetime.combine(today, alarm_time(ev))
                except ValueError:
                    continue
                key = occurrence_key(sc["id"], ev["id"], today)
                occ = self.store.occurrences.get(key)
                path = resolve_sound(ev.get("sound", ""))
                if not path:
                    sound = "No sound"
                elif not os.path.isfile(path):
                    sound = "Missing file"
                elif path.startswith(REC_DIR):
                    sound = "🎤 Recording"
                else:
                    sound = os.path.basename(path)
                skipped = sched_skipped or self.store.is_skipped(today, sc["id"], ev["id"])
                if occ:                                   # what really happened always wins
                    status = self.STATUS_TEXT.get(occ["status"], occ["status"])
                elif not ev["enabled"]:
                    status = "Off"
                elif skipped:
                    status = "Skipped"
                elif not path:
                    status = "No sound"
                elif not os.path.isfile(path):
                    status = "Missing file"
                elif dt < now - SCHEDULE_GRACE:
                    status = "Missed"
                else:
                    status = "Upcoming"
                rows.append(dict(iid=key, kind="event", dt=dt, time=ev["time"], label=ev["label"], sched=sc["name"], sid=sc["id"],
                                 eid=ev["id"], sound=sound, output=output_label(sc.get("output", "")), status=status,
                                 skipped=skipped, note=(occ or {}).get("note", ""), path=path, volume=sc["volume"],
                                 out=sc.get("output", "")))
        for a in self.store.alarms:
            nf = next_fire(a, now)
            last = datetime.fromisoformat(a["last_fired"]) if a.get("last_fired") else None
            if a["id"] in self.ring_windows:
                dt, status = (nf or last or now), ("Playing" if a["id"] in self.once_play else "Ringing")
            elif nf and nf.date() == today:
                dt, status = nf, "Upcoming"
            elif last and last.date() == today:
                dt, status = last, "Rang"
            else:
                continue
            rows.append(dict(iid="alarm:" + a["id"], kind="alarm", dt=dt, time=f"{dt:%H:%M}", label=a["label"], sched="Alarm",
                             sid=None, eid=a["id"], sound=os.path.basename(a["sound"]), output=output_label(a.get("output", "")),
                             status=status, skipped=False, note="", path=a["sound"], volume=a["volume"], out=a.get("output", "")))
        rows.sort(key=lambda r: (r["dt"], r["sched"], r["label"]))
        return rows

    def _refresh_today(self) -> None:
        now = datetime.now()
        self.l_today_title.config(text=f"Today · {now:%A, %d %B}")
        names = ["All schedules"] + [sc["name"] for sc in self.store.schedules]
        self.cb_today_filter["values"] = names
        if self.v_today_filter.get() not in names:
            self.v_today_filter.set("All schedules")
        flt = self.v_today_filter.get()
        rows = [r for r in self._today_rows() if flt == "All schedules" or r["sched"] == flt]
        live = ("Upcoming", "Playing", "Playing soon", "Ringing")
        upcoming = [r for r in rows if r["dt"] >= now or r["status"] in live]
        earlier = [r for r in rows if r not in upcoming]
        sel = self.ttree.selection()
        self.ttree.delete(*self.ttree.get_children())
        for r in upcoming + (earlier if self._today_expanded else []):
            tag = ("playing" if r["status"] in ("Playing", "Ringing") else "problem" if r["status"] in ("Missed", "Failed", "Missing file")
                   else "off" if r["status"] in ("Off", "Skipped", "No sound") else "earlier" if r in earlier else "")
            self.ttree.insert("", "end", iid=r["iid"], tags=(tag,), values=(
                r["time"], r["label"], r["sched"], r["sound"], r["output"], r["status"] + (f"  ({r['note']})" if r["note"] and r["status"] in ("Missed", "Failed") else "")))
        if sel and self.ttree.exists(sel[0]):
            self.ttree.selection_set(sel[0])
        self.b_today_earlier.config(text=("▾" if self._today_expanded else "▸") + f"  Earlier today ({len(earlier)})",
                                    state="normal" if earlier else "disabled")
        # empty states
        self.l_today_empty.pack_forget(); self.b_today_create.pack_forget()
        if not self.store.schedules and not rows:
            self.l_today_empty.config(text="No schedules yet.  A schedule is a named list of timed messages – for example “Son — School day” "
                                           "with a recorded “Breakfast is ready” at 07:20 on weekdays.")
            self.l_today_empty.pack(anchor="w", pady=(0, 8), before=self.ttree)
            self.b_today_create.pack(anchor="w", pady=(0, 12), before=self.ttree)
        elif not rows:
            self.l_today_empty.config(text="Nothing is planned for today.  Schedules that are off or not set for this weekday are not shown.")
            self.l_today_empty.pack(anchor="w", pady=(0, 8), before=self.ttree)
        self._today_update_buttons()
        self._refresh_today_countdown()

    def _refresh_today_countdown(self) -> None:
        ev = self.scheduler.next_event()
        if self.current_ann:
            e, sc = self.current_ann["event"], self.current_ann["schedule"]
            self.l_today_next.config(text=f"▶ Playing now:  {e['label']}  ·  {sc['name']}  on {output_label(sc.get('output', ''))}")
            if not self.b_stop_msg.winfo_ismapped():
                self.b_stop_msg.pack(side="right")
            return
        self.b_stop_msg.pack_forget()
        if ev:
            what = "Next message" if ev[1].get("kind") == "event" else "Next alarm"
            self.l_today_next.config(text=f"{what}:  {ev[1]['label']}  ·  {ev[0]:%A %d %b at %H:%M}  ·  in {fmt_delta(ev[0] - datetime.now())}")
        else:
            self.l_today_next.config(text="Nothing is due.  Turn on a schedule or set an alarm.")

    def _today_toggle_earlier(self) -> None:
        self._today_expanded = not self._today_expanded
        self._refresh_today()

    def _today_selected(self) -> dict | None:
        sel = self.ttree.selection()
        if not sel:
            return None
        return next((r for r in self._today_rows() if r["iid"] == sel[0]), None)

    @staticmethod
    def _can_skip(r: dict | None) -> bool:
        """Only something that has not happened yet can be skipped for today."""
        return bool(r and r["kind"] == "event" and r["dt"] >= datetime.now() - SCHEDULE_GRACE
                    and r["status"] in ("Upcoming", "Skipped", "No sound"))

    def _today_update_buttons(self) -> None:
        r = self._today_selected()
        self.b_today_skip.pack_forget(); self.b_today_unskip.pack_forget()
        can = self._can_skip(r)
        if r and r["kind"] == "event" and r["skipped"] and can:
            self.b_today_unskip.pack(side="right")
        else:
            self.b_today_skip.pack(side="right")
            self.b_today_skip.config(state="normal" if can and not (r and r["skipped"]) else "disabled")
        self.b_today_preview.config(state="normal" if r and r["path"] and os.path.isfile(r["path"]) else "disabled")

    def _today_menu(self, e) -> None:
        iid = self.ttree.identify_row(e.y)
        if iid:
            self.ttree.selection_set(iid)
            self._today_update_buttons()
            r = self._today_selected()
            self.today_menu.entryconfig("Skip today", state="normal" if self._can_skip(r) and not r["skipped"] else "disabled")
            self.today_menu.entryconfig("Undo skip", state="normal" if self._can_skip(r) and r["skipped"] else "disabled")
            self.today_menu.entryconfig("Preview sound", state="normal" if r and r["path"] and os.path.isfile(r["path"]) else "disabled")
            self.today_menu.entryconfig("Stop message", state="normal" if self.current_ann else "disabled")
            self.today_menu.tk_popup(e.x_root, e.y_root)

    def _today_skip(self, on: bool) -> None:
        r = self._today_selected()
        if not r:
            return
        if r["kind"] != "event":
            self.l_today_hint.config(text="Individual alarms are turned off in the Alarms view (select it there and press Turn on / off).")
            return
        today = datetime.now().date()
        if not self._can_skip(r):
            return
        if on and self.store.is_skipped(today, r["sid"]):
            return
        if not on and self.store.is_skipped(today, r["sid"]):
            # the whole schedule was skipped: bring back only this event, keep the others skipped
            sc = self.store.get_schedule(r["sid"])
            self.store.set_skip(today, r["sid"], None, False)
            for other in (sc["events"] if sc else []):
                if other["id"] != r["eid"]:
                    self.store.set_skip(today, r["sid"], other["id"], True)
        self.store.set_skip(today, r["sid"], r["eid"], on)
        if on:
            self._cancel_announcements(r["sid"], r["eid"], "skipped for today")
        log(f"{'skip' if on else 'undo skip'} today: '{r['label']}' ({r['sched']})")
        self.l_today_hint.config(text=(f"“{r['label']}” is skipped for today only. Tomorrow it runs as usual." if on
                                       else f"“{r['label']}” is back on for today."))
        self._refresh_today(); self._refresh_schedules(); self._apply_power(); self._tick_indicators()

    def _today_preview(self) -> None:
        r = self._today_selected()
        if r:
            self._preview_sound(r["path"], int(r["volume"]), r["out"], self.l_today_hint)

    # ----- Schedules page
    def _build_schedules(self) -> None:
        P, F = self.PALETTE, self.F
        page = self.pages["schedules"]
        right = tk.Frame(page, bg=P["bg"])
        right.pack(side="right", fill="y")
        left = tk.Frame(page, bg=P["bg"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))

        # list card
        c1 = self._card(left, fill="both", expand=True)
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Schedules", style="Title.TLabel").pack(side="left")
        ttk.Button(row, text="＋  Create schedule", style="Accent.TButton", command=self._sched_new).pack(side="right")
        cols = ("on", "name", "days", "output", "count")
        self.stree = ttk.Treeview(c1, columns=cols, show="headings", height=3, selectmode="browse")
        for c, (txt, w, st) in {"on": ("", 36, False), "name": ("Schedule", 140, True), "days": ("Repeats", 95, False),
                                "output": ("Speaker", 110, True), "count": ("Events", 55, False)}.items():
            self.stree.heading(c, text=txt, anchor="center" if c == "on" else "w")
            self.stree.column(c, width=w, anchor="center" if c == "on" else "w", stretch=st)
        self.stree.tag_configure("off", foreground=P["muted"])
        self.stree.pack(fill="both", expand=True, pady=(12, 6))
        self.stree.bind("<<TreeviewSelect>>", self._sched_on_select)
        self.stree.bind("<Double-1>", lambda e: self._sched_toggle())
        self.l_sched_empty = ttk.Label(c1, text="No schedules yet.  Create one for each person – for example “Son — School day” – "
                                                "then add timed messages such as “Wake up” or “Breakfast is ready”.",
                                       style="Muted.TLabel", justify="left", wraplength=420)
        acts = ttk.Frame(c1, style="Card.TFrame")
        acts.pack(fill="x")
        self.b_sched_toggle = ttk.Button(acts, text="Turn on / off", style="Soft.TButton", command=self._sched_toggle)
        self.b_sched_toggle.pack(side="left")
        self.b_sched_dup = ttk.Button(acts, text="Duplicate", style="Soft.TButton", command=self._sched_duplicate)
        self.b_sched_dup.pack(side="left", padx=8)
        self.b_sched_skip = ttk.Button(acts, text="Skip today", style="Soft.TButton", command=self._sched_skip_today)
        self.b_sched_skip.pack(side="left")
        self.mb_sched_more = ttk.Menubutton(acts, text="⋯", style="Icon.TButton", direction="below", width=3)
        m = tk.Menu(self.mb_sched_more, tearoff=0)
        m.add_command(label="Delete schedule…", command=self._sched_delete)
        self.mb_sched_more["menu"] = m
        self.mb_sched_more.pack(side="right")
        ttk.Label(c1, text="Tip: click a schedule to edit it, double-click to turn it on or off.  A skipped schedule runs again tomorrow.",
                  style="Muted.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(8, 0))

        # editor card
        c2 = self._card(right, fill="both", expand=True)
        self.sched_editor = c2
        row = ttk.Frame(c2, style="Card.TFrame")
        row.pack(fill="x")
        self.l_sched_title = ttk.Label(row, text="New schedule", style="Title.TLabel")
        self.l_sched_title.pack(side="left")
        self.v_senabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Schedule is on", variable=self.v_senabled, style="Card.TCheckbutton").pack(side="right")
        self.l_sched_saved = ttk.Label(row, text="", style="Good.TLabel")
        self.l_sched_saved.pack(side="right", padx=(0, 16))
        grid = ttk.Frame(c2, style="Card.TFrame")
        grid.pack(fill="x", pady=(6, 0))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Name", style="Card.TLabel", width=9).grid(row=0, column=0, sticky="nw", pady=(6, 0))
        self.v_sname = tk.StringVar()
        self.e_sname = ttk.Entry(grid, textvariable=self.v_sname, font=F["base"], width=34)
        self.e_sname.grid(row=0, column=1, sticky="w")
        self.l_err_name = NoteLabel(grid, text="", style="Bad.TLabel")
        self.l_err_name.grid(row=1, column=1, sticky="w"); self.l_err_name.grid_remove()

        ttk.Label(grid, text="Repeats", style="Card.TLabel").grid(row=2, column=0, sticky="nw", pady=(12, 0))
        days = ttk.Frame(grid, style="Card.TFrame")
        days.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.v_days = [tk.BooleanVar(value=i < 5) for i in range(7)]
        for i, n in enumerate(DAY_NAMES):
            ttk.Checkbutton(days, text=n, variable=self.v_days[i], style="Seg.Toolbutton").pack(side="left", padx=(0, 3))
        short = ttk.Frame(grid, style="Card.TFrame")
        short.grid(row=3, column=1, sticky="w", pady=(6, 0))
        for txt, val in (("Weekdays", WEEKDAYS), ("Every day", EVERY_DAY), ("Weekends", WEEKEND)):
            ttk.Button(short, text=txt, style="Soft.TButton", command=lambda v=val: self._sched_set_days(v)).pack(side="left", padx=(0, 4))
        self.l_err_days = NoteLabel(grid, text="", style="Bad.TLabel")
        self.l_err_days.grid(row=4, column=1, sticky="w"); self.l_err_days.grid_remove()

        ttk.Label(grid, text="Speaker", style="Card.TLabel").grid(row=5, column=0, sticky="nw", pady=(14, 0))
        spk = ttk.Frame(grid, style="Card.TFrame")
        spk.grid(row=5, column=1, sticky="we", pady=(10, 0))
        self.v_sched_output = tk.StringVar(value=self.DEFAULT_OUTPUT)
        self.cb_sched_output = ttk.Combobox(spk, textvariable=self.v_sched_output, state="readonly", width=34)
        self.cb_sched_output["values"] = list(self._output_map)
        self.cb_sched_output.pack(side="left")
        self.cb_sched_output.bind("<<ComboboxSelected>>", lambda e: self._on_output_selected(var=self.v_sched_output))
        ttk.Button(spk, text="↻", style="Icon.TButton", command=lambda: self._refresh_outputs(load_airplay=True)).pack(side="left", padx=(4, 8))
        ttk.Button(spk, text="🔈  Test speaker", style="Soft.TButton", command=self._sched_test_speaker).pack(side="left")

        ttk.Label(grid, text="Volume", style="Card.TLabel").grid(row=6, column=0, sticky="nw", pady=(12, 0))
        vol = ttk.Frame(grid, style="Card.TFrame")
        vol.grid(row=6, column=1, sticky="w", pady=(8, 0))
        self.v_svol = tk.IntVar(value=80)
        ttk.Scale(vol, from_=0, to=100, orient="horizontal", variable=self.v_svol, length=260,
                  command=lambda v: self.l_svol.config(text=f"{int(float(v))} %")).pack(side="left")
        self.l_svol = ttk.Label(vol, text="80 %", style="Card.TLabel", font=F["bold"], width=6)
        self.l_svol.pack(side="left", padx=(10, 0))

        self.l_sched_hint = NoteLabel(grid, text="", style="Muted.TLabel", wraplength=600, justify="left")
        self.l_sched_hint.grid(row=7, column=1, sticky="w", pady=(4, 0)); self.l_sched_hint.grid_remove()

        ttk.Separator(c2).pack(fill="x", pady=6)

        # events list, replaced by the event editor while an event is being edited
        self.ev_list = ttk.Frame(c2, style="Card.TFrame")
        self.ev_list.pack(fill="both", expand=True)
        row = ttk.Frame(self.ev_list, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Events", style="Card.TLabel", font=F["bold"]).pack(side="left")
        ttk.Button(row, text="＋  Add event", style="Accent.TButton", command=self._event_add).pack(side="right")
        cols = ("time", "label", "sound", "on")
        self.etree = ttk.Treeview(self.ev_list, columns=cols, show="headings", height=3, selectmode="browse")
        for c, (txt, w, st) in {"time": ("Time", 70, False), "label": ("Activity", 200, True), "sound": ("Sound", 190, True), "on": ("", 50, False)}.items():
            self.etree.heading(c, text=txt, anchor="w")
            self.etree.column(c, width=w, anchor="w", stretch=st)
        self.etree.tag_configure("off", foreground=P["muted"])
        self.etree.pack(fill="both", expand=True, pady=(8, 6))
        self.etree.bind("<Double-1>", lambda e: self._event_edit())
        self.l_ev_empty = ttk.Label(self.ev_list, text="No events yet.  Press Add event – each event needs only a time, a name and (optionally) a message.",
                                    style="Muted.TLabel")
        erow = ttk.Frame(self.ev_list, style="Card.TFrame")
        erow.pack(fill="x")
        ttk.Button(erow, text="Edit event", style="Soft.TButton", command=self._event_edit).pack(side="left")
        ttk.Button(erow, text="Remove event", style="Danger.TButton", command=self._event_remove).pack(side="left", padx=8)
        ttk.Label(erow, text="Events use the schedule's days, speaker and volume.", style="Muted.TLabel").pack(side="right")

        self._build_event_editor(c2)

        save = ttk.Frame(c2, style="Card.TFrame")
        save.pack(fill="x", pady=(10, 0), side="bottom")
        self.b_sched_save = ttk.Button(save, text="Save schedule", style="Accent.TButton", command=self._sched_save)
        self.b_sched_save.pack(side="left")
        ttk.Button(save, text="Cancel", style="Soft.TButton", command=self._sched_cancel).pack(side="left", padx=8)
        self.l_sched_err = ttk.Label(save, text="", style="Bad.TLabel")
        self.l_sched_err.pack(side="left", padx=12)

    def _build_event_editor(self, parent) -> None:
        P, F = self.PALETTE, self.F
        box = self.ev_box = ttk.Frame(parent, style="Card.TFrame")       # packed in place of ev_list while editing
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x")
        self.l_ev_title = ttk.Label(row, text="New event", style="Card.TLabel", font=F["bold"])
        self.l_ev_title.pack(side="left")
        g = ttk.Frame(box, style="Card.TFrame")
        g.pack(fill="x", pady=(8, 0))
        ttk.Label(g, text="Time", style="Card.TLabel", width=9).grid(row=0, column=0, sticky="w")
        t = ttk.Frame(g, style="Card.TFrame")
        t.grid(row=0, column=1, sticky="w")
        self.v_eh = tk.StringVar(value="07"); self.v_em = tk.StringVar(value="00")
        ttk.Spinbox(t, from_=0, to=23, width=3, textvariable=self.v_eh, format="%02.0f", wrap=True, font=F["time"], justify="center").pack(side="left")
        ttk.Label(t, text=":", style="Card.TLabel", font=F["time"]).pack(side="left", padx=4)
        ttk.Spinbox(t, from_=0, to=59, width=3, textvariable=self.v_em, format="%02.0f", wrap=True, font=F["time"], justify="center").pack(side="left")
        ttk.Label(t, text="Activity", style="Card.TLabel").pack(side="left", padx=(22, 8))
        self.v_elabel = tk.StringVar()
        self.e_elabel = ttk.Entry(t, textvariable=self.v_elabel, font=F["base"], width=24)
        self.e_elabel.pack(side="left")
        self.v_eon = tk.BooleanVar(value=True)
        ttk.Checkbutton(t, text="On", variable=self.v_eon, style="Card.TCheckbutton").pack(side="left", padx=(14, 0))
        self.l_err_event = NoteLabel(g, text="", style="Bad.TLabel")
        self.l_err_event.grid(row=1, column=1, sticky="w"); self.l_err_event.grid_remove()

        ttk.Label(g, text="Message", style="Card.TLabel").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        snd = ttk.Frame(g, style="Card.TFrame")
        snd.grid(row=2, column=1, sticky="we", pady=(6, 0))
        # state A: the chosen sound (or none)
        self.ev_sound_row = ttk.Frame(snd, style="Card.TFrame")
        self.ev_sound_row.pack(fill="x")
        self.l_ev_sound = ttk.Label(self.ev_sound_row, text="No sound – the event is shown in Today but nothing plays", style="Card.TLabel", wraplength=330, justify="left")
        self.l_ev_sound.pack(side="left")
        self.b_ev_preview = ttk.Button(self.ev_sound_row, text="▶  Preview", style="Soft.TButton", command=self._event_preview)
        self.b_ev_remove = ttk.Button(self.ev_sound_row, text="Remove sound", style="Soft.TButton", command=self._event_remove_sound)
        pick = ttk.Frame(snd, style="Card.TFrame")
        pick.pack(fill="x", pady=(8, 0))
        self.ev_pick_row = pick
        self.b_ev_rec = ttk.Button(pick, text="🎤  Record my voice", style="Soft.TButton", command=self._ev_record_start)
        self.b_ev_rec.pack(side="left")
        ttk.Button(pick, text="Choose audio file…", style="Soft.TButton", command=self._event_choose_file).pack(side="left", padx=8)
        ttk.Label(pick, text="Microphone", style="Muted.TLabel").pack(side="left", padx=(16, 6))
        self.cb_ev_mic = ttk.Combobox(pick, textvariable=self.v_mic, state="readonly", width=22, values=[d[1] for d in self.devices], font=F["small"])
        self.cb_ev_mic.pack(side="left")
        # state B: recording in progress
        self.ev_rec_row = ttk.Frame(snd, style="Card.TFrame")
        self.l_ev_rec = ttk.Label(self.ev_rec_row, text="●  Recording…", style="Rec.TLabel", wraplength=300, justify="left")
        self.l_ev_rec.pack(side="left")
        self.ev_meter = ttk.Progressbar(self.ev_rec_row, length=120, maximum=100, style="Meter.Horizontal.TProgressbar")
        self.ev_meter.pack(side="left", padx=12)
        ttk.Button(self.ev_rec_row, text="■  Stop recording", style="Danger.TButton", command=self._ev_record_stop).pack(side="left")
        ttk.Button(self.ev_rec_row, text="Cancel", style="Soft.TButton", command=self._ev_record_cancel).pack(side="left", padx=8)
        # state C: a take waiting for a decision
        self.ev_take_row = ttk.Frame(snd, style="Card.TFrame")
        self.l_ev_take = ttk.Label(self.ev_take_row, text="", style="Card.TLabel", wraplength=560, justify="left")
        self.l_ev_take.pack(anchor="w")
        tb = ttk.Frame(self.ev_take_row, style="Card.TFrame")
        tb.pack(fill="x", pady=(6, 0))
        ttk.Button(tb, text="▶  Preview", style="Soft.TButton", command=lambda: self._preview_sound(self.ev_take, int(self.v_svol.get()), self._get_output(self.v_sched_output), self.l_ev_hint)).pack(side="left")
        ttk.Button(tb, text="Record again", style="Soft.TButton", command=self._ev_record_again).pack(side="left", padx=8)
        ttk.Button(tb, text="Use recording", style="Accent.TButton", command=self._ev_take_use).pack(side="left")
        ttk.Button(tb, text="Discard", style="Soft.TButton", command=self._ev_take_discard).pack(side="left", padx=8)
        self.l_ev_hint = ttk.Label(snd, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.l_ev_hint.pack(anchor="w", pady=(6, 0))

        done = ttk.Frame(box, style="Card.TFrame")
        done.pack(fill="x", pady=(8, 0))
        ttk.Button(done, text="Done", style="Accent.TButton", command=self._event_done).pack(side="left")
        ttk.Button(done, text="Cancel", style="Soft.TButton", command=self._event_cancel).pack(side="left", padx=8)
        ttk.Label(done, text="Done keeps the event in the list – press Save schedule to store it.", style="Muted.TLabel").pack(side="left", padx=8)

    def _toggle_more(self) -> None:
        if self.more_card_outer.winfo_ismapped():
            self.more_card_outer.pack_forget()
            self.b_more.config(text="▸  More options  (sleep, snooze, fade-in)")
        else:
            self.more_card_outer.pack(fill="x", pady=(8, 0))
            self.b_more.config(text="▾  More options")

    def _on_repeat_change(self) -> None:
        """The date only matters for a one-time alarm; hide it for repeating ones."""
        if self.v_repeat.get() == "once":
            if not self.date_row.winfo_ismapped():
                self.date_row.pack(fill="x", pady=(10, 0), after=self.time_row)
        else:
            self.date_row.pack_forget()

    def _on_mode_change(self) -> None:
        """The give-up timeout only applies when ringing in a loop; a whole-file play stops on its own."""
        if self.v_mode.get() == "play":
            self.ring_len.pack_forget()
            if not self.l_mode_hint.winfo_ismapped():
                self.l_mode_hint.pack(side="left", padx=(2, 0))
        else:
            self.l_mode_hint.pack_forget()
            if not self.ring_len.winfo_ismapped():
                self.ring_len.pack(side="left")

    def _on_sound_change(self) -> None:
        p = self.v_sound.get().strip()
        if not p:
            self.l_sound_name.config(text="No sound chosen yet")
            self.l_sound_path.config(text="")
            return
        folder = os.path.dirname(p).replace(os.path.expanduser("~"), "~")
        if len(folder) > 48:
            folder = "…" + folder[-47:]
        self.l_sound_name.config(text=os.path.basename(p))
        self.l_sound_path.config(text=folder if os.path.isfile(p) else "file not found")

    # ----- form helpers
    DEFAULT_OUTPUT = "System default output"

    LOAD_AIRPLAY = "__load_airplay__"

    def _output_boxes(self) -> list[tuple]:
        boxes = [(self.cb_output, self.v_output)]
        if hasattr(self, "cb_sched_output"):
            boxes.append((self.cb_sched_output, self.v_sched_output))
        return boxes

    def _refresh_outputs(self, load_airplay: bool = False) -> None:
        """Fill 'Play on': system default, local devices, then AirPlay speakers (macOS, read from Music).
        AirPlay devices are only read when Music is already running, or when the user asks (↻ / list entry),
        so opening the alarm clock never opens Music by itself."""
        currents = [(var, self._get_output(var)) for _cb, var in self._output_boxes()]
        self._output_map = {self.DEFAULT_OUTPUT: ""}
        for n in Player.output_devices():
            self._output_map[n] = n
        if self.airplay:
            try:
                for d in self.airplay.devices(launch=load_airplay):
                    label = f"AirPlay: {d['name']} ({d['kind']})" + ("" if d["available"] else "  (offline)")
                    self._output_map[label] = AirPlayPlayer.PREFIX + d["name"]
            except Exception as e:
                log(f"AirPlay device list failed: {e}")
                if hasattr(self, "l_form_hint"):
                    self.l_form_hint.config(text=f"Could not read AirPlay speakers from Music: {e}")
            if not any(v.startswith(AirPlayPlayer.PREFIX) for v in self._output_map.values()):
                self._output_map["AirPlay speakers…  (select to load them from Music)"] = self.LOAD_AIRPLAY
        for cb, _var in self._output_boxes():
            cb["values"] = list(self._output_map)
        for var, current in currents:
            self._set_output(current, var)

    def _on_output_selected(self, _e=None, var: tk.StringVar | None = None) -> None:
        var = var or self.v_output
        if self._output_map.get(var.get()) == self.LOAD_AIRPLAY:
            self._refresh_outputs(load_airplay=True)
            first = next((lbl for lbl, v in self._output_map.items() if v.startswith(AirPlayPlayer.PREFIX)), None)
            var.set(first or self.DEFAULT_OUTPUT)
            if not first:
                hint = self.l_form_hint if var is self.v_output else self.l_sched_hint
                hint.config(text="Music found no AirPlay speakers. Check they are on and on the same Wi-Fi.")

    def _set_output(self, value: str, var: tk.StringVar | None = None) -> None:
        """Show an output in a combobox, even if that device is currently unplugged/offline."""
        var = var or self.v_output
        for label, v in self._output_map.items():
            if v == value:
                var.set(label)
                return
        if value:
            label = output_label(value) + "  (not connected)"
            self._output_map[label] = value
            for cb, _v in self._output_boxes():
                cb["values"] = list(self._output_map)
            var.set(label)
        else:
            var.set(self.DEFAULT_OUTPUT)

    def _get_output(self, var: tk.StringVar | None = None) -> str:
        v = self._output_map.get((var or self.v_output).get(), "")
        return "" if v == self.LOAD_AIRPLAY else v

    def _is_airplay(self, value: str) -> bool:
        return bool(self.airplay) and value.startswith(AirPlayPlayer.PREFIX)

    def _play_airplay(self, alarm: dict | None, path: str, volume: int, device: str, loop: bool, fade: int, hint=None) -> None:
        """Start AirPlay playback in a worker thread; failures come back through the event queue."""
        def work():
            try:
                self.airplay.play(path, volume, device, loop=loop, fade_seconds=fade, on_started=lambda: self.events.put(
                    ("notice", alarm, f"Playing on AirPlay speaker “{device}” via Music")))
                if alarm is not None and not loop:      # play() returns once the whole file has ended
                    self.events.put(("finished", alarm["id"], None))
            except Exception as e:
                log(f"AirPlay playback failed ({device}): {e}")
                self.events.put(("airplay_failed", alarm, f"{e}"))
        (hint or self.l_form_hint).config(text=f"Sending to AirPlay speaker “{device}” via Music…")
        threading.Thread(target=work, daemon=True).start()

    def _stop_airplay(self) -> None:
        if self.airplay and (self.airplay.playing or self.airplay._track_id):
            threading.Thread(target=self.airplay.stop, daemon=True).start()

    def _set_date(self, d: date) -> None:
        self.v_year.set(f"{d.year:04d}"); self.v_month.set(f"{d.month:02d}"); self.v_day.set(f"{d.day:02d}")

    def _set_datetime(self, dt: datetime) -> None:
        self._set_date(dt.date())
        self.v_hour.set(f"{dt.hour:02d}"); self.v_min.set(f"{dt.minute:02d}")

    def _load_form(self, a: dict) -> None:
        self.editing_id = a["id"] if self.store.get(a["id"]) else None
        self.v_label.set(a["label"])
        self._set_date(date.fromisoformat(a["date"]))
        h, m = a["time"].split(":")
        self.v_hour.set(h); self.v_min.set(m)
        self.v_repeat.set(a["repeat"])
        self.v_sound.set(a["sound"])
        self._set_output(a.get("output", ""))
        self.v_volume.set(a["volume"])
        self.v_ring.set(str(a.get("ring_minutes", 10)))
        self.v_mode.set(a.get("mode", "alarm"))
        self._on_volume()
        self.b_save.config(text="Save changes" if self.editing_id else "Save alarm")
        self.l_editor_title.config(text=f"Editing “{a['label']}”" if self.editing_id else "New alarm")
        self.l_form_hint.config(text="")
        self._on_repeat_change()

    def _read_form(self) -> dict | None:
        try:
            d = date(int(self.v_year.get()), int(self.v_month.get()), int(self.v_day.get()))
            h, m = int(self.v_hour.get()), int(self.v_min.get())
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError("hour/minute out of range")
            ring = max(1, int(self.v_ring.get()))
        except ValueError as e:
            log(f"form validation: {e}")
            messagebox.showerror(APP_NAME, "Please check the date and time.\n\n"
                                 "Every field needs a number, the day must exist in that month, "
                                 "hours are 0–23 and minutes 0–59.")
            return None
        sound = self.v_sound.get().strip()
        if not sound or not os.path.isfile(sound):
            messagebox.showerror(APP_NAME, "Step ② is missing: choose a sound file or record your voice first.")
            return None
        a = self.store.get(self.editing_id) if self.editing_id else None
        a = dict(a) if a else new_alarm()
        a.update({
            "label": self.v_label.get().strip() or "Alarm",
            "date": d.isoformat(),
            "time": f"{h:02d}:{m:02d}",
            "repeat": self.v_repeat.get(),
            "sound": sound,
            "output": self._get_output(),
            "volume": int(self.v_volume.get()),
            "ring_minutes": ring,
            "mode": self.v_mode.get(),
            "enabled": True,
            "last_fired": None,
        })
        when = next_fire(a, datetime.now())
        if a["repeat"] == "once" and (when is None or when < datetime.now()):
            messagebox.showerror(APP_NAME, "That date and time is already in the past.")
            return None
        return a

    # ----- actions
    def _new(self) -> None:
        self._load_form(new_alarm(self.store.settings))
        self.tree.selection_remove(self.tree.selection())

    def _save(self) -> None:
        a = self._read_form()
        if not a:
            return
        # remember what the user chose so the next new alarm starts from it
        self.store.settings.update({"last_sound": a["sound"], "last_volume": a["volume"],
                                    "last_repeat": a["repeat"], "last_output": a.get("output", ""),
                                    "last_mode": a.get("mode", "alarm")})
        self.store.upsert(a)
        self.scheduler.ringing.discard(a["id"])
        self._refresh_list(select=a["id"])
        self._apply_power()
        nf = next_fire(a)
        self.l_form_hint.config(text=f"Saved – rings {nf:%a %d %b %H:%M}" if nf else "Saved")
        self.editing_id = a["id"]
        self.b_save.config(text="Save changes")
        self.l_editor_title.config(text=f"Editing “{a['label']}”")

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.store.get(sel[0]) if sel else None

    def _on_select(self, _e=None) -> None:
        a = self._selected()
        if a:
            self._load_form(a)

    def _toggle_selected(self) -> None:
        a = self._selected()
        if not a:
            return
        a = dict(a)
        a["enabled"] = not a["enabled"]
        if a["enabled"]:
            a["last_fired"] = None
            if a["repeat"] == "once" and next_fire(a) and next_fire(a) < datetime.now():
                messagebox.showinfo(APP_NAME, "This one-time alarm is in the past. Edit its date/time and save it.")
                return
        self.store.upsert(a)
        self._refresh_list(select=a["id"])
        self._apply_power()

    def _delete_selected(self) -> None:
        a = self._selected()
        if a and messagebox.askyesno(APP_NAME, f"Delete alarm “{a['label']}”?"):
            self.store.delete(a["id"])
            self._refresh_list()
            self._apply_power()
            self._new()

    def _ring_selected(self) -> None:
        a = self._selected()
        if a:
            self.events.put(("ring", a, datetime.now()))

    def _browse(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose an alarm sound",
            initialdir=REC_DIR if os.path.isdir(REC_DIR) else os.path.expanduser("~"),
            filetypes=[("Audio files", " ".join("*" + e for e in SUPPORTED_AUDIO)), ("All files", "*.*")])
        if p:
            self.v_sound.set(p)

    def _test_play(self) -> None:
        p = self.v_sound.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showerror(APP_NAME, "Choose a sound file or record your voice first.")
            return
        if self.current_ann:
            self.l_form_hint.config(text="A schedule message is playing right now. Stop it first (■ Stop message in the header), then preview.")
            return
        out = self._get_output()
        if self._is_airplay(out):
            self._play_airplay(None, p, int(self.v_volume.get()), out[len(AirPlayPlayer.PREFIX):], loop=False, fade=0)
            return
        try:
            warning = self.player.play(p, self.v_volume.get(), loop=False, device=out)
            self.l_form_hint.config(text=warning or f"Playing on {self._get_output() or 'system default output'}")
        except Exception as e:
            log(f"test playback failed for {p}: {e}")
            messagebox.showerror(APP_NAME, f"Could not play {os.path.basename(p)}.\n\n"
                                 "The file may be damaged or in a format this app cannot decode. "
                                 "Try another file, or convert this one to MP3/WAV.\n\n"
                                 f"Details: {e}")

    def _stop_all(self) -> None:
        if self.current_ann:
            self._finish_announcement("stopped", "stopped by you")
        self.player.stop()
        self._stop_airplay()
        for aid in list(self.ring_windows):
            self._dismiss(aid)

    def _on_volume(self) -> None:
        v = int(self.v_volume.get())
        self.l_volume.config(text=f"{v} %")
        if self.player.is_playing():
            self.player.set_volume(v)
        if self.airplay and self.airplay.playing:          # Music is slow: debounce to one call per 400 ms
            if self._vol_job:
                self.after_cancel(self._vol_job)
            self._vol_job = self.after(400, lambda: threading.Thread(target=self.airplay.set_volume, args=(v,), daemon=True).start())

    def _refresh_mics(self) -> None:
        self.devices = Recorder.input_devices()
        self.cb_mic["values"] = [d[1] for d in self.devices]
        if hasattr(self, "cb_ev_mic"):
            self.cb_ev_mic["values"] = [d[1] for d in self.devices]
        if self.devices:
            self.v_mic.set(self.devices[0][1])

    def _toggle_record(self) -> None:
        if self.recorder.recording:
            try:
                path = self.recorder.stop()
            except Exception as e:
                log(f"recording save failed: {e}")
                self.b_rec.config(text="● Record")
                self.l_rec.config(text="")
                messagebox.showerror(APP_NAME, "Could not save the recording.\n\n"
                                     "The microphone may have disconnected, or the recordings folder is not "
                                     "writable. Check the microphone and try again.\n\n"
                                     f"Details: {e}")
                return
            self.b_rec.config(text="● Record")
            self.meter["value"] = 0
            self.v_sound.set(path)
            peak, gain = self.recorder.last_peak, self.recorder.last_gain_db
            if peak < QUIET_PEAK:
                self.l_rec.config(text="Almost nothing was recorded", foreground="#c62828")
                messagebox.showwarning(APP_NAME, "Almost nothing was recorded.\n\n"
                                       f"Microphone used: {self.v_mic.get()}\n\n"
                                       "Either this is not the microphone you are speaking into, its mute switch "
                                       "is on (headset booms often mute when flipped up), or macOS has not "
                                       "allowed this app to use the microphone (System Settings → Privacy & "
                                       "Security → Microphone). Pick another microphone in the list and try again.")
            elif gain >= 6:
                self.l_rec.config(text=f"Saved – was quiet ({peak*100:.0f}%), boosted {gain:+.0f} dB", foreground="#e65100")
            else:
                self.l_rec.config(text=f"Saved {os.path.basename(path)} (level {peak*100:.0f}%)", foreground="")
            return
        dev = next((d[0] for d in self.devices if d[1] == self.v_mic.get()), None)
        try:
            self.recorder.start(dev, REC_DIR)
        except Exception as e:
            messagebox.showerror(APP_NAME, "Could not start recording.\n\n"
                                 f"{e}\n\nOn macOS make sure this app (or Terminal) is allowed to use the "
                                 "microphone in System Settings → Privacy & Security → Microphone.")
            return
        self._rec_owner = "alarm"
        self.b_rec.config(text="■ Stop")
        self.l_rec.config(text="Recording…", foreground="")
        self._update_meter()

    def _update_meter(self) -> None:
        if not self.recorder.recording:
            return
        self.meter["value"] = min(100, self.recorder.level * 140)
        secs = self.recorder.elapsed
        if secs > 2 and self.recorder.peak < 0.02:
            self.l_rec.config(text=f"Recording… {int(secs)} s – very quiet! Is “{self.v_mic.get()[:28]}” the right mic?",
                              foreground="#c62828")
        else:
            self.l_rec.config(text=f"Recording… {int(secs)} s   (peak {self.recorder.peak*100:.0f}%)", foreground="")
        self.after(80, self._update_meter)

    def _settings_changed(self) -> None:
        s = self.store.settings
        s["keep_awake"] = bool(self.v_keep.get())
        s["schedule_wake"] = bool(self.v_wake.get())
        s["force_system_volume"] = bool(self.v_sysvol.get())
        for key, var in (("system_volume", self.v_sysvol_level), ("snooze_minutes", self.v_snooze),
                         ("fade_seconds", self.v_fade)):
            try:
                s[key] = int(var.get())
            except ValueError:
                pass  # field is mid-edit (empty); keep the previous value
        self.store.save()
        self._apply_power()

    def _remember_wake(self, when: datetime | None) -> None:
        """Called from the power worker thread: persist the registered wake so the next launch can cancel it."""
        with self.store.lock:
            self.store.settings["registered_wake"] = when.isoformat(timespec="seconds") if when else ""
            try:
                self.store.save()
            except OSError as e:
                log(f"could not remember the registered wake: {e}")

    def _retry_wake(self) -> None:
        ev = self.scheduler.next_event()
        self.power.retry_wake(ev[0] if ev else None)
        self._tick_indicators()

    # ----- list / status
    def _refresh_list(self, select: str | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        now = datetime.now()
        repeat_txt = {"once": "Just once", "daily": "Every day", "weekdays": "Weekdays"}
        rows = sorted(self.store.alarms, key=lambda a: (not a["enabled"], next_fire(a, now) or datetime.max))
        for a in rows:
            nf = next_fire(a, now)
            self.tree.insert("", "end", iid=a["id"], tags=("on" if a["enabled"] else "off",), values=(
                "●" if a["enabled"] else "○", a["time"], a["label"], repeat_txt.get(a["repeat"], a["repeat"]),
                "Playing now" if a["id"] in self.once_play else (f"{nf:%a %d %b %H:%M}" if nf else "Off"),
                ("▶ " if a.get("mode") == "play" else "") + os.path.basename(a["sound"]), output_label(a.get("output", ""))))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
        if self.store.alarms:
            self.l_empty.pack_forget()
        elif not self.l_empty.winfo_ismapped():
            self.l_empty.pack(anchor="w", pady=(0, 10), before=self.acts)

    def _apply_power(self) -> None:
        s = self.store.settings
        ev = self.scheduler.next_event()
        armed = ev is not None
        playing = bool(self.ring_windows) or self.current_ann is not None
        self.power.set_keep_awake(bool(s["keep_awake"]) and (armed or playing))
        self.power.set_wake(ev[0] if (armed and s["schedule_wake"]) else None)

    def _tick_indicators(self) -> None:
        ev = self.scheduler.next_event()
        now = datetime.now()
        s = self.store.settings
        if self.ring_windows:
            playing_only = len(self.ring_windows) == len(self.once_play)
            self._set_pill(self.l_armed, "▶  Playing now" if playing_only else "🔔  Ringing now", "ring")
        elif self.current_ann:
            self._set_pill(self.l_armed, "▶  Playing a message", "ring")
        elif ev and ev[1].get("kind") == "event":
            self._set_pill(self.l_armed, f"●  Message set · plays in {fmt_delta(ev[0] - now)}", "good")
        elif ev:
            self._set_pill(self.l_armed, f"●  Alarm set · rings in {fmt_delta(ev[0] - now)}", "good")
        else:
            self._set_pill(self.l_armed, "○  No alarm set", "neutral")
        if self.power.keeping_awake:
            self._set_pill(self.l_awake, "●  Computer will stay awake", "good")
        elif (self.ring_windows or self.current_ann) and not s.get("keep_awake"):
            self._set_pill(self.l_awake, "△  Computer may fall asleep while playing (option is off)", "warn")
        elif ev and not s.get("keep_awake"):
            self._set_pill(self.l_awake, "△  Computer may fall asleep (option is off)", "warn")
        elif ev or self.ring_windows or self.current_ann:
            self._set_pill(self.l_awake, "△  Could not keep the computer awake", "bad")
        else:
            self._set_pill(self.l_awake, "○  Not keeping the computer awake", "neutral")
        state = self.power.wake_state
        if state == "registered" and self.power.wake_target:
            self._set_pill(self.l_wake, f"●  Will wake from sleep at {self.power.wake_target:%H:%M}", "good")
        elif state == "registering":
            self._set_pill(self.l_wake, "…  Setting up wake from sleep", "neutral")
        elif state == "declined":
            self._set_pill(self.l_wake, "△  Wake from sleep not set – password was declined", "warn")
        elif state == "failed":
            self._set_pill(self.l_wake, "△  Wake from sleep could not be set", "bad")
        elif state == "unsupported":
            self._set_pill(self.l_wake, "○  Wake from sleep is not available here", "neutral")
        elif ev and s.get("schedule_wake") and ev[0] - now < timedelta(seconds=WAKE_LEAD_SECONDS + 1):
            self._set_pill(self.l_wake, "○  Alarm is too soon to need a wake-up", "neutral")
        elif ev and not s.get("schedule_wake"):
            self._set_pill(self.l_wake, "○  Wake from sleep is off", "neutral")
        else:
            self._set_pill(self.l_wake, "○  No wake-up scheduled", "neutral")
        if state in ("declined", "failed") and ev:
            self.b_retry_wake.pack(side="left", padx=6)
        else:
            self.b_retry_wake.pack_forget()
        if self.current_ann and not self.ring_windows:        # one click to silence a message from any view
            if not self.b_stop_hdr.winfo_ismapped():
                self.b_stop_hdr.pack(side="left", padx=6)
        else:
            self.b_stop_hdr.pack_forget()

    def _tick_status(self) -> None:
        ev = self.scheduler.next_event()
        now = datetime.now()
        self.l_clock.config(text=f"{now:%H:%M}")
        self.l_date.config(text=f"{now:%A, %d %B %Y}   {now:%S}s")
        if ev:
            what = "Next message" if ev[1].get("kind") == "event" else "Next alarm"
            self.l_next.config(text=f"{what}:  {ev[1]['label']}  ·  {ev[0]:%A %d %b at %H:%M}  ·  in {fmt_delta(ev[0] - now)}")
        else:
            self.l_next.config(text="Nothing is set yet.  Create a schedule, or set an alarm – it takes three steps.")
        if self.v_page.get() == "today":
            self._refresh_today_countdown()
        self.l_status.config(text=f"Alarms and recordings are kept in {BASE_DIR}")
        self._tick_indicators()
        if (self.airplay and ev and self._is_airplay(ev[1].get("output", "")) and self._prewarmed != ev[1]["id"]
                and timedelta(0) <= ev[0] - now <= timedelta(minutes=3)):
            self._prewarmed = ev[1]["id"]
            threading.Thread(target=self.airplay.prewarm, daemon=True).start()
        # refresh "next ring" column once a minute
        if now.second == 0:
            sel = self.tree.selection()
            self._refresh_list(select=sel[0] if sel else None)
            if self.v_page.get() == "today":
                self._refresh_today()
        self.after(1000, self._tick_status)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, a, when = self.events.get_nowait()
                if kind == "ring":
                    self._ring(a, when)
                elif kind == "notice":
                    self.l_form_hint.config(text=when)
                elif kind == "announce":
                    self.announcements.push(a)
                    self._pump_announcements()
                elif kind == "occurrence":
                    self._apply_power(); self._tick_indicators()
                    if self.v_page.get() == "today":
                        self._refresh_today()
                elif kind == "finished":
                    if a in self.once_play:           # ignore if the user already pressed STOP
                        self._dismiss(a, finished=True)
                    elif self.current_ann and a == "ann:" + self.current_ann["key"]:
                        self._finish_announcement("played")
                elif kind == "airplay_failed" and a is not None and str(a.get("id", "")).startswith("ann:"):
                    self._announcement_airplay_failed(a, when)
                elif kind == "airplay_failed":
                    self.l_form_hint.config(text=f"AirPlay failed: {when}  –  playing on this computer instead.")
                    self.bell()
                    if a is None or a["id"] in self.ring_windows:
                        path = a["sound"] if a else self.v_sound.get()
                        vol = int(a["volume"] if a else self.v_volume.get())
                        once = a is not None and a["id"] in self.once_play
                        if once:
                            self.once_play[a["id"]].update(via="local", started=True)
                        try:
                            self.player.play(path, vol, loop=a is not None and not once, device="")
                            if a is not None:
                                self._take_local_stream(a["id"])
                        except Exception as e:
                            log(f"fallback playback failed: {e}")
                            if once:
                                self.after(0, lambda aid=a["id"]: self._dismiss(aid))
                elif kind == "missed":
                    self._refresh_list()
                    self._apply_power(); self._tick_indicators()
                    messagebox.showwarning(APP_NAME, f"Alarm “{a['label']}” was missed (it was due {when:%a %H:%M} "
                                                     "while the computer was off or asleep).")
        except queue.Empty:
            pass
        self.after(250, self._poll_events)

    # ----- ringing
    def _ring(self, a: dict, when: datetime) -> None:
        s = self.store.settings
        use_airplay = self._is_airplay(a.get("output", ""))
        if s.get("force_system_volume") and not use_airplay:
            threading.Thread(target=set_system_volume, args=(s.get("system_volume", 80),), daemon=True).start()
        fade = int(s.get("fade_seconds", 20) or 0)
        once = a.get("mode") == "play"           # whole file once, then stop by itself
        if once:
            self.once_play[a["id"]] = {"via": "airplay" if use_airplay else "local", "t0": time.monotonic(),
                                       "started": False, "label": None}
        problem = ""
        if not os.path.isfile(a["sound"]):
            problem = (f"The sound file for “{a['label']}” is missing:\n{a['sound']}\n\n"
                       "It may have been moved or deleted. Choose another file below and save the alarm.")
        elif use_airplay:
            if self.current_ann and self.current_ann.get("via") == "airplay":
                log(f"routine message '{self.current_ann['event']['label']}' interrupted by AirPlay alarm {a['id']}")
                self._finish_announcement("interrupted", "an alarm rang during this message", stop_audio=False)
            self._play_airplay(a, a["sound"], int(a["volume"]), a["output"][len(AirPlayPlayer.PREFIX):], loop=not once, fade=fade)
        else:
            try:
                warning = self.player.play(a["sound"], 0 if fade else a["volume"], loop=not once,
                                           device=a.get("output", ""))
                self._take_local_stream(a["id"])
                if once:
                    self.once_play[a["id"]]["started"] = True
                if warning:
                    self.l_form_hint.config(text=warning)
                if fade:
                    self._start_fade(a["volume"], fade)
            except Exception as e:
                log(f"playback failed for {a['sound']}: {e}")
                problem = (f"“{a['label']}” could not play {os.path.basename(a['sound'])}.\n\n"
                           "The file may be damaged or in a format this app cannot decode. "
                           "Try another file, or convert this one to MP3/WAV.\n\n"
                           f"Details: {e}")
        if problem:
            self.bell()
            self.after(100, lambda: messagebox.showerror(APP_NAME, problem))
            if once:                              # nothing to wait for: no "Now playing" window
                self.once_play.pop(a["id"], None)
                self.scheduler.ringing.discard(a["id"])
                self._refresh_list(); self._apply_power(); self._tick_indicators()
                return
        self._refresh_list()
        self._apply_power()

        if a["id"] in self.ring_windows:
            self.ring_windows[a["id"]].lift()
            return
        P, F = self.PALETTE, self.F
        win = tk.Toplevel(self, bg=P["header"])
        win.title(("▶ " if once else "⏰ ") + a["label"])
        if not once:                              # a two-hour talk must not pin a window over everything
            win.attributes("-topmost", True)
        w, h = 600, 470
        win.geometry(f"{w}x{h}+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
        win.protocol("WM_DELETE_WINDOW", lambda: self._dismiss(a["id"]))
        tk.Label(win, text="▶  NOW PLAYING" if once else "⏰  ALARM", font=F["small_b"], bg=P["header"], fg="#C7D0E4").pack(pady=(30, 0))
        tk.Label(win, text=f"{when:%H:%M}", font=F["ring_time"], bg=P["header"], fg="white").pack()
        tk.Label(win, text=a["label"], font=F["ring_name"], bg=P["header"], fg="white").pack()
        sub = tk.Label(win, text=f"{when:%A, %d %B}", font=F["base"], bg=P["header"], fg="#C7D0E4")
        sub.pack(pady=(2, 22))
        ttk.Button(win, text="■   STOP", style="Stop.TButton", command=lambda: self._dismiss(a["id"])).pack()
        if once:
            sub.config(text=os.path.basename(a["sound"]))
            self.once_play[a["id"]]["label"] = tk.Label(win, text="0:00:00 played", font=F["base"], bg=P["header"], fg="#C7D0E4")
            self.once_play[a["id"]]["label"].pack(pady=(14, 0))
            tk.Label(win, text="Stops by itself when the file ends  ·  Enter or Esc also stops it",
                     font=F["small"], bg=P["header"], fg="#8E9BB8").pack(pady=(14, 0))
        else:
            ttk.Button(win, text=f"Snooze {s.get('snooze_minutes', 5)} minutes", style="Ghost.TButton",
                       command=lambda: self._snooze(a)).pack(pady=(14, 0))
            tk.Label(win, text="Enter or Esc also stops it", font=F["small"], bg=P["header"], fg="#8E9BB8").pack(pady=(14, 0))
        win.bind("<Return>", lambda e: self._dismiss(a["id"]))
        win.bind("<Escape>", lambda e: self._dismiss(a["id"]))
        self.ring_windows[a["id"]] = win
        if once:
            self.after(1000, lambda: self._watch_once(a["id"]))
        else:
            self.ring_timeouts[a["id"]] = self.after(int(a.get("ring_minutes", 10)) * 60_000,
                                                     lambda: self._dismiss(a["id"], timed_out=True))
        self.b_stop.pack(fill="x", side="top", before=self.body)
        self.deiconify(); self.lift(); win.lift()
        if not once:
            win.focus_force()
        self._tick_indicators()

    def _take_local_stream(self, aid: str) -> None:
        """pygame has one music stream: note who owns it and close a whole-file play it just interrupted."""
        self._local_owner = aid
        if self.current_ann and self.current_ann.get("via") == "local":
            log(f"routine message '{self.current_ann['event']['label']}' interrupted by alarm {aid}")
            self._finish_announcement("interrupted", "an alarm rang during this message", stop_audio=False)
        for other, st in list(self.once_play.items()):
            if other != aid and st["via"] == "local":
                log(f"whole-file play {other} interrupted by {aid}")
                self.after(0, lambda o=other: self._dismiss(o))

    def _watch_once(self, aid: str) -> None:
        """Whole-file mode: show elapsed time and stop when the file has finished playing."""
        st = self.once_play.get(aid)
        win = self.ring_windows.get(aid)
        if not st or not win or not win.winfo_exists():
            return
        secs = int(time.monotonic() - st["t0"])
        if st["label"]:
            st["label"].config(text=f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d} played")
        if st["via"] == "airplay":
            pass                                  # the AirPlay worker posts "finished" when Music reaches the end
        elif st["started"] and not self.player.is_playing():
            self.events.put(("finished", aid, None))
            return
        self.after(1000, lambda: self._watch_once(aid))


    def _start_fade(self, target: int, seconds: int) -> None:
        if self._fade_job:
            self.after_cancel(self._fade_job)
        t0 = time.monotonic()

        def step():
            frac = min(1.0, (time.monotonic() - t0) / seconds)
            self.player.set_volume(round(target * frac))
            if frac < 1.0 and self.player.is_playing():
                self._fade_job = self.after(250, step)   # 4 steps per second
            else:
                self._fade_job = None
                if frac >= 1.0:
                    self.player.set_volume(target)

        self.player.set_volume(0)
        self._fade_job = self.after(250, step)

    def _snooze(self, a: dict) -> None:
        mins = int(self.store.settings.get("snooze_minutes", 5))
        when = self.scheduler.snooze(a, mins)
        log(f"snoozed '{a['label']}' until {when:%H:%M:%S}")
        self._dismiss(a["id"])
        self._apply_power()

    def _dismiss(self, alarm_id: str, timed_out: bool = False, finished: bool = False) -> None:
        win = self.ring_windows.pop(alarm_id, None)
        self.once_play.pop(alarm_id, None)
        if win:
            win.destroy()
        t = self.ring_timeouts.pop(alarm_id, None)
        if t:
            self.after_cancel(t)
        self.scheduler.ringing.discard(alarm_id)
        if alarm_id == self._local_owner:         # its sound must stop even if another alarm window is open
            self._local_owner = None
            self.player.stop()
            if self._fade_job:
                self.after_cancel(self._fade_job)
                self._fade_job = None
        if not self.ring_windows:
            self.player.stop()
            gone = self.store.get(alarm_id) or {}
            if not (self.current_ann and self.current_ann.get("via") == "airplay") or self._is_airplay(gone.get("output", "")):
                self._stop_airplay()
            self.b_stop.pack_forget()
            self.after(400, self._pump_announcements)
            if self._fade_job:
                self.after_cancel(self._fade_job)
                self._fade_job = None
        if timed_out:
            log(f"alarm {alarm_id} stopped after ring timeout")
        if finished:
            log(f"alarm {alarm_id} finished playing its file")
        self._refresh_list()
        self._apply_power()
        self._tick_indicators()


    # ================================================================== schedules: list
    def _sched_selected(self) -> dict | None:
        sel = self.stree.selection()
        return self.store.get_schedule(sel[0]) if sel else None

    def _refresh_schedules(self) -> None:
        today = date.today()
        keep = self.stree.selection()
        self.stree.delete(*self.stree.get_children())
        for sc in sorted(self.store.schedules, key=lambda x: (not x["enabled"], x["name"].lower())):
            skipped = self.store.is_skipped(today, sc["id"])
            self.stree.insert("", "end", iid=sc["id"], tags=("on" if sc["enabled"] else "off",), values=(
                "●" if sc["enabled"] else "○", sc["name"] or "(no name)",
                days_label(sc["days"]) + ("  · skipped today" if skipped else ""),
                output_label(sc.get("output", "")), len(sc["events"])))
        want = keep[0] if keep and self.stree.exists(keep[0]) else (self.sdraft["id"] if self.sdraft and self.stree.exists(self.sdraft["id"]) else None)
        if want and self.stree.selection() != (want,):
            self.stree.selection_set(want)          # (a changed selection fires _sched_on_select once)
        if self.store.schedules:
            self.l_sched_empty.pack_forget()
        elif not self.l_sched_empty.winfo_ismapped():
            self.l_sched_empty.pack(anchor="w", pady=(0, 10), before=self.b_sched_toggle.master)
        self._sched_update_buttons()

    def _sched_update_buttons(self) -> None:
        today = date.today()
        sc = self._sched_selected()
        has = sc is not None
        for b in (self.b_sched_toggle, self.b_sched_dup, self.b_sched_skip, self.mb_sched_more):
            b.config(state="normal" if has else "disabled")
        self.b_sched_skip.config(text="Undo skip" if has and self.store.is_skipped(today, sc["id"]) else "Skip today")
        if hasattr(self, "cb_today_filter"):
            self.cb_today_filter["values"] = ["All schedules"] + [x["name"] for x in self.store.schedules]

    def _sched_on_select(self, _e=None) -> None:
        sc = self._sched_selected()
        self._sched_update_buttons()
        if not sc or (self.sdraft and self.sdraft["id"] == sc["id"]):
            return
        if self.sdraft and not self._sched_confirm_discard():
            self.stree.selection_set(self.sdraft["id"]) if self.stree.exists(self.sdraft["id"]) else self.stree.selection_remove(*self.stree.selection())
            return
        self._sched_load(sc)

    def _sched_toggle(self) -> None:
        sc = self._sched_selected()
        if not sc:
            return
        sc = dict(sc, enabled=not sc["enabled"])
        self.store.upsert_schedule(sc)
        if not sc["enabled"]:
            self._cancel_announcements(sc["id"], None, "schedule was turned off")
        else:
            self._mark_past_events(sc)
        if self.sdraft and self.sdraft["id"] == sc["id"]:
            self.sdraft["enabled"] = sc["enabled"]; self.v_senabled.set(sc["enabled"])
            self.sdraft_saved = json.dumps(sc, sort_keys=True)
        log(f"schedule '{sc['name']}' turned {'on' if sc['enabled'] else 'off'}")
        self._after_schedule_change()

    def _sched_duplicate(self) -> None:
        sc = self._sched_selected()
        if not sc or (self.sdraft and not self._sched_confirm_discard()):
            return
        d = duplicate_schedule(sc)
        self.store.upsert_schedule(d)
        self._refresh_schedules()
        self.stree.selection_set(d["id"])
        self._sched_load(d)
        self.l_sched_hint.config(text="This copy is switched off until you save it with “Schedule is on”.")

    def _sched_skip_today(self) -> None:
        sc = self._sched_selected()
        if not sc:
            return
        today = date.today()
        on = not self.store.is_skipped(today, sc["id"])
        self.store.set_skip(today, sc["id"], None, on)
        if on:
            self._cancel_announcements(sc["id"], None, "skipped for today")
        log(f"{'skip' if on else 'undo skip'} today: schedule '{sc['name']}'")
        self.l_sched_hint.config(text=(f"“{sc['name']}” is skipped for today only – it runs again tomorrow." if on
                                       else f"“{sc['name']}” is back on for today."))
        self._after_schedule_change()

    def _sched_delete(self) -> None:
        sc = self._sched_selected()
        if not sc:
            return
        n = len(sc["events"])
        if not messagebox.askyesno(APP_NAME, f"Delete “{sc['name']}” and its {n} event{'s' if n != 1 else ''}?\n\n"
                                             "Recordings and audio files are kept."):
            return
        self._cancel_announcements(sc["id"], None, "schedule was deleted")
        self.store.delete_schedule(sc["id"])
        log(f"schedule '{sc['name']}' deleted")
        if self.sdraft and self.sdraft["id"] == sc["id"]:
            self.sdraft = None
            self._sched_load(new_schedule(self.store.settings))
        self._after_schedule_change()

    def _after_schedule_change(self) -> None:
        self._refresh_schedules(); self._refresh_today(); self._apply_power(); self._tick_indicators()

    def _mark_past_events(self, sc: dict) -> None:
        """Events whose time today has already passed when a schedule is saved/turned on must not play late."""
        now = datetime.now()
        for ev in sc["events"]:
            due = self.store.event_due(sc, ev, now.date())
            if due and due <= now:
                self.store.record(occurrence_key(sc["id"], ev["id"], now.date()), "missed", "its time had already passed when saved")

    # ================================================================== schedules: editor
    def _sched_load(self, sc: dict) -> None:
        if self._ev_recording():
            self._ev_record_cancel()
        self._ev_take_discard(silent=True)
        stored = self.store.get_schedule(sc["id"])
        self.sdraft = json.loads(json.dumps(sc))
        self.sdraft_saved = json.dumps(stored, sort_keys=True) if stored else ""
        self.v_sname.set(sc["name"])
        for i in range(7):
            self.v_days[i].set(i in sc["days"])
        self._set_output(sc.get("output", ""), self.v_sched_output)
        self.v_svol.set(int(sc["volume"])); self.l_svol.config(text=f"{int(sc['volume'])} %")
        self.v_senabled.set(bool(sc["enabled"]))
        self.l_sched_title.config(text=f"Editing “{sc['name']}”" if stored else "New schedule")
        for lab in (self.l_err_name, self.l_err_days, self.l_sched_err, self.l_sched_hint, self.l_sched_saved):
            lab.config(text="")
        self.ev_draft = None
        self._event_show_editor(False)
        self._refresh_events()

    def _sched_new(self) -> None:
        if self.sdraft and not self._sched_confirm_discard():
            return
        self.stree.selection_remove(*self.stree.selection())
        self._sched_load(new_schedule(self.store.settings))
        self._refresh_schedules()
        self.l_sched_hint.config(text="Name it, pick the days and speaker, then add events. It stays off until saved as on.")
        self.e_sname.focus_set()

    def _sched_read_form(self) -> None:
        if not self.sdraft:
            return
        self.sdraft.update(name=self.v_sname.get().strip(), days=[i for i in range(7) if self.v_days[i].get()],
                           output=self._get_output(self.v_sched_output), volume=int(self.v_svol.get()),
                           enabled=bool(self.v_senabled.get()))

    def _sched_dirty(self) -> bool:
        if not self.sdraft:
            return False
        self._sched_read_form()
        if self.ev_draft is not None:
            return True
        if not self.sdraft_saved:
            return bool(self.sdraft["name"] or self.sdraft["events"])
        return json.dumps(self.sdraft, sort_keys=True) != self.sdraft_saved

    def _sched_confirm_discard(self) -> bool:
        """True when it is fine to leave the editor (nothing unsaved, or the user chose to drop the changes)."""
        if not self._sched_dirty():
            return True
        name = self.sdraft.get("name") or "this schedule"
        if messagebox.askyesno(APP_NAME, f"You have unsaved changes to “{name}”.\n\nDiscard them?"):
            if self._ev_recording():
                self._ev_record_cancel()
            self._ev_take_discard(silent=True)
            self.ev_draft = None
            self.sdraft = None
            return True
        return False

    def _sched_set_days(self, days) -> None:
        for i in range(7):
            self.v_days[i].set(i in days)

    def _sched_test_speaker(self) -> None:
        self._preview_sound(self._tone_path(), int(self.v_svol.get()), self._get_output(self.v_sched_output), self.l_sched_hint,
                            what="test sound")

    def _tone_path(self) -> str:
        """A short two-note chime for 'Test speaker', generated once into the recordings folder's cache."""
        path = os.path.join(REC_DIR, ".test-tone.wav")
        if not os.path.isfile(path):
            import math, struct
            rate, frames = 22050, bytearray()
            for i in range(int(rate * 1.2)):
                t = i / rate
                f = 660 if t < 0.6 else 880
                v = math.exp(-3 * (t % 0.6)) * math.sin(2 * math.pi * f * t)
                frames += struct.pack("<h", int(v * 14000))
            os.makedirs(REC_DIR, exist_ok=True)
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))
        return path

    def _sched_save(self) -> None:
        if not self.sdraft:
            return
        if self.ev_draft is not None:                 # finish the open event first; stop if it is not valid
            self._event_done()
            if self.ev_draft is not None:
                return
        self._sched_read_form()
        errs = validate_schedule(self.sdraft)
        self.l_err_name.config(text=errs.get("name", ""))
        self.l_err_days.config(text=errs.get("days", ""))
        bad_events = [k for k in errs if k.startswith("event:")]
        self.l_sched_err.config(text="One of the events needs attention – see the list." if bad_events else "")
        if bad_events:
            eid = bad_events[0].split(":", 1)[1]
            if self.etree.exists(eid):
                self.etree.selection_set(eid)
        if errs:
            return
        sc = self.sdraft
        for ev in sc["events"]:
            ev["sound"] = portable_sound(ev.get("sound", ""))
            ev["label"] = ev["label"].strip()
        sc["events"].sort(key=lambda e: e["time"])
        stored = self.store.get_schedule(sc["id"]) or {"enabled": False, "events": []}
        was_on = bool(stored.get("enabled"))
        now = datetime.now()
        old_times = {e["id"]: e["time"] for e in stored["events"]}
        with self.store.lock:
            for ev in sc["events"]:
                key = occurrence_key(sc["id"], ev["id"], now.date())
                try:
                    later = datetime.combine(now.date(), alarm_time(ev)) > now
                except ValueError:
                    later = False
                if ev["id"] in old_times and old_times[ev["id"]] != ev["time"] and later and key in self.store.occurrences:
                    self.store.occurrences.pop(key)           # moved to a later time today: let it play then
        self.store.upsert_schedule(json.loads(json.dumps(sc)))
        self.store.settings.update({"last_output": sc["output"], "last_volume": sc["volume"]})
        self.store.save()
        if sc["enabled"]:
            self._mark_past_events(sc)
        self._cancel_announcements(sc["id"], None, "schedule was changed", keep_playing=True)
        if not sc["enabled"] and was_on:
            self._cancel_announcements(sc["id"], None, "schedule was turned off")
        self.sdraft_saved = json.dumps(self.store.get_schedule(sc["id"]), sort_keys=True)
        self.l_sched_title.config(text=f"Editing “{sc['name']}”")
        nxt = self.store.next_announcement()
        on_text = ("Saved – it is off, nothing plays until you turn it on." if not sc["enabled"] else
                   "Saved – the schedule is on.")
        self.l_sched_hint.config(text=on_text)
        self.l_sched_saved.config(text="✓ Saved")
        if self._saved_job:
            self.after_cancel(self._saved_job)
        self._saved_job = self.after(4000, lambda: self.l_sched_saved.config(text=""))
        log(f"schedule '{sc['name']}' saved ({len(sc['events'])} events, {'on' if sc['enabled'] else 'off'})")
        self._after_schedule_change()
        self.stree.selection_set(sc["id"])

    def _sched_cancel(self) -> None:
        if not self.sdraft:
            return
        stored = self.store.get_schedule(self.sdraft["id"])
        if self._sched_dirty() and not messagebox.askyesno(APP_NAME, "Throw away the changes you made here?"):
            return
        if self._ev_recording():
            self._ev_record_cancel()
        self._ev_take_discard(silent=True)
        self.ev_draft = None
        self._sched_load(stored or new_schedule(self.store.settings))
        if not stored:
            self.stree.selection_remove(*self.stree.selection())
        self._refresh_schedules()

    # ================================================================== events (inside the schedule editor)
    def _refresh_events(self) -> None:
        keep = self.etree.selection()
        self.etree.delete(*self.etree.get_children())
        for ev in sorted(self.sdraft["events"], key=lambda e: e["time"]) if self.sdraft else []:
            path = resolve_sound(ev.get("sound", ""))
            snd = ("No sound" if not path else "Missing file" if not os.path.isfile(path)
                   else "🎤 Recording" if path.startswith(REC_DIR) else os.path.basename(path))
            self.etree.insert("", "end", iid=ev["id"], tags=("on" if ev["enabled"] else "off",),
                              values=(ev["time"], ev["label"] or "(no name)", snd, "on" if ev["enabled"] else "off"))
        if keep and self.etree.exists(keep[0]):
            self.etree.selection_set(keep[0])
        if self.sdraft and self.sdraft["events"]:
            self.l_ev_empty.pack_forget()
        elif not self.l_ev_empty.winfo_ismapped():
            self.l_ev_empty.pack(anchor="w", pady=(0, 8), before=self.etree)

    def _event_show_editor(self, on: bool) -> None:
        if on:
            self.ev_list.pack_forget()
            self.ev_box.pack(fill="both", expand=True)
        else:
            self.ev_box.pack_forget()
            self.ev_list.pack(fill="both", expand=True)

    def _event_add(self) -> None:
        if not self.sdraft:
            self._sched_new()
        if self.ev_draft is not None and not self._event_done():
            return
        last = max((e["time"] for e in self.sdraft["events"]), default="")
        if last:
            h, m = (int(x) for x in last.split(":"))
            nxt = (datetime(2000, 1, 1, h, m) + timedelta(minutes=20)).strftime("%H:%M")
        else:
            nxt = "07:00"
        self.ev_draft = new_event(nxt)
        self._event_load_form("New event")
        self.e_elabel.focus_set()

    def _event_edit(self) -> None:
        sel = self.etree.selection()
        if not sel or not self.sdraft:
            return
        if self.ev_draft is not None and not self._event_done():
            return
        ev = next((e for e in self.sdraft["events"] if e["id"] == sel[0]), None)
        if ev:
            self.ev_draft = dict(ev)
            self._event_load_form(f"Editing “{ev['label']}”")

    def _event_load_form(self, title: str) -> None:
        ev = self.ev_draft
        h, m = ev["time"].split(":")
        self.v_eh.set(h); self.v_em.set(m)
        self.v_elabel.set(ev["label"])
        self.v_eon.set(bool(ev["enabled"]))
        self.l_ev_title.config(text=title)
        self.l_err_event.config(text=""); self.l_ev_hint.config(text="")
        self._event_sound_ui()
        self._event_show_editor(True)

    def _ev_recording(self) -> bool:
        return self.recorder.recording and self._rec_owner == "event"

    def _event_sound_ui(self) -> None:
        """Show the right one of: chosen sound / recording in progress / a take waiting for a decision."""
        for f in (self.ev_sound_row, self.ev_pick_row, self.ev_rec_row, self.ev_take_row):
            f.pack_forget()
        if self._ev_recording() and self.ev_draft is not None:
            self.ev_rec_row.pack(fill="x")
        elif self.ev_take:
            self.ev_take_row.pack(fill="x")
        else:
            self.ev_sound_row.pack(fill="x")
            self.ev_pick_row.pack(fill="x", pady=(8, 0))
            path = resolve_sound(self.ev_draft.get("sound", "")) if self.ev_draft else ""
            self.b_ev_preview.pack_forget(); self.b_ev_remove.pack_forget()
            if not path:
                self.l_ev_sound.config(text="No sound – the event is shown in Today but nothing plays", style="Card.TLabel")
            elif not os.path.isfile(path):
                self.l_ev_sound.config(text=f"{os.path.basename(path)} was not found – it may have been moved or deleted. Choose or record it again.", style="Bad.TLabel")
                self.b_ev_remove.pack(side="left", padx=(12, 0))
            else:
                kind = "Your recording" if path.startswith(REC_DIR) else "Audio file"
                self.l_ev_sound.config(text=f"{kind}:  {os.path.basename(path)}", style="Card.TLabel")
                self.b_ev_preview.pack(side="left", padx=(12, 0))
                self.b_ev_remove.pack(side="left", padx=8)
        self.l_ev_hint.pack_forget(); self.l_ev_hint.pack(anchor="w", pady=(6, 0))

    def _event_read_form(self) -> None:
        self.ev_draft.update(time=f"{int(self.v_eh.get() or 0):02d}:{int(self.v_em.get() or 0):02d}" if (self.v_eh.get() or "").strip().isdigit() and (self.v_em.get() or "").strip().isdigit() else f"{self.v_eh.get()}:{self.v_em.get()}",
                             label=self.v_elabel.get().strip(), enabled=bool(self.v_eon.get()))

    def _event_done(self) -> bool:
        if self.ev_draft is None:
            return True
        if self.recorder.recording:
            self.l_ev_hint.config(text="Stop the recording first (or cancel it).")
            return False
        if self.ev_take:
            self.l_ev_hint.config(text="Decide about the recording first: Use recording, Record again or Discard.")
            return False
        self._event_read_form()
        errs = validate_schedule({"name": "x", "days": [0], "events": [self.ev_draft]})
        msg = errs.get(f"event:{self.ev_draft['id']}", "")
        self.l_err_event.config(text=msg)
        if msg:
            return False
        evs = self.sdraft["events"]
        for i, e in enumerate(evs):
            if e["id"] == self.ev_draft["id"]:
                evs[i] = self.ev_draft
                break
        else:
            evs.append(self.ev_draft)
        evs.sort(key=lambda e: e["time"])
        eid = self.ev_draft["id"]
        self.ev_draft = None
        self._event_show_editor(False)
        self._refresh_events()
        self.etree.selection_set(eid)
        return True

    def _event_cancel(self) -> None:
        if self._ev_recording():
            self._ev_record_cancel()
        self._ev_take_discard(silent=True)
        self.ev_draft = None
        self._event_show_editor(False)
        self._refresh_events()

    def _event_remove(self) -> None:
        sel = self.etree.selection()
        if sel and self.sdraft:
            self.sdraft["events"] = [e for e in self.sdraft["events"] if e["id"] != sel[0]]
            self._refresh_events()
            self.l_sched_hint.config(text="Event removed from the list – press Save schedule to make it final, Cancel to get it back.")

    def _event_choose_file(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose the message to play",
            initialdir=REC_DIR if os.path.isdir(REC_DIR) else os.path.expanduser("~"),
            filetypes=[("Audio files", " ".join("*" + e for e in SUPPORTED_AUDIO)), ("All files", "*.*")])
        if p and self.ev_draft is not None:
            self.ev_draft["sound"] = portable_sound(p)
            self._event_sound_ui()

    def _event_preview(self) -> None:
        if self.ev_draft is not None:
            self._preview_sound(resolve_sound(self.ev_draft.get("sound", "")), int(self.v_svol.get()),
                                self._get_output(self.v_sched_output), self.l_ev_hint)

    def _event_remove_sound(self) -> None:
        if self.ev_draft is not None:
            self.ev_draft["sound"] = ""          # the file itself is never deleted here
            self._event_sound_ui()

    # ----- recording a message for an event
    def _ev_record_start(self) -> None:
        if self.recorder.recording:
            self.l_ev_hint.config(text="A recording is already running in the Alarms view. Stop it there first.")
            return
        if self.ev_take:
            self._ev_take_discard(silent=True)
        dev = next((d[0] for d in self.devices if d[1] == self.v_mic.get()), None)
        try:
            self.recorder.start(dev, REC_DIR)
        except Exception as e:
            log(f"recording start failed: {e}")
            messagebox.showerror(APP_NAME, "Could not start recording.\n\n"
                                 f"{e}\n\nOn macOS make sure this app (or Terminal) is allowed to use the "
                                 "microphone in System Settings → Privacy & Security → Microphone, then try again.")
            return
        self._rec_owner = "event"
        self.l_ev_hint.config(text="Speak your message, then press Stop recording.")
        self._event_sound_ui()
        self._ev_meter()

    def _ev_meter(self) -> None:
        if not self._ev_recording() or self.ev_draft is None:
            return
        self.ev_meter["value"] = min(100, self.recorder.level * 140)
        secs = int(self.recorder.elapsed)
        if secs > 2 and self.recorder.peak < 0.02:
            self.l_ev_rec.config(text=f"●  Recording…  {secs} s   very quiet – is this the right microphone?")
        else:
            self.l_ev_rec.config(text=f"●  Recording…  {secs} s   (level {self.recorder.peak*100:.0f}%)")
        self.after(80, self._ev_meter)

    def _ev_record_stop(self) -> None:
        if not self._ev_recording():
            return
        secs = int(self.recorder.elapsed)
        try:
            path = self.recorder.stop()
        except Exception as e:
            log(f"recording save failed: {e}")
            messagebox.showerror(APP_NAME, "Could not save the recording.\n\nThe microphone may have disconnected, or the "
                                 "recordings folder is not writable. Check the microphone and try again.\n\n"
                                 f"Details: {e}")
            self._event_sound_ui()
            return
        self.ev_take = path
        peak, gain = self.recorder.last_peak, self.recorder.last_gain_db
        if peak < QUIET_PEAK:
            self.l_ev_take.config(text=f"Recorded {secs} s – but almost nothing was heard. Check the microphone (“{self.v_mic.get()[:30]}”), then Record again.",
                                  style="Bad.TLabel")
        elif gain >= 6:
            self.l_ev_take.config(text=f"Recorded {secs} s – it was quiet, so it was boosted {gain:+.0f} dB.", style="Warn.TLabel")
        else:
            self.l_ev_take.config(text=f"Recorded {secs} s.", style="Card.TLabel")
        self.l_ev_hint.config(text="Listen with Preview, then press Use recording.")
        self._event_sound_ui()

    def _ev_record_cancel(self) -> None:
        if self._ev_recording():
            try:
                path = self.recorder.stop()
                self._delete_take(path)
            except Exception as e:
                log(f"recording cancel: {e}")
        self.l_ev_hint.config(text="Recording cancelled – the previous sound is unchanged.")
        if self.ev_draft is not None:
            self._event_sound_ui()

    def _ev_record_again(self) -> None:
        self._ev_take_discard(silent=True)
        self._ev_record_start()

    def _ev_take_use(self) -> None:
        if self.ev_take and self.ev_draft is not None:
            self.ev_draft["sound"] = portable_sound(self.ev_take)
            self.ev_take = ""
            self.l_ev_hint.config(text="Recording attached. Press Done, then Save schedule.")
            self._event_sound_ui()

    def _ev_take_discard(self, silent: bool = False) -> None:
        if self.ev_take:
            self._delete_take(self.ev_take)
            self.ev_take = ""
            if not silent:
                self.l_ev_hint.config(text="Recording discarded – the previous sound is unchanged.")
        if self.ev_draft is not None:
            self._event_sound_ui()

    def _delete_take(self, path: str) -> None:
        """Remove a recording we made but nobody uses.  Never touches files outside recordings/ or files in use."""
        try:
            drafts = list((self.sdraft or {}).get("events", [])) + ([self.ev_draft] if self.ev_draft else [])
            if path and path.startswith(REC_DIR) and os.path.isfile(path) and self.store.sound_users(path) == 0 and \
                    not any(resolve_sound(e.get("sound", "")) == path for e in drafts):
                os.remove(path)
                log(f"unused recording removed: {os.path.basename(path)}")
        except OSError as e:
            log(f"could not remove recording {path}: {e}")

    # ================================================================== preview (never touches the schedule)
    def _preview_sound(self, path: str, volume: int, output: str, hint, what: str = "sound") -> None:
        if not path or not os.path.isfile(path):
            hint.config(text="There is no sound file to play – record a message or choose a file first.")
            return
        if self.ring_windows or self.current_ann or self.recorder.recording:
            hint.config(text="Something is playing or recording right now. Stop it first, then preview.")
            return
        if self._is_airplay(output):
            self._play_airplay(None, path, volume, output[len(AirPlayPlayer.PREFIX):], loop=False, fade=0, hint=hint)
            return
        try:
            warning = self.player.play(path, volume, loop=False, device=output)
            hint.config(text=warning or f"Playing the {what} on {output or 'the system default output'} at {volume} %.")
        except Exception as e:
            log(f"preview failed for {path}: {e}")
            messagebox.showerror(APP_NAME, f"Could not play {os.path.basename(path)}.\n\n"
                                 "The file may be damaged or in a format this app cannot decode. "
                                 "Try another file, or convert this one to MP3/WAV.\n\n"
                                 f"Details: {e}")

    # ================================================================== routine messages (announcements)
    def _pump_announcements(self) -> None:
        """Play the next queued message if the single audio stream is free.  Alarms always win."""
        if self.current_ann or not len(self.announcements):
            return
        if self.ring_windows or self._local_owner:
            self.after(1000, self._pump_announcements)      # an alarm is ringing/playing: wait
            return
        occ = self.announcements.pop_playable(datetime.now(), self.store)
        if not occ:
            self._refresh_today()
            return
        sc, ev, path = occ["schedule"], occ["event"], occ["path"]
        out = sc.get("output", "")
        occ["via"] = "airplay" if self._is_airplay(out) else "local"
        occ["note"] = ""
        self.current_ann = occ
        self.store.record(occ["key"], "playing")
        log(f"routine message playing: '{ev['label']}' ({sc['name']}) on {out or 'system default'}")
        if occ["via"] == "airplay":
            pseudo = {"id": "ann:" + occ["key"], "label": ev["label"], "sound": path, "volume": sc["volume"], "output": out}
            self._play_airplay(pseudo, path, int(sc["volume"]), out[len(AirPlayPlayer.PREFIX):], loop=False, fade=0, hint=self.l_today_hint)
        else:
            try:
                warning = self.player.play(path, int(sc["volume"]), loop=False, device=out)
                if warning:
                    occ["note"] = "played on the system default output – the chosen speaker was not connected"
                    log(f"routine message '{ev['label']}': {warning}")
                self._ann_job = self.after(500, self._watch_announcement)
            except Exception as e:
                log(f"routine message '{ev['label']}' failed: {e}")
                self.current_ann = None
                self.store.record(occ["key"], "failed", "the file could not be played")
                self.after(200, self._pump_announcements)
        self._apply_power(); self._tick_indicators(); self._refresh_today()

    def _watch_announcement(self) -> None:
        occ = self.current_ann
        if not occ or occ.get("via") != "local":
            return
        if self.player.is_playing():
            self._ann_job = self.after(500, self._watch_announcement)
        else:
            self._finish_announcement("played")

    def _finish_announcement(self, status: str, note: str = "", stop_audio: bool = True) -> None:
        occ = self.current_ann
        if not occ:
            return
        self.current_ann = None
        if self._ann_job:
            self.after_cancel(self._ann_job)
            self._ann_job = None
        if stop_audio:
            if occ.get("via") == "airplay":
                self._stop_airplay()
            elif self._local_owner is None:
                self.player.stop()
        self.store.record(occ["key"], status, note or occ.get("note", ""))
        log(f"routine message '{occ['event']['label']}' {status}" + (f" ({note})" if note else ""))
        self._apply_power(); self._tick_indicators(); self._refresh_today()
        self.after(300, self._pump_announcements)

    def _stop_announcement(self) -> None:
        if self.current_ann:
            self._finish_announcement("stopped", "stopped by you")

    def _announcement_airplay_failed(self, pseudo: dict, why: str) -> None:
        occ = self.current_ann
        if not occ or pseudo["id"] != "ann:" + occ["key"]:
            return
        if self._local_owner or self.ring_windows:        # an alarm is using the speakers: the alarm wins
            self._finish_announcement("interrupted", "an alarm rang during this message", stop_audio=False)
            return
        log(f"routine message '{occ['event']['label']}': AirPlay failed ({why}); playing on this computer")
        try:
            self.player.play(occ["path"], int(occ["schedule"]["volume"]), loop=False, device="")
            occ["via"] = "local"
            occ["note"] = "AirPlay speaker unavailable – played on this computer"
            self._ann_job = self.after(500, self._watch_announcement)
            self._refresh_today()
        except Exception as e:
            log(f"routine message fallback failed: {e}")
            self._finish_announcement("failed", "the speaker was unavailable and the file could not be played here", stop_audio=False)

    def _cancel_announcements(self, sid: str | None, eid: str | None, why: str, keep_playing: bool = False) -> None:
        """Drop queued messages of a schedule/event and, unless keep_playing, stop one that is playing."""
        for occ in self.announcements.cancel(sid, eid):
            self.store.record(occ["key"], "skipped", why)
            log(f"routine message '{occ['event']['label']}' cancelled: {why}")
        cur = self.current_ann
        if cur and not keep_playing and cur["schedule"]["id"] == sid and (eid is None or cur["event"]["id"] == eid):
            self._finish_announcement("stopped", why)

    def _on_close(self) -> None:
        if self.sdraft and not self._sched_confirm_discard():
            return
        if self.ring_windows or self.current_ann:
            msg = ("A message is playing right now. Quitting will stop it." if self.current_ann and not self.ring_windows
                   else "A file is still playing. Quitting will stop it." if self.once_play and len(self.ring_windows) == len(self.once_play)
                   else "An alarm is ringing right now. Quitting will silence it.")
            if not messagebox.askyesno(APP_NAME, msg + "\n\nQuit anyway?"):
                return
        elif self.scheduler.next_event():
            if not messagebox.askyesno(APP_NAME, "An alarm or schedule is still set. Alarms and messages only play while "
                                                 "this window is open.\n\nQuit anyway?"):
                return
        if self.current_ann:
            self._finish_announcement("stopped", "the app was closed")
        for occ in self.announcements.cancel():
            self.store.record(occ["key"], "missed", "the app was closed before it played")
        self.scheduler.stop()
        self.player.stop()
        if self.airplay and self.airplay.playing:
            self.airplay.stop()
        if self.recorder.recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.power.shutdown()
        self.destroy()


def main() -> None:
    os.makedirs(REC_DIR, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
