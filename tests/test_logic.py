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
    assert ac.output_label("") == "default"
    assert ac.output_label("airplay:LivingRoom") == "AirPlay: LivingRoom"
    assert ac.output_label("Mac mini Speakers") == "Mac mini Speakers"
    assert ac.AirPlayPlayer._q('He said "hi"') == 'He said \\"hi\\"'


def test_normalize():
    from array import array
    import math
    quiet = array("h", [int(300 * math.sin(i / 10)) + 50 for i in range(4000)])   # ~1 % peak with DC offset
    out, peak, gain = ac.normalize_int16(quiet)
    assert 0.008 < peak < 0.012 and 35 < gain <= 40.0
    assert max(abs(x) for x in out) > 0.8 * 32767 and abs(sum(out) / len(out)) < 200
    loud = array("h", [int(30000 * math.sin(2 * math.pi * i / 100)) for i in range(4000)])   # whole periods: no DC
    out2, peak2, gain2 = ac.normalize_int16(loud)
    assert gain2 == 0.0 and out2 is loud                                       # already fine: untouched
    offset = array("h", [x + 500 for x in loud])                              # loud but with a DC offset
    out3, peak3, gain3 = ac.normalize_int16(offset)
    assert gain3 == 0.0 and abs(sum(out3) / len(out3)) < 64 and max(out3) <= 30001   # offset removed, not attenuated
    silence = array("h", [0] * 1000)
    assert ac.normalize_int16(silence)[1] == 0.0
    assert ac.normalize_int16(array("h"))[1] == 0.0


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
    test_next_fire(); test_alarm_output_roundtrip(); test_normalize(); test_scheduler(); print("OK")
