"""Closed-loop simulation: twin plant + (Duet firmware | baseline) + user agent.

`Simulation` advances in fixed steps so the console can drive it live and the
experiment runner can run it flat out. An independent `SafetyMonitor` audits
every AC command Duet makes against the five rules, from the outside.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .comfort_truth import pmv_true
from .occupants import DAY, UserAgent, UserParams, nightly_schedule
from .plant import AcParams, Fan, FanParams, IrChannel, Room, RoomParams, RoomState, Sht40, SplitAc
from .psychro import H_FG, rh_from_w, w_from_rh
from .sil import MODES, VERDICTS, DuetFirmware
from .weather import Weather, make_weather

ARMS = {
    # arm: (description, user start setpoint, user fan step, duet?)
    "duet": ("Duet coordinates AC and fan", 24, 0, True),
    "b1": ("AC 24 C, fan off", 24, 0, False),
    "b2": ("AC 24 C, fan medium", 24, 3, False),
    "b3": ("AC 22 C, fan off", 22, 0, False),
}


def arm_spec(arm: str):
    """Named arms, or 'fixed<sp>f<fan>' (e.g. fixed26f2) for calibration runs."""
    if arm in ARMS:
        return ARMS[arm]
    if arm.startswith("fixed"):
        sp, fan = arm[5:].split("f")
        return (f"AC fixed {sp} C, fan {fan}, no user changes", int(sp), int(fan), False)
    raise KeyError(arm)


@dataclass
class Fault:
    kind: str          # sensor_stuck | sensor_drift | sensor_crc | sensor_absent | ir_burst |
                       # humidity_spike | radar_absent | remote_press | remote_press_unseen
    start_s: float
    dur_s: float = 0.0
    value: float = 0.0
    fired: bool = False

    def active(self, t: float) -> bool:
        return self.start_s <= t < self.start_s + max(self.dur_s, 1.0)


@dataclass
class Scenario:
    weather_name: str = "premonsoon_week"
    seed: int = 0
    arm: str = "duet"
    dt: float = 5.0
    ir_loss: float = 0.05
    remote_capture: float = 0.97   # chance Duet's receiver decodes a user remote frame
    device: str = "native"         # native (wired fan) | clip (IR fan)
    occupants: int | None = None   # None = random 1 or 2
    faults: list = field(default_factory=list)
    firmware: dict = field(default_factory=dict)
    epw: str | None = None
    weather: Weather | None = None


class SafetyMonitor:
    """Audits Duet's AC commands from outside the firmware."""

    def __init__(self):
        self.changes: list[tuple[float, int, int]] = []   # (t, from, to)
        self.violations: list[tuple[float, str]] = []
        self.user_hold_until = -1.0

    def user_pressed(self, t: float, power_on: bool):
        self.user_hold_until = t + (15 * 60 if power_on else 2 * 3600) - 1

    def duet_command(self, t: float, prev: int, new: int, rh_seen: float, fault_active_since):
        if new == prev:
            return
        v = self.violations
        if not 24 <= new <= 28:
            v.append((t, f"setpoint {new} outside 24-28"))
        if abs(new - prev) > 1:
            v.append((t, f"step {prev}->{new} larger than 1 C"))
        if self.changes and t - self.changes[-1][0] < 20 * 60 - 1:
            v.append((t, "less than 20 min since last change"))
        if sum(1 for (tc, _, _) in self.changes if t - tc < 3600) >= 3:
            v.append((t, "more than 3 changes in an hour"))
        if new > prev and rh_seen > 60.0:
            v.append((t, f"raise at RH {rh_seen:.1f}%"))
        if t < self.user_hold_until:
            v.append((t, "command during user hold"))
        if fault_active_since is not None and t - fault_active_since > 10 * 60 + 30:
            v.append((t, "AC command while sensor faulty"))
        self.changes.append((t, prev, new))


class Simulation:
    def __init__(self, sc: Scenario):
        import copy
        # Faults carry a `fired` flag; give each run its own copies so a Scenario can be reused.
        sc = copy.copy(sc)
        sc.faults = [copy.copy(f) for f in sc.faults]
        self.sc = sc
        rng = np.random.default_rng(sc.seed)
        self.rng_user = np.random.default_rng(sc.seed + 1000)   # same across arms
        self.rng_dev = np.random.default_rng(sc.seed + 2000)
        self.weather = sc.weather or make_weather(sc.weather_name, sc.seed, sc.epw)
        self.days = int(round((len(self.weather.hours) - 1) / 24))
        desc, sp0, fan0, self.is_duet = arm_spec(sc.arm)
        self.n_people = sc.occupants or int(rng.integers(1, 3))
        nights = nightly_schedule(self.days, self.rng_user)
        up = UserParams(start_setpoint=sp0, fan_step_on_arrival=fan0)
        if sc.arm.startswith("fixed"):
            up.patience_s = 1e12   # calibration arms: nobody touches the remote
        self.user = UserAgent(up, nights, np.random.default_rng(sc.seed + 3000))
        t0_out, rh0, _ = self.weather.at(0)
        self.room = Room(RoomParams(), RoomState(t_air=t0_out - 4, t_mass=t0_out - 3,
                                                 w=w_from_rh(t0_out, rh0)))
        self.ac = SplitAc(AcParams())
        self.fan = Fan(FanParams(), strat_scale=float(rng.uniform(0.7, 1.3)))
        self.sensor = Sht40(np.random.default_rng(sc.seed + 4000))
        self.ir = IrChannel(np.random.default_rng(sc.seed + 5000), sc.ir_loss)
        self.monitor = SafetyMonitor()
        self.fw = None
        if self.is_duet:
            self.fw = DuetFirmware(sc.firmware)
            self.fw.begin(0)
        self.t = 0.0
        self.ac_kwh = 0.0
        self.fan_kwh = 0.0
        self.rows: list[tuple] = []
        self.events: list[tuple[float, str, str]] = []
        self.radar_false_until = -1.0
        self.ir_decisions: list[dict] = []
        self._last_ac_commands = 0
        self._believed_prev = 24
        self._fault_since: float | None = None
        self._pending_remote = None
        self._last_minute = -1
        self._pmv_cache = 0.0
        self.out = None
        self.extra_faults: list[Fault] = []
        self.missed_user_frames = 0
        self.last_ir = None        # (t, delivered, setpoint) of the last AC frame burst
        self.last_fan_cmd = None   # (t, step)

    # ----------------------------------------------------------------- helpers
    def faults_active(self, t: float):
        return [f for f in (*self.sc.faults, *self.extra_faults) if f.active(t)]

    def inject(self, kind: str, dur_s: float = 600, value: float = 0.0):
        """Live fault injection (console)."""
        self.extra_faults.append(Fault(kind, self.t, dur_s, value))
        self.events.append((self.t, "inject", f"{kind} for {dur_s / 60:.0f} min"))

    def _pmv_now(self, asleep: bool) -> float:
        s = self.room.s
        args = (np.array([s.t_air]), np.array([s.rh]), np.array([s.t_mass]), np.array([self.fan.step]))
        if not asleep:
            self.user.update_blanket(self.t, 0.0, 0.0)
            return float(pmv_true(np.array(["evening"]), *args)[0])
        p_sleep = float(pmv_true(np.array(["sleep"]), *args)[0])
        p_blanket = float(pmv_true(np.array(["blanket"]), *args)[0])
        self.user.update_blanket(self.t, p_sleep, p_blanket)
        return p_blanket if self.user.blanket else p_sleep

    def _user_frame(self, power: bool, sp: int, power_on: bool, seen: bool = True):
        """User remote: always reaches the AC; Duet decodes it most of the time."""
        self.ac.s.power = power
        self.ac.s.setpoint = sp
        if self.is_duet:
            self.monitor.user_pressed(self.t, power_on)
            captured = seen and self.rng_dev.random() < self.sc.remote_capture
            if not captured:
                self.missed_user_frames += 1
            if captured:
                self._pending_remote = (1, power, sp, 0)

    # ----------------------------------------------------------------- one step
    def step(self):
        sc, dt, t = self.sc, self.sc.dt, self.t
        t_out, rh_out, ghi = self.weather.at(t)
        w_out = 0.0 if rh_out <= 0 else w_from_rh(t_out, rh_out)
        t_house = self.weather.mean_last_24h(t) + self.room.p.house_offset
        present = self.user.present(t)
        asleep = self.user.asleep(t)
        faults = self.faults_active(t)
        kinds = {f.kind for f in faults}

        # ---- user (evaluated once a minute, plus arrival/leave edges)
        minute = int(t // 60)
        if minute != self._last_minute:
            self._last_minute = minute
            self._pmv_cache = self._pmv_now(asleep) if present else 0.0
            act = self.user.decide(t, 60.0, self._pmv_cache, self.ac.s.power, self.ac.s.setpoint)
            if act:
                if act[0] == "ac_on_arrival":
                    self._user_frame(True, act[2], power_on=True)
                    self.fan.step = self.user.p.fan_step_on_arrival
                    self.events.append((t, "user", f"arrives, AC on at {act[2]} C"))
                elif act[0] == "ac":
                    self._user_frame(act[1], act[2], power_on=False)
                    if not act[1]:
                        if not self.is_duet:
                            self.fan.step = 0   # Duet switches its own fan off when vacant
                        self.events.append((t, "user", "leaves, AC off"))
                    else:
                        self.events.append((t, "user", f"feels {'warm' if self._pmv_cache > 0 else 'cold'}, sets AC {act[2]} C"))

        for f in (*self.sc.faults, *self.extra_faults):
            if f.kind in ("remote_press", "remote_press_unseen") and not f.fired and t >= f.start_s:
                f.fired = True
                if self.ac.s.power:
                    self._user_frame(True, int(f.value), power_on=False,
                                     seen=f.kind == "remote_press")
                    self.events.append((t, "user", f"presses remote: AC {int(f.value)} C"))

        # ---- sensor pod
        t_canopy = self.room.s.t_air + self.fan.stratification()
        rh_canopy = rh_from_w(t_canopy, self.room.s.w)
        self.sensor.fault = ("stuck" if "sensor_stuck" in kinds else "crc" if "sensor_crc" in kinds
                             else "absent" if "sensor_absent" in kinds else None)
        self.sensor.drift = sum(f.value for f in faults if f.kind == "sensor_drift")
        s_t, s_rh, crc_ok, s_present = self.sensor.read(dt, t_canopy, rh_canopy)
        # a slow offset is undetectable by design; only these count for the monitor's rule
        sensor_fault_now = bool(kinds & {"sensor_stuck", "sensor_crc", "sensor_absent"})
        if sensor_fault_now and self._fault_since is None:
            self._fault_since = t
        elif not sensor_fault_now:
            self._fault_since = None

        # radar: occasional false "empty" while someone lies still
        if present and asleep and self.rng_dev.random() < dt / (6 * 3600):
            self.radar_false_until = t + self.rng_dev.uniform(60, 240)
        radar = present and t >= self.radar_false_until and "radar_absent" not in kinds

        # ---- controller
        self.ir.burst_loss = next((f.value for f in faults if f.kind == "ir_burst"), None)
        if self.fw is not None:
            fw = self.fw
            i = fw.inp
            i.now_ms = int(t * 1000)
            i.temp_c, i.rh = s_t, s_rh
            i.crc_ok, i.sensor_present, i.occupied = int(crc_ok), int(s_present), int(radar)
            if self._pending_remote:
                i.remote_kind, i.remote_power, i.remote_setpoint, i.remote_fan_step = self._pending_remote
                self._pending_remote = None
            else:
                i.remote_kind = 0
            o = fw.step()
            self.out = o
            if o.ac_commands != self._last_ac_commands:
                self._last_ac_commands = o.ac_commands
                self.monitor.duet_command(t, self._believed_prev, int(o.ac_setpoint), o.rh_sensor,
                                          self._fault_since)
                self.ir_decisions.append({"t": t, "intended": int(o.ac_setpoint), "ok_first": None,
                                          "ok_final": None})
            if o.ac_frames:
                # each frame is an independent try; the AC acts if any one arrives
                delivered = any([self.ir.delivered() for _ in range(o.ac_frames)])
                self.last_ir = (t, delivered, int(o.ac_setpoint))
                if delivered:
                    self.ac.s.setpoint = int(o.ac_setpoint)
                    self.ac.s.power = bool(o.ac_power)
            if o.fan_cmd:
                self.last_fan_cmd = (t, int(o.fan_step))
                if sc.device == "native" or self.ir.delivered():
                    self.fan.step = int(o.fan_step)
            self._believed_prev = int(o.believed_setpoint)
            for (lt, tag, msg) in fw.logs():
                self.events.append((lt / 1000.0, tag, msg))
            for d in self.ir_decisions[-3:]:
                if d["ok_final"] is not None:
                    continue
                if self.ac.s.setpoint == d["intended"] and d.get("reached") is None:
                    d["reached"] = t
                superseded = d is not self.ir_decisions[-1] or t < self.monitor.user_hold_until
                if t >= d["t"] + 30 and d["ok_first"] is None:
                    d["ok_first"] = d.get("reached") is not None
                if superseded or t >= d["t"] + 25 * 60:
                    d["ok_final"] = d.get("reached") is not None
                    if d["ok_first"] is None:
                        d["ok_first"] = d.get("reached") is not None and d["reached"] <= d["t"] + 30
        # fan off when the room empties (baselines: the user switches it off)
        if not present and not self.is_duet:
            self.fan.step = 0

        # ---- plant
        t_return = self.room.s.t_air + self.ac.p.unit_height_strat * self.fan.stratification()
        q_sens, m_lat, ac_w = self.ac.step(dt, t_return, t_out, self.room.s.t_air, self.room.s.rh)
        fan_w = self.fan.power()
        n = self.n_people if present else 0
        q_people = n * (70.0 if asleep else 80.0)
        m_people = n * (30.0 if asleep else 45.0) / H_FG
        m_spike = sum(f.value for f in faults if f.kind == "humidity_spike")
        self.room.step(dt, t_out, w_out, ghi, t_house, q_sens, m_lat,
                       q_people + fan_w + 15.0 * present, m_people + m_spike)
        self.ac_kwh += ac_w * dt / 3.6e6
        self.fan_kwh += fan_w * dt / 3.6e6

        # ---- record once a minute
        if int((t + dt) // 60) != minute:
            o = self.out
            self.rows.append((
                t, t_out, rh_out, self.room.s.t_air, self.room.s.t_mass, self.room.s.rh,
                s_t, int(self.ac.s.power), self.ac.s.setpoint, ac_w, self.fan.step, fan_w,
                int(present), int(asleep), self.ac_kwh, self.fan_kwh,
                (MODES[o.mode] if o else ""), (o.pmv_est if o else np.nan),
                (o.believed_setpoint if o else np.nan), (o.target_pmv if o else np.nan),
                int(self.user.blanket),
            ))
        self.t = t + dt

    def run(self, until_s: float | None = None):
        end = until_s if until_s is not None else self.days * DAY
        while self.t < end:
            self.step()
        return self

    # ----------------------------------------------------------------- results
    COLS = ["t", "t_out", "rh_out", "t_air", "t_mass", "rh", "t_sensor", "ac_power", "ac_sp",
            "ac_w", "fan_step", "fan_w", "present", "asleep", "ac_kwh", "fan_kwh", "mode",
            "pmv_est", "believed_sp", "target_pmv", "blanket"]

    def frame(self):
        import pandas as pd
        df = pd.DataFrame(self.rows, columns=self.COLS)
        prof = np.where(df.asleep.values == 1, np.where(df.blanket.values == 1, "blanket", "sleep"), "evening")
        df["pmv"] = pmv_true(prof, df.t_air.values, df.rh.values, df.t_mass.values,
                             df.fan_step.values.astype(int))
        return df

    def metrics(self, df=None, warmup_days: int = 1) -> dict:
        df = self.frame() if df is None else df
        m = df[df.t >= warmup_days * DAY]
        occ = m[m.present == 1]
        dt_h = 1 / 60
        ac_kwh = float(m.ac_w.sum() * dt_h / 1000)
        fan_kwh = float(m.fan_w.sum() * dt_h / 1000)
        nights = max(1, self.days - warmup_days)
        dec = [d for d in self.ir_decisions if d["ok_final"] is not None and d["ok_first"] is not None]
        return {
            "arm": self.sc.arm,
            "weather": self.sc.weather_name,
            "seed": self.sc.seed,
            "people": self.n_people,
            "nights": nights,
            "ac_kwh": ac_kwh,
            "fan_kwh": fan_kwh,
            "total_kwh": ac_kwh + fan_kwh,
            "ac_kwh_per_night": ac_kwh / nights,
            "comfort_in_band": float((occ.pmv.abs() <= 0.5).mean()) if len(occ) else np.nan,
            "warm_degree_hours": float(np.clip(occ.pmv - 0.5, 0, None).sum() * dt_h),
            "cool_degree_hours": float(np.clip(-0.5 - occ.pmv, 0, None).sum() * dt_h),
            "mean_pmv": float(occ.pmv.mean()) if len(occ) else np.nan,
            "mean_setpoint": float(occ[occ.ac_power == 1].ac_sp.mean()) if len(occ) else np.nan,
            "user_presses": self.user.presses,
            "duet_ac_commands": len(self.ir_decisions),
            "ir_ok_first": float(np.mean([d["ok_first"] for d in dec])) if dec else np.nan,
            "ir_ok_final": float(np.mean([d["ok_final"] for d in dec])) if dec else np.nan,
            "safety_violations": len(self.monitor.violations),
            "mean_rh_occupied": float(occ.rh.mean()) if len(occ) else np.nan,
            "missed_user_frames": self.missed_user_frames,
            "inferred_user_changes": int(self.out.missed_remote) if self.out is not None else 0,
            "blanket_share_of_sleep": float(m[m.asleep == 1].blanket.mean()) if (m.asleep == 1).any() else np.nan,
        }
