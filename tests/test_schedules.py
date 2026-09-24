"""Headless tests for family schedules.  Run: .venv/bin/python tests/test_schedules.py"""
import json, os, queue, shutil, sys, tempfile, time
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alarm_clock as ac

MON = datetime(2026, 9, 21, 6, 0)          # Monday
SAT = datetime(2026, 9, 26, 6, 0)          # Saturday


def fresh(tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    return ac.AlarmStore(os.path.join(tmp, "alarms.json")), tmp


def wav(tmp, name="msg.wav"):
    p = os.path.join(tmp, name); open(p, "wb").close(); return p


def son(tmp, enabled=True):
    s = ac.new_schedule(); s.update(name="Son — School day", days=list(ac.WEEKDAYS), enabled=enabled, output="Kids room")
    e1 = ac.new_event("07:00"); e1.update(label="Wake up", sound=wav(tmp, "wake.wav"))
    e2 = ac.new_event("07:20"); e2.update(label="Breakfast", sound=wav(tmp, "breakfast.wav"))
    e3 = ac.new_event("07:50"); e3.update(label="Leave", sound="")            # no sound: shown, never played
    s["events"] = [e1, e2, e3]
    return s


def test_days_and_next():
    store, tmp = fresh()
    s = son(tmp); store.upsert_schedule(s)
    assert store.next_announcement(MON)[0] == datetime(2026, 9, 21, 7, 0)
    assert store.next_announcement(SAT)[0] == datetime(2026, 9, 28, 7, 0)          # weekend → next Monday
    assert store.next_announcement(datetime(2026, 9, 21, 7, 5))[0] == datetime(2026, 9, 21, 7, 20)
    late = datetime(2026, 9, 21, 7, 21, 30)                                         # 07:20 still within 2-min grace
    assert store.next_announcement(late)[0] == datetime(2026, 9, 21, 7, 20)
    assert store.next_announcement(datetime(2026, 9, 21, 7, 23))[0] == datetime(2026, 9, 22, 7, 0)
    # midnight / week boundary: an event at 00:05 on Sundays, asked late on Saturday night
    w = ac.new_schedule(); w.update(name="Weekend", days=list(ac.WEEKEND), enabled=True)
    ev = ac.new_event("00:05"); ev.update(label="Midnight", sound=wav(tmp, "m.wav")); w["events"] = [ev]
    store.upsert_schedule(w)
    assert store.next_announcement(datetime(2026, 9, 26, 23, 59))[0] == datetime(2026, 9, 27, 0, 5)
    assert store.next_announcement(datetime(2026, 9, 27, 0, 10))[0] == datetime(2026, 9, 28, 7, 0)   # son, Monday, comes first
    assert ac.days_label(ac.WEEKDAYS) == "Weekdays" and ac.days_label([5, 6]) == "Weekends"
    assert ac.days_label(ac.EVERY_DAY) == "Every day" and ac.days_label([0, 2]) == "Mon Wed"
    # the combined "next audible thing" prefers whichever comes first
    a = ac.new_alarm(); a.update(date="2026-09-21", time="06:30", repeat="once", sound=wav(tmp, "a.wav")); store.upsert(a)
    assert store.next_event(MON)[1]["id"] == a["id"]
    a["enabled"] = False; store.upsert(a)
    assert store.next_event(datetime(2026, 9, 21, 6, 45))[1]["kind"] == "event"


def test_enable_disable_skip_undo_expiry():
    store, tmp = fresh()
    s = son(tmp); store.upsert_schedule(s)
    sid, eid = s["id"], s["events"][0]["id"]
    s["enabled"] = False; store.upsert_schedule(s)
    assert store.next_announcement(MON) is None                                   # whole schedule off
    s["enabled"] = True; s["events"][0]["enabled"] = False; store.upsert_schedule(s)
    assert store.next_announcement(MON)[0] == datetime(2026, 9, 21, 7, 20)         # one event off
    s["events"][0]["enabled"] = True; store.upsert_schedule(s)
    store.set_skip(MON.date(), sid, None, True)                                    # skip whole schedule today
    assert store.is_skipped(MON.date(), sid) and store.next_announcement(MON)[0] == datetime(2026, 9, 22, 7, 0)
    store.set_skip(MON.date(), sid, None, False)                                   # undo skip
    assert not store.is_skipped(MON.date(), sid) and store.next_announcement(MON)[0] == datetime(2026, 9, 21, 7, 0)
    store.set_skip(MON.date(), sid, eid, True)                                     # skip one event today
    assert store.next_announcement(MON)[0] == datetime(2026, 9, 21, 7, 20)
    assert not store.is_skipped(MON.date() + timedelta(days=1), sid, eid)          # tomorrow is untouched
    store.prune(MON.date() + timedelta(days=1))                                    # next day: still kept (a 23:59 skip must hold at 00:01)
    assert MON.date().isoformat() in store.exceptions
    store.prune(MON.date() + timedelta(days=2))                                    # two days on: expired by itself
    assert store.exceptions == {}
    # scheduler records a skipped occurrence when the time passes, so undoing afterwards never replays it
    store.set_skip(MON.date(), sid, eid, True)
    q = queue.Queue(); sch = ac.Scheduler(store, q)
    sch.tick(datetime(2026, 9, 21, 7, 0, 1))
    posted = [q.get_nowait() for _ in range(q.qsize())]
    assert all(k == "occurrence" for k, *_ in posted)                              # only a notice, never a play request
    assert store.occurrences[ac.occurrence_key(sid, eid, MON.date())]["status"] == "skipped"


def test_scheduler_dispatch_lateness_restart():
    store, tmp = fresh()
    s = son(tmp); store.upsert_schedule(s)
    sid, e1, e2 = s["id"], s["events"][0]["id"], s["events"][1]["id"]
    q = queue.Queue(); sch = ac.Scheduler(store, q)
    sch.tick(datetime(2026, 9, 21, 6, 59, 59)); assert q.empty()
    sch.tick(datetime(2026, 9, 21, 7, 0, 0))
    kind, occ, due = q.get_nowait()
    assert kind == "announce" and occ["event"]["id"] == e1 and due == datetime(2026, 9, 21, 7, 0)
    sch.tick(datetime(2026, 9, 21, 7, 0, 1)); assert q.empty()                    # no double dispatch
    # restart: a new scheduler over a reloaded store must not replay it
    store2 = ac.AlarmStore(store.path); q2 = queue.Queue(); sch2 = ac.Scheduler(store2, q2)
    sch2.tick(datetime(2026, 9, 21, 7, 0, 30)); assert q2.empty()
    # woke up 10 minutes late: breakfast is missed, not played (2-minute limit), and no burst of messages
    sch2.tick(datetime(2026, 9, 21, 7, 30))
    posted = [q2.get_nowait() for _ in range(q2.qsize())]
    assert all(k == "occurrence" for k, *_ in posted)                             # notices only, nothing to play
    assert store2.occurrences[ac.occurrence_key(sid, e2, MON.date())]["status"] == "missed"
    # the event without a sound never gets an occurrence
    assert ac.occurrence_key(sid, s["events"][2]["id"], MON.date()) not in store2.occurrences
    # individual alarms keep their own 30-minute policy
    a = ac.new_alarm(); a.update(date="2026-09-21", time="07:25", repeat="once", sound=wav(tmp, "a.wav")); store2.upsert(a)
    sch2.tick(datetime(2026, 9, 21, 7, 40)); assert q2.get_nowait()[0] == "ring"
    # Saturday: nothing from a weekday schedule
    sch2.tick(datetime(2026, 9, 26, 7, 0, 0)); assert q2.empty()
    # midnight: a Friday 23:59 message reached at Saturday 00:01 (inside the 2-minute limit) still plays once
    late = ac.new_event("23:59"); late.update(label="Lights out", sound=wav(tmp, "l.wav"))
    s2 = store2.get_schedule(sid); s2["events"].append(late); store2.upsert_schedule(s2)
    assert store2.next_announcement(datetime(2026, 9, 26, 0, 0, 30))[0] == datetime(2026, 9, 25, 23, 59)
    sch2.tick(datetime(2026, 9, 26, 0, 1, 0))
    posted = [q2.get_nowait() for _ in range(q2.qsize())]                        # Friday's morning events → missed notices
    plays = [(occ, due) for k, occ, due in posted if k == "announce"]
    assert len(plays) == 1 and plays[0][0]["event"]["id"] == late["id"] and plays[0][1] == datetime(2026, 9, 25, 23, 59)
    sch2.tick(datetime(2026, 9, 26, 0, 1, 30)); assert q2.empty()
    # a queued/playing record left behind by a crash is shown as missed after a restart, never replayed
    store2.occurrences[ac.occurrence_key(sid, late["id"], date(2026, 9, 25))]["status"] = "playing"; store2.save()
    store3 = ac.AlarmStore(store2.path)
    assert store3.occurrences[ac.occurrence_key(sid, late["id"], date(2026, 9, 25))]["status"] == "missed"


def test_dst_gap_is_missed_not_moved():
    if not hasattr(time, "tzset"):
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"; time.tzset()
    try:
        gap = datetime(2026, 3, 8, 2, 30)          # 02:30 does not exist on 8 Mar 2026 in New York
        assert not ac.local_time_exists(gap)
        assert ac.local_time_exists(datetime(2026, 3, 8, 3, 30))
        assert ac.local_time_exists(datetime(2026, 11, 1, 1, 30))   # repeated hour exists (the key is per date, so it plays once)
        store, tmp = fresh()
        s = ac.new_schedule(); s.update(name="X", days=list(ac.EVERY_DAY), enabled=True)
        ev = ac.new_event("02:30"); ev.update(label="Gap", sound=wav(tmp)); s["events"] = [ev]; store.upsert_schedule(s)
        q = queue.Queue(); sch = ac.Scheduler(store, q)
        sch.tick(datetime(2026, 3, 8, 3, 0, 5))
        occ = store.occurrences[ac.occurrence_key(s["id"], ev["id"], date(2026, 3, 8))]
        assert all(q.get_nowait()[0] == "occurrence" for _ in range(q.qsize()))     # a notice, never a play request
        assert occ["status"] == "missed" and "did not exist" in occ["note"]
        assert store.next_announcement(datetime(2026, 3, 7, 12, 0))[0] == datetime(2026, 3, 9, 2, 30)  # skips the gap day
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_migration_and_roundtrip():
    tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "alarms.json")
    a = ac.new_alarm(); a.update(label="Old", sound="/music/x.mp3", last_fired="2026-09-20T07:00:00")
    v1 = {"alarms": [a], "settings": {"snooze_minutes": 9, "keep_awake": False}}
    json.dump(v1, open(path, "w"))
    store = ac.AlarmStore(path)
    assert os.path.exists(path + ".backup-v1")                                   # original kept before upgrading
    assert store.alarms[0] == a and store.settings["snooze_minutes"] == 9 and store.settings["keep_awake"] is False
    assert json.load(open(path)) == v1                                           # nothing rewritten until a save
    s = son(tmp); s["events"][0]["sound"] = ac.portable_sound(os.path.join(ac.BASE_DIR, "recordings", "v.wav"))
    assert s["events"][0]["sound"] == "recordings/v.wav"                         # app-owned recording → portable
    assert ac.portable_sound("/music/x.mp3") == "/music/x.mp3"                   # external file → untouched
    assert ac.resolve_sound("recordings/v.wav") == os.path.join(ac.BASE_DIR, "recordings", "v.wav")
    store.upsert_schedule(s); store.set_skip(date(2099, 1, 1), s["id"], None, True); store.record("k:e:2099-01-01", "played")
    saved = json.load(open(path))
    assert saved["version"] == ac.DATA_VERSION and saved["alarms"][0] == a
    again = ac.AlarmStore(path)
    assert again.schedules == store.schedules and again.exceptions == store.exceptions and again.occurrences == store.occurrences
    assert again.alarms[0]["last_fired"] == "2026-09-20T07:00:00"
    # a corrupt file is kept aside, never overwritten with an empty configuration
    open(path, "w").write("{not json")
    broken = ac.AlarmStore(path)
    assert broken.load_problem and broken.alarms == [] and open(path).read() == "{not json"
    assert any(f.startswith("alarms.json.broken-") for f in os.listdir(tmp))


def test_duplicate_delete_validate():
    store, tmp = fresh()
    s = son(tmp); s["enabled"] = True; store.upsert_schedule(s)
    d = ac.duplicate_schedule(s)
    assert d["id"] != s["id"] and d["name"] == "Son — School day (copy)" and d["enabled"] is False
    assert [e["id"] for e in d["events"]] != [e["id"] for e in s["events"]] and len(set(e["id"] for e in d["events"])) == 3
    assert [e["sound"] for e in d["events"]] == [e["sound"] for e in s["events"]]   # shares the audio files
    store.upsert_schedule(d)
    assert store.sound_users(s["events"][0]["sound"]) == 2
    store.delete_schedule(d["id"])
    assert store.get_schedule(d["id"]) is None and store.get_schedule(s["id"]) is not None
    assert os.path.isfile(s["events"][0]["sound"]) and store.sound_users(s["events"][0]["sound"]) == 1
    assert ac.validate_schedule(s) == {}
    bad = ac.duplicate_schedule(s); bad["name"] = " "; bad["days"] = []; bad["events"][0]["time"] = "25:00"; bad["events"][1]["label"] = ""
    errs = ac.validate_schedule(bad)
    assert set(errs) == {"name", "days", f"event:{bad['events'][0]['id']}", f"event:{bad['events'][1]['id']}"}
    assert ac.new_schedule()["enabled"] is False                                 # new schedules start switched off


def test_queue_collisions_expiry_cancel_routing():
    store, tmp = fresh()
    s1 = son(tmp); s2 = ac.duplicate_schedule(s1); s2.update(name="Daughter", enabled=True, output="Girls room")
    s2["events"][0]["time"] = "07:00"                                          # same minute as the son's wake-up
    store.upsert_schedule(s1); store.upsert_schedule(s2)
    q = queue.Queue(); sch = ac.Scheduler(store, q)
    sch.tick(datetime(2026, 9, 21, 7, 0, 0))
    aq = ac.AnnouncementQueue()
    while not q.empty():
        aq.push(q.get_nowait()[1])
    assert len(aq) == 2
    aq.push(aq.pending[0]); assert len(aq) == 2                                  # no duplicates
    first = aq.pop_playable(datetime(2026, 9, 21, 7, 0, 1), store)
    assert first["schedule"]["name"] == "Daughter" and first["path"].endswith("wake.wav")   # deterministic: name order
    assert first["schedule"]["output"] == "Girls room"                          # each message routes to its own speaker
    second = aq.pop_playable(datetime(2026, 9, 21, 7, 0, 30), store)
    assert second["schedule"]["output"] == "Kids room"
    # a message that waited longer than the limit is missed, then the queue is empty
    sch.tick(datetime(2026, 9, 21, 7, 20, 0)); aq.push(q.get_nowait()[1]); aq.push(q.get_nowait()[1])
    assert aq.pop_playable(datetime(2026, 9, 21, 7, 23), store) is None
    k = ac.occurrence_key(s1["id"], s1["events"][1]["id"], date(2026, 9, 21))
    assert store.occurrences[k]["status"] == "missed" and len(aq) == 0
    # cancelling one schedule leaves the other's messages queued
    store2, tmp2 = fresh(); s1 = son(tmp2); s2 = ac.duplicate_schedule(s1); s2["enabled"] = True
    store2.upsert_schedule(s1); store2.upsert_schedule(s2)
    q = queue.Queue(); sch = ac.Scheduler(store2, q); sch.tick(datetime(2026, 9, 21, 7, 0, 0))
    aq = ac.AnnouncementQueue()
    while not q.empty():
        aq.push(q.get_nowait()[1])
    gone = aq.cancel(sid=s1["id"])
    assert len(gone) == 1 and len(aq) == 1 and aq.pending[0]["schedule"]["id"] == s2["id"]
    # missing sound file → failed, not played, and the next one still plays
    os.remove(aq.pending[0]["path"] if "path" in aq.pending[0] else ac.resolve_sound(aq.pending[0]["event"]["sound"]))
    assert aq.pop_playable(datetime(2026, 9, 21, 7, 0, 5), store2) is None
    kk = ac.occurrence_key(s2["id"], s2["events"][0]["id"], date(2026, 9, 21))
    assert store2.occurrences[kk]["status"] == "failed"


if __name__ == "__main__":
    for t in (test_days_and_next, test_enable_disable_skip_undo_expiry, test_scheduler_dispatch_lateness_restart,
              test_dst_gap_is_missed_not_moved, test_migration_and_roundtrip, test_duplicate_delete_validate,
              test_queue_collisions_expiry_cancel_routing):
        t()
    print("OK")
