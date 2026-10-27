"""HTTP API tests for the console server (real server on a free port, real engine)."""
import json
import threading
import time
import urllib.error
import urllib.request as U
from http.server import ThreadingHTTPServer

import pytest

from engine import Engine, Settings
from server import make_handler


@pytest.fixture(scope="module")
def base():
    eng = Engine(Settings(month=5, nights=1, seed=1))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    for _ in range(100):                              # wait for the warm-up fast-forward
        if not get(url, "/api/state")["job"]:
            break
        time.sleep(0.2)
    yield url, eng
    httpd.shutdown()
    eng.stop()


def get(url, path):
    return json.loads(U.urlopen(url + path, timeout=10).read())


def post(url, obj, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode()
    req = U.Request(url + "/api/cmd", data=data, headers={"Content-Type": "application/json"})
    try:
        r = U.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_and_info(base):
    url, _ = base
    assert b"Duet" in U.urlopen(url + "/").read()
    info = get(url, "/api/info")
    assert info["firmware"].startswith("duet-fw") and "5" in info["months"]


def test_state_is_incremental_per_client(base):
    url, eng = base
    s = get(url, "/api/state")
    assert s["hist_duet"] and s["calendar"]["month_name"] == "May"
    cur = f"since={s['hist_duet'][-1][0]}&ev={s['ev_next']}&gen={s['gen']}"
    eng.command({"cmd": "speed", "value": 3600})
    time.sleep(1.0)
    a, b = get(url, "/api/state?" + cur), get(url, "/api/state?" + cur)
    # two tabs with the same cursor both receive the new rows (no stealing)
    assert a["hist_duet"] and b["hist_duet"]
    assert a["hist_duet"][0] == b["hist_duet"][0]
    assert all(r[0] > s["hist_duet"][-1][0] for r in a["hist_duet"])


def test_commands_validate_input(base):
    url, _ = base
    assert post(url, {"cmd": "nope"})[0] == 400
    assert post(url, {"cmd": "fault", "name": "nope"})[0] == 400
    assert post(url, None, raw=b"not json")[0] == 400
    assert post(url, None, raw=b"[1,2]")[0] == 400
    assert post(url, {"cmd": "speed", "value": 600}) == (200, {"ok": True})
    assert post(url, {"cmd": "fault", "name": "sensor_crc"}) == (200, {"ok": True})


def test_body_size_limit(base):
    url, _ = base
    assert post(url, None, raw=b"x" * (70 * 1024))[0] == 413


def test_unknown_paths_and_figure_whitelist(base):
    url, _ = base
    for path in ("/nope", "/figures/../README.md", "/figures/secret.png"):
        with pytest.raises(urllib.error.HTTPError) as e:
            U.urlopen(url + path)
        assert e.value.code == 404


def test_jump_back_replays_and_resets_cursors(base):
    url, eng = base
    s0 = get(url, "/api/state")
    assert post(url, {"cmd": "jump", "to": 86400 + 22 * 3600})[0] == 200
    for _ in range(100):
        s = get(url, f"/api/state?gen={s0['gen']}")
        if not s["job"]:
            break
        time.sleep(0.2)
    assert s["gen"] > s0["gen"] and s["fresh"]
    assert abs(s["t"] - (86400 + 22 * 3600)) < 3600


def test_configure_switches_month(base):
    url, _ = base
    assert post(url, {"cmd": "configure", "settings": {"month": 8, "nights": 1, "seed": 2, "start_day": 5}})[0] == 200
    s = get(url, "/api/state")
    assert s["calendar"] == {"month": 8, "month_name": "August", "start_day": 5}
    assert "August" in s["weather"]["label"]


def test_settings_are_clamped():
    s = Settings.from_dict({"month": 99, "nights": 50, "ir_loss": 3, "baseline": "evil", "occupants": 9})
    assert (s.month, s.nights, s.ir_loss, s.baseline, s.occupants) == (10, 7, 0.5, "b2", 2)
