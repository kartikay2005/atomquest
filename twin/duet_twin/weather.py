"""Weather for the twin.

Two sources:
  EpwWeather       reads an EnergyPlus weather file (use the ISHRAE Jaipur file from
                   climate.onebuilding.org for the final report runs).
  JaipurSynthetic  a seeded generator built from approximate Jaipur monthly climate
                   normals, used until the EPW file is dropped into twin/data/.

Both expose hourly arrays and `at(t_s)` with linear interpolation, where t_s is
seconds since 00:00 of the scenario's first day.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .psychro import rh_from_w, w_from_rh

# Approximate Jaipur monthly normals: (mean daily max C, mean daily min C,
# morning RH %, afternoon RH %, clear-sky peak horizontal irradiance W/m2).
# Replace with the ISHRAE EPW for report numbers; these are labelled approximate.
JAIPUR_NORMALS = {
    4: (37.8, 21.9, 35, 15, 950),
    5: (40.6, 25.9, 38, 18, 980),
    6: (39.3, 27.3, 55, 32, 900),
    7: (34.2, 26.1, 78, 62, 650),
    8: (32.4, 24.9, 82, 68, 600),
    9: (33.6, 23.5, 72, 50, 750),
}


@dataclass
class Weather:
    hours: np.ndarray       # hour index from scenario start
    t_out: np.ndarray       # C
    rh_out: np.ndarray      # %
    ghi: np.ndarray         # W/m2
    label: str = ""

    def at(self, t_s: float) -> tuple[float, float, float]:
        h = t_s / 3600.0
        i = int(h)
        if i >= len(self.hours) - 1:
            i = len(self.hours) - 2
        f = h - i
        t = self.t_out[i] * (1 - f) + self.t_out[i + 1] * f
        rh = self.rh_out[i] * (1 - f) + self.rh_out[i + 1] * f
        g = self.ghi[i] * (1 - f) + self.ghi[i + 1] * f
        return float(t), float(rh), float(g)

    def mean_last_24h(self, t_s: float) -> float:
        i = int(t_s / 3600.0)
        lo = max(0, i - 24)
        return float(self.t_out[lo:i + 1].mean())


def jaipur_synthetic(month: int, days: int, seed: int, heat_offset: float = 0.0,
                     label: str | None = None) -> Weather:
    """Seeded diurnal weather from monthly normals.

    Daily max/min wander as AR(1) around the normals (sd ~1.3 C); the diurnal
    shape peaks at 15:00 and bottoms at 05:30; humidity ratio is held near the
    month's typical value with slow noise so RH swings inversely with temperature.
    """
    tmax0, tmin0, rh_m, rh_a, gpk = JAIPUR_NORMALS[month]
    rng = np.random.default_rng(seed)
    n = days * 24 + 1
    hours = np.arange(n, dtype=float)
    t = np.empty(n)
    rh = np.empty(n)
    g = np.empty(n)
    # typical humidity ratio from the morning RH at the daily minimum
    w_typ = 0.5 * (w_from_rh(tmin0 + 1, rh_m) + w_from_rh(tmax0 - 1, rh_a))
    a_max = a_min = a_w = 0.0
    for d in range(days + 1):
        a_max = 0.6 * a_max + rng.normal(0, 1.0)
        a_min = 0.6 * a_min + rng.normal(0, 0.8)
        a_w = 0.7 * a_w + rng.normal(0, 0.06)
        cloud = float(np.clip(rng.normal(0.15 if month in (7, 8) else 0.05, 0.1), 0, 0.6))
        tmax = tmax0 + heat_offset + a_max
        tmin = min(tmin0 + heat_offset + a_min, tmax - 6)
        w_day = w_typ * (1 + a_w)
        for hr in range(24):
            k = d * 24 + hr
            if k >= n:
                break
            # piecewise cosine: min at 5.5 h, max at 15 h
            if 5.5 <= hr <= 15:
                x = (hr - 5.5) / 9.5
                frac = 0.5 - 0.5 * math.cos(math.pi * x)
            else:
                hh = hr + 24 if hr < 5.5 else hr
                x = (hh - 15) / 14.5
                frac = 0.5 + 0.5 * math.cos(math.pi * x)
            t[k] = tmin + (tmax - tmin) * frac
            rh[k] = rh_from_w(t[k], w_day)
            sun = max(0.0, math.sin(math.pi * (hr - 6.0) / 13.0)) if 6 <= hr <= 19 else 0.0
            g[k] = gpk * sun * (1 - cloud)
    return Weather(hours, t, rh, g,
                   label or f"Jaipur synthetic, month {month}, {days} d, seed {seed}")


def load_epw(path: str | Path, start_month: int, start_day: int, days: int) -> Weather:
    """Slice an EPW file (8760 hourly rows after 8 header lines)."""
    rows = Path(path).read_text(errors="ignore").splitlines()[8:]
    data = [r.split(",") for r in rows if r.count(",") > 20]
    start = None
    for i, r in enumerate(data):
        if int(r[1]) == start_month and int(r[2]) == start_day and int(r[3]) == 1:
            start = i
            break
    if start is None:
        raise ValueError("start date not in EPW")
    sl = data[start:start + days * 24 + 1]
    t = np.array([float(r[6]) for r in sl])
    rh = np.array([float(r[8]) for r in sl])
    ghi = np.array([float(r[13]) for r in sl])
    return Weather(np.arange(len(sl), dtype=float), t, rh, ghi,
                   f"EPW {Path(path).name} from {start_month}/{start_day}, {days} d")


# Scenarios used in the report
WEATHER_SETS = {
    "hottest_day": dict(month=5, days=2, heat_offset=3.5),   # ~44 C afternoon
    "premonsoon_week": dict(month=5, days=8, heat_offset=0.0),
    "monsoon_week": dict(month=8, days=8, heat_offset=0.0),
}


def make_weather(name: str, seed: int, epw: str | None = None) -> Weather:
    spec = WEATHER_SETS[name]
    if epw:
        start = {"hottest_day": (5, 25), "premonsoon_week": (5, 10), "monsoon_week": (8, 5)}[name]
        return load_epw(epw, start[0], start[1], spec["days"])
    return jaipur_synthetic(spec["month"], spec["days"], seed, spec["heat_offset"],
                            label=f"{name} (Jaipur synthetic, seed {seed})")
WEATHER_SETS["fault_nights"] = dict(month=5, days=3, heat_offset=0.0)
