"""Headless tests for scheduling logic.  Run: .venv/bin/python tests/test_logic.py"""
import os, sys, queue, tempfile
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alarm_clock as ac


def test_next_fire():
    now = datetime(2026, 9, 23, 21, 30)
    a = ac.new_alarm(); a.update(date="2026-09-24", time="07:00", repeat="once")
    assert ac.next_fire(a, now) == datetime(2026, 9, 24, 7, 0)
    a["last_fired"] = "2026-09-24T07:00:00"
    assert ac.next_fire(a, now) is None
    d = ac.new_alarm(); d.update(date="2026-09-20", time="07:00", repeat="daily")
    assert ac.next_fire(d, now) == datetime(2026, 9, 24, 7, 0)
    assert ac.next_fire(d, datetime(2026, 9, 24, 7, 10)) == datetime(2026, 9, 24, 7, 0)   # within grace
    assert ac.next_fire(d, datetime(2026, 9, 24, 8, 10)) == datetime(2026, 9, 25, 7, 0)   # beyond grace
    d["last_fired"] = "2026-09-24T07:00:00"
    assert ac.next_fire(d, datetime(2026, 9, 24, 7, 10)) == datetime(2026, 9, 25, 7, 0)   # already rang
    w = ac.new_alarm(); w.update(date="2026-09-25", time="06:30", repeat="weekdays")      # Friday
    assert ac.next_fire(w, datetime(2026, 9, 25, 7, 0)) == datetime(2026, 9, 25, 6, 30)
    assert ac.next_fire(w, datetime(2026, 9, 25, 8, 0)) == datetime(2026, 9, 28, 6, 30)   # Monday
    assert ac.next_round_hour(datetime(2026, 9, 23, 21, 36)) == datetime(2026, 9, 23, 22, 0)
    assert ac.next_round_hour(datetime(2026, 9, 23, 23, 5)) == datetime(2026, 9, 24, 0, 0)


def test_alarm_output_roundtrip():
    tmp = tempfile.mkdtemp()
    store = ac.AlarmStore(os.path.join(tmp, "alarms.json"))
    a = ac.new_alarm({"last_output": "Bedroom Speakers"}); assert a["output"] == "Bedroom Speakers"
    store.upsert(a)
    again = ac.AlarmStore(store.path)
    assert again.alarms[0]["output"] == "Bedroom Speakers"
    assert ac.new_alarm()["output"] == ""                     # default = system output
    old = dict(a); del old["output"]                          # alarms.json from before this feature
    assert old.get("output", "") == ""


def test_scheduler():
    tmp = tempfile.mkdtemp()
    store = ac.AlarmStore(os.path.join(tmp, "alarms.json"))
    snd = os.path.join(tmp, "x.wav"); open(snd, "wb").close()
    o = ac.new_alarm(); o.update(date="2026-09-24", time="07:00", repeat="once", sound=snd); store.upsert(o)
    q = queue.Queue(); s = ac.Scheduler(store, q)
    s.tick(datetime(2026, 9, 24, 6, 59, 59)); assert q.empty()
    s.tick(datetime(2026, 9, 24, 7, 0, 0)); kind, al, _ = q.get_nowait()
    assert kind == "ring" and not al["enabled"]
    s.tick(datetime(2026, 9, 24, 7, 0, 1)); assert q.empty()                 # no double fire
    m = ac.new_alarm(); m.update(date="2026-09-24", time="07:00", repeat="once", sound=snd); store.upsert(m)
    s.tick(datetime(2026, 9, 24, 9, 0)); assert q.get_nowait()[0] == "missed"
    s.snooze(o, 5)
    s.tick(datetime.now() + timedelta(minutes=4)); assert q.empty()
    s.ringing.clear()
    s.tick(datetime.now() + timedelta(minutes=6)); assert q.get_nowait()[0] == "ring"
    assert store.next_event() is None or store.next_event()[1]["id"] not in (o["id"], m["id"])


if __name__ == "__main__":
    test_next_fire(); test_alarm_output_roundtrip(); test_scheduler(); print("OK")
