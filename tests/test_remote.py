"""Headless tests for the phone remote web server (bound to 127.0.0.1 only).  Run: .venv/bin/python tests/test_remote.py"""
import http.client, json, math, os, ssl, struct, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alarm_clock as ac

TMP = tempfile.mkdtemp()
ac.LOG_FILE = os.path.join(TMP, "alarmclock.log")
ac.REC_DIR = os.path.join(TMP, "recordings")
ac.CERT_FILE = os.path.join(TMP, "phone-remote-cert.pem")
ac.REMOTE_BIND = "127.0.0.1"
calls = []


def dispatch(action, payload):
    calls.append((action, payload))
    if action == "state":
        return {"host": "mac", "playing": "", "next": "", "today": [], "tomorrow": [], "schedules": [], "https_url": ""}
    if action == "attach":
        return {"ok": True, "note": "attached " + os.path.basename(payload["path"])}
    return {"ok": True, "note": action}


def req(srv, method, path, body=None, headers=None, cookie=""):
    if srv.https:
        c = http.client.HTTPSConnection("127.0.0.1", srv.port, context=ssl._create_unverified_context(), timeout=10)
    else:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = "remote=" + cookie
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def wav_bytes(seconds=1.0, amp=12000):
    rate = 8000
    frames = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * seconds)))
    p = os.path.join(TMP, "tone.wav")
    with wave.open(p, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(frames)
    with open(p, "rb") as f:
        return f.read()


def test_helpers():
    assert ac.make_pin().isdigit() and len(ac.make_pin()) == 6
    assert ac.pin_label("483921") == "483 921"
    assert ac.lan_ip().count(".") == 3


def test_server(https: bool):
    cert = ac.CERT_FILE if https and ac.ensure_cert() else ""
    if https and not cert:
        print("  (openssl missing: https test skipped)")
        return
    srv = ac.RemoteServer(dispatch, "123456", 0, cert)
    assert srv.start(), srv.error
    try:
        assert srv.https == https
        assert srv.url("10.0.0.5").startswith("https://" if https else "http://")
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        # login page without a session; the api is refused
        r, data = req(srv, "GET", "/"); assert r.status == 200 and b"Enter the PIN" in data
        r, data = req(srv, "POST", "/api/state", b"{}", {"Content-Type": "application/json"}); assert r.status == 401
        # wrong pin, then the right one
        r, _ = req(srv, "POST", "/login", b"pin=000000", form); assert r.status == 401
        r, _ = req(srv, "POST", "/login", b"pin=123456", form); assert r.status == 303
        cookie = r.getheader("Set-Cookie"); assert cookie.startswith("remote=") and "HttpOnly" in cookie and ("Secure" in cookie) == https
        token = cookie.split(";")[0].split("=", 1)[1]
        r, data = req(srv, "GET", "/", cookie=token); assert r.status == 200 and b"<title>Alarm Clock</title>" in data and b"Record message" in data
        r, data = req(srv, "POST", "/api/state", b"{}", {"Content-Type": "application/json"}, cookie=token)
        assert r.status == 200 and json.loads(data)["host"] == "mac"
        r, data = req(srv, "POST", "/api/skip", json.dumps({"sid": "s1", "eid": "e1", "day": "tomorrow", "on": True}).encode(), cookie=token)
        assert json.loads(data)["ok"] and calls[-1] == ("skip", {"sid": "s1", "eid": "e1", "day": "tomorrow", "on": True})
        r, data = req(srv, "POST", "/api/whatever", b"{}", cookie=token); assert r.status == 404
        r, data = req(srv, "POST", "/api/stop", b"[1,2]", cookie=token); assert r.status == 400
        r, _ = req(srv, "POST", "/api/stop", b"{}", cookie="nope"); assert r.status == 401      # a made-up token is no session
        # upload a WAV recording: saved under recordings/, normalised, "attach" dispatched, raw upload removed
        wav = wav_bytes()
        r, data = req(srv, "POST", "/api/record", wav, {"Content-Type": "audio/wav", "X-Schedule": "s1", "X-Event": "e1"}, cookie=token)
        j = json.loads(data); assert r.status == 200 and j["ok"], j
        assert calls[-1][0] == "attach" and calls[-1][1]["sid"] == "s1" and calls[-1][1]["peak"] > 0.3
        saved = calls[-1][1]["path"]; assert os.path.isfile(saved) and saved.startswith(ac.REC_DIR) and saved.endswith("_phone.wav")
        with wave.open(saved) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getnframes() == 8000
        assert not [f for f in os.listdir(ac.REC_DIR) if "_upload" in f]
        # empty and malformed uploads
        r, data = req(srv, "POST", "/api/record", b"x", {"Content-Type": "audio/wav", "X-Schedule": "s1", "X-Event": "e1"}, cookie=token)
        assert r.status == 400 and "empty" in json.loads(data)["error"]
        r, data = req(srv, "POST", "/api/record", wav, {"Content-Type": "audio/wav", "X-Schedule": "../x", "X-Event": "e1"}, cookie=token)
        assert r.status == 400
        # five wrong PINs lock the login for a while, even for the right PIN
        for _ in range(5):
            req(srv, "POST", "/login", b"pin=111111", form)
        r, data = req(srv, "POST", "/login", b"pin=123456", form)
        assert r.status == 401 and b"Too many wrong PINs" in data
    finally:
        srv.stop()
    assert not srv.running


if __name__ == "__main__":
    test_helpers(); test_server(False); test_server(True); print("OK")
