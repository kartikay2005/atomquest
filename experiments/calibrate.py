"""Step 1 of the validation plan: does the twin agree with published field data
before we trust it?

Runs the room with the AC fixed at 24..28 C (nobody touching the remote) over the
same pre-monsoon week and seeds, and compares
  - AC energy saved per 1 C raise          vs BEE ~6 %/C (conservative) and the
  - saving at 26 C and 28 C vs 24 C        Indian bedroom field study (Gokarakonda
                                           et al. 2023: 21 % and 44 %)
  - cooling effect of the fan steps        vs ASHRAE 55 SET method (computed)
Writes experiments/out/calibration.csv and prints a pass/fail table.
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from duet_twin import Scenario, Simulation  # noqa: E402

OUT = ROOT / "experiments" / "out"
SEEDS = range(6)


def run(args):
    sp, fan, seed = args
    sim = Simulation(Scenario(weather_name="premonsoon_week", seed=seed, arm=f"fixed{sp}f{fan}",
                              occupants=1)).run()
    m = sim.metrics()
    return {"setpoint": sp, "fan": fan, "seed": seed, "ac_kwh": m["ac_kwh"],
            "mean_pmv": m["mean_pmv"], "in_band": m["comfort_in_band"]}


def main(reuse: bool = False):
    OUT.mkdir(parents=True, exist_ok=True)
    if reuse and (OUT / "calibration.csv").exists():
        df = pd.read_csv(OUT / "calibration.csv")
    else:
        jobs = [(sp, f, s) for sp in (24, 25, 26, 27, 28) for f in (0, 2) for s in SEEDS]
        with ProcessPoolExecutor() as ex:
            rows = list(ex.map(run, jobs))
        df = pd.DataFrame(rows)
        df.to_csv(OUT / "calibration.csv", index=False)
    g = df[df.fan == 0].groupby("setpoint").ac_kwh.mean()
    per_c = (1 - g[28] / g[24]) / 4   # average saving per C, relative to 24 C (as the field study reports)
    s26 = 1 - g[26] / g[24]
    s28 = 1 - g[28] / g[24]
    pm = df.groupby(["setpoint", "fan"]).mean_pmv.mean().unstack()
    checks = [
        ("AC saving per 1 C, relative to 24 C", f"{per_c:.1%}",
         "11 %/C field (BEE quotes ~6 %); band 8-13 %", 0.08 <= per_c <= 0.13),
        ("AC saving at 26 C vs 24 C", f"{s26:.1%}", "21 % measured (band 15-27 %)", 0.15 <= s26 <= 0.27),
        ("AC saving at 28 C vs 24 C", f"{s28:.1%}", "44 % measured (band 35-50 %)", 0.35 <= s28 <= 0.50),
        ("Comfort at 28 C with fan step 2 (mean PMV)", f"{pm.loc[28, 2]:+.2f}", "acceptable: at or below +0.5",
         pm.loc[28, 2] <= 0.5),
    ]
    print(f"\nAC kWh per week by setpoint (fan off, {len(SEEDS)} seeds):")
    print(g.round(2).to_string())
    print("\nmean PMV by setpoint x fan step:")
    print(pm.round(2).to_string())
    print("\n{:<45} {:>9}  {:<42} {}".format("check", "twin", "published", "result"))
    for name, val, ref, ok in checks:
        print(f"{name:<45} {val:>9}  {ref:<42} {'PASS' if ok else 'FAIL'}")
    pd.DataFrame(checks, columns=["check", "twin", "published", "pass"]).to_csv(
        OUT / "calibration_checks.csv", index=False)
    return all(c[3] for c in checks)


if __name__ == "__main__":
    sys.exit(0 if main(reuse="--reuse" in sys.argv) else 1)
