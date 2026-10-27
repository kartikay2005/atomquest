"""Small psychrometric helpers (sea-level pressure; Jaipur is ~430 m, effect < 5%)."""
from __future__ import annotations

import math

P_ATM = 101325.0 * (1 - 2.25577e-5 * 430) ** 5.25588  # Jaipur station pressure, Pa
H_FG = 2.45e6  # J/kg, latent heat of vaporisation
CP_AIR = 1006.0
RHO_AIR = 1.16  # kg/m3 at ~30 C, 430 m


def p_sat(t_c: float) -> float:
    """Saturation vapour pressure over water, Pa (Magnus-Tetens)."""
    return 610.94 * math.exp(17.625 * t_c / (t_c + 243.04))


def w_from_rh(t_c: float, rh: float) -> float:
    """Humidity ratio kg/kg from dry bulb and RH (%)."""
    pv = max(1e-3, rh / 100.0 * p_sat(t_c))
    return 0.62198 * pv / (P_ATM - pv)


def rh_from_w(t_c: float, w: float) -> float:
    pv = w * P_ATM / (0.62198 + w)
    return max(0.0, min(100.0, 100.0 * pv / p_sat(t_c)))


def dew_point(t_c: float, rh: float) -> float:
    a, b = 17.625, 243.04
    g = math.log(max(rh, 0.1) / 100.0) + a * t_c / (b + t_c)
    return b * g / (a - g)
