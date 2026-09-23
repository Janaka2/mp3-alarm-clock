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

APP_NAME = "Alarm Clock"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

GRACE = timedelta(minutes=30)          # fire an alarm that is overdue by at most this much (e.g. after sleep)
WAKE_LEAD_SECONDS = 60                 # wake the machine this many seconds before the alarm
SUPPORTED_AUDIO = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aiff", ".aif")


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
    """Thread-safe list of alarms + settings persisted to alarms.json."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.alarms: list[dict] = []
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.alarms = data.get("alarms", [])
            self.settings.update(data.get("settings", {}))
        except (OSError, ValueError) as e:
            log(f"Could not read {self.path}: {e}")

    def save(self) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"alarms": self.alarms, "settings": self.settings}, f, indent=2)
            os.replace(tmp, self.path)

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

    def next_event(self, now: datetime | None = None) -> tuple[datetime, dict] | None:
        now = now or datetime.now()
        with self.lock:
            events = [(nf, a) for a in self.alarms if (nf := next_fire(a, now))]
        return min(events, key=lambda e: e[0]) if events else None


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
            pygame.mixer.init()
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

    def set_volume(self, volume: int) -> None:
        if self.ok:
            import pygame
            pygame.mixer.music.set_volume(max(0, min(100, volume)) / 100.0)

    def stop(self) -> None:
        if self.ok:
            import pygame
            pygame.mixer.music.stop()
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass

    def is_playing(self) -> bool:
        if not self.ok:
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
    def play(self, path: str, volume: int, device: str, loop: bool = True, fade_seconds: int = 0) -> None:
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
        if fade_seconds:
            t0 = time.monotonic()
            while not self._stop_flag.is_set():
                frac = min(1.0, (time.monotonic() - t0) / fade_seconds)
                self.set_volume(round(volume * frac))
                if frac >= 1.0:
                    break
                self._stop_flag.wait(1.0)
        if not loop:   # test playback: wait for the track to end, then tidy up
            while not self._stop_flag.is_set() and self.is_playing():
                self._stop_flag.wait(1.0)
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
            return
        if ok:
            self.wake_state, self.wake_detail = "registered", ""
            log(f"system wake registered for {new}")
        else:
            self.wake_state = "declined" if declined else "failed"
            self.wake_detail = detail
            self._declined_target = new if declined else None
            self._wake_target = None
            log(f"system wake NOT registered ({self.wake_state}): {detail}")

    def _mac_wake(self, old, new) -> None:
        fmt = "%m/%d/%y %H:%M:%S"
        parts = []
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
        due = [s for s in self.snoozes if s[0] <= now]
        for s in due:
            self.snoozes.remove(s)
            self.ringing.add(s[1]["id"])
            log(f"snoozed alarm due: '{s[1]['label']}'")
            self.events.put(("ring", s[1], s[0]))

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
        self.events: queue.Queue = queue.Queue()
        self.scheduler = Scheduler(self.store, self.events)
        self.editing_id: str | None = None
        self.ring_windows: dict[str, tk.Toplevel] = {}
        self.ring_timeouts: dict[str, str] = {}
        self._fade_job: str | None = None

        self._build_ui()
        self._load_form(new_alarm(self.store.settings))
        self._refresh_list()
        self.scheduler.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._poll_events)
        self.after(1000, self._tick_status)
        self._apply_power()
        self._tick_indicators()
        if not self.player.ok:
            messagebox.showerror(APP_NAME, "Sound output is not available, so alarms will be silent.\n\n"
                                 "Usually this means no speakers/headphones are connected, or the audio "
                                 "library did not install correctly. Plug in an output device, or delete the "
                                 ".venv folder next to the app and start it again to reinstall.\n\n"
                                 f"Details: {self.player.error}")
        log(f"started; data folder: {BASE_DIR}")

    # ----- UI construction
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if IS_WIN:
            style.theme_use("vista")
        pad = {"padx": 6, "pady": 4}

        # Alarm list
        top = ttk.LabelFrame(self, text="Alarms")
        top.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        cols = ("on", "when", "repeat", "label", "sound", "output", "vol")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=6, selectmode="browse")
        heads = {"on": ("On", 40), "when": ("Next ring", 150), "repeat": ("Repeat", 80),
                 "label": ("Label", 130), "sound": ("Sound", 200), "output": ("Output", 150), "vol": ("Vol", 40)}
        for c in cols:
            self.tree.heading(c, text=heads[c][0])
            self.tree.column(c, width=heads[c][1], anchor="w", stretch=(c in ("label", "sound", "output")))
        self.tree.pack(fill="both", expand=True, side="top", padx=6, pady=6)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._toggle_selected())

        bar = ttk.Frame(top)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bar, text="New alarm", command=self._new).pack(side="left")
        ttk.Button(bar, text="Enable / disable", command=self._toggle_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="Delete", command=self._delete_selected).pack(side="left")
        ttk.Button(bar, text="Ring selected now (test)", command=self._ring_selected).pack(side="right")

        # Editor
        ed = ttk.LabelFrame(self, text="Alarm details")
        ed.pack(fill="x", padx=10, pady=4)
        for i in range(8):
            ed.columnconfigure(i, weight=0)
        ed.columnconfigure(7, weight=1)

        ttk.Label(ed, text="Label").grid(row=0, column=0, sticky="w", **pad)
        self.v_label = tk.StringVar()
        ttk.Entry(ed, textvariable=self.v_label, width=28).grid(row=0, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(ed, text="Repeat").grid(row=0, column=4, sticky="e", **pad)
        self.v_repeat = tk.StringVar(value="once")
        rep = ttk.Frame(ed)
        rep.grid(row=0, column=5, columnspan=3, sticky="w", **pad)
        for txt, val in (("Once", "once"), ("Every day", "daily"), ("Weekdays", "weekdays")):
            ttk.Radiobutton(rep, text=txt, value=val, variable=self.v_repeat).pack(side="left", padx=(0, 8))

        ttk.Label(ed, text="Date").grid(row=1, column=0, sticky="w", **pad)
        df = ttk.Frame(ed)
        df.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        self.v_year = tk.StringVar(); self.v_month = tk.StringVar(); self.v_day = tk.StringVar()
        ttk.Spinbox(df, from_=2024, to=2100, width=6, textvariable=self.v_year, format="%04.0f").pack(side="left")
        ttk.Label(df, text="-").pack(side="left")
        ttk.Spinbox(df, from_=1, to=12, width=4, textvariable=self.v_month, format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(df, text="-").pack(side="left")
        ttk.Spinbox(df, from_=1, to=31, width=4, textvariable=self.v_day, format="%02.0f", wrap=True).pack(side="left")
        ttk.Button(df, text="Today", width=7, command=lambda: self._set_date(date.today())).pack(side="left", padx=(8, 2))
        ttk.Button(df, text="Tomorrow", width=9,
                   command=lambda: self._set_date(date.today() + timedelta(days=1))).pack(side="left")

        ttk.Label(ed, text="Time").grid(row=1, column=4, sticky="e", **pad)
        tf = ttk.Frame(ed)
        tf.grid(row=1, column=5, columnspan=3, sticky="w", **pad)
        self.v_hour = tk.StringVar(); self.v_min = tk.StringVar()
        ttk.Spinbox(tf, from_=0, to=23, width=4, textvariable=self.v_hour, format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tf, text=":").pack(side="left")
        ttk.Spinbox(tf, from_=0, to=59, width=4, textvariable=self.v_min, format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tf, text="(24 h)").pack(side="left", padx=(6, 8))
        ttk.Button(tf, text="+1 min", width=7, command=lambda: self._set_datetime(datetime.now() + timedelta(minutes=1))).pack(side="left")
        ttk.Button(tf, text="+10 min", width=8, command=lambda: self._set_datetime(datetime.now() + timedelta(minutes=10))).pack(side="left", padx=2)

        ttk.Label(ed, text="Sound file").grid(row=2, column=0, sticky="w", **pad)
        self.v_sound = tk.StringVar()
        ttk.Entry(ed, textvariable=self.v_sound).grid(row=2, column=1, columnspan=5, sticky="we", **pad)
        sf = ttk.Frame(ed)
        sf.grid(row=2, column=6, columnspan=2, sticky="w", **pad)
        ttk.Button(sf, text="Browse…", command=self._browse).pack(side="left")
        self.b_test = ttk.Button(sf, text="▶ Test", width=7, command=self._test_play)
        self.b_test.pack(side="left", padx=4)
        ttk.Button(sf, text="■ Stop", width=7, command=self._stop_all).pack(side="left")

        ttk.Label(ed, text="Volume").grid(row=3, column=0, sticky="w", **pad)
        self.v_volume = tk.IntVar(value=80)
        vs = ttk.Scale(ed, from_=0, to=100, orient="horizontal", variable=self.v_volume,
                       command=lambda v: self._on_volume())
        vs.grid(row=3, column=1, columnspan=4, sticky="we", **pad)
        self.l_volume = ttk.Label(ed, text="80 %", width=6)
        self.l_volume.grid(row=3, column=5, sticky="w", **pad)
        ttk.Label(ed, text="Ring for up to").grid(row=3, column=6, sticky="e", **pad)
        rf = ttk.Frame(ed)
        rf.grid(row=3, column=7, sticky="w", **pad)
        self.v_ring = tk.StringVar(value="10")
        ttk.Spinbox(rf, from_=1, to=120, width=4, textvariable=self.v_ring).pack(side="left")
        ttk.Label(rf, text="min").pack(side="left", padx=4)

        # Output device (per alarm)
        ttk.Label(ed, text="Play on").grid(row=4, column=0, sticky="w", **pad)
        of = ttk.Frame(ed)
        of.grid(row=4, column=1, columnspan=7, sticky="we", **pad)
        self.v_output = tk.StringVar(value=self.DEFAULT_OUTPUT)
        self.cb_output = ttk.Combobox(of, textvariable=self.v_output, state="readonly", width=34)
        self.cb_output.pack(side="left")
        self.cb_output.bind("<<ComboboxSelected>>", self._on_output_selected)
        ttk.Button(of, text="↻", width=2, command=lambda: self._refresh_outputs(load_airplay=True)).pack(side="left", padx=(2, 8))
        ttk.Label(of, text="speakers / headset / HDMI" + (" / AirPlay (HomePod, Apple TV) – chosen per alarm" if IS_MAC else " – chosen per alarm"),
                  foreground="#666").pack(side="left")
        self._refresh_outputs()

        # Recorder
        ttk.Label(ed, text="Record voice").grid(row=5, column=0, sticky="w", **pad)
        rcf = ttk.Frame(ed)
        rcf.grid(row=5, column=1, columnspan=7, sticky="we", **pad)
        self.devices = Recorder.input_devices()
        self.v_mic = tk.StringVar(value=self.devices[0][1] if self.devices else "No microphone found")
        self.cb_mic = ttk.Combobox(rcf, textvariable=self.v_mic, state="readonly", width=34,
                                   values=[d[1] for d in self.devices])
        self.cb_mic.pack(side="left")
        ttk.Button(rcf, text="↻", width=2, command=self._refresh_mics).pack(side="left", padx=(2, 8))
        self.b_rec = ttk.Button(rcf, text="● Record", width=10, command=self._toggle_record)
        self.b_rec.pack(side="left")
        self.meter = ttk.Progressbar(rcf, length=140, maximum=100)
        self.meter.pack(side="left", padx=8)
        self.l_rec = ttk.Label(rcf, text="")
        self.l_rec.pack(side="left")

        # Save row
        sv = ttk.Frame(ed)
        sv.grid(row=6, column=0, columnspan=8, sticky="we", **pad)
        self.b_save = ttk.Button(sv, text="Add alarm", command=self._save)
        self.b_save.pack(side="left")
        ttk.Button(sv, text="Clear form", command=self._new).pack(side="left", padx=6)
        self.l_form_hint = ttk.Label(sv, text="", foreground="#666")
        self.l_form_hint.pack(side="left", padx=10)

        # Power options
        po = ttk.LabelFrame(self, text="Sleep / power")
        po.pack(fill="x", padx=10, pady=4)
        s = self.store.settings
        self.v_keep = tk.BooleanVar(value=s["keep_awake"])
        self.v_wake = tk.BooleanVar(value=s["schedule_wake"])
        self.v_sysvol = tk.BooleanVar(value=s["force_system_volume"])
        self.v_sysvol_level = tk.StringVar(value=str(s["system_volume"]))
        self.v_snooze = tk.StringVar(value=str(s["snooze_minutes"]))
        ttk.Checkbutton(po, text="Keep computer awake while an alarm is armed",
                        variable=self.v_keep, command=self._settings_changed).grid(row=0, column=0, sticky="w", **pad)
        wake_txt = "Schedule a system wake before the next alarm"
        if IS_MAC:
            wake_txt += "  (asks for your Mac password)"
        elif IS_LINUX:
            wake_txt += "  (asks for your password, uses rtcwake)"
        else:
            wake_txt += "  (wake timer)"
        ttk.Checkbutton(po, text=wake_txt, variable=self.v_wake,
                        command=self._settings_changed).grid(row=1, column=0, sticky="w", **pad)
        f2 = ttk.Frame(po)
        f2.grid(row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(f2, text="When ringing, set the system output volume to",
                        variable=self.v_sysvol, command=self._settings_changed).pack(side="left")
        ttk.Spinbox(f2, from_=10, to=100, width=4, textvariable=self.v_sysvol_level,
                    command=self._settings_changed).pack(side="left", padx=4)
        ttk.Label(f2, text="%      Snooze").pack(side="left")
        ttk.Spinbox(f2, from_=1, to=60, width=4, textvariable=self.v_snooze,
                    command=self._settings_changed).pack(side="left", padx=4)
        ttk.Label(f2, text="min      Fade in over").pack(side="left")
        self.v_fade = tk.StringVar(value=str(s.get("fade_seconds", 20)))
        ttk.Spinbox(f2, from_=0, to=120, width=4, textvariable=self.v_fade,
                    command=self._settings_changed).pack(side="left", padx=4)
        ttk.Label(f2, text="s").pack(side="left")
        for var in (self.v_sysvol_level, self.v_snooze, self.v_fade):
            var.trace_add("write", lambda *_: self._settings_changed())

        # State indicators: armed / awake / OS wake – always visible
        st = ttk.Frame(po)
        st.grid(row=3, column=0, sticky="we", **pad)
        self.l_armed = ttk.Label(st, text="", width=34, anchor="w")
        self.l_armed.pack(side="left")
        self.l_awake = ttk.Label(st, text="", width=26, anchor="w")
        self.l_awake.pack(side="left")
        self.l_wake = ttk.Label(st, text="", anchor="w")
        self.l_wake.pack(side="left")
        self.b_retry_wake = ttk.Button(st, text="Retry", width=6, command=self._retry_wake)

        # Big stop button + status bar
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", side="bottom")
        self.l_status = ttk.Label(bottom, text="", anchor="w", relief="sunken", padding=(8, 4))
        self.l_status.pack(fill="x", side="bottom")
        self.b_stop = tk.Button(bottom, text="■  STOP ALARM", font=("Helvetica", 18, "bold"),
                                bg="#c62828", fg="white", activebackground="#8e0000",
                                activeforeground="white", height=2, command=self._stop_all)
        # (packed only while ringing – see _ring / _dismiss)

    # ----- form helpers
    DEFAULT_OUTPUT = "System default output"

    LOAD_AIRPLAY = "__load_airplay__"

    def _refresh_outputs(self, load_airplay: bool = False) -> None:
        """Fill 'Play on': system default, local devices, then AirPlay speakers (macOS, read from Music).
        AirPlay devices are only read when Music is already running, or when the user asks (↻ / list entry),
        so opening the alarm clock never opens Music by itself."""
        current = self._get_output()
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
        self.cb_output["values"] = list(self._output_map)
        self._set_output(current)

    def _on_output_selected(self, _e=None) -> None:
        if self._output_map.get(self.v_output.get()) == self.LOAD_AIRPLAY:
            self._refresh_outputs(load_airplay=True)
            first = next((lbl for lbl, v in self._output_map.items() if v.startswith(AirPlayPlayer.PREFIX)), None)
            self.v_output.set(first or self.DEFAULT_OUTPUT)
            if not first:
                self.l_form_hint.config(text="Music found no AirPlay speakers. Check they are on and on the same Wi-Fi.")

    def _set_output(self, value: str) -> None:
        """Show an alarm's output in the combobox, even if that device is currently unplugged/offline."""
        for label, v in self._output_map.items():
            if v == value:
                self.v_output.set(label)
                return
        if value:
            label = output_label(value) + "  (not connected)"
            self._output_map[label] = value
            self.cb_output["values"] = list(self._output_map)
            self.v_output.set(label)
        else:
            self.v_output.set(self.DEFAULT_OUTPUT)

    def _get_output(self) -> str:
        v = self._output_map.get(self.v_output.get(), "")
        return "" if v == self.LOAD_AIRPLAY else v

    def _is_airplay(self, value: str) -> bool:
        return bool(self.airplay) and value.startswith(AirPlayPlayer.PREFIX)

    def _play_airplay(self, alarm: dict | None, path: str, volume: int, device: str, loop: bool, fade: int) -> None:
        """Start AirPlay playback in a worker thread; failures come back through the event queue."""
        def work():
            try:
                self.airplay.play(path, volume, device, loop=loop, fade_seconds=fade)
                self.events.put(("notice", alarm, f"Playing on AirPlay speaker “{device}” via Music"))
            except Exception as e:
                log(f"AirPlay playback failed ({device}): {e}")
                self.events.put(("airplay_failed", alarm, f"{e}"))
        self.l_form_hint.config(text=f"Sending to AirPlay speaker “{device}” via Music…")
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
        self._on_volume()
        self.b_save.config(text="Save changes" if self.editing_id else "Add alarm")
        self.l_form_hint.config(text=f"Editing “{a['label']}”" if self.editing_id else "")
        self._form_id = a["id"]

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
            messagebox.showerror(APP_NAME, "Please choose a sound file (or record one) first.")
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
                                    "last_repeat": a["repeat"], "last_output": a.get("output", "")})
        self.store.upsert(a)
        self.scheduler.ringing.discard(a["id"])
        self._refresh_list(select=a["id"])
        self._apply_power()
        nf = next_fire(a)
        self.l_form_hint.config(text=f"Saved – rings {nf:%a %d %b %H:%M}" if nf else "Saved")
        self.editing_id = a["id"]
        self.b_save.config(text="Save changes")

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
            messagebox.showerror(APP_NAME, "Choose a sound file first.")
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

    def _retry_wake(self) -> None:
        ev = self.scheduler.next_event()
        self.power.retry_wake(ev[0] if ev else None)
        self._tick_indicators()

    # ----- list / status
    def _refresh_list(self, select: str | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        now = datetime.now()
        rows = sorted(self.store.alarms, key=lambda a: (not a["enabled"], next_fire(a, now) or datetime.max))
        for a in rows:
            nf = next_fire(a, now)
            when = f"{nf:%a %d %b %H:%M}" if nf else ("—" if not a["enabled"] else "?")
            self.tree.insert("", "end", iid=a["id"], values=(
                "✔" if a["enabled"] else "", when, a["repeat"], a["label"],
                os.path.basename(a["sound"]), output_label(a.get("output", "")), a["volume"]))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)

    def _apply_power(self) -> None:
        s = self.store.settings
        ev = self.scheduler.next_event()
        armed = ev is not None
        self.power.set_keep_awake(bool(s["keep_awake"]) and armed)
        self.power.set_wake(ev[0] if (armed and s["schedule_wake"]) else None)

    def _tick_indicators(self) -> None:
        ev = self.scheduler.next_event()
        now = datetime.now()
        if self.ring_windows:
            self.l_armed.config(text="🔔 RINGING", foreground="#c62828")
        elif ev:
            self.l_armed.config(text=f"● Armed – next ring in {fmt_delta(ev[0] - now)}", foreground="#2e7d32")
        else:
            self.l_armed.config(text="○ No alarm armed", foreground="#666")
        if self.power.keeping_awake:
            self.l_awake.config(text="● Keeping computer awake", foreground="#2e7d32")
        elif ev and not self.store.settings.get("keep_awake"):
            self.l_awake.config(text="○ Computer may sleep (keep-awake is off)", foreground="#e65100")
        elif ev:
            self.l_awake.config(text="○ Computer may sleep (keep-awake failed, see log)", foreground="#c62828")
        else:
            self.l_awake.config(text="○ Not holding computer awake", foreground="#666")
        state = self.power.wake_state
        colour = {"registered": "#2e7d32", "registering": "#666", "declined": "#e65100",
                  "failed": "#c62828"}.get(state, "#666")
        wake_txt = self.power.describe_wake()
        if ev and self.store.settings.get("schedule_wake") and state == "none" and self.power.wake_target is None:
            wake_txt = "no OS wake needed (alarm is less than a minute away)" if ev[0] - now < timedelta(
                seconds=WAKE_LEAD_SECONDS + 1) else wake_txt
        self.l_wake.config(text="●  " + wake_txt if state == "registered" else "○  " + wake_txt, foreground=colour)
        if state in ("declined", "failed") and ev:
            self.b_retry_wake.pack(side="left", padx=6)
        else:
            self.b_retry_wake.pack_forget()

    def _tick_status(self) -> None:
        ev = self.scheduler.next_event()
        now = datetime.now()
        if ev:
            txt = f"Next: “{ev[1]['label']}” {ev[0]:%a %d %b %H:%M} (in {fmt_delta(ev[0] - now)})"
        else:
            txt = "No alarm armed"
        self.l_status.config(text=f"{now:%H:%M:%S}   {txt}   •   data folder: {BASE_DIR}")
        self._tick_indicators()
        if (self.airplay and ev and self._is_airplay(ev[1].get("output", "")) and self._prewarmed != ev[1]["id"]
                and timedelta(0) <= ev[0] - now <= timedelta(minutes=3)):
            self._prewarmed = ev[1]["id"]
            threading.Thread(target=self.airplay.prewarm, daemon=True).start()
        # refresh "next ring" column once a minute
        if now.second == 0:
            sel = self.tree.selection()
            self._refresh_list(select=sel[0] if sel else None)
        self.after(1000, self._tick_status)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, a, when = self.events.get_nowait()
                if kind == "ring":
                    self._ring(a, when)
                elif kind == "notice":
                    self.l_form_hint.config(text=when)
                elif kind == "airplay_failed":
                    self.l_form_hint.config(text=f"AirPlay failed: {when}  –  playing on this computer instead.")
                    self.bell()
                    if a is None or a["id"] in self.ring_windows:
                        path = a["sound"] if a else self.v_sound.get()
                        vol = int(a["volume"] if a else self.v_volume.get())
                        try:
                            self.player.play(path, vol, loop=a is not None, device="")
                        except Exception as e:
                            log(f"fallback playback failed: {e}")
                elif kind == "missed":
                    self._refresh_list()
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
        problem = ""
        if not os.path.isfile(a["sound"]):
            problem = (f"The sound file for “{a['label']}” is missing:\n{a['sound']}\n\n"
                       "It may have been moved or deleted. Pick another file in Alarm details and save.")
        elif use_airplay:
            self._play_airplay(a, a["sound"], int(a["volume"]), a["output"][len(AirPlayPlayer.PREFIX):], loop=True, fade=fade)
        else:
            try:
                warning = self.player.play(a["sound"], 0 if fade else a["volume"], loop=True,
                                           device=a.get("output", ""))
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
        self._refresh_list()
        self._apply_power()

        if a["id"] in self.ring_windows:
            self.ring_windows[a["id"]].lift()
            return
        win = tk.Toplevel(self)
        win.title("⏰ " + a["label"])
        win.attributes("-topmost", True)
        win.geometry("460x300")
        win.protocol("WM_DELETE_WINDOW", lambda: self._dismiss(a["id"]))
        ttk.Label(win, text="⏰", font=("Helvetica", 40)).pack(pady=(14, 0))
        ttk.Label(win, text=a["label"], font=("Helvetica", 20, "bold")).pack()
        ttk.Label(win, text=f"{when:%A %d %B %Y  %H:%M}").pack(pady=(0, 10))
        tk.Button(win, text="■  STOP", font=("Helvetica", 22, "bold"), bg="#c62828", fg="white",
                  activebackground="#8e0000", activeforeground="white", width=14, height=2,
                  command=lambda: self._dismiss(a["id"])).pack(pady=4)
        ttk.Button(win, text=f"Snooze {s.get('snooze_minutes', 5)} min",
                   command=lambda: self._snooze(a)).pack(pady=(6, 0))
        win.bind("<Return>", lambda e: self._dismiss(a["id"]))
        win.bind("<Escape>", lambda e: self._dismiss(a["id"]))
        self.ring_windows[a["id"]] = win
        self.ring_timeouts[a["id"]] = self.after(int(a.get("ring_minutes", 10)) * 60_000,
                                                 lambda: self._dismiss(a["id"], timed_out=True))
        self.b_stop.pack(fill="x", side="top", padx=10, pady=6)
        self.deiconify(); self.lift(); win.lift(); win.focus_force()
        self._tick_indicators()

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

    def _dismiss(self, alarm_id: str, timed_out: bool = False) -> None:
        win = self.ring_windows.pop(alarm_id, None)
        if win:
            win.destroy()
        t = self.ring_timeouts.pop(alarm_id, None)
        if t:
            self.after_cancel(t)
        self.scheduler.ringing.discard(alarm_id)
        if not self.ring_windows:
            self.player.stop()
            self._stop_airplay()
            self.b_stop.pack_forget()
            if self._fade_job:
                self.after_cancel(self._fade_job)
                self._fade_job = None
        if timed_out:
            log(f"alarm {alarm_id} stopped after ring timeout")
        self._refresh_list()
        self._apply_power()
        self._tick_indicators()

    def _on_close(self) -> None:
        if self.ring_windows:
            if not messagebox.askyesno(APP_NAME, "An alarm is ringing right now. Quitting will silence it.\n\nQuit anyway?"):
                return
        elif self.scheduler.next_event():
            if not messagebox.askyesno(APP_NAME, "An alarm is still armed. Alarms only ring while this window "
                                                 "is open.\n\nQuit anyway?"):
                return
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
