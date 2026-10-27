"""Fault-injection suite: one scenario per fault, each with an explicit pass rule.

Every scenario runs the real firmware for three days of pre-monsoon weather and
injects the fault at 01:00 on the second night (t = 2 days + 1 h), while the
occupant is asleep. Writes experiments/out/faults.csv and prints pass/fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from duet_twin import Fault, Scenario, Simulation  # noqa: E402

OUT = ROOT / "experiments" / "out"
T0 = 2 * 86400 + 3600  # 01:00, night 2


def run(faults, seed=7, **kw):
    sim = Simulation(Scenario(weather_name="fault_nights", seed=seed, arm="duet", occupants=1,
                              faults=faults, **kw)).run()
    return sim, sim.frame()


def window(df, a, b):
    return df[(df.t >= a) & (df.t < b)]


def ac_changes(sim, a, b):
    return [c for c in sim.monitor.changes if a <= c[0] < b]


def scenario_stuck():
    sim, df = run([Fault("sensor_stuck", T0, 3600)])
    w = window(df, T0 + 11 * 60, T0 + 3600)
    safe = (w["mode"] == "safe").mean()
    fan_moves = w.fan_step.nunique() == 1
    n = len(ac_changes(sim, T0 + 11 * 60, T0 + 3600))
    rec = (window(df, T0 + 3600 + 6 * 60, T0 + 3600 + 30 * 60)["mode"] != "safe").all()
    ok = safe == 1.0 and n == 0 and fan_moves and rec
    return ok, f"safe {safe:.0%} of fault window after 11 min, {n} AC commands, fan held={fan_moves}, recovered={rec}", sim


def scenario_crc():
    sim, df = run([Fault("sensor_crc", T0, 1800)])
    w = window(df, T0 + 60, T0 + 1800)
    safe = (w["mode"] == "safe").mean()
    n = len(ac_changes(sim, T0, T0 + 1800))
    ok = safe > 0.95 and n == 0
    return ok, f"safe {safe:.0%} of window, {n} AC commands", sim


def scenario_drift():
    sim, df = run([Fault("sensor_drift", T0, 4 * 3600, value=2.0)])
    w = window(df, T0, T0 + 4 * 3600)
    worst = w.pmv.max()
    ok = len(sim.monitor.violations) == 0 and worst <= 0.7
    return ok, (f"no rule broken; reads 2 C warm so Duet errs cool (max true PMV {worst:+.2f}); "
                "slow drift needs a second sensor to detect"), sim


def scenario_ir_burst():
    sim, df = run([Fault("ir_burst", T0 - 3 * 3600, 3600, value=1.0)])  # 22:00-23:00 decisions
    after = window(df, T0 - 2 * 3600 + 30 * 60, T0 + 4 * 3600)
    synced = (after.ac_sp == after.believed_sp).mean()
    ok = synced > 0.95 and len(sim.monitor.violations) == 0
    return ok, f"AC matched Duet's belief {synced:.0%} of the time after the burst", sim


def scenario_humidity():
    sim, df = run([Fault("humidity_spike", T0 - 2 * 3600, 6 * 3600, value=1.2e-4)])
    w = window(df, T0 - 2 * 3600, T0 + 4 * 3600)
    raises = [c for c in ac_changes(sim, T0 - 2 * 3600, T0 + 4 * 3600) if c[2] > c[1]]
    high = w.rh.max()
    bad = [c for c in raises if np.interp(c[0], df.t, df.rh) > 60]
    ok = len(bad) == 0
    return ok, f"RH peaked {high:.0f}%; {len(bad)} raises above 60 % RH", sim


def scenario_remote():
    sim, df = run([Fault("remote_press", T0, 0, value=23)])
    n = len(ac_changes(sim, T0, T0 + 2 * 3600 - 60))
    held = window(df, T0 + 60, T0 + 2 * 3600 - 60)
    # every setpoint the AC had during the hold came from the user's remote
    follows = (held.ac_sp == held.believed_sp).mean()
    ok = n == 0 and follows > 0.99 and (held["mode"] == "user_hold").mean() > 0.99
    return ok, f"{n} Duet commands in the 2 h hold; Duet tracked the user's own settings {follows:.0%} of it", sim


def scenario_remote_unseen():
    sim, df = run([Fault("remote_press_unseen", T0, 0, value=25)])
    n = len(ac_changes(sim, T0, T0 + 2 * 3600 - 60))
    inferred = int(sim.out.missed_remote)
    ok = n == 0
    return ok, f"Duet never saw the frame; inferred a user change {inferred}x from the room; {n} commands in the 2 h after", sim


def scenario_radar():
    sim, df = run([Fault("radar_absent", T0, 30 * 60)])
    w = window(df, T0, T0 + 35 * 60)
    vac = (w["mode"] == "vacant").mean()
    ok = vac == 0
    return ok, f"vacant {vac:.0%} of a 30 min radar dropout while asleep", sim


SCENARIOS = [
    ("Sensor frozen for 60 min", "Safe mode within 11 min; no AC commands; fan held; recovers", scenario_stuck),
    ("Sensor CRC errors for 30 min", "Safe mode; no AC commands", scenario_crc),
    ("Sensor reads 2 C high for 4 h", "No rule broken; occupant never warm (PMV <= +0.7)", scenario_drift),
    ("IR blocked for 60 min during raises", "AC back in sync with Duet after the burst", scenario_ir_burst),
    ("Humidity spike above 60 %", "No setpoint raise while RH > 60 %", scenario_humidity),
    ("User presses remote: 23 C at 01:00", "Duet sends nothing for 2 h; follows the user", scenario_remote),
    ("User's remote frame missed by Duet", "No Duet command for 2 h after the user's change", scenario_remote_unseen),
    ("Radar misses a still sleeper 30 min", "Room not declared empty; fan not switched off", scenario_radar),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, rule, fn in SCENARIOS:
        ok, detail, sim = fn()
        v = len(sim.monitor.violations)
        rows.append({"fault": name, "pass_rule": rule, "result": "PASS" if ok and v == 0 else "FAIL",
                     "detail": detail, "safety_violations": v})
        print(f"{'PASS' if ok and v == 0 else 'FAIL'}  {name:<40} {detail}")
    pd.DataFrame(rows).to_csv(OUT / "faults.csv", index=False)
    return all(r["result"] == "PASS" for r in rows)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
