"""Physical plant: bedroom, inverter split AC, BLDC ceiling fan, sensor pod, IR link.

Every parameter is a named field so the report can list it. Values marked
ASSUMPTION are ours and get calibrated against field data in calibrate.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .psychro import CP_AIR, H_FG, RHO_AIR, dew_point, rh_from_w, w_from_rh


# --------------------------------------------------------------------------- room
@dataclass
class RoomParams:
    length: float = 3.5
    width: float = 3.5
    height: float = 3.0
    ach: float = 0.5                 # infiltration, air changes per hour (ASSUMPTION)
    window_area: float = 1.5         # m2, U 5.6, SHGC 0.6 (single glazing)
    window_u: float = 5.6
    shgc: float = 0.6
    ext_wall_area: float = 9.0       # one external 230 mm brick wall, net of window
    ext_wall_u: float = 2.0
    int_ua: float = 110.0            # W/K to the rest of the house via partitions, floor, ceiling (ASSUMPTION)
    ua_air_mass: float = 560.0       # W/K, interior surfaces ~70 m2 x 8 W/m2K
    c_air: float = 3.0e5             # J/K, air + furniture fast mass (ASSUMPTION)
    c_mass: float = 8.0e6            # J/K, interior wall/slab mass (ASSUMPTION)
    moisture_buffer: float = 6.0     # effective multiplier on air moisture capacity
    house_offset: float = 1.5        # rest of house runs this much above 24 h mean outdoor

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height

    @property
    def ua_infil(self) -> float:
        return self.ach * self.volume / 3600.0 * RHO_AIR * CP_AIR

    @property
    def ua_window(self) -> float:
        return self.window_area * self.window_u


@dataclass
class RoomState:
    t_air: float = 31.0   # occupied-zone air temperature, C
    t_mass: float = 32.0  # wall/slab node, also used as mean radiant temperature
    w: float = 0.012      # humidity ratio, kg/kg

    @property
    def rh(self) -> float:
        return rh_from_w(self.t_air, self.w)


class Room:
    """2R2C grey-box room (air node + mass node) with a moisture balance."""

    def __init__(self, p: RoomParams, state: RoomState | None = None):
        self.p = p
        self.s = state or RoomState()
        self.m_air = RHO_AIR * p.volume  # kg

    def step(self, dt: float, t_out: float, w_out: float, ghi: float, t_house: float,
             q_ac_sens: float, m_ac_latent: float, q_internal: float, m_internal: float) -> None:
        p, s = self.p, self.s
        # vertical-surface solar ~ 0.35 of horizontal for an E/W wall (rough)
        i_vert = 0.35 * ghi
        q_win_solar = p.shgc * p.window_area * i_vert
        t_solair = t_out + 0.7 * i_vert / 20.0
        q_air = ((p.ua_infil + p.ua_window) * (t_out - s.t_air)
                 + p.ua_air_mass * (s.t_mass - s.t_air)
                 + q_win_solar + q_internal - q_ac_sens)
        q_mass = (p.ext_wall_area * p.ext_wall_u * (t_solair - s.t_mass)
                  + p.int_ua * (t_house - s.t_mass)
                  + p.ua_air_mass * (s.t_air - s.t_mass))
        s.t_air += dt * q_air / p.c_air
        s.t_mass += dt * q_mass / p.c_mass
        m_inf = p.ach * p.volume / 3600.0 * RHO_AIR
        dw = (m_inf * (w_out - s.w) + m_internal - m_ac_latent) / (self.m_air * p.moisture_buffer)
        s.w = max(0.002, s.w + dt * dw)


# --------------------------------------------------------------------------- AC
@dataclass
class AcParams:
    rated_capacity_w: float = 5275.0  # 1.5 ton
    cap_min_frac: float = 0.30
    cap_max_frac: float = 1.10
    cop_rated: float = 3.6            # full load at 35 C outdoor (5-star inverter class)
    cop_out_slope: float = 0.025      # fractional COP loss per C outdoor above 35 C
    cop_in_slope: float = 0.025       # fractional COP gain per C warmer return air
    cop_partload_peak: float = 1.30   # COP multiplier at ~50 % load
    standby_w: float = 3.0            # indoor unit electronics when on but compressor idle
    indoor_fan_w: float = 35.0
    kp: float = 0.6                   # capacity fraction per C of error
    ki: float = 0.6 / 900.0           # integral, 1/(C s)
    unit_height_strat: float = 0.8    # AC return sensor sees this share of ceiling stratification
    coil_approach_full: float = 14.0  # coil runs this far below setpoint at full load
    coil_approach_min: float = 7.0    # and this far at minimum load


@dataclass
class AcState:
    power: bool = False
    setpoint: int = 24
    integ: float = 0.0
    compressor_on: bool = False
    frac: float = 0.0
    last_sp: int = 24


class SplitAc:
    def __init__(self, p: AcParams | None = None):
        self.p = p or AcParams()
        self.s = AcState()

    def cop(self, t_out: float, t_return: float, plr: float) -> float:
        p = self.p
        f_out = 1.0 - p.cop_out_slope * (t_out - 35.0)
        f_in = 1.0 + p.cop_in_slope * (t_return - 27.0)
        # part-load curve: rises to a peak near 50 % then falls toward minimum capacity
        x = min(max(plr, 0.25), 1.1)
        f_pl = 1.0 + (p.cop_partload_peak - 1.0) * (1.0 - ((x - 0.5) / 0.5) ** 2)
        return max(1.2, p.cop_rated * f_out * f_in * max(0.85, f_pl))

    def step(self, dt: float, t_return: float, t_out: float, t_air: float, rh_air: float):
        """Returns (sensible W removed, latent kg/s removed, electric W)."""
        p, s = self.p, self.s
        if not s.power:
            s.integ, s.compressor_on, s.frac = 0.0, False, 0.0
            return 0.0, 0.0, 0.0
        if s.setpoint != s.last_sp:
            s.integ = min(s.integ, 0.0)          # new target: drop the wound-up integral
        s.last_sp = s.setpoint
        err = t_return - s.setpoint
        # anti-windup: the integral alone never asks for more than 60 % or less than -30 %
        s.integ = min(max(s.integ + err * dt, -0.3 / p.ki), 0.6 / p.ki)
        demand = p.kp * err + p.ki * s.integ
        # inverter cycles off below minimum capacity with hysteresis
        if s.compressor_on and demand < p.cap_min_frac * 0.6 and err < -0.5:
            s.compressor_on = False
        elif not s.compressor_on and (err > 0.5 or demand > p.cap_min_frac):
            s.compressor_on = True
        if not s.compressor_on:
            s.frac = 0.0
            return 0.0, 0.0, p.standby_w + p.indoor_fan_w * 0.5
        frac = min(max(demand, p.cap_min_frac), p.cap_max_frac)
        s.frac = frac
        q_total = frac * p.rated_capacity_w
        # latent share: coil temperature vs room dew point
        approach = p.coil_approach_min + (p.coil_approach_full - p.coil_approach_min) * \
            (frac - p.cap_min_frac) / (p.cap_max_frac - p.cap_min_frac)
        t_coil = s.setpoint - approach
        dp = dew_point(t_air, rh_air)
        shr = 1.0 if dp <= t_coil else max(0.62, 1.0 - 0.06 * (dp - t_coil))
        q_sens = q_total * shr
        m_lat = q_total * (1.0 - shr) / H_FG
        elec = q_total / self.cop(t_out, t_return, frac / p.cap_max_frac * 1.1) + p.indoor_fan_w
        return q_sens, m_lat, elec


# --------------------------------------------------------------------------- fan
@dataclass
class FanParams:
    max_w: float = 35.0            # Atomberg 1200 mm BLDC at top speed (product spec)
    electronics_w: float = 4.0     # ASSUMPTION: BLDC driver overhead when running
    steps: int = 6
    # Ceiling-to-bed stratification per step, C (ASSUMPTION; firmware has a calibrated copy)
    strat: tuple = (1.0, 0.6, 0.4, 0.3, 0.2, 0.2, 0.1)


class Fan:
    def __init__(self, p: FanParams | None = None, strat_scale: float = 1.0):
        self.p = p or FanParams()
        self.step = 0
        self.strat_scale = strat_scale

    def power(self) -> float:
        if self.step <= 0:
            return 0.0
        return self.p.electronics_w + (self.p.max_w - self.p.electronics_w) * (self.step / self.p.steps) ** 3

    def stratification(self) -> float:
        return self.p.strat[max(0, min(self.step, 6))] * self.strat_scale


# --------------------------------------------------------------------------- sensor pod
class Sht40:
    """Canopy SHT40: offset, noise, first-order lag, 0.01 quantisation, injected faults."""

    def __init__(self, rng: np.random.Generator, tau_s: float = 60.0):
        self.rng = rng
        self.off_t = float(np.clip(rng.normal(0, 0.1), -0.2, 0.2))
        self.off_rh = float(np.clip(rng.normal(0, 0.9), -1.8, 1.8))
        self.tau = tau_s
        self.t = None
        self.rh = None
        self.fault: str | None = None   # None | 'stuck' | 'drift' | 'crc' | 'absent'
        self.stuck_val = (0.0, 0.0)
        self.drift = 0.0

    def read(self, dt: float, t_true: float, rh_true: float):
        if self.t is None:
            self.t, self.rh = t_true, rh_true
        a = 1.0 - math.exp(-dt / self.tau)
        self.t += a * (t_true - self.t)
        self.rh += a * (rh_true - self.rh)
        t = round(self.t + self.off_t + self.rng.normal(0, 0.03) + self.drift, 2)
        rh = round(min(100.0, max(0.0, self.rh + self.off_rh + self.rng.normal(0, 0.25))), 2)
        crc_ok, present = True, True
        if self.fault == "stuck":
            t, rh = self.stuck_val
        elif self.fault == "crc":
            crc_ok = self.rng.random() > 0.5
        elif self.fault == "absent":
            present = False
        if self.fault != "stuck":
            self.stuck_val = (t, rh)
        return t, rh, crc_ok, present


class IrChannel:
    """Line-of-sight IR link from the pod to the AC (and fan, in Clip mode)."""

    def __init__(self, rng: np.random.Generator, loss: float = 0.05):
        self.rng = rng
        self.loss = loss
        self.burst_loss: float | None = None

    def delivered(self) -> bool:
        p = self.burst_loss if self.burst_loss is not None else self.loss
        return self.rng.random() >= p
