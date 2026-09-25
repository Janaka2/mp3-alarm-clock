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

## Visual system (since the 2026-09-24 redesign)
- ttk theme `clam` + `App.PALETTE` tokens (navy header `#1F2A44`, page `#F3F5F9`, white cards, accent blue `#3A6FF0`, good/warn/bad greens-oranges-reds) and `App.F` fonts (Helvetica Neue / Segoe UI, 13 pt base, 46 pt clock). Never hard-code a colour in a widget; add a token.
- Layout: header (title, next-alarm line, three status **pills**, big clock) → two columns: left = *Your alarms* card + collapsed *More options*; right = editor card with numbered steps ① When ② Sound ③ Where → *Save alarm*. The editor column is packed first (`side="right"`) so it keeps its natural width; the list column flexes.
- Plain words everywhere: "Just once / Every day / Weekdays", "Choose a file…", "Record my voice", "Preview", "Plays on". Technical settings (caffeinate, pmset, system volume) live only under *More options* with a one-line muted explanation.
- Pills use `_set_pill(label, text, tone)`; tone ∈ good / warn / bad / ring / neutral. Text starts with ● △ ○ 🔔 so state is readable without colour.
- Buttons: `Accent.TButton` for the one primary action per card, `Soft.TButton` for the rest, `Danger.TButton` for delete, `Stop.TButton` (big red) only for stopping a ring. All are ttk so colours render on macOS too (plain `tk.Button` ignores `bg` there).
- Verify layout numerically when screenshots aren't possible: instantiate `App`, pump `update()`, and compare `winfo_reqwidth()` with `winfo_width()` for every mapped widget (see the session's geometry check); nothing except the Treeview may request more than it gets, at both the default and the minimum window size.

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

## Sound from a link (since 2026-09-25)
- Third source next to "Choose a file…" and "Record my voice": **🔗 Use a link…**. In the alarm editor the link row (Link · entry · Get the sound · Cancel) *replaces* the button row while open, and progress / errors go to `l_rec` – the editor column must not grow (it already fills 1400x840 exactly; `brow` stays ≈ 540 px). To make room, "▶ Preview" turns into "■ Stop" while a preview plays (`_sync_preview_button`, refreshed by `_tick_status`) instead of a separate Stop button.
- In the event editor it is state D of `_event_sound_ui` (`ev_link_open`), with the hint line reporting progress; Done refuses while a fetch runs, Cancel / another schedule / closing the app abort it.
- Feedback while fetching: "Looking up the YouTube link… → Downloading from YouTube… 42 % → Converting to MP3…", then the title replaces the file name with "YouTube · 3:32" underneath and the status line says what to do next (Preview, Save). Failures are one sentence in red on the same line, the row stays open so the link can be corrected.
- The clipboard is pre-filled into the Link field when it holds a web address. Enter = Get the sound.
- Lists show "🔗 title"; a lost file says "saved copy missing – use the link again" and the ring-time error names the site and title.

## Two play modes (since 2026-09-24)
- `alarm["mode"]`: `alarm` = loop the file until STOP / snooze / `ring_minutes` timeout (classic). `play` = play the file once from start to end (long talks, 2-hour sermons, albums) and stop by itself; no timeout, no snooze, window not topmost, no focus stealing, elapsed-time counter, `_watch_once` polls `player.is_playing()` (pygame) or Music every 5 s in a worker thread (AirPlay) and posts `("finished", aid, None)`.
- Keep-awake must also hold while anything is playing (`_apply_power`: armed **or** ring_windows), otherwise a long file is cut by idle sleep.
- The form remembers the last mode (`settings.last_mode`); the give-up timeout row is hidden in `play` mode and replaced by a one-line explanation.

## Family schedules (since 2026-09-24)
- Three views behind the header: **Today** (default), **Schedules**, **Alarms** (the classic editor, unchanged). Navigation is a `Nav.Toolbutton` segmented control; pages are frames under `App.pages`, swapped by `_show_page`.
- A **schedule** = name + repeat days (Mon–Sun toggles, shortcuts Weekdays / Every day / Weekends) + one speaker + one volume + on/off switch + a list of **events** (time, activity, optional sound, on/off). Events never repeat these settings. New schedules are created **off** and stay off until saved with "Schedule is on".
- One editor for everything: `sdraft` is an unsaved copy; `_sched_dirty()` compares it with the stored JSON, and leaving the editor with changes asks once (`_sched_confirm_discard`). Event editing replaces the event list in place (`_event_show_editor`) – no modal windows.
- Recording inside an event: **Record my voice** → red "Recording… N s" row with level meter, Stop recording, Cancel → take row with Preview / Record again / Use recording / Discard. A take nobody uses is deleted (`_delete_take`, only files under `recordings/` with no users); the previously saved sound is never touched until Use recording + Done + Save.
- **Today** rows: time · activity · schedule · sound · speaker · status, statuses `Upcoming / Playing soon / Playing / Played / Missed / Failed / Skipped / Stopped / Interrupted / No sound / Off / Missing file`. Earlier rows collapse behind "Earlier today (n)". Skip today / Undo skip work per event (and undo a whole-schedule skip); the row menu is right-click / ctrl-click.
- Playing a message never marks the activity done, and Preview (`_preview_sound`) never touches `occurrences` or the queue. Preview is refused with a sentence while something rings, plays or records.
- Wording: "message" for schedule sounds, "alarm" for individual alarms; pills say "Message set · plays in …" vs "Alarm set · rings in …" and "Playing a message".
- Layout rule still holds: measure `winfo_reqheight` vs `winfo_height` on the Schedules page **with the event editor open** at 1400x840 and 1240x760 (see `build/probe/smoke.py` pattern in the session notes); `NoteLabel` hints take no space while empty.
