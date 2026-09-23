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

## Recording – `Recorder` (sounddevice)
- `RawInputStream(dtype='int16', channels=1)` → bytes chunks → `wave` module. No numpy dependency on purpose (keeps the portable install small); level meter = max|sample| over the chunk via `array('h')`.
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
