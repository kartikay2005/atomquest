# Duet — software prototype (AtomQuest 2026, Track 1, Team Edision)

Duet coordinates a ceiling fan with the room's split AC: it raises the AC setpoint
and adds just enough air movement to keep ASHRAE 55 comfort constant. This repo is
the Round 2 prototype: **the real device firmware running in closed loop against a
physics-based digital twin of a Jaipur bedroom**, plus the experiments that produce
every number in the report.

```
comfort/      ASHRAE 55 comfort tables (pythermalcomfort 4.7.0) -> C header for the firmware
firmware/     C++ control core + safety governor (one source for chip and laptop)
  src/core/     controller, governor, comfort lookup   (no heap, no RTTI)
  src/sil/      C ABI so the twin can drive the core   (libduet_sil.so)
  src/esp32/    ESP32-C3 drivers: SHT40, LD2410C, IR via IRremoteESP8266, Wi-Fi/MQTT
  test/         unit tests (142 checks)
twin/         Python digital twin: room 2R2C + moisture, inverter AC, BLDC fan, sensor
              pod, IR link, occupants with a comfort-driven remote-pressing user
  tests/        Hypothesis property tests of the safety rules against the real firmware
experiments/  calibration vs field data, fault-injection suite, Duet vs baselines matrix
console/      live engineer console (FastAPI + WebSocket + one HTML page)
wokwi/        browser circuit simulation (next block)
hardware/     schematic, PCB, enclosure CAD, BOM (next block)
```

## Quick start (Windows, macOS or Linux)

```bash
pip install numpy scipy pandas pythermalcomfort==4.7.0 fastapi uvicorn hypothesis pytest
python comfort/build_tables.py                       # writes firmware/include/duet/comfort_table.h
cmake -S firmware -B firmware/build && cmake --build firmware/build --config Release
firmware/build/test_core                             # 142 passed, 0 failed
pytest twin/tests -q                                 # property tests
python console/server.py                             # open http://localhost:8000
```
On Windows with Visual Studio the library is `firmware/build/Release/duet_sil.dll`;
point the twin at it with `set DUET_SIL_LIB=firmware\build\Release\duet_sil.dll`.

Experiments (each writes CSVs to `experiments/out/`):

```bash
python experiments/calibrate.py        # twin vs Indian bedroom field data (4 checks)
python experiments/faults.py           # 7 injected faults, each with a pass rule
python experiments/run_matrix.py --seeds 30   # Duet vs 3 baselines, 3 weathers
```

## How the pieces fit

1. `comfort/build_tables.py` computes PMV with ASHRAE 55's SET-based cooling effect for
   every (temperature, humidity, fan step) and writes two int8 tables (Evening: met 1.0
   clo 0.5; Sleep: met 0.8 clo 0.9) into a C header. The Sleep profile is below the
   library's met 1.0 input limit, so it is generated with `limit_inputs=False` (stated).
2. The firmware core reads sensors through a HAL. On the chip the HAL talks to the SHT40,
   LD2410C and IR hardware; in SIL it is fed by the twin every 5 simulated seconds.
3. Every AC command passes the **safety governor**: 24–28 °C, at most 1 °C per 20 min and
   3 changes per hour, no raise above 60 % RH, nothing while the sensor is untrusted, and
   the user's remote always wins (2 h hold after a user change).
4. The twin computes true comfort at the bed with its own mean radiant temperature, runs
   a user who presses the remote when uncomfortable (and pulls up a blanket when cold
   asleep), and an independent **SafetyMonitor** audits every Duet command.

## Model assumptions to calibrate in Round 3

| Parameter | Value used | Source / status |
| --- | --- | --- |
| Air speed at the bed per fan step | 0.15–0.80 m/s | Assumption inside CBE measured 0.35–0.6 m/s room averages |
| Fan power per step | 4 W + 31 W × (step/6)³ | 35 W top speed is the product spec; curve is an assumption |
| Ceiling-to-bed stratification | 1.0 °C (fan off) to 0.1 °C (step 6) | Assumption; ±30 % per seed in the twin |
| AC part-load COP curve | rated 3.6, peak ×1.3 at 50 % load | Shape assumption; calibrated so the twin matches field data |
| Room R/C values | see `plant.py` | Grey-box; checked by `calibrate.py` |
| Weather | Jaipur synthetic from monthly normals | Replace with the ISHRAE EPW: `Scenario(epw="IND_RJ_Jaipur...epw")` |

## Firmware on the real board

`pio run -e esp32c3` builds the same core with the ESP32 drivers (`platformio.ini`).
Set Wi-Fi, MQTT host and AC brand with build flags. Not compiled in this environment
(the PlatformIO toolchain download was rate-limited); compile it once on a laptop.
