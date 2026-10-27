"""Duet console web server. Standard library only - nothing extra to install.

    python console/server.py            then open http://127.0.0.1:8000
    python run.py                       does the same after checking everything

Routes
    GET  /                      the console page
    GET  /api/info              firmware version, library path, months, faults
    GET  /api/state?since=&ev=&gen=   live snapshot + new chart rows + new log lines
    POST /api/cmd               {"cmd": "speed"|"pause"|"jump"|"jump_hour"|"fault"|"remote"|
                                         "preference"|"configure", ...}
    GET  /api/comfort           the ASHRAE 55 comfort tables the firmware uses
    GET  /api/results           experiment summary tables (CSV -> JSON)
    GET  /figures/<name>.png    report figures from experiments/out/

The browser asks for /api/state about four times a second. Plain HTTP is used on
purpose: it works in every browser and needs no WebSocket library.
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engine, Settings  # noqa: E402

FIGURES = {"fig1_night_trace.png", "fig2_savings.png", "fig3_calibration.png"}
RESULT_FILES = ("summary", "calibration_checks", "faults")
MAX_BODY = 64 * 1024


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def make_handler(engine: Engine, mqtt=None):
    page = (ROOT / "console" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "DuetConsole/0.2"

        def log_message(self, fmt, *args):  # quiet; errors still reach stderr via send_error
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                if u.path == "/api/info":
                    return self._json(engine.info())
                if u.path == "/api/state":
                    since = float(q.get("since", ["-1"])[0])
                    ev = int(q.get("ev", ["0"])[0])
                    gen = int(q.get("gen", ["-1"])[0])
                    snap = engine.snapshot(since, max(0, ev), gen)
                    if mqtt:
                        mqtt.publish(snap)
                    return self._json(snap)
                if u.path == "/api/comfort":
                    p = ROOT / "comfort" / "out" / "comfort_tables.json"
                    return self._send(200, p.read_bytes(), "application/json", cache=True)
                if u.path == "/api/results":
                    out = {}
                    for name in RESULT_FILES:
                        p = ROOT / "experiments" / "out" / f"{name}.csv"
                        if p.exists():
                            out[name] = read_csv(p)
                    return self._json(out)
                if u.path.startswith("/figures/"):
                    name = u.path.rsplit("/", 1)[-1]
                    p = ROOT / "experiments" / "out" / name
                    if name in FIGURES and p.exists():  # whitelist: no path traversal
                        return self._send(200, p.read_bytes(), "image/png", cache=True)
                    return self._json({"error": "figure not found - run experiments first"}, 404)
                return self._json({"error": "not found"}, 404)
            except (ValueError, KeyError) as e:
                return self._json({"error": f"bad request: {e}"}, 400)

        def do_POST(self):
            if urlparse(self.path).path != "/api/cmd":
                return self._json({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json({"error": "empty or too large"}, 413)
            try:
                cmd = json.loads(self.rfile.read(n))
                if not isinstance(cmd, dict):
                    raise ValueError("expected a JSON object")
                res = engine.command(cmd)
            except (ValueError, KeyError, TypeError) as e:
                return self._json({"ok": False, "error": f"bad command: {e}"}, 400)
            return self._json(res, 200 if res.get("ok") else 400)

    return Handler


class MqttBridge:
    """Optional: publish Duet's state to an MQTT broker with Home Assistant discovery."""

    def __init__(self, host: str, port: int = 1883, room: str = "bedroom"):
        import paho.mqtt.client as mqtt  # optional dependency
        self.room = room
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"duet-{room}")
        self.c.connect(host, port)
        self.c.loop_start()
        dev = {"identifiers": [f"duet_{room}"], "name": f"Duet {room}", "manufacturer": "Team Edision",
               "model": "Duet Native (SIL)"}
        st = f"duet/{room}/state"
        sensors = [("t", "Room temperature", "{{ value_json.fw.t_sensor }}", "°C", "temperature"),
                   ("rh", "Room humidity", "{{ value_json.fw.rh_sensor }}", "%", "humidity"),
                   ("sp", "AC setpoint", "{{ value_json.fw.believed_sp }}", "°C", None),
                   ("fan", "Fan step", "{{ value_json.fan_step }}", None, None),
                   ("mode", "Duet mode", "{{ value_json.fw.mode }}", None, None)]
        for key, name, tmpl, unit, cls in sensors:
            cfg = {"name": name, "state_topic": st, "value_template": tmpl,
                   "unique_id": f"duet_{room}_{key}", "device": dev}
            if unit:
                cfg["unit_of_measurement"] = unit
            if cls:
                cfg["device_class"] = cls
            self.c.publish(f"homeassistant/sensor/duet_{room}_{key}/config", json.dumps(cfg), retain=True)
        self._last = 0.0

    def publish(self, snap: dict):
        if time.time() - self._last >= 1.0:
            self._last = time.time()
            self.c.publish(f"duet/{self.room}/state", json.dumps(snap["duet"]))


def free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port between {start} and {start + 19}")


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True,
          settings: Settings | None = None, mqtt_host: str | None = None):
    engine = Engine(settings)
    mqtt = MqttBridge(mqtt_host) if mqtt_host else None
    port = free_port(host, port)
    httpd = ThreadingHTTPServer((host, port), make_handler(engine, mqtt))
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"Duet console running at {url}   (Ctrl+C to stop)")
    print(f"  firmware: {engine.info()['firmware']}  from {engine.info()['library']}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        engine.stop()
        httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="Duet engineer console")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = this computer only (default). 0.0.0.0 exposes the controls to your network.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--month", type=int, default=5)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--baseline", default="b2", choices=["b1", "b2", "b3"])
    ap.add_argument("--mqtt", default=None, help="optional MQTT broker host, e.g. localhost")
    a = ap.parse_args()
    serve(a.host, a.port, not a.no_browser, Settings(month=a.month, seed=a.seed, baseline=a.baseline), a.mqtt)


if __name__ == "__main__":
    main()
