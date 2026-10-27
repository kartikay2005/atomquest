"""Occupants: a nightly schedule and a comfort-driven user who presses the remote.

The same user agent acts in every arm (baselines and Duet), with the same seed,
so differences in remote presses are caused by the controller, not by luck.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DAY = 86400


@dataclass
class Night:
    arrive: float  # s since scenario start
    sleep: float
    wake: float
    leave: float


@dataclass
class UserParams:
    start_setpoint: int = 24       # what they set on the remote when they switch on
    fan_step_on_arrival: int = 0   # 0 = fan left off (only ~20 % always use it with AC)
    warm_threshold_awake: float = 0.7
    warm_threshold_asleep: float = 1.0
    cold_threshold: float = -1.0
    patience_s: float = 600.0      # sustained discomfort before acting
    cooldown_s: float = 1800.0
    lower_by: int = 1
    min_setpoint: int = 18


def nightly_schedule(days: int, rng: np.random.Generator, first_day: int = 1) -> list[Night]:
    """Day 0 is warm-up. Bedroom used ~22:00-07:00 with randomised times."""
    nights = []
    for d in range(first_day, days):
        base = d * DAY
        arrive = base + 22 * 3600 + rng.normal(0, 25 * 60)
        sleep = base + 23 * 3600 + rng.normal(0, 25 * 60)
        wake = base + DAY + 6.5 * 3600 + rng.normal(0, 20 * 60)
        leave = wake + 25 * 60 + abs(rng.normal(0, 10 * 60))
        nights.append(Night(arrive, max(sleep, arrive + 600), wake, leave))
    return nights


class UserAgent:
    """Turns the AC on at arrival, off at leaving, and reacts to sustained discomfort."""

    def __init__(self, p: UserParams, nights: list[Night], rng: np.random.Generator):
        self.p = p
        self.nights = nights
        self.rng = rng
        self.warm_since: float | None = None
        self.cold_since: float | None = None
        self.last_press = -1e9
        self.presses = 0
        self.blanket = False
        self._cold_asleep_since: float | None = None
        self._warm_blanket_since: float | None = None

    def update_blanket(self, t: float, pmv_sleep: float, pmv_blanket: float) -> None:
        """A sleeper who stays cool for 20 min pulls up a blanket; kicks it off if warm."""
        if not self.asleep(t):
            self.blanket = False
            self._cold_asleep_since = self._warm_blanket_since = None
            return
        if not self.blanket:
            self._cold_asleep_since = (self._cold_asleep_since or t) if pmv_sleep < -0.5 else None
            if self._cold_asleep_since is not None and t - self._cold_asleep_since >= 1200:
                self.blanket, self._cold_asleep_since = True, None
        else:
            self._warm_blanket_since = (self._warm_blanket_since or t) if pmv_blanket > 0.5 else None
            if self._warm_blanket_since is not None and t - self._warm_blanket_since >= 1200:
                self.blanket, self._warm_blanket_since = False, None

    def night_at(self, t: float) -> Night | None:
        for n in self.nights:
            if n.arrive <= t < n.leave:
                return n
        return None

    def present(self, t: float) -> bool:
        return self.night_at(t) is not None

    def asleep(self, t: float) -> bool:
        n = self.night_at(t)
        return n is not None and n.sleep <= t < n.wake

    def decide(self, t: float, dt: float, pmv: float, ac_power: bool, ac_setpoint: int):
        """Return a remote action or None.
        Actions: ('ac', power, setpoint) or ('fan', step)."""
        n = self.night_at(t)
        if n is None:
            if ac_power:
                return ("ac", False, ac_setpoint)
            return None
        if t - n.arrive < dt * 1.5 and not ac_power:
            # arrival: switch the AC on at the habitual setpoint, set the fan
            return ("ac_on_arrival", True, self.p.start_setpoint)
        if not ac_power:
            return None
        warm_lim = self.p.warm_threshold_asleep if self.asleep(t) else self.p.warm_threshold_awake
        self.warm_since = (self.warm_since if self.warm_since is not None else t) if pmv > warm_lim else None
        self.cold_since = (self.cold_since if self.cold_since is not None else t) if pmv < self.p.cold_threshold else None
        if t - self.last_press < self.p.cooldown_s:
            return None
        if self.warm_since is not None and t - self.warm_since >= self.p.patience_s:
            sp = max(self.p.min_setpoint, ac_setpoint - self.p.lower_by)
            if sp != ac_setpoint:
                self.last_press, self.warm_since = t, None
                self.presses += 1
                return ("ac", True, sp)
        if self.cold_since is not None and t - self.cold_since >= self.p.patience_s * 1.5:
            self.last_press, self.cold_since = t, None
            self.presses += 1
            return ("ac", True, min(30, ac_setpoint + 1))
        return None
