# Duet testing strategy

Run everything with one command: `python run.py test` (about one minute).

## What can go wrong, and which test catches it

The prototype has four layers. Each layer is tested where its bugs are cheapest to find.

```
        /  Browser end-to-end  \        1 test: the page really updates and its buttons work
       /  HTTP API + engine     \       8 tests: commands, cursors, limits, many tabs
      /  Closed loop (twin + fw) \      10 tests + 8 fault scenarios + 3 x 30-seed matrix
     /  Contract (Python <-> C++) \     7 tests: struct layout, determinism, misuse
    /  Units: firmware + physics   \    142 C++ checks, 24 physics tests, 2 property fuzzers
```

| Layer | Risk | Test type | File |
| --- | --- | --- | --- |
| Safety governor | Approves an unsafe AC change | Unit checks per rule + property fuzz (400 random request histories) | `firmware/test/test_core.cpp`, `twin/tests/test_safety_properties.py` |
| Whole controller | Some sequence of sensor readings, remote presses and faults leads to an unsafe command | Property fuzz of the real firmware (300 histories, 800+ audited AC changes; a guard test fails if the fuzz stops reaching the AC) | `twin/tests/test_safety_properties.py` |
| Comfort lookup | Table and library disagree | Unit (C++ against known PMV values) and twin grid against pythermalcomfort | `test_core.cpp`, `tests/test_twin_physics.py` |
| Python ↔ C++ bridge | Struct layout drift corrupts memory; stale library | Contract: sizes compared with the C library at load time and in tests; misuse (step before begin, begin twice, limit change after start) | `tests/test_firmware_contract.py`, `twin/duet_twin/sil.py` |
| Physics models | Room, AC, fan or weather wrong | Unit: energy balance direction, COP vs temperature, latent share vs setpoint, fan power curve, weather shape per month | `tests/test_twin_physics.py` |
| Model credibility | The twin is not the real world | Calibration against Indian bedroom field data (4 checks) | `experiments/calibrate.py` |
| Closed loop | Determinism, pairing, safety over a night | Integration: same seed same result, paired rooms share weather and person, zero violations | `tests/test_simulation.py` |
| Fault handling | Duet misbehaves under faults | 8 scenarios with pass rules written before running; 4 also run as tests | `experiments/faults.py`, `tests/test_simulation.py` |
| Console server | Bad input crashes it; tabs steal each other's data; path traversal | API tests on a real server: validation, 413 limit, 404 whitelist, two clients with one cursor, replay resets cursors | `tests/test_console_api.py` |
| Console page | Page loads but never updates (the bug users hit first) | Browser test with Chromium: clock advances, fault appears in log, scenario switch works, no JS errors | `tests/test_e2e_browser.py` (skipped if Playwright is absent) |

## Independent checking

The simulation contains a `SafetyMonitor` that re-checks every AC command Duet makes
from outside the firmware, using its own copy of the rules. It is how the one real
safety bug was found (a missed remote press let Duet override the user once in 30
simulated weeks); see `docs/RESULTS.md` section 4.

## Not covered yet (Round 3)

- The ESP32 drivers (`firmware/src/esp32/`) are compiled only on the target; test them
  on the board with a logic analyser on I2C, UART and the IR LED.
- Real IR frames for each AC brand: record the brand's remote once and replay it.
- Long-run soak test on hardware (7 days) for timer wrap-around and memory.
