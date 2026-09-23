---
name: audio-io
description: Audio playback (pygame-ce, imported as pygame.mixer) and microphone recording (sounddevice RawInputStream → WAV) in the alarm clock, including volume, fade, system-volume forcing and macOS mic permission. Load before changing Player, Recorder, set_system_volume or the record/test UI.
---

# Audio playback & recording

## Playback – `Player` (pygame.mixer.music)
- Formats: MP3, OGG, WAV, FLAC (SDL_mixer). M4A/AIFF work on most builds; if `load()` raises, tell the user to convert (see [[plain-language-errors]]).
- Alarms loop (`play(-1)`); test playback plays once. Volume is `set_volume(v/100)`; fade-in is done in the GUI by repeatedly calling `set_volume` (`App._start_fade`), not in Player.
- **Per-alarm output device** (`alarm['output']`, '' = system default): `Player.play(..., device=)` calls `_ensure_device`, which re-opens the mixer with `pygame.mixer.init(devicename=…)` only when the device changes (~40 ms). Device names come from `pygame._sdl2.audio.get_audio_device_names(False)`. If the device is gone, it falls back to the default and returns a plain-language warning that the UI shows in the form hint – ring first, warn second. Only one device can be open at a time; two alarms ringing simultaneously on different outputs → the later one wins (logged).
- `Player.ok` is False when no output device exists; every play path must handle that without crashing.
- `set_system_volume()` is best effort and platform specific (osascript / pactl / VK_VOLUME_UP). Runs in a thread when ringing because osascript can take a second. Never let it block the ring.
- `PYGAME_HIDE_SUPPORT_PROMPT=1` is set before import to keep the console quiet.

## AirPlay – `AirPlayPlayer` (macOS, via the Music app)
- Output values `airplay:<name>`. SDL/CoreAudio cannot target a specific AirPlay receiver, so playback goes through Music with osascript: select `current AirPlay devices`, `add` the file to the library, `play`, and on stop `delete` the temporary track and restore the previous speaker selection + `sound volume`.
- Every Music call is a subprocess (0.1–3 s; longer if Music must launch) → `play()`/`stop()` run in worker threads (`App._play_airplay`, `App._stop_airplay`); results come back through `App.events` as `("notice", …)` / `("airplay_failed", …)`. Failure → fall back to the local default output and say why (offline speaker, Automation permission -1743).
- Fade-in for AirPlay happens inside `AirPlayPlayer.play` (1 step/s) because Tk `after` steps would each cost an osascript round-trip. The slider is debounced to one call per 400 ms.
- `devices()` only queries Music when it is already running unless `launch=True` (↻ button / list entry) – opening the alarm clock must never open Music. `prewarm()` launches Music 3 min before an AirPlay alarm.
- Test without waking the household: route to the computer's own AirPlay entry (`"<Mac name>"`), which exercises the identical code path. Never send test audio to a HomePod unasked.

## Recording – `Recorder` (sounddevice)
- `RawInputStream(dtype='int16', channels=1)` → bytes chunks → `wave` module. No numpy dependency on purpose (keeps the portable install small); level meter = max|sample| over the chunk via `array('h')`.
- **Level normalisation** (`normalize_int16`): every take gets DC-offset removal and peak normalisation to -1 dBFS, capped at +40 dB. USB/Bluetooth headsets and webcams routinely deliver -30…-45 dBFS peaks (measured on the author's machine: 0.6–10 % of full scale) and macOS exposes no input gain for many of them (`osascript -e 'get volume settings'` → `input volume:missing value`), so software gain is the only fix. Peak < 0.3 % = "almost nothing recorded" → warn with the mic name (wrong device, muted boom, or no mic permission).
- The UI shows the peak live and turns red after 2 s if the take is still below 2 % – the user must learn *while recording* that the wrong mic is selected, not after.
- The default entry names the actual default device: `System default microphone  (CORSAIR HS80…)`.
- Sample rate: device default (`query_devices(dev)['default_samplerate']`), fallback 44100.
- Output path: `recordings/voice_YYYY-MM-DD_HH-MM-SS.wav` next to the app; after Stop the path is put straight into the Sound field so "record → save alarm" is two clicks.
- Device list = `[(None, 'System default microphone')] + inputs with max_input_channels > 0`. Provide the ↻ refresh button because Bluetooth headsets appear/disappear.
- macOS microphone permission belongs to the *launching* app: Terminal when started via the `.command`, the `.app` itself when built with PyInstaller (needs `NSMicrophoneUsageDescription` in Info.plist – `build_standalone.sh` adds it). If the stream opens but records silence, that's the permission dialog having been declined.

## Verify
```bash
.venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"
.venv/bin/python -c "import pygame; pygame.mixer.init(); print(pygame.mixer.get_init())"
```
Record 3 s, check the WAV is non-zero: `python -c "import wave;w=wave.open('recordings/<file>.wav');print(w.getnframes()/w.getframerate(),'s')"`.
