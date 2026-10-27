"""Duet launcher: one command that checks everything, builds what is missing, and runs.

    python run.py              check, build if needed, start the console, open the browser
    python run.py check        only run the checks (no server)
    python run.py test         run every automated test
    python run.py experiments  regenerate all results and figures (takes ~15 min)
    python run.py build        force a rebuild of the firmware library
    add --no-browser to start the console without opening a browser tab

Works on Windows, macOS and Linux with Python 3.10+.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "twin"))
NEEDED = ["numpy", "scipy", "pandas", "pythermalcomfort"]
TEST_NEEDED = ["pytest", "hypothesis", "matplotlib"]

OK, BAD, WARN = "[ ok ]", "[FAIL]", "[warn]"


def say(tag: str, msg: str):
    print(f"{tag} {msg}", flush=True)


def check_python():
    if sys.version_info < (3, 10):
        say(BAD, f"Python {sys.version.split()[0]} is too old. Install Python 3.10 or newer.")
        sys.exit(1)
    say(OK, f"Python {sys.version.split()[0]}")


def check_packages(names: list[str]) -> bool:
    missing = []
    for n in names:
        try:
            importlib.import_module(n)
        except ImportError:
            missing.append(n)
    if missing:
        say(BAD, "Missing Python packages: " + ", ".join(missing))
        print(f"       Install them with:\n         {sys.executable} -m pip install -r requirements.txt")
        return False
    say(OK, "Python packages: " + ", ".join(names))
    return True


def ensure_comfort_tables() -> bool:
    header = ROOT / "firmware" / "include" / "duet" / "comfort_table.h"
    grid = ROOT / "comfort" / "out" / "truth_grid.npz"
    if header.exists() and grid.exists():
        say(OK, "Comfort tables present")
        return True
    say(WARN, "Comfort tables missing - generating them (about 20 s)")
    r = subprocess.run([sys.executable, str(ROOT / "comfort" / "build_tables.py")])
    if r.returncode != 0:
        say(BAD, "Could not build comfort tables")
        return False
    say(OK, "Comfort tables generated")
    return True


def build_firmware(force: bool = False) -> bool:
    cmake = shutil.which("cmake")
    if not cmake:
        say(WARN, "CMake not found - cannot build the firmware library from source")
        return False
    build = ROOT / "firmware" / "build"
    if force and build.exists():
        shutil.rmtree(build)
    say(WARN, "Building the firmware library with CMake")
    cfg = [cmake, "-S", str(ROOT / "firmware"), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"]
    if os.name == "nt" and shutil.which("g++") and not shutil.which("cl"):
        cfg += ["-G", "MinGW Makefiles"]
    r1 = subprocess.run(cfg)
    r2 = subprocess.run([cmake, "--build", str(build), "--config", "Release"]) if r1.returncode == 0 else r1
    if r2.returncode != 0:
        say(BAD, "Firmware build failed (see the compiler output above)")
        return False
    return True


def check_firmware(force_build: bool = False) -> bool:
    from duet_twin import sil
    if force_build or sil.find_lib() is None:
        built = build_firmware(force_build)
        if not built and sil.find_lib() is None:
            say(BAD, "No firmware library for this computer.")
            print("       Install CMake and a C++ compiler (Windows: 'Visual Studio Build Tools' with the C++\n"
                  "       workload, or MinGW-w64), then run `python run.py build`.\n"
                  "       Prebuilt libraries are included for Windows x64 and Linux x64.")
            return False
    try:
        lib = sil.lib()
        fw = sil.DuetFirmware()
        fw.begin(0)
        fw.step()
        fw.close()
    except Exception as e:  # stale or broken library
        say(BAD, f"Firmware library did not load: {e}")
        if build_firmware(force=True):
            say(WARN, "Rebuilt - run the command again")
        return False
    say(OK, f"Firmware library {sil.DuetFirmware.version()}  ({Path(lib._duet_path).relative_to(ROOT)})")
    return True


def smoke_test() -> bool:
    """Run one simulated hour of both rooms: proves the whole loop is connected."""
    from duet_twin import Scenario, Simulation
    try:
        for arm in ("b2", "duet"):
            sim = Simulation(Scenario(weather_name="hottest_day", seed=0, arm=arm, occupants=1))
            sim.run(22.5 * 3600 + 86400)
        assert sim.out is not None and sim.rows, "Duet produced no output"
    except Exception as e:
        say(BAD, f"Simulation smoke test failed: {type(e).__name__}: {e}")
        return False
    say(OK, f"Closed loop works: firmware mode '{['auto','user_hold','safe','vacant','idle'][sim.out.mode]}', "
            f"room {sim.room.s.t_air:.1f} °C, AC {sim.ac.s.setpoint} °C")
    return True


def checks(force_build=False) -> bool:
    print("Duet - checking your setup\n")
    check_python()
    return (check_packages(NEEDED) and ensure_comfort_tables() and check_firmware(force_build)
            and smoke_test())


def run_tests() -> int:
    if not (checks() and check_packages(TEST_NEEDED)):
        return 1
    from duet_twin import sil
    exe = next((p for p in [ROOT / "firmware" / "build" / n for n in ("test_core", "test_core.exe")]
                + [Path(sil.find_lib()).parent / "test_core.exe"] if p.exists()), None)
    rc = 0
    if exe:
        print(f"\n== Firmware unit tests ({exe.name})")
        rc |= subprocess.run([str(exe)]).returncode
    else:
        say(WARN, "Firmware unit-test binary not built (needs CMake); skipping")
    print("\n== Python tests (twin, firmware contract, safety properties, console API)")
    rc |= subprocess.run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests"), str(ROOT / "twin" / "tests")]).returncode
    print("\nALL TESTS PASSED" if rc == 0 else "\nSOME TESTS FAILED")
    return rc


def run_experiments() -> int:
    if not (checks() and check_packages(TEST_NEEDED)):
        return 1
    for script, args in (("calibrate.py", []), ("faults.py", []), ("run_matrix.py", ["--seeds", "30"]),
                         ("make_figures.py", [])):
        print(f"\n== experiments/{script}")
        r = subprocess.run([sys.executable, str(ROOT / "experiments" / script), *args])
        if r.returncode != 0:
            say(BAD, f"{script} failed")
            return r.returncode
    say(OK, "Results in experiments/out/ (also shown on the console's Results tab)")
    return 0


def main():
    os.chdir(ROOT)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    cmd = args[0] if args else "run"
    if cmd == "check":
        sys.exit(0 if checks() else 1)
    if cmd == "build":
        sys.exit(0 if checks(force_build=True) else 1)
    if cmd == "test":
        sys.exit(run_tests())
    if cmd == "experiments":
        sys.exit(run_experiments())
    if cmd != "run":
        print(__doc__)
        sys.exit(2)
    if not checks():
        print("\nFix the item marked [FAIL] above, then run `python run.py` again.")
        sys.exit(1)
    print()
    sys.path.insert(0, str(ROOT / "console"))
    from server import serve
    serve(open_browser="--no-browser" not in sys.argv)


if __name__ == "__main__":
    main()
