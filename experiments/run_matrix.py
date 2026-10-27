"""Step 3 of the validation plan: Duet vs three baselines over randomised runs.

Matrix: weather sets x arms x seeds. Same seed = same weather, occupants, sensor
offsets and IR losses across arms, so each Duet run has a paired baseline.
Usage: python experiments/run_matrix.py [--seeds 30] [--weather premonsoon_week,...]
Writes experiments/out/matrix.csv and experiments/out/summary.csv.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
from duet_twin import Scenario, Simulation  # noqa: E402

OUT = ROOT / "experiments" / "out"
ARMS = ["duet", "b1", "b2", "b3"]


def run(args):
    weather, arm, seed, ir_loss = args
    sim = Simulation(Scenario(weather_name=weather, seed=seed, arm=arm, ir_loss=ir_loss)).run()
    m = sim.metrics()
    m["ir_loss"] = ir_loss
    if sim.out is not None:
        m["reconciles"] = int(sim.out.reconciles)
        m["resends"] = int(sim.out.ac_resends)
    return m


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for w, g in df.groupby("weather"):
        d = g[g.arm == "duet"].set_index("seed")
        for base in ["b1", "b2", "b3"]:
            b = g[g.arm == base].set_index("seed").reindex(d.index)
            ac_sav = 1 - d.ac_kwh / b.ac_kwh
            tot_sav = 1 - d.total_kwh / b.total_kwh
            q = lambda s: (float(np.nanmedian(s)), float(np.nanpercentile(s, 5)), float(np.nanpercentile(s, 95)))
            rows.append({
                "weather": w, "vs": base, "n": len(d),
                "ac_saving_median": q(ac_sav)[0], "ac_saving_p5": q(ac_sav)[1], "ac_saving_p95": q(ac_sav)[2],
                "net_saving_median": q(tot_sav)[0],
                "duet_in_band": float(d.comfort_in_band.median()), "base_in_band": float(b.comfort_in_band.median()),
                "duet_presses_per_night": float((d.user_presses / d.nights).mean()),
                "base_presses_per_night": float((b.user_presses / b.nights).mean()),
                "duet_ac_kwh_night": float((d.ac_kwh / d.nights).median()),
                "base_ac_kwh_night": float((b.ac_kwh / b.nights).median()),
                "ir_success": float(d.ir_ok_final.mean()),
                "safety_violations": int(d.safety_violations.sum()),
                "missed_user_frames": int(d.missed_user_frames.sum()),
                "inferred_user_changes": int(d.inferred_user_changes.sum()),
                "duet_blanket_share": float(d.blanket_share_of_sleep.mean()),
                "base_blanket_share": float(b.blanket_share_of_sleep.mean()),
            })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--weather", default="premonsoon_week,monsoon_week,hottest_day")
    ap.add_argument("--ir-loss", type=float, default=0.05)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(w, arm, s, a.ir_loss) for w in a.weather.split(",") for s in range(a.seeds) for arm in ARMS]
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor() as ex:
        for i, m in enumerate(ex.map(run, jobs, chunksize=2), 1):
            rows.append(m)
            if i % 20 == 0:
                print(f"  {i}/{len(jobs)} runs, {time.time() - t0:.0f} s", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "matrix.csv", index=False)
    s = summarise(df)
    s.to_csv(OUT / "summary.csv", index=False)
    pd.set_option("display.width", 200)
    print(s.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
