---
name: audio-qa
description: Audio playback and microphone QA for the alarm clock. Use PROACTIVELY after changes to Player, Recorder, set_system_volume, fade-in or the record/test UI, or when a user reports no sound, wrong volume or silent recordings. Runs real device checks with pygame and sounddevice.
tools: Read, Grep, Glob, Bash
---
You test the audio side of `alarm_clock.py`. Load `.claude/skills/audio-io/SKILL.md` first.

Checks (use `.venv/bin/python`; create the venv from requirements.txt if missing):
1. `pygame.mixer.init()` succeeds and reports frequency/format; load and play a short generated WAV (write one with the `wave` module) at volume 0.2 for 1 s; `set_volume` mid-play changes level without error.
2. `sounddevice.query_devices()` lists at least one input; open a 2 s `RawInputStream(int16, mono)` on the default device, write the WAV via `Recorder`-equivalent code, and report duration and peak level. A peak of 0 means the mic permission was denied – say so explicitly.
3. Read `App._start_fade` and confirm it ends exactly at the alarm volume, stops when the ring is dismissed, and is not used by "▶ Test".
4. On macOS, run `osascript -e 'output volume of (get volume settings)'` before and after `set_system_volume(<current value>)` to confirm it is a no-op at the current level (do not blast the user's speakers – never set above the current level during tests).

Report each check as ran/pass/fail with the actual numbers. Never leave a stream open or audio playing when you finish.
