# Duet — software prototype

**AtomQuest 2026 · Track 1 (Fan) · Team Edision**

Duet makes a ceiling fan and a split AC work together. Moving air makes a room feel cooler,
so Duet lets the AC run a few degrees warmer and turns the fan up just enough that you feel
the same. The AC is the big electricity user, so the bill goes down while you stay comfortable.

There is no hardware yet, so this repo shows Duet as a **virtual hardware prototype**: the
real device code runs against a simulated bedroom, and you can watch the two side by side.

---

## Run it (Windows, macOS or Linux)

You need **Python 3.10 or newer**. In a terminal, inside this folder:

```bash
pip install -r requirements.txt
python run.py
```

`run.py` checks everything, builds anything missing, starts the console and opens it in
your browser at **http://127.0.0.1:8000**. Leave the terminal window open while you use it;
close it (or press Ctrl+C) to stop.

You do **not** need a C++ compiler on Windows x64 or Linux x64: a ready-built copy of the
firmware library is included. On a Mac, install CMake and Xcode's command line tools once.

Other commands:

| Command | What it does |
| --- | --- |
| `python run.py check` | Only check the setup and run a one-hour simulation |
| `python run.py test` | Run every automated test (about a minute) |
| `python run.py experiments` | Recompute all results and figures (about 15 minutes) |
| `python run.py build` | Rebuild the firmware library from source (needs CMake + a compiler) |

---

## How it works, in the simplest terms

Think of two identical bedrooms in Jaipur on the same night, with the same person in each:

- **Normal room:** the person sets the AC to 24 °C and leaves the fan on medium, like most homes.
- **Duet room:** the same, but Duet is watching and quietly adjusting the AC and the fan.

Both rooms are simulated on your computer. Duet's "brain" is the **actual code that would
run on the device**, not an imitation of it.

### The five pieces

```
   ┌──────────┐   heat    ┌──────────────┐   air temp,   ┌────────────┐
   │ Weather  │ ────────▶ │ Bedroom twin │ ─ humidity ─▶ │ Sensor pod │
   │ (Jaipur) │           │ walls, air,  │               │ SHT40 +    │
   └──────────┘           │ person       │ ◀── air ───┐  │ radar      │
                          └──────────────┘    speed   │  └─────┬──────┘
                              ▲                        │        │ readings
                      cooling │                   ┌────┴───┐    ▼
                          ┌───┴──────┐            │  Fan   │ ┌──────────────┐
                          │ Split AC │            └────▲───┘ │ Duet firmware│
                          └───▲──────┘                 └──── │ (real C++)   │
                              │ IR frame  ┌──────────┐ fan   └──────┬───────┘
                              └────────── │ Safety   │ ◀── request ─┘
                                          │ governor │
                                          └──────────┘
```

1. **Weather** (`twin/duet_twin/weather.py`): outside temperature, humidity and sun for any
   month from March to October, built from Jaipur's climate averages.
2. **Bedroom twin** (`twin/duet_twin/plant.py`): a small physics model of the room. Heat
   leaks in through the wall and window, the person adds heat and moisture, and the AC
   takes heat out. It also models the split AC (an inverter that slows down as the room
   nears its setpoint) and the fan (power rises steeply with speed).
3. **Sensor pod** (simulated SHT40 and radar): reads the air near the ceiling, with the same
   noise, delay and occasional faults a real sensor has.
4. **Duet firmware** (`firmware/src/core/`): the real C++ code. Every 30 seconds it picks the
   lowest fan speed that keeps you comfortable. Every 5 minutes it asks, *"would one degree
   warmer still be comfortable if the fan helped?"* If yes, it asks to raise the AC.
5. **Safety governor** (`firmware/src/core/governor.cpp`): the only way to reach the AC.
   It refuses anything outside 24–28 °C, more than 1 °C per 20 minutes, more than 3
   changes an hour, any raise above 60 % humidity, anything while the sensor is faulty,
   and anything after the person used their own remote (they always win).

### How the pieces talk to each other

This is the part that is hard to see, so step by step:

- **Python ↔ C++.** The firmware is compiled into a library file
  (`libduet_sil.dll` on Windows, `.so` on Linux). Python loads it with `ctypes`, a standard
  way to call C code from Python. Every 5 simulated seconds Python packs the sensor
  readings into a small record and calls one function, `duet_sil_step()`. The firmware
  replies with what it wants to do: change the fan, send an IR frame, and its current
  thinking. That function call stands in for the wires on the real board. File:
  `twin/duet_twin/sil.py`.
- **Simulation ↔ browser.** `console/engine.py` runs both rooms in a background thread.
  `console/server.py` is a tiny web server (Python's built-in one). Four times a second
  the page asks it "what's new since my last question?" and draws the answer. Buttons send
  short commands back, like "inject a sensor fault".
- **On the real device** the same firmware files are compiled for the ESP32-C3 chip, and
  the record-passing is replaced by real drivers (`firmware/src/esp32/`): I2C for the SHT40,
  UART for the radar, and an IR LED for the AC.

Open the console's **How it connects** tab to see this loop drawn with live values.

### What happens every 5 simulated seconds

1. The twin moves the room forward 5 seconds: heat in, cooling out, moisture in and out.
2. The sensor pod reads the room (with noise) and the radar says whether someone is there.
3. Python calls the firmware with those readings.
4. The firmware updates its picture of the room and, when its timers say so, decides on
   the fan and the AC.
5. Any AC change goes through the safety governor, then out as an IR frame. Some frames
   are lost on purpose, like in a real room, so you can see Duet notice and resend.
6. The twin applies the new AC setting and fan speed, and the loop repeats.

A simulated person lives in both rooms. If they feel too warm for 10 minutes they press
the AC remote, and if they feel cold while asleep they pull up a blanket. Fewer remote
presses and fewer blankets mean Duet kept them comfortable.

---

## Using the console

- **Scenario:** pick a month (May is hot and dry, August is humid), a start date, the
  number of nights, a heatwave offset, one or two people, how lossy the IR link is, and
  which normal room to compare against. Press *Apply and restart*.
- **Time:** ▶/❚❚ or the space bar plays and pauses. Speeds go from real time to 3600×
  (one simulated hour per second). Click the timeline, or 21:00 / Midnight / 03:00 / 06:00,
  to jump. Jumping backwards replays the run exactly, because the same seed gives the same night.
- **Try to break it:** inject faults or press the user's remote, then read the firmware log.
- **Results tab:** the experiment tables and the report figures.

## What the experiments found

See `docs/RESULTS.md`. In short: against a room at 24 °C with the fan already on medium,
Duet saved about 20 % of AC energy in a May week and 19 % in a monsoon week, kept people
comfortable more of the time, and broke zero safety rules in 90 simulated weeks. On the
hottest night it saved nothing, because the AC had no slack to give.

## Folder map

```
run.py            one command to check, build, test and run
requirements.txt  Python packages
comfort/          builds the ASHRAE 55 comfort tables the firmware uses
firmware/         the device code (C++), its tests, ESP32 drivers, prebuilt libraries
twin/             the simulated bedroom, weather, person, and the Python <-> C++ bridge
console/          the web console (server + one HTML page)
experiments/      calibration, fault scenarios, Duet-vs-normal experiment, figures
tests/            automated tests (see docs/TESTING.md)
docs/             RESULTS.md, TESTING.md
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Page loads but every number shows "—" | The page cannot reach the simulation. Check the terminal for an error, then reload. (Older versions needed an extra WebSocket package; this version uses plain HTTP and needs nothing extra.) |
| "Server offline" in the top bar | The terminal running `run.py` was closed. Start it again. |
| `Missing Python packages` | Run `pip install -r requirements.txt` with the same Python you use for `run.py`. |
| `No firmware library for this computer` | Install CMake and a C++ compiler, then `python run.py build`. Windows x64 and Linux x64 need neither. |
| `struct sizes ... do not match` | An old build of the firmware library: `python run.py build`. |
| Port 8000 is busy | The console picks the next free port and prints it in the terminal. |

## Honest limits

All numbers are simulated. The weather is generated from Jaipur's monthly averages until an
ISHRAE weather file is dropped into `twin/data/` (the console then offers it). The fan's air
speed per step, its power curve and the ceiling-to-bed temperature difference are assumptions
to measure in Round 3 with a real fan, an anemometer and a plug-in energy meter.
