"""Contract tests for the boundary between Python and the compiled firmware (ctypes)."""
import ctypes as C

import pytest

from duet_twin import sil


def test_struct_layout_matches_the_c_library():
    L = sil.lib()
    assert L.duet_sil_sizeof_inputs() == C.sizeof(sil.Inputs)
    assert L.duet_sil_sizeof_outputs() == C.sizeof(sil.Outputs)


def test_version_and_names():
    assert sil.DuetFirmware.version().startswith("duet-fw")
    L = sil.lib()
    assert [L.duet_sil_mode_name(i).decode() for i in range(5)] == sil.MODES
    assert [L.duet_sil_verdict_name(i).decode() for i in range(10)] == sil.VERDICTS


def test_unknown_parameter_is_rejected():
    with pytest.raises(KeyError):
        sil.DuetFirmware({"no_such_knob": 1})


def test_safety_limits_cannot_change_after_start():
    fw = sil.DuetFirmware({"max_setpoint": 27})
    fw.begin(0)
    assert sil.lib().duet_sil_set(fw.h, b"max_setpoint", 30.0) == 0   # refused
    assert sil.lib().duet_sil_set(fw.h, b"preference", 0.2) == 1       # tunable is fine
    fw.close()


def test_step_before_begin_and_begin_twice_are_safe():
    fw = sil.DuetFirmware()
    out = fw.step()                       # no controller yet: must not crash
    assert out.ac_frames == 0
    fw.begin(0)
    fw.begin(1000)                        # restart cleanly
    fw.inp.now_ms, fw.inp.temp_c, fw.inp.rh, fw.inp.crc_ok, fw.inp.sensor_present = 6000, 27.0, 50.0, 1, 1
    assert fw.step().t_sensor == pytest.approx(27.0)
    fw.close()


def _drive(fw, n):
    i = fw.inp
    outs = []
    for k in range(n):
        i.now_ms = 20 * 3600_000 + k * 5000
        i.temp_c, i.rh, i.crc_ok, i.sensor_present, i.occupied = 26.0 + (k % 3) * 0.01, 50.0, 1, 1, 1
        i.remote_kind = 1 if k == 1 else 0
        i.remote_power, i.remote_setpoint = 1, 24
        o = fw.step()
        outs.append((o.mode, o.fan_step, o.ac_frames, o.believed_setpoint, round(o.pmv_est, 4)))
    return outs


def test_firmware_is_deterministic():
    a, b = sil.DuetFirmware(), sil.DuetFirmware()
    a.begin(20 * 3600_000); b.begin(20 * 3600_000)
    assert _drive(a, 2000) == _drive(b, 2000)


def test_log_lines_come_out_in_order():
    fw = sil.DuetFirmware()
    fw.begin(0)
    _drive(fw, 50)
    lines = list(fw.logs())
    assert lines and lines[0][1] == "boot"
    assert [t for t, _, _ in lines] == sorted(t for t, _, _ in lines)
    assert list(fw.logs()) == []          # drained
