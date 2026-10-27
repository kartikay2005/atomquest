"""Ground-truth comfort for scoring runs: ASHRAE 55 PMV at the bed with the twin's
own mean radiant temperature, interpolated from the dense grid built by
comfort/build_tables.py (pythermalcomfort 4.7.0)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

GRID = Path(__file__).resolve().parents[2] / "comfort" / "out" / "truth_grid.npz"


@lru_cache(maxsize=1)
def _interp():
    z = np.load(GRID)
    out = {}
    for prof in ("evening", "sleep", "blanket"):
        g = z[prof]  # [t, rh, step, d]
        out[prof] = [RegularGridInterpolator((z["t"], z["rh"], z["d"]), g[:, :, s, :],
                                             bounds_error=False, fill_value=None)
                     for s in range(g.shape[2])]
    return out, (z["t"][0], z["t"][-1]), (z["rh"][0], z["rh"][-1]), (z["d"][0], z["d"][-1])


def pmv_true(profile: np.ndarray, t_air: np.ndarray, rh: np.ndarray, t_rad: np.ndarray,
             step: np.ndarray) -> np.ndarray:
    """Vectorised PMV. profile: array of 'evening'/'sleep'."""
    interp, tr, rr, dr = _interp()
    t = np.clip(t_air, *tr)
    h = np.clip(rh, *rr)
    d = np.clip(t_rad - t_air, *dr)
    out = np.full(len(t), np.nan)
    for prof in ("evening", "sleep", "blanket"):
        for s in range(len(interp[prof])):
            m = (profile == prof) & (step == s)
            if m.any():
                out[m] = interp[prof][s](np.column_stack([t[m], h[m], d[m]]))
    return out
