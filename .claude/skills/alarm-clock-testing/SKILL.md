---
name: alarm-clock-testing
description: How to verify the alarm clock end-to-end – headless logic test for next_fire/Scheduler, launching the GUI from the .command launcher, ring/snooze/record smoke test, and the manual sleep/wake checks. Load before claiming any change works.
---

# Testing the alarm clock

## 1. Headless logic (fast, always run)
```bash
python3 -m py_compile alarm_clock.py
.venv/bin/python tests/test_logic.py
```
`tests/test_logic.py` covers `next_fire` for once/daily/weekdays, the GRACE catch-up, `Scheduler.tick` firing/missed/snooze. Add a case there whenever you touch scheduling.

## 2. GUI smoke test (before any "done")
1. `bash "Start Alarm Clock.command"` (macOS) – window must appear with no traceback in the terminal.
2. Browse an MP3 (or click ● Record, speak 3 s, ■ Stop) → ▶ Test → slider changes loudness live → ■ Stop.
3. Click "+1 min" → "Add alarm" → list shows it, status bar shows "in 59s", indicators show Armed + Keeping awake; if "Schedule a system wake" is on, no password prompt should appear for an alarm < 60 s away.
4. Wait: ring window pops on top, main window shows the red STOP bar, volume fades in. Snooze → re-rings after N min. STOP → everything quiet, indicators back to "No alarm armed".
5. Close with an alarm armed → warning dialog.

## 3. Sleep / wake (manual, per OS – see [[power-management]])
- Set an alarm 4 min out, confirm "OS wake registered", sleep the machine (Apple menu → Sleep / Start → Sleep). It must wake ≈ 1 min early and ring.
- Confirm in `alarmclock.log`: `system wake registered`, then `alarm due`.

## 4. Portability
Copy the folder to a new location without `.venv`, double-click, repeat step 3 of the smoke test. See [[portable-packaging]].

Report results honestly: say which of 1–4 you actually ran and what happened. Never report step 3 as passed without physically sleeping the machine.
