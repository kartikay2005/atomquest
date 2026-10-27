"""Property-based tests (Hypothesis) of Duet's safety rules against the real firmware.

1. The governor alone: any sequence of requests, clock jumps, humidity, sensor and
   hold states; whatever it approves must satisfy all five rules.
2. The whole controller, fuzzed through the SIL library with random sensor
   readings, presence, remote presses and faults; every AC command it emits is
   audited from outside.
Run: pytest twin/tests -q
"""
from __future__ import annotations

import ctypes as C
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings, target
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from duet_twin.sil import DuetFirmware, lib  # noqa: E402

MIN = 60_000
APPROVED = 0

step_st = st.tuples(
    st.integers(0, 40),                    # minutes to advance
    st.integers(-3, 3),                    # requested delta from current setpoint
    st.floats(20, 90, allow_nan=False),    # RH %
    st.booleans(),                         # sensor ok
    st.booleans(),                         # user hold
    st.booleans(),                         # AC power
)


@settings(max_examples=400, deadline=None)
@given(start_sp=st.integers(20, 30), steps=st.lists(step_st, min_size=1, max_size=60))
def test_governor_never_approves_an_unsafe_change(start_sp, steps):
    L = lib()
    L.duet_gov_create.restype = C.c_void_p
    L.duet_gov_check.argtypes = [C.c_void_p, C.c_int64, C.c_int, C.c_int, C.c_int, C.c_float,
                                 C.c_int, C.c_int, C.c_int]
    L.duet_gov_record.argtypes = [C.c_void_p, C.c_int64]
    L.duet_gov_destroy.argtypes = [C.c_void_p]
    g = C.c_void_p(L.duet_gov_create())
    now, sp, applied = 0, start_sp, []
    try:
        for dmin, delta, rh, ok, hold, power in steps:
            now += dmin * MIN
            req = sp + delta
            v = L.duet_gov_check(g, now, 1, int(power), sp, rh, int(ok), int(hold), req)
            if v == APPROVED and req != sp:
                assert 24 <= req <= 28
                assert abs(req - sp) <= 1
                assert power and ok and not hold
                assert not (req > sp and rh > 60.0)
                assert not applied or now - applied[-1] >= 20 * MIN
                assert sum(1 for t in applied if now - t < 60 * MIN) < 3
                L.duet_gov_record(g, now)
                applied.append(now)
                sp = req
    finally:
        L.duet_gov_destroy(g)


event_st = st.tuples(
    st.integers(1, 900),                     # ticks of 5 s at this condition (up to 75 min)
    st.floats(-0.5, 0.5, allow_nan=False),   # temperature drift per block
    st.floats(30, 85, allow_nan=False),      # RH
    st.booleans(),                           # occupied
    st.sampled_from(["follow", "follow", "follow", "none", "none", "remote_ac", "remote_fan", "crc",
                     "stuck", "off"]),     # follow = room settles where the AC setpoint puts it
    st.integers(18, 30),                     # remote setpoint
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(blocks=st.lists(event_st, min_size=5, max_size=40), t0=st.floats(24, 32))
def test_controller_commands_are_always_safe(blocks, t0):
    fw = DuetFirmware()
    fw.begin(20 * 3600 * 1000)
    now = 20 * 3600 * 1000
    temp = t0
    i = fw.inp
    prev_believed = None
    prev_power = False
    changes = []
    hold_until = -1
    stuck = None
    for ticks, drift, rh, occ, ev, rsp in blocks:
        for k in range(ticks):
            now += 5000
            if ev == "follow" and prev_believed is not None:
                temp = prev_believed + 0.6          # a regulating AC: bed sits just above setpoint
            else:
                temp = min(40.0, max(16.0, temp + drift / ticks))
            i.now_ms = now
            i.temp_c = stuck if (ev == "stuck" and stuck is not None) else temp + ((k % 3) - 1) * 0.01
            i.rh = rh
            i.crc_ok = 0 if (ev == "crc" and k % 2 == 0) else 1
            i.sensor_present = 1
            i.occupied = int(occ)
            i.remote_kind = 0
            if k == 0 and ev in ("remote_ac", "off"):
                i.remote_kind, i.remote_power, i.remote_setpoint = 1, int(ev != "off"), rsp
            elif k == 0 and ev == "remote_fan":
                i.remote_kind, i.remote_fan_step = 2, rsp % 7
            if ev == "stuck" and stuck is None:
                stuck = i.temp_c
            if ev != "stuck":
                stuck = None
            before = prev_believed
            was_on = prev_power
            o = fw.step()
            if i.remote_kind == 1:
                # the user's remote wins: Duet adopts it and holds
                assert o.believed_setpoint == rsp or not i.remote_power
                if not i.remote_power:
                    hold_until = -1
                elif not was_on:
                    hold_until = now + 15 * MIN                # switched on
                elif before is not None and rsp != before:
                    hold_until = now + 2 * 3600 * 1000         # user changed it
                before = int(o.believed_setpoint)
            if o.ac_frames:
                assert o.sensor_ok == 1, "AC frame while the sensor is untrusted"
                assert now >= hold_until or before is None, "AC frame during user hold"
                assert 24 <= o.ac_setpoint <= 28
                if before is not None and o.ac_setpoint != before:
                    assert abs(o.ac_setpoint - before) <= 1
                    assert not (o.ac_setpoint > before and o.rh_sensor > 60.0)
                    assert not changes or now - changes[-1] >= 20 * MIN
                    assert sum(1 for t in changes if now - t < 60 * MIN) < 3
                    changes.append(now)
            prev_believed = int(o.believed_setpoint)
            prev_power = bool(o.believed_power)
    fw.close()
    AUDITED.append(len(changes))
    target(float(len(changes)), label="AC changes audited")


AUDITED: list[int] = []


def test_fuzz_actually_exercised_ac_commands():
    # Guards against a vacuous fuzz: the controller must actually have changed the AC,
    # under the oracle's eye, many times across the generated histories.
    print(f"AC changes audited: {sum(AUDITED)} across {len(AUDITED)} histories")
    assert sum(AUDITED) > 100, sum(AUDITED)
