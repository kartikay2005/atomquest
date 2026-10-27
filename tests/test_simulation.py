"""Integration tests: twin + real firmware in closed loop."""
import pytest

from duet_twin import Fault, Scenario, Simulation
from duet_twin.sim import SafetyMonitor

ONE_NIGHT = 2 * 86400  # warm-up day + one night (hottest_day set is two days)


def run(arm, seed=0, **kw):
    return Simulation(Scenario(weather_name="hottest_day", seed=seed, arm=arm, occupants=1, **kw)).run()


def test_same_seed_same_result():
    a, b = run("duet", 4), run("duet", 4)
    assert a.metrics() == b.metrics()


def test_paired_rooms_share_weather_and_person():
    a, b = run("b2", 5), run("duet", 5)
    assert [n.arrive for n in a.user.nights] == [n.arrive for n in b.user.nights]
    assert (a.weather.t_out == b.weather.t_out).all()


def test_duet_night_is_safe_and_comfortable():
    d, b = run("duet", 1), run("b2", 1)
    md, mb = d.metrics(), b.metrics()
    assert md["safety_violations"] == 0
    assert md["comfort_in_band"] >= mb["comfort_in_band"] - 0.02
    assert md["user_presses"] <= mb["user_presses"]


def test_reusing_a_scenario_fires_its_faults_every_time():
    sc = Scenario(weather_name="hottest_day", seed=2, arm="duet", occupants=1,
                  faults=[Fault("remote_press", 86400 + 23.5 * 3600, 0, 25)])
    for _ in range(2):
        sim = Simulation(sc).run()
        assert any("presses remote" in e[2] for e in sim.events)
    assert sc.faults[0].fired is False          # the caller's object is untouched


def test_safety_monitor_catches_each_rule():
    m = SafetyMonitor()
    m.duet_command(0, 24, 26, 50, None)               # step > 1
    m.duet_command(100, 26, 27, 50, None)             # too soon
    m.duet_command(5000, 27, 29, 50, None)            # out of range + step
    m.duet_command(9000, 27, 28, 70, None)            # humidity
    m.user_pressed(10000, False)
    m.duet_command(10500, 28, 27, 50, None)           # during hold
    kinds = " ".join(v for _, v in m.violations)
    for word in ("larger than 1", "less than 20 min", "outside 24-28", "raise at RH", "user hold"):
        assert word in kinds


def test_sensor_fault_puts_duet_in_safe_mode():
    sim = run("duet", 3, faults=[Fault("sensor_stuck", 86400 + 23.5 * 3600, 3600)])
    df = sim.frame()
    w = df[(df.t > 86400 + 23.5 * 3600 + 11 * 60) & (df.t < 86400 + 24.5 * 3600)]
    assert (w["mode"] == "safe").all()
    assert sim.metrics()["safety_violations"] == 0


@pytest.mark.parametrize("scenario", ["scenario_remote_unseen", "scenario_ir_burst", "scenario_humidity",
                                      "scenario_remote"])
def test_fault_scenarios_pass(scenario):
    """The same pass rules as experiments/faults.py, run as tests."""
    import faults
    ok, detail, sim = getattr(faults, scenario)()
    assert ok, detail
    assert not sim.monitor.violations, sim.monitor.violations
