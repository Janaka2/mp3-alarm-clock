---
name: alarm-ux
description: Alarm-clock UX patterns for this project – snooze, repeat schedules, fade-in, countdown, big Stop button, sensible defaults and always-visible state. Load before touching anything in the App class of alarm_clock.py or adding a user-facing feature.
---

# Alarm-clock UX patterns

The product is judged on details people feel every morning, not on the framework. Apply these rules to every UI change in `alarm_clock.py`.

## Non-negotiables
1. **Big obvious Stop.** Ringing must be stoppable with one click on a large red button (main window `b_stop` and the ring popup), and with Enter / Escape. Never hide Stop behind a menu or a confirm dialog.
2. **Snooze** is one click, uses `settings.snooze_minutes`, and re-rings via `Scheduler.snooze` (not a new persisted alarm).
3. **Repeat modes**: once / every day / weekdays. `next_fire()` in the core section is the single source of truth for "when does it ring next"; the UI never recomputes it.
4. **Fade-in volume** (`settings.fade_seconds`, 0 disables): start at 0 and ramp to the alarm volume with `App._start_fade`. Test playback (`▶ Test`) does NOT fade – the user wants to hear the real level.
5. **Visible countdown**: the list column "Next ring", the status bar "in 7h 12m" and the armed indicator must all agree (they all call `next_fire` / `Scheduler.next_event`).
6. **Sensible defaults**: a new alarm is usable without changing anything – date today, time = next round hour (`next_round_hour`), sound / volume / repeat remembered from the last saved alarm (`settings.last_*`). Keep this true when adding fields: every new field needs a remembered or safe default.
7. **Clear state feedback** – three indicators are always visible (see [[power-management]] for what they mean):
   - armed / not armed / RINGING
   - keeping computer awake / may sleep
   - OS wake registered / NOT registered (+ why, + Retry button)
8. **Ring timeout** (`ring_minutes`) so a forgotten alarm doesn't play forever; log when it times out.
9. **Missed alarms** (overdue longer than `GRACE`) are reported once in plain language and disabled – never silently dropped.

## Interaction details
- Selecting a row loads it into the form; the Save button label switches between "Add alarm" and "Save changes". Don't introduce a modal editor.
- Double-click a row = enable/disable toggle.
- Quick buttons "+1 min" / "+10 min" exist for testing; keep them – users use them to try a sound.
- All times shown as `%a %d %b %H:%M` (24 h). Don't mix 12 h and 24 h.
- Any long-running thing (password prompt, pip install) must run off the Tk thread; Tk widgets are touched only from the main thread (scheduler → `events` queue → `_poll_events`).

## When reviewing a change, ask
- Can I still stop it in one click while half asleep?
- Does the status bar tell the truth about armed / awake / wake-registered right now?
- Does a brand-new alarm still work with zero edits?
- Did an error path fall back to a stack trace instead of a sentence? (see [[plain-language-errors]])
