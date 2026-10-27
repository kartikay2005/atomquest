"""Unit tests for the physical models in the digital twin (no firmware needed)."""
import math

import numpy as np
import pytest
from pythermalcomfort.models import pmv_ppd_ashrae

from duet_twin.comfort_truth import pmv_true
from duet_twin.plant import AcParams, Fan, FanParams, Room, RoomParams, RoomState, SplitAc
from duet_twin.psychro import dew_point, rh_from_w, w_from_rh
from duet_twin.weather import JAIPUR_NORMALS, jaipur_synthetic


@pytest.mark.parametrize("t,rh", [(20, 30), (27, 55), (35, 20), (30, 80)])
def test_humidity_ratio_round_trip(t, rh):
    assert rh_from_w(t, w_from_rh(t, rh)) == pytest.approx(rh, abs=0.05)


def test_dew_point_below_air_temperature():
    assert dew_point(30, 50) < 30
    assert dew_point(25, 100) == pytest.approx(25, abs=0.1)


@pytest.mark.parametrize("month", sorted(JAIPUR_NORMALS))
def test_weather_matches_monthly_normals(month):
    w = jaipur_synthetic(month, 8, seed=1)
    tmax, tmin = JAIPUR_NORMALS[month][:2]
    daily = w.t_out[:192].reshape(8, 24)
    assert abs(daily.max(1).mean() - tmax) < 2.0
    assert abs(daily.min(1).mean() - tmin) < 2.0
    assert 13 <= int(np.argmax(daily.mean(0))) <= 16        # warmest mid-afternoon
    assert 3 <= int(np.argmin(daily.mean(0))) <= 7          # coolest before dawn
    assert (w.ghi[:192].reshape(8, 24)[:, 0] == 0).all()     # no sun at midnight
    assert ((w.rh_out >= 0) & (w.rh_out <= 100)).all()


def test_weather_is_deterministic_per_seed():
    a, b, c = (jaipur_synthetic(5, 3, s) for s in (7, 7, 8))
    assert np.array_equal(a.t_out, b.t_out) and not np.array_equal(a.t_out, c.t_out)


def test_ac_power_bounds_and_efficiency_falls_when_hotter():
    ac = SplitAc(AcParams())
    ac.s.power, ac.s.setpoint = True, 24
    for _ in range(200):
        q, m, w = ac.step(5, 30.0, 40.0, 30.0, 45.0)
    assert 1000 < w < 2600 and q > 3000                     # full-ish load, sane watts
    assert ac.cop(45, 27, 0.5) < ac.cop(35, 27, 0.5) < ac.cop(25, 27, 0.5)
    ac.s.power = False
    assert ac.step(5, 30, 40, 30, 45) == (0.0, 0.0, 0.0)


def test_ac_removes_less_moisture_at_a_higher_setpoint():
    # Same moisture in the air (same humidity ratio), room 1.5 C above each setpoint:
    # a warmer coil condenses less, so the latent share of cooling falls.
    w = w_from_rh(25.5, 60)
    share = {}
    for sp in (24, 28):
        ac = SplitAc(AcParams()); ac.s.power, ac.s.setpoint = True, sp
        t = sp + 1.5
        for _ in range(100):
            q, m, _ = ac.step(5, t, 38, t, rh_from_w(t, w))
        share[sp] = m * 2.45e6 / (q + m * 2.45e6)
    assert share[28] < share[24]


def test_fan_power_rises_with_step_and_tops_at_spec():
    f = Fan(FanParams())
    p = []
    for s in range(7):
        f.step = s
        p.append(f.power())
    assert p[0] == 0 and all(b > a for a, b in zip(p, p[1:])) and p[6] == pytest.approx(35.0)


def test_room_heats_without_ac_and_cools_with_it():
    hot = Room(RoomParams(), RoomState(t_air=28, t_mass=30, w=0.01))
    for _ in range(720):
        hot.step(5, 40, 0.01, 0, 33, 0, 0, 80, 0)
    assert hot.s.t_air > 29
    cool = Room(RoomParams(), RoomState(t_air=30, t_mass=30, w=0.01))
    for _ in range(720):
        cool.step(5, 40, 0.01, 0, 33, 2500, 0, 80, 0)
    assert cool.s.t_air < 27


@pytest.mark.parametrize("prof,met,clo", [("evening", 1.0, 0.5), ("sleep", 0.8, 0.9)])
@pytest.mark.parametrize("t,rh,step,v", [(24, 50, 0, 0.10), (27, 55, 2, 0.25), (29, 40, 5, 0.60)])
def test_truth_grid_matches_ashrae_library(prof, met, clo, t, rh, step, v):
    ref = pmv_ppd_ashrae(tdb=t, tr=t + 1, vr=v, rh=rh, met=met, clo=clo, limit_inputs=False,
                         round_output=False).pmv
    got = pmv_true(np.array([prof]), np.array([t]), np.array([rh]), np.array([t + 1.0]), np.array([step]))[0]
    assert got == pytest.approx(float(ref), abs=0.02)
