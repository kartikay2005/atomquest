"""Report figures from experiments/out/*.csv (run after calibrate.py and run_matrix.py).

fig1_night_trace.png   one night, Duet vs baseline: temperature + setpoint, fan step, comfort
fig2_savings.png       AC saving per run vs each baseline, by weather (median and 5-95 %)
fig3_calibration.png   twin AC energy vs setpoint against the Indian field study
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "twin"))
OUT = ROOT / "experiments" / "out"
ACCENT, BASE, BAND = "#0b7a5c", "#8a929b", "#e3f3ec"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.titleweight": "bold", "axes.titlesize": 11, "figure.dpi": 150})


def night_trace(seed=3, night=2):
    from duet_twin import Scenario, Simulation
    frames = {}
    for arm in ("b2", "duet"):
        s = Simulation(Scenario(weather_name="premonsoon_week", seed=seed, arm=arm, occupants=1)).run()
        frames[arm] = s.frame()
    a, b = night * 86400 + 21 * 3600, (night + 1) * 86400 + 7.5 * 3600
    fig, ax = plt.subplots(3, 1, figsize=(8, 7), sharex=True, gridspec_kw={"height_ratios": [2.2, 1, 1.4]})
    for arm, col, lab in (("b2", BASE, "Normal room (AC 24 °C, fan medium)"), ("duet", ACCENT, "Duet room")):
        d = frames[arm]
        d = d[(d.t >= a) & (d.t < b)]
        h = (d.t - night * 86400) / 3600
        ax[0].plot(h, d.t_air, color=col, lw=2, label=lab)
        ax[0].step(h, d.ac_sp.where(d.ac_power == 1), color=col, lw=1, ls="--", where="post")
        ax[1].step(h, d.fan_step, color=col, lw=1.8, where="post")
        ax[2].plot(h, d.pmv.where(d.present == 1), color=col, lw=1.6)
    ax[0].set_ylabel("°C")
    ax[0].set_title("Duet trades AC degrees for fan steps and holds comfort through the night")
    ax[0].legend(frameon=False, loc="upper right")
    ax[0].text(0.01, 0.04, "dashed = AC setpoint", transform=ax[0].transAxes, color="#5d6670", fontsize=8)
    ax[1].set_ylabel("Fan step")
    ax[1].set_yticks(range(0, 7, 2))
    ax[2].axhspan(-0.5, 0.5, color=BAND, zorder=0)
    ax[2].set_ylabel("PMV at bed")
    ax[2].set_ylim(-1.5, 1.5)
    ax[2].text(0.01, 0.82, "comfortable band (|PMV| ≤ 0.5); blanket counted when used", transform=ax[2].transAxes,
               color="#5d6670", fontsize=8)
    ticks = np.arange(21, 31.5, 2)
    ax[2].set_xticks(ticks, [f"{int(t % 24):02d}:00" for t in ticks])
    fig.tight_layout()
    fig.savefig(OUT / "fig1_night_trace.png")
    plt.close(fig)


def savings():
    df = pd.read_csv(OUT / "matrix.csv")
    order = [("premonsoon_week", "Pre-monsoon week"), ("hottest_day", "Hottest day"), ("monsoon_week", "Monsoon week")]
    bases = [("b1", "vs AC 24 °C, fan off"), ("b2", "vs AC 24 °C, fan medium"), ("b3", "vs AC 22 °C, fan off")]
    fig, ax = plt.subplots(figsize=(9, 4.6))
    y = 0
    for w, wl in order:
        g = df[df.weather == w]
        d = g[g.arm == "duet"].set_index("seed")
        for k, (bk, bl) in enumerate(bases):
            bb = g[g.arm == bk].set_index("seed").reindex(d.index)
            s = (1 - d.ac_kwh / bb.ac_kwh) * 100
            med, lo, hi = np.median(s), np.percentile(s, 5), np.percentile(s, 95)
            col = ACCENT if bk == "b2" else BASE
            ax.plot([lo, hi], [y, y], color=col, lw=2, alpha=0.6)
            ax.plot(med, y, "o", color=col, ms=7)
            ax.text(hi + 1, y, f"{med:.0f}%", va="center", fontsize=9, color=col)
            y -= 1
        y -= 0.6
    ax.set_yticks([], [])
    yy = 0
    for w, wl in order:
        for bk, bl in bases:
            ax.text(-6, yy, f"{wl}, {bl}", ha="right", va="center", fontsize=8.5)
            yy -= 1
        yy -= 0.6
    ax.axvline(15, color="#c23b32", lw=1, ls=":")
    ax.text(15.4, 0.55, "abstract target 15%", color="#c23b32", fontsize=8, va="bottom")
    ax.set_xlim(-5, max(60, ax.get_xlim()[1]))
    ax.set_xlabel("AC energy saved by Duet, %  (dot = median, line = 5th-95th percentile, 30 paired runs)")
    fig.suptitle("Duet saves ~20% against the toughest baseline in May and the monsoon;\nnothing on the hottest night, when the AC has no slack", x=0.02, ha="left", fontsize=11, fontweight="bold")
    ax.spines["left"].set_visible(False)
    fig.subplots_adjust(left=0.36, right=0.97, top=0.84, bottom=0.13)
    fig.savefig(OUT / "fig2_savings.png")
    plt.close(fig)


def calibration():
    df = pd.read_csv(OUT / "calibration.csv")
    g = df[df.fan == 0].groupby("setpoint").ac_kwh.mean()
    rel = g / g[24]
    field = {24: 1.0, 26: 0.79, 28: 0.56}
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    ax.plot(rel.index, rel.values * 100, "o-", color=ACCENT, lw=2, label="Digital twin (6 seeds, May week)")
    ax.plot(list(field), [v * 100 for v in field.values()], "s", color="#3f3f3f", ms=7,
            label="Field study, Indian bedrooms (2023)")
    ax.set_xlabel("AC setpoint, °C")
    ax.set_ylabel("AC energy, % of 24 °C")
    ax.set_ylim(40, 105)
    ax.set_title("The twin matches measured savings before we trust it")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "fig3_calibration.png")
    plt.close(fig)


if __name__ == "__main__":
    calibration()
    savings()
    night_trace()
    print("figures written to", OUT)
