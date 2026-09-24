---
name: power-management
description: OS sleep / wake handling for the alarm clock – caffeinate and pmset on macOS, SetThreadExecutionState and waitable wake timers on Windows, systemd-inhibit and rtcwake on Linux, plus the GRACE catch-up logic. Load before changing PowerManager, Scheduler, or anything about "the PC was asleep".
---

# OS power management (the hard part of this project)

Two separate jobs, both in `PowerManager` in `alarm_clock.py`:

| Job | macOS | Windows | Linux |
|---|---|---|---|
| Keep awake while armed (no admin) | `caffeinate -i -s -w <pid>` child process | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED)` – must be called on a long-lived thread (we use the Tk main thread) | `systemd-inhibit --what=sleep:idle … sleep infinity` child process |
| Real wake from sleep | `pmset schedule wake "MM/dd/yy HH:mm:ss"` via `osascript … with administrator privileges` (GUI password prompt) | `CreateWaitableTimerW` + `SetWaitableTimer(…, fResume=TRUE)` with an **absolute** FILETIME; a thread must be waiting on the handle; user's power plan must allow wake timers | `pkexec rtcwake -m no -t <epoch>` |

## Rules
- Wake is scheduled `WAKE_LEAD_SECONDS` (60 s) **before** the alarm so audio/network are up when it rings.
- `set_wake()` acts **only when the target changes**. Never re-prompt for a password every tick. If the user declines, remember `_declined_target` and expose "Retry" in the UI.
- Always cancel our previous macOS wake (`pmset schedule cancel wake "<old>"`) in the same admin command as the new one → one prompt, no stale wakes piling up. Never use `pmset schedule cancelall` (it kills other apps' schedules).
- `wake_state` ∈ none / registering / registered / declined / failed / unsupported drives the indicator; `describe_wake()` is the only text the UI shows. The UI must never claim "registered" unless the OS call returned success.
- Sleep catch-up: the scheduler compares wall-clock `datetime.now()` each second. After a wake it fires anything overdue by ≤ `GRACE` (30 min) immediately, and marks older ones **missed** (event `"missed"`). Do not use monotonic timers for alarm timing – they don't advance during sleep.
- The app only rings while it is running. The close handler warns if an alarm is armed. Don't remove that warning.
- `pmset schedule` and `caffeinate -s` behave differently on battery vs AC (laptops with lid closed may only dark-wake and won't play through the speakers). Document; don't try to "fix" with hacks.

## How to verify (manually – these can't be unit-tested)
- macOS: `pmset -g sched` lists the wake we registered; `pmset -g assertions` shows `PreventUserIdleSystemSleep` from caffeinate while armed. Put the Mac to sleep manually with an alarm 3 min away; it must wake ~1 min early and ring.
- Windows: `powercfg /waketimers` lists our timer; `powercfg /requests` shows the SYSTEM request. Check "Allow wake timers" in the advanced power plan.
- Linux: `cat /sys/class/rtc/rtc0/wakealarm` shows the epoch; `systemd-inhibit --list`.
- Always check `alarmclock.log` – every power action logs its result.

Related: [[alarm-ux]] for how state is shown, [[plain-language-errors]] for the wording when a prompt is declined.

## Routine messages (schedules) – since 2026-09-24
- `Scheduler._tick_schedules` dispatches `("announce", occ, due)` once per (schedule, event, date): the occurrence is recorded in `AlarmStore.occurrences` **before** the event is queued, so restarts, sleep and repeated ticks never replay it.
- Lateness limit `SCHEDULE_GRACE` = 2 min (alarms keep `GRACE` = 30 min). Anything older is recorded `missed` with a note ("the computer was off or asleep", "waited too long behind other sounds") – no burst of stale family reminders after a wake.
- DST: a wall-clock time that does not exist (`local_time_exists` false) is recorded `missed` ("clocks went forward"); a repeated hour cannot fire twice because the key is per date.
- Keep-awake and the OS wake use `AlarmStore.next_event()` = min(next alarm, next audible schedule event). Events without sound, disabled events/schedules and skipped occurrences are excluded, so they never arm a wake.
- The GUI's `AnnouncementQueue` serialises messages on the single pygame stream; alarms always take the stream (`_take_local_stream` marks a playing message `interrupted`, never replays it).
