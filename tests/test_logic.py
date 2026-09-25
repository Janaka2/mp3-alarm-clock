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


def test_play_mode():
    # default is the classic alarm; the last saved mode is remembered for the next new alarm
    assert ac.new_alarm()["mode"] == "alarm"
    assert ac.new_alarm(dict(ac.DEFAULT_SETTINGS, last_mode="play"))["mode"] == "play"
    assert ac.new_alarm(dict(ac.DEFAULT_SETTINGS, last_mode="alarm"))["mode"] == "alarm"


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


def test_links():
    """Web links (YouTube / SoundCloud): detection, wording and cache names – no network needed."""
    assert ac.is_link("https://www.youtube.com/watch?v=abc") and ac.is_link("youtu.be/abc") and ac.is_link(" www.soundcloud.com/x ")
    assert not ac.is_link("") and not ac.is_link("/Users/me/song.mp3") and not ac.is_link("wake up")
    assert ac.clean_link(" youtu.be/abc ") == "https://youtu.be/abc"
    assert ac.clean_link("https://x.y/z") == "https://x.y/z"
    assert ac.valid_link(" youtu.be/abc ") == "https://youtu.be/abc"
    assert ac.valid_link("not a link") == "" and ac.valid_link("https://nodot/x") == "" and ac.valid_link("") == ""
    assert "unavailable" in "this video is unavailable" and "private or has been removed" in ac.explain_link_error("ERROR: [youtube] a: This video is unavailable", "YouTube")
    assert ac.link_site("https://youtu.be/abc") == "YouTube"
    assert ac.link_site("https://m.youtube.com/watch?v=1") == "YouTube"
    assert ac.link_site("https://soundcloud.com/nasa/x") == "SoundCloud"
    assert ac.link_site("https://www.example.org/a") == "example.org"
    assert ac.link_stem({"extractor_key": "Youtube", "id": "jNQXAC9IVRw"}) == "youtube_jNQXAC9IVRw"
    assert ac.link_stem({"extractor": "soundcloud:search", "id": "12/3 4"}) == "soundcloud_12_3_4"
    assert ac.check_link_info({"_type": "playlist", "entries": []}).startswith("That link is a whole playlist")
    assert ac.check_link_info({"is_live": True, "formats": [1]}).startswith("That is a live stream")
    assert ac.check_link_info({"duration": 13 * 3600, "formats": [1]}).startswith("That is longer than")
    assert ac.check_link_info({"duration": 120, "formats": [1], "title": "x"}) == ""
    assert "internet connection" in ac.explain_link_error("ERROR: Unable to download webpage: <urlopen error [Errno 8]>", "YouTube")
    assert "private or has been removed" in ac.explain_link_error("ERROR: [youtube] abc: Video unavailable", "YouTube")
    assert "Paste the address of one YouTube video" in ac.explain_link_error("ERROR: Unsupported URL: https://x", "example.org")
    assert "Check that it is the address" in ac.explain_link_error("ERROR: Unsupported URL: https://x", "YouTube")
    assert "out of date" in ac.explain_link_error("ERROR: Unable to extract player; please report this issue", "YouTube")
    assert ac.fmt_duration(19) == "0:19" and ac.fmt_duration(3725) == "1:02:05" and ac.fmt_duration(None) == "0:00"
    # sound_title: link title wins over the file name, missing files are said so
    tmp = tempfile.mkdtemp(); f = os.path.join(tmp, "youtube_x.mp3"); open(f, "wb").write(b"\0" * 10)
    assert ac.sound_title({"sound": f, "link": {"title": "Morning raga", "url": "https://youtu.be/x"}}) == "🔗 Morning raga"
    assert ac.sound_title({"sound": f}) == "youtube_x.mp3"
    assert ac.sound_title({"sound": os.path.join(tmp, "gone.mp3"), "link": {"title": "t"}}) == "Missing file"
    assert ac.sound_title({"sound": ""}) == "No sound"
    # a new alarm remembers where the last sound came from
    a = ac.new_alarm({"last_sound": f, "last_link": {"url": "https://youtu.be/x", "title": "Morning raga", "site": "YouTube", "duration": 19}})
    assert a["link"]["title"] == "Morning raga" and a["sound"] == f
    assert "link" not in ac.new_alarm({"last_sound": f}) and "link" not in ac.new_alarm({"last_sound": "", "last_link": {"url": "u"}})
    store = ac.AlarmStore(os.path.join(tmp, "alarms.json")); store.upsert(a)
    assert ac.AlarmStore(store.path).alarms[0]["link"]["site"] == "YouTube"
    assert store.sound_users(f) == 1


if __name__ == "__main__":
    test_next_fire(); test_alarm_output_roundtrip(); test_normalize(); test_play_mode(); test_scheduler(); test_links(); print("OK")
