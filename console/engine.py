"""The console's simulation engine.

One background thread owns two simulated rooms that run side by side on the same
weather, occupant and seed:
    base = a normal room (a baseline the user picks)
    duet = the same room with Duet's real firmware (C++ library, loaded via ctypes)
Browser tabs never touch the simulation directly; they read snapshots and send
commands. Each tab keeps its own cursor, so any number of tabs can watch.
"""
from __future__ import annotations

import bisect
import math
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from duet_twin import Fault, Scenario, Simulation  # noqa: E402
from duet_twin.comfort_truth import pmv_true  # noqa: E402
from duet_twin.sil import MODES, VERDICTS, DuetFirmware  # noqa: E402
from duet_twin.weather import JAIPUR_NORMALS, MONTH_NAMES, jaipur_synthetic, load_epw  # noqa: E402

DAY = 86400
HIST_WINDOW = 12 * 3600
BASELINES = {"b1": "AC 24 °C, fan off", "b2": "AC 24 °C, fan on medium", "b3": "AC 22 °C, fan off"}
FAULTS = {
    # name: (kind, minutes, value, also hits the normal room, label)
    "sensor_stuck": ("sensor_stuck", 30, 0.0, False, "Temperature sensor frozen (30 min)"),
    "sensor_crc": ("sensor_crc", 15, 0.0, False, "Sensor data corrupted (15 min)"),
    "ir_burst": ("ir_burst", 40, 1.0, False, "IR path blocked (40 min)"),
    "humidity_spike": ("humidity_spike", 90, 1.2e-4, True, "Humidity spike, e.g. wet clothes (90 min)"),
    "radar_absent": ("radar_absent", 30, 0.0, False, "Radar loses a still sleeper (30 min)"),
}
SPEEDS = [1, 60, 300, 600, 1800, 3600]


@dataclass
class Settings:
    month: int = 5
    start_day: int = 10          # day of month the run starts (day 0 is warm-up)
    nights: int = 3
    heat: float = 0.0            # heatwave offset added to the month's normals, C
    occupants: int = 1
    baseline: str = "b2"
    device: str = "native"       # native | clip
    ir_loss: float = 0.05
    remote_capture: float = 0.97
    seed: int = 3
    view_from: float = 22.5      # hour on night 1 the view opens at (occupant arrives ~22:00)
    epw: str = ""                # file name in twin/data/ (optional)

    @staticmethod
    def from_dict(d: dict) -> "Settings":
        s = Settings()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
        s.month = min(10, max(3, s.month))
        s.nights = min(7, max(1, s.nights))
        s.start_day = min(28, max(1, s.start_day))
        s.heat = min(5.0, max(-3.0, s.heat))
        s.occupants = 1 if s.occupants < 2 else 2
        s.ir_loss = min(0.5, max(0.0, s.ir_loss))
        s.remote_capture = min(1.0, max(0.5, s.remote_capture))
        s.view_from = min(23.75, max(0.0, s.view_from))
        if s.baseline not in BASELINES:
            s.baseline = "b2"
        if s.device not in ("native", "clip"):
            s.device = "native"
        return s


def epw_files() -> list[str]:
    d = ROOT / "twin" / "data"
    return sorted(p.name for p in d.glob("*.epw")) if d.exists() else []


class PmvCache:
    """True comfort (PMV at the bed) per recorded minute, computed once per row."""

    def __init__(self):
        self.vals: list[float | None] = []

    def update(self, rows):
        n0 = len(self.vals)
        if n0 >= len(rows):
            return
        a = rows[n0:]
        t_air = np.array([r[3] for r in a]); t_rad = np.array([r[4] for r in a])
        rh = np.array([r[5] for r in a]); step = np.array([r[10] for r in a], dtype=int)
        present = np.array([r[12] for r in a]); asleep = np.array([r[13] for r in a])
        blanket = np.array([r[20] for r in a])
        prof = np.where(asleep == 1, np.where(blanket == 1, "blanket", "sleep"), "evening")
        p = pmv_true(prof, t_air, rh, t_rad, step)
        self.vals += [round(float(v), 2) if pr else None for v, pr in zip(p, present)]


class Engine:
    def __init__(self, settings: Settings | None = None):
        self.lock = threading.RLock()
        self.speed = 600.0
        self.paused = False
        self.job: dict | None = None      # fast-forward in progress
        self.error: str | None = None
        self.notice: str | None = None
        self.generation = 0               # bumps on every rebuild; tabs reset their cursors
        self.pref = 0.0                   # app slider value, re-applied after every rebuild
        self._stop = False
        self.configure(settings or Settings())
        self.thread = threading.Thread(target=self._loop, daemon=True, name="duet-engine")
        self.thread.start()

    # ------------------------------------------------------------------ build
    def _weather(self, s: Settings):
        days = s.nights + 1
        if s.epw:
            w = load_epw(ROOT / "twin" / "data" / s.epw, s.month, s.start_day, days)
        else:
            w = jaipur_synthetic(s.month, days, s.seed, s.heat,
                                 label=f"Jaipur, {MONTH_NAMES[s.month]} (synthetic from climate normals)")
        return w

    def _build(self, s: Settings):
        w = self._weather(s)
        common = dict(weather_name=f"m{s.month}", seed=s.seed, occupants=s.occupants,
                      ir_loss=s.ir_loss, remote_capture=s.remote_capture, device=s.device, weather=w)
        base = Simulation(Scenario(arm=s.baseline, **common))
        duet = Simulation(Scenario(arm="duet", **common))
        duet.fw.set("preference", self.pref)
        return base, duet

    def configure(self, s: Settings):
        """Rebuild both rooms for new settings and fast-forward to the view start."""
        with self.lock:
            self.settings = s
            self.base, self.duet = self._build(s)
            self.pmv = {"base": PmvCache(), "duet": PmvCache()}
            self.generation += 1
            self.error = None
            self.notice = None
            self._start_job(DAY + s.view_from * 3600, f"Preparing {MONTH_NAMES[s.month]} scenario")

    def _start_job(self, target: float, label: str):
        self.job = {"target": float(target), "from": self.duet.t, "label": label}

    def jump(self, t_target: float):
        with self.lock:
            end = self.end
            t_target = max(0.0, min(end - 60, float(t_target)))
            if t_target < self.duet.t - 1:
                # The run is deterministic: replay from the start to reach an earlier time.
                faults_lost = bool(self.duet.extra_faults)
                self.base, self.duet = self._build(self.settings)
                self.pmv = {"base": PmvCache(), "duet": PmvCache()}
                self.generation += 1
                self.notice = ("Replayed from the start; faults you injected live were not replayed."
                               if faults_lost else None)
                self._start_job(t_target, "Replaying to an earlier time")
            else:
                self._start_job(t_target, "Skipping ahead")

    @property
    def end(self) -> float:
        return self.duet.days * DAY

    # ------------------------------------------------------------------ loop
    def _loop(self):
        last = time.perf_counter()
        while not self._stop:
            time.sleep(0.04)
            now = time.perf_counter()
            wall = now - last
            last = now
            try:
                with self.lock:
                    if self.job:
                        self._run_job()
                    elif not self.paused and self.duet.t < self.end - 60:
                        n = int(wall * self.speed // self.duet.sc.dt)
                        self._acc = getattr(self, "_acc", 0.0) + wall * self.speed - n * self.duet.sc.dt
                        if self._acc >= self.duet.sc.dt:
                            n += 1
                            self._acc -= self.duet.sc.dt
                        for _ in range(min(n, 1500)):
                            self._step()
                        if self.duet.t >= self.end - 60:
                            self.paused = True
                            self.notice = "Reached the end of the scenario. Pick another start time or restart."
            except Exception as e:  # keep the server alive and tell the UI
                self.error = f"{type(e).__name__}: {e}"
                self.paused = True
                self.job = None

    def _step(self):
        self.base.step()
        self.duet.step()

    def _run_job(self):
        t_end = time.perf_counter() + 0.12   # yield to HTTP requests often
        while self.duet.t < self.job["target"] and time.perf_counter() < t_end:
            for _ in range(60):
                self._step()
        if self.duet.t >= self.job["target"]:
            self.job = None

    def stop(self):
        self._stop = True

    # ------------------------------------------------------------------ commands
    def command(self, c: dict) -> dict:
        kind = c.get("cmd")
        with self.lock:
            if kind == "speed":
                v = float(c.get("value", 600))
                self.speed = min(SPEEDS, key=lambda s: abs(s - v))
            elif kind == "pause":
                self.paused = bool(c.get("value", not self.paused))
            elif kind == "jump":
                self.jump(float(c["to"]))
            elif kind == "jump_hour":  # next occurrence of an hour of day
                h = float(c["hour"]) * 3600
                t = self.duet.t
                target = math.floor(t / DAY) * DAY + h
                if target <= t + 60:
                    target += DAY
                self.jump(target)
            elif kind == "fault":
                name = c.get("name")
                if name not in FAULTS:
                    return {"ok": False, "error": f"unknown fault {name}"}
                kind_, minutes, value, both, _ = FAULTS[name]
                self.duet.inject(kind_, minutes * 60, value)
                if both:
                    self.base.inject(kind_, minutes * 60, value)
            elif kind == "remote":
                sp = int(c["setpoint"])
                if not 16 <= sp <= 30:
                    return {"ok": False, "error": "setpoint must be 16-30"}
                pressed = False
                for sim in (self.base, self.duet):
                    if sim.ac.s.power:
                        sim.extra_faults.append(Fault("remote_press", sim.t + 1, 0, sp))
                        pressed = True
                if not pressed:
                    return {"ok": False, "error": "The AC is off (nobody home) - the remote does nothing now."}
            elif kind == "preference":
                v = max(-0.3, min(0.3, float(c["value"])))
                self.duet.fw.set("preference", v)
                self.pref = v
            elif kind == "configure":
                self.configure(Settings.from_dict(c.get("settings", {})))
            else:
                return {"ok": False, "error": f"unknown command {kind}"}
        return {"ok": True}

    # ------------------------------------------------------------------ snapshots
    @staticmethod
    def _room(sim: Simulation, pmv_now) -> dict:
        s = sim.room.s
        r = sim.rows[-1] if sim.rows else None
        present = sim.user.present(sim.t)
        d = {
            "t_air": round(s.t_air, 2), "rh": round(s.rh, 1), "t_wall": round(s.t_mass, 2),
            "ac_power": bool(sim.ac.s.power), "ac_sp": sim.ac.s.setpoint,
            "ac_w": round(r[9] if r else 0.0, 0), "fan_step": sim.fan.step, "fan_w": round(sim.fan.power(), 1),
            "ac_kwh": round(sim.ac_kwh, 3), "fan_kwh": round(sim.fan_kwh, 3),
            "present": present, "asleep": sim.user.asleep(sim.t), "blanket": bool(sim.user.blanket),
            "pmv": pmv_now if present else None, "presses": sim.user.presses,
        }
        if sim.out is not None:
            o = sim.out
            d["fw"] = {
                "mode": MODES[o.mode], "profile": "sleep" if o.profile == 1 else "evening",
                "pmv_est": round(o.pmv_est, 2), "target": round(o.target_pmv, 2),
                "believed_sp": int(o.believed_setpoint), "sensor_ok": bool(o.sensor_ok),
                "t_sensor": round(o.t_sensor, 2), "rh_sensor": round(o.rh_sensor, 1),
                "t_bed_est": round(o.t_occ, 2), "occupied": bool(o.occupied),
                "verdict": VERDICTS[o.last_verdict], "commands": int(o.ac_commands),
                "frames": int(o.ac_frames_total), "resends": int(o.ac_resends + o.reconciles),
                "inferred_user": int(o.missed_remote), "fan_commands": int(o.fan_commands),
                "rejected": {VERDICTS[i]: int(o.rejected[i]) for i in range(10) if o.rejected[i]},
                "violations": len(sim.monitor.violations),
                "last_ir": sim.last_ir, "last_fan": sim.last_fan_cmd,
            }
        return d

    def _window_metrics(self, key: str, sim: Simulation, t0: float) -> dict:
        rows = sim.rows
        if not rows:
            return {}
        i = bisect.bisect_left(rows, t0, key=lambda r: r[0])
        if i >= len(rows):
            return {"ac_kwh": 0.0, "fan_kwh": 0.0, "comfort": None, "minutes": 0}
        p = self.pmv[key].vals[i:]
        occ = [v for v in p if v is not None]
        start = rows[i - 1] if i > 0 else None
        ac0 = start[14] if start else 0.0
        fan0 = start[15] if start else 0.0
        return {
            "ac_kwh": round(rows[-1][14] - ac0, 3), "fan_kwh": round(rows[-1][15] - fan0, 3),
            "comfort": round(sum(abs(v) <= 0.5 for v in occ) / len(occ), 3) if occ else None,
            "minutes": len(occ),
        }

    def _history(self, key: str, sim: Simulation, since: float) -> list:
        rows = sim.rows
        lo = max(since, sim.t - HIST_WINDOW)
        i = bisect.bisect_right(rows, lo, key=lambda r: r[0])
        p = self.pmv[key].vals
        return [[int(r[0]), round(r[3], 2), (int(r[8]) if r[7] else None), int(r[9]), int(r[10]),
                 p[k], round(r[1], 1), round(r[5], 1), int(r[20])]
                for k, r in enumerate(rows[i:], start=i)]

    def snapshot(self, since: float = -1.0, ev_since: int = 0, gen: int = -1) -> dict:
        with self.lock:
            fresh = gen != self.generation
            if fresh:
                since, ev_since = -1.0, 0
            for k, sim in (("base", self.base), ("duet", self.duet)):
                self.pmv[k].update(sim.rows)
            t = self.duet.t
            tout, rhout, ghi = self.duet.weather.at(min(t, self.end - 1))
            # "tonight" = since the most recent 18:00
            night0 = math.floor((t - 18 * 3600) / DAY) * DAY + 18 * 3600
            run0 = DAY  # day 0 is warm-up
            last_pmv = lambda k: (self.pmv[k].vals[-1] if self.pmv[k].vals else None)
            s = self.settings
            snap = {"pref": self.pref,
                "gen": self.generation, "fresh": fresh, "t": t, "end": self.end,
                "speed": self.speed, "paused": self.paused,
                "job": ({"label": self.job["label"],
                         "progress": round((t - self.job["from"]) / max(1.0, self.job["target"] - self.job["from"]), 3)}
                        if self.job else None),
                "error": self.error, "notice": self.notice,
                "settings": asdict(s),
                "calendar": {"month": s.month, "month_name": MONTH_NAMES[s.month], "start_day": s.start_day},
                "weather": {"label": self.duet.weather.label, "t_out": round(tout, 1),
                            "rh_out": round(rhout, 0), "sun": round(ghi, 0)},
                "baseline": {"arm": s.baseline, "label": BASELINES[s.baseline]},
                "base": self._room(self.base, last_pmv("base")),
                "duet": self._room(self.duet, last_pmv("duet")),
                "tonight": {"from": night0, "base": self._window_metrics("base", self.base, night0),
                            "duet": self._window_metrics("duet", self.duet, night0)},
                "run": {"from": run0, "base": self._window_metrics("base", self.base, run0),
                        "duet": self._window_metrics("duet", self.duet, run0)},
                "hist_base": self._history("base", self.base, since),
                "hist_duet": self._history("duet", self.duet, since),
                "events": [[round(e[0]), e[1], e[2]] for e in self.duet.events[ev_since:]][-150:],
                "ev_next": len(self.duet.events),
                "nights": [[n.arrive, n.sleep, n.wake, n.leave] for n in self.duet.user.nights],
            }
            return snap

    def info(self) -> dict:
        return {
            "firmware": DuetFirmware.version(), "library": DuetFirmware.path(),
            "months": {m: {"name": MONTH_NAMES[m], "max": v[0], "min": v[1], "rh_am": v[2], "rh_pm": v[3]}
                       for m, v in JAIPUR_NORMALS.items()},
            "baselines": BASELINES, "faults": {k: v[4] for k, v in FAULTS.items()},
            "speeds": SPEEDS, "epw": epw_files(), "python": sys.version.split()[0],
        }
