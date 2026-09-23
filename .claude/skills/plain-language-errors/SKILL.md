---
name: plain-language-errors
description: Wording and handling rules for every user-facing error in the alarm clock – missing file, no mic permission, admin prompt declined, audio device gone, past date. Load before adding a messagebox, log line or status text.
---

# Errors in plain language

Every user-visible error answers three things in ≤ 3 short sentences: **what happened, why it probably happened, what to do now.** Technical detail (exception text) goes on its own line after that, and always into `alarmclock.log` via `log()`.

| Situation | Say | Never say |
|---|---|---|
| Sound file missing at ring time | "The sound file for “Wake up” is missing: …/song.mp3. It may have been moved or deleted. Pick another file in Alarm details and save." | `FileNotFoundError` |
| File won't decode | "… could not play song.m4a. Try another file, or convert this one to MP3/WAV." | "pygame.error: Unrecognized audio format" alone |
| Mic won't open | "Could not start recording. On macOS allow this app (or Terminal) to use the microphone in System Settings → Privacy & Security → Microphone." | "PortAudioError -9986" alone |
| Admin/password prompt declined | Indicator: "OS wake NOT registered – password prompt declined" + Retry button. No modal. | Re-prompting automatically |
| Wake unsupported / failed | "OS wake NOT registered – <reason>" in the indicator; keep-awake still works, say so. | Pretending it's registered |
| Date/time in the past | "That date and time is already in the past." | `ValueError` |
| Alarm missed while machine was off | "Alarm “X” was missed (it was due Tue 07:00 while the computer was off or asleep)." – shown once, alarm disabled | Silently skipping |
| Quit with an alarm armed | "An alarm is still armed. Alarms only ring while this window is open. Quit anyway?" | Quitting silently |

## Mechanics
- Modal `messagebox` only for things that need a decision or block the current action. Persistent conditions (wake not registered, may sleep) live in the indicator row, not in pop-ups.
- Errors raised inside the scheduler thread are caught and logged; the thread must never die (an alarm that silently stops being checked is the worst failure this app can have).
- Ring first, complain second: when playback fails at alarm time we still show the ring window and `bell()`, then the error.
- Keep the wording consistent with [[alarm-ux]] indicator texts; when you change one, grep for the other.
