"""Duet engineer console: live A/B twin with fault injection.

Runs two rooms in lockstep on the same weather, occupants and seed:
  left  = a baseline (normal room), right = Duet running the real firmware (SIL).
Streams state to the browser over a WebSocket; the page can change speed,
inject faults, press the user's remote and move the preference slider.
Optionally publishes Duet's telemetry to an MQTT broker with Home Assistant
discovery (--mqtt localhost).

Run:  python console/server.py  then open http://localhost:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from duet_twin import Fault, Scenario, Simulation  # noqa: E402
from duet_twin.comfort_truth import pmv_true  # noqa: E402
from duet_twin.sil import MODES, VERDICTS, DuetFirmware  # noqa: E402

DAY = 86400
START_AT = DAY + 20.5 * 3600   # fast-forward to 20:30 on night 1

FAULTS = {
    # name: (kind, minutes, value, applies_to_baseline_too)
    "sensor_stuck": ("sensor_stuck", 30, 0.0, False),
    "sensor_crc": ("sensor_crc", 15, 0.0, False),
    "ir_burst": ("ir_burst", 40, 1.0, False),
    "humidity_spike": ("humidity_spike", 90, 1.2e-4, True),
    "radar_absent": ("radar_absent", 30, 0.0, False),
}


class Twin:
    def __init__(self, weather="premonsoon_week", seed=3, baseline="b2"):
        self.weather_name, self.seed, self.baseline = weather, seed, baseline
        self.base = Simulation(Scenario(weather_name=weather, seed=seed, arm=baseline, occupants=1))
        self.duet = Simulation(Scenario(weather_name=weather, seed=seed, arm="duet", occupants=1))
        self.speed = 600.0
        self.paused = False
        self._acc = 0.0
        self._sent_rows = {"base": 0, "duet": 0}
        self._sent_events = 0
        self.base.run(START_AT)
        self.duet.run(START_AT)
        self._sent_rows = {"base": len(self.base.rows), "duet": len(self.duet.rows)}
        # keep history from 12 h before the start for the charts
        self._hist_from = START_AT - 12 * 3600
        self._sent_events = len(self.duet.events)

    @property
    def end(self) -> float:
        return self.duet.days * DAY - 60

    def advance(self, wall_dt: float):
        if self.paused or self.duet.t >= self.end:
            return
        self._acc += wall_dt * self.speed
        dt = self.duet.sc.dt
        n = int(self._acc // dt)
        self._acc -= n * dt
        for _ in range(min(n, 4000)):
            self.base.step()
            self.duet.step()

    def inject(self, name: str):
        kind, minutes, value, both = FAULTS[name]
        self.duet.inject(kind, minutes * 60, value)
        if both:
            self.base.inject(kind, minutes * 60, value)

    def remote(self, setpoint: int):
        for sim in (self.base, self.duet):
            if sim.ac.s.power:
                sim.extra_faults.append(Fault("remote_press", sim.t + 1, 0, setpoint))

    def preference(self, value: float):
        if self.duet.fw:
            self.duet.fw.set("preference", value)

    # ------------------------------------------------------------------ snapshot
    @staticmethod
    def _room(sim: Simulation) -> dict:
        s = sim.room.s
        rows = sim.rows
        r = rows[-1] if rows else None
        ac_w = r[9] if r else 0.0
        present = sim.user.present(sim.t)
        d = {
            "t_air": round(s.t_air, 2), "rh": round(s.rh, 1), "t_mass": round(s.t_mass, 2),
            "ac_power": bool(sim.ac.s.power), "ac_sp": sim.ac.s.setpoint, "ac_w": round(ac_w, 1),
            "fan_step": sim.fan.step, "fan_w": round(sim.fan.power(), 1),
            "ac_kwh": round(sim.ac_kwh, 3), "fan_kwh": round(sim.fan_kwh, 3),
            "present": present, "asleep": sim.user.asleep(sim.t),
            "pmv": round(sim._pmv_cache, 2) if present else None,
            "presses": sim.user.presses, "blanket": bool(sim.user.blanket),
        }
        if sim.out is not None:
            o = sim.out
            d.update({
                "mode": MODES[o.mode], "pmv_est": round(o.pmv_est, 2), "target": round(o.target_pmv, 2),
                "believed_sp": int(o.believed_setpoint), "sensor_ok": bool(o.sensor_ok),
                "t_sensor": round(o.t_sensor, 2), "rh_sensor": round(o.rh_sensor, 1),
                "verdict": VERDICTS[o.last_verdict], "commands": int(o.ac_commands),
                "frames": int(o.ac_frames_total), "resends": int(o.ac_resends + o.reconciles),
                "violations": len(sim.monitor.violations),
            })
        return d

    def _new_history(self, key: str, sim: Simulation) -> list:
        rows = sim.rows[self._sent_rows[key]:]
        self._sent_rows[key] = len(sim.rows)
        return self._hist(rows, sim)

    @staticmethod
    def _hist(rows, sim) -> list:
        if not rows:
            return []
        a = np.array([[r[0], r[3], r[4], r[5], r[8], r[9], r[10], r[12], r[13], r[7], r[20]] for r in rows], dtype=float)
        prof = np.where(a[:, 8] == 1, np.where(a[:, 10] == 1, "blanket", "sleep"), "evening")
        pmv = pmv_true(prof, a[:, 1], a[:, 3], a[:, 2], a[:, 6].astype(int))
        out = []
        for i, r in enumerate(a):
            out.append([int(r[0]), round(r[1], 2), int(r[4]) if r[9] else None, round(r[5], 0), int(r[6]),
                        round(float(pmv[i]), 2) if r[7] else None])
        return out

    def snapshot(self, full=False) -> dict:
        t = self.duet.t
        w = self.duet.weather.at(t)
        if full:
            hb = self._hist([r for r in self.base.rows if r[0] >= self._hist_from], self.base)
            hd = self._hist([r for r in self.duet.rows if r[0] >= self._hist_from], self.duet)
        else:
            hb = self._new_history("base", self.base)
            hd = self._new_history("duet", self.duet)
        ev = self.duet.events[self._sent_events:] if not full else self.duet.events[-60:]
        self._sent_events = len(self.duet.events)
        return {
            "type": "full" if full else "tick",
            "t": t, "speed": self.speed, "paused": self.paused,
            "weather": {"label": self.duet.weather.label, "t_out": round(w[0], 1), "rh_out": round(w[1], 0)},
            "baseline": {"arm": self.baseline, "label": {"b1": "AC 24 °C, fan off", "b2": "AC 24 °C, fan medium",
                                                         "b3": "AC 22 °C, fan off"}[self.baseline]},
            "base": self._room(self.base), "duet": self._room(self.duet),
            "hist_base": hb, "hist_duet": hd,
            "events": [[round(e[0]), e[1], e[2]] for e in ev][-80:],
            "seed": self.seed, "fw": DuetFirmware.version(),
        }


app = FastAPI(title="Duet console")
STATE: dict = {"twin": None, "mqtt": None}


@app.get("/")
def index():
    return FileResponse(ROOT / "console" / "index.html")


@app.get("/api/comfort")
def comfort():
    return JSONResponse(json.loads((ROOT / "comfort" / "out" / "comfort_tables.json").read_text()))


@app.get("/api/results")
def results():
    out = {}
    for name in ("summary", "calibration_checks", "faults"):
        p = ROOT / "experiments" / "out" / f"{name}.csv"
        if p.exists():
            import pandas as pd
            out[name] = pd.read_csv(p).to_dict(orient="records")
    return JSONResponse(out)


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    twin: Twin = STATE["twin"]
    await sock.send_text(json.dumps(twin.snapshot(full=True)))
    last = time.perf_counter()

    async def reader():
        nonlocal twin
        while True:
            msg = json.loads(await sock.receive_text())
            c = msg.get("cmd")
            if c == "speed":
                twin.speed = float(msg["value"])
            elif c == "pause":
                twin.paused = not twin.paused
            elif c == "fault":
                twin.inject(msg["name"])
            elif c == "remote":
                twin.remote(int(msg["setpoint"]))
            elif c == "preference":
                twin.preference(float(msg["value"]))
            elif c == "reset":
                twin = STATE["twin"] = Twin(msg.get("weather", "premonsoon_week"), int(msg.get("seed", 3)),
                                            msg.get("baseline", "b2"))
                await sock.send_text(json.dumps(twin.snapshot(full=True)))

    task = asyncio.create_task(reader())
    try:
        while True:
            await asyncio.sleep(0.1)
            now = time.perf_counter()
            twin = STATE["twin"]
            twin.advance(now - last)
            last = now
            snap = twin.snapshot()
            await sock.send_text(json.dumps(snap))
            if STATE["mqtt"]:
                STATE["mqtt"].publish(snap)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task.cancel()


class MqttBridge:
    """Publishes Duet telemetry to MQTT with Home Assistant discovery (ADR-5)."""

    def __init__(self, host: str, port: int = 1883, room: str = "bedroom"):
        import paho.mqtt.client as mqtt
        self.room = room
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"duet-{room}")
        self.c.connect(host, port)
        self.c.loop_start()
        dev = {"identifiers": [f"duet_{room}"], "name": f"Duet {room}", "manufacturer": "Team Edision",
               "model": "Duet Native (SIL)"}
        st = f"duet/{room}/state"
        cfgs = {
            f"homeassistant/sensor/duet_{room}_t/config": {"name": "Room temperature", "state_topic": st,
                "value_template": "{{ value_json.t_sensor }}", "unit_of_measurement": "°C",
                "device_class": "temperature", "unique_id": f"duet_{room}_t", "device": dev},
            f"homeassistant/sensor/duet_{room}_rh/config": {"name": "Room humidity", "state_topic": st,
                "value_template": "{{ value_json.rh_sensor }}", "unit_of_measurement": "%",
                "device_class": "humidity", "unique_id": f"duet_{room}_rh", "device": dev},
            f"homeassistant/sensor/duet_{room}_sp/config": {"name": "AC setpoint", "state_topic": st,
                "value_template": "{{ value_json.believed_sp }}", "unit_of_measurement": "°C",
                "unique_id": f"duet_{room}_sp", "device": dev},
            f"homeassistant/sensor/duet_{room}_fan/config": {"name": "Fan step", "state_topic": st,
                "value_template": "{{ value_json.fan_step }}", "unique_id": f"duet_{room}_fan", "device": dev},
            f"homeassistant/sensor/duet_{room}_mode/config": {"name": "Duet mode", "state_topic": st,
                "value_template": "{{ value_json.mode }}", "unique_id": f"duet_{room}_mode", "device": dev},
            f"homeassistant/binary_sensor/duet_{room}_occ/config": {"name": "Occupancy", "state_topic": st,
                "value_template": "{{ 'ON' if value_json.present else 'OFF' }}", "device_class": "occupancy",
                "unique_id": f"duet_{room}_occ", "device": dev},
        }
        for topic, payload in cfgs.items():
            self.c.publish(topic, json.dumps(payload), retain=True)
        self._last = 0.0

    def publish(self, snap: dict):
        now = time.time()
        if now - self._last < 1.0:
            return
        self._last = now
        self.c.publish(f"duet/{self.room}/state", json.dumps(snap["duet"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--weather", default="premonsoon_week")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--baseline", default="b2", choices=["b1", "b2", "b3"])
    ap.add_argument("--mqtt", default=None, help="MQTT broker host, e.g. localhost")
    a = ap.parse_args()
    STATE["twin"] = Twin(a.weather, a.seed, a.baseline)
    if a.mqtt:
        STATE["mqtt"] = MqttBridge(a.mqtt)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
