"""ctypes binding to the firmware's software-in-the-loop library (libduet_sil.so)."""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path

LIB_DEFAULT = Path(__file__).resolve().parents[2] / "firmware" / "build" / (
    "duet_sil.dll" if os.name == "nt" else "libduet_sil.so")

MODES = ["auto", "user_hold", "safe", "vacant", "idle"]
VERDICTS = ["approved", "out_of_range", "step_too_large", "too_soon", "hourly_limit",
            "humidity_block", "sensor_fault", "user_hold", "ac_off", "ac_unknown"]


class Inputs(C.Structure):
    _fields_ = [("now_ms", C.c_int64), ("temp_c", C.c_float), ("rh", C.c_float),
                ("crc_ok", C.c_uint8), ("sensor_present", C.c_uint8), ("occupied", C.c_uint8),
                ("remote_kind", C.c_uint8), ("remote_power", C.c_uint8),
                ("remote_setpoint", C.c_int8), ("remote_fan_step", C.c_int8), ("_pad", C.c_uint8)]


class Outputs(C.Structure):
    _fields_ = [("ac_frames", C.c_uint8), ("ac_power", C.c_uint8), ("ac_setpoint", C.c_int8),
                ("fan_cmd", C.c_uint8), ("fan_step", C.c_int8), ("mode", C.c_uint8),
                ("profile", C.c_uint8), ("sensor_ok", C.c_uint8), ("occupied", C.c_uint8),
                ("believed_setpoint", C.c_int8), ("believed_power", C.c_uint8),
                ("last_verdict", C.c_uint8),
                ("t_sensor", C.c_float), ("rh_sensor", C.c_float), ("t_occ", C.c_float),
                ("pmv_est", C.c_float), ("target_pmv", C.c_float), ("bias_c", C.c_float),
                ("ac_commands", C.c_uint32), ("ac_frames_total", C.c_uint32),
                ("ac_resends", C.c_uint32), ("fan_commands", C.c_uint32),
                ("sensor_faults", C.c_uint32), ("user_ac_presses", C.c_uint32),
                ("user_fan_presses", C.c_uint32), ("reconciles", C.c_uint32), ("missed_remote", C.c_uint32),
                ("rejected", C.c_uint32 * 10)]


_lib = None


def lib(path: str | Path | None = None):
    global _lib
    if _lib is None:
        p = Path(path or os.environ.get("DUET_SIL_LIB", LIB_DEFAULT))
        if not p.exists():
            raise FileNotFoundError(f"{p} not found - build it: cmake -S firmware -B firmware/build && cmake --build firmware/build")
        L = C.CDLL(str(p))
        L.duet_sil_create.restype = C.c_void_p
        L.duet_sil_destroy.argtypes = [C.c_void_p]
        L.duet_sil_set.argtypes = [C.c_void_p, C.c_char_p, C.c_double]
        L.duet_sil_set.restype = C.c_int
        L.duet_sil_begin.argtypes = [C.c_void_p, C.c_int64]
        L.duet_sil_step.argtypes = [C.c_void_p, C.POINTER(Inputs), C.POINTER(Outputs)]
        L.duet_sil_pop_log.argtypes = [C.c_void_p, C.POINTER(C.c_int64), C.c_char_p, C.c_int,
                                       C.c_char_p, C.c_int]
        L.duet_sil_pop_log.restype = C.c_int
        L.duet_sil_version.restype = C.c_char_p
        _lib = L
    return _lib


class DuetFirmware:
    """One Duet device running the real control core."""

    def __init__(self, params: dict | None = None):
        self.L = lib()
        self.h = C.c_void_p(self.L.duet_sil_create())
        for k, v in (params or {}).items():
            if not self.L.duet_sil_set(self.h, k.encode(), float(v)):
                raise KeyError(f"unknown firmware parameter {k}")
        self.inp = Inputs()
        self.out = Outputs()
        self._tag = C.create_string_buffer(16)
        self._msg = C.create_string_buffer(160)
        self._t = C.c_int64()

    def begin(self, now_ms: int) -> None:
        self.L.duet_sil_begin(self.h, now_ms)

    def step(self) -> Outputs:
        self.L.duet_sil_step(self.h, C.byref(self.inp), C.byref(self.out))
        return self.out

    def set(self, key: str, value: float) -> None:
        self.L.duet_sil_set(self.h, key.encode(), float(value))

    def logs(self):
        while self.L.duet_sil_pop_log(self.h, C.byref(self._t), self._tag, 16, self._msg, 160):
            yield self._t.value, self._tag.value.decode(), self._msg.value.decode()

    def close(self) -> None:
        if self.h:
            self.L.duet_sil_destroy(self.h)
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def version() -> str:
        return lib().duet_sil_version().decode()
