# Duet — simulation results (firmware duet-fw 0.1.0, run 27 Oct 2026)

All numbers are **simulated**: the real Duet control firmware (C++) running in closed
loop against the digital twin of a 3.5 × 3.5 m Jaipur bedroom with a 1.5-ton inverter
split AC and a 1200 mm BLDC fan. Weather is a Jaipur generator built from approximate
monthly climate normals; re-run with the ISHRAE EPW file before quoting final numbers.

## 1. The twin agrees with field data (`experiments/calibrate.py`)

| Check | Twin | Published | Result |
| --- | --- | --- | --- |
| AC saving per 1 °C, relative to 24 °C | 9.7 %/°C | ~11 %/°C, Indian bedrooms (BEE quotes 6 % as a floor) | PASS |
| AC saving at 26 °C vs 24 °C | 20.9 % | 21 % measured (Gokarakonda et al. 2023) | PASS |
| AC saving at 28 °C vs 24 °C | 38.9 % | 44 % measured | PASS |
| Comfort at 28 °C with fan step 2 | PMV +0.32 | acceptable (≤ +0.5) | PASS |

The twin slightly *under*-states savings at 28 °C, so Duet's results below are conservative.
Figure: `experiments/out/fig3_calibration.png`.

## 2. Duet vs normal rooms (`experiments/run_matrix.py`, 30 paired seeds per cell)

Same weather, occupant, sensor offsets and IR losses in every arm of a seed. IR frame
loss 5 %. The headline is against **baseline 2: AC 24 °C with the fan on medium**, so
the saving cannot come from merely turning the fan on.

| Weather | AC saving vs AC 24 °C + fan (median, 5–95 %) | vs AC 24 °C, no fan | vs AC 22 °C | AC kWh/night Duet vs base 2 |
| --- | --- | --- | --- | --- |
| Pre-monsoon week (May) | **20 %** (16–22 %) | 29 % | 38 % | 3.87 vs 4.87 |
| Monsoon week (Aug) | **19 %** (−2–30 %) | 31 % | 43 % | 2.53 vs 3.03 |
| Hottest night (~44 °C day) | **0 %** (0–7 %) | −1 % | −2 % | 3.80 vs 4.00 |

Including the fan's own energy, the net saving is within 1 point of the AC saving
(the fan draws 5–15 W against an AC drawing 300–1,800 W).

| Comfort and behaviour (pre-monsoon week) | Duet | AC 24 °C + fan | AC 24 °C, no fan |
| --- | --- | --- | --- |
| Occupied time comfortable (abs PMV ≤ 0.5, blanket counted) | 96 % | 79 % | 86 % |
| Sleep time the occupant needed a blanket | 4 % | 95 % | 94 % |
| Remote presses per night (discomfort) | 0.005 | 1.0 | 0.2 |
| IR commands that took effect, first try / after checks | 99.9 % / 100 % | — | — |
| Safety rule breaches (independent monitor), 90 Duet runs | **0** | — | — |

Why the hottest night saves nothing: the AC runs flat out until morning and the room
never settles, so Duet never raises the setpoint; it still improves comfort with the
fan (79 % vs 59 % in band). Why the monsoon range is wide: on humid nights the 60 % RH
rule blocks raises, exactly as designed.

Figures: `fig1_night_trace.png` (one night), `fig2_savings.png` (all cells).

## 3. Safety: property tests and fault injection

- `firmware/build/test_core`: 142 unit checks pass.
- `pytest twin/tests`: Hypothesis throws 400 random request sequences at the governor and
  300 fuzzed sensor/presence/remote/fault histories at the full controller; every AC
  command is audited against the five rules. 0 failures.
- `experiments/faults.py`, 8 scenarios, all PASS:

| Fault | Pass rule | Result |
| --- | --- | --- |
| Sensor frozen 60 min | Safe mode within 11 min, no AC commands, fan held, recovers | PASS |
| Sensor CRC errors 30 min | Safe mode, no AC commands | PASS |
| Sensor reads 2 °C high for 4 h | No rule broken; occupant never warm | PASS (slow drift is undetectable with one sensor; Duet errs cool) |
| IR blocked 60 min during raises | AC back in sync with Duet afterwards | PASS |
| Humidity spike to 71 % | No raise above 60 % RH | PASS |
| User sets 23 °C on the remote | No Duet command for 2 h | PASS |
| User's remote frame missed by Duet | Duet infers the change from the room, no command for 2 h | PASS |
| Radar loses a still sleeper 30 min | Room not declared empty | PASS |

## 4. What the simulation changed in the firmware

The twin found four real gaps, all fixed and re-tested:
1. A missed remote press let Duet override a user (found by the safety monitor in 1 of 30
   weeks). Duet now infers unseen user changes from the room's thermal response.
2. A long IR blockage left Duet's belief and the real AC apart for the night. Duet now
   resends its full-state frame until the room agrees.
3. A wrong prior for the bed-to-setpoint offset caused false alarms. Duet now learns it
   from the first settled period at the user's own setpoint.
4. A radar dropout over a still sleeper would have switched the fan off. Vacancy now
   takes 60 min at night.

## 5. Limits to state in the report

- Weather is synthetic from approximate normals until the ISHRAE EPW is used.
- Fan air speed per step, fan power curve and ceiling stratification are assumptions
  (calibrate with an anemometer and plug meter in Round 3).
- Comfort is ASHRAE 55 PMV; the Sleep profile uses met 0.8 (below the library's default
  input limit, generated with limits off) and clo 0.9.
- Duet's inference of missed remote presses also fires occasionally without a missed
  press (it then pauses for 2 h, costing savings, never comfort).
- The fan step sometimes alternates between two neighbouring steps every ~15 min late
  at night; widening the fan hysteresis is a tuning item.
