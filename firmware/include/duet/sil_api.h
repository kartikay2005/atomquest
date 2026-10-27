// C ABI for software-in-the-loop: the Python twin drives the real control core
// through these calls (ctypes). Mirrors the HAL; nothing here exists on the chip.
#pragma once
#include <stdint.h>

#if defined(_WIN32)
#define DUET_API __declspec(dllexport)
#else
#define DUET_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int64_t now_ms;          // virtual clock (ms since day 0, 00:00 local)
  float temp_c;            // SHT40 reading as the twin produced it
  float rh;
  uint8_t crc_ok;
  uint8_t sensor_present;
  uint8_t occupied;        // LD2410C presence output
  uint8_t remote_kind;     // 0 none, 1 AC frame, 2 fan code
  uint8_t remote_power;
  int8_t remote_setpoint;
  int8_t remote_fan_step;
  uint8_t _pad;
} DuetSilInputs;

typedef struct {
  // actuation produced during this tick
  uint8_t ac_frames;       // IR frames sent this tick (0, 2 ...)
  uint8_t ac_power;        // state carried by those frames
  int8_t ac_setpoint;
  uint8_t fan_cmd;         // 1 if a fan command was sent this tick
  int8_t fan_step;
  // telemetry
  uint8_t mode;            // duet::Mode
  uint8_t profile;         // duet::Profile
  uint8_t sensor_ok;
  uint8_t occupied;
  int8_t believed_setpoint;
  uint8_t believed_power;
  uint8_t last_verdict;
  float t_sensor, rh_sensor, t_occ, pmv_est, target_pmv, bias_c;
  uint32_t ac_commands, ac_frames_total, ac_resends, fan_commands, sensor_faults;
  uint32_t user_ac_presses, user_fan_presses, reconciles, missed_remote;
  uint32_t rejected[10];
} DuetSilOutputs;

DUET_API void* duet_sil_create(void);
DUET_API void duet_sil_destroy(void* h);
// Set a tunable before begin(). Returns 0 if the key is unknown.
DUET_API int duet_sil_set(void* h, const char* key, double value);
DUET_API void duet_sil_begin(void* h, int64_t now_ms);
DUET_API void duet_sil_step(void* h, const DuetSilInputs* in, DuetSilOutputs* out);
// Pop one log line. Returns 1 if a line was written.
DUET_API int duet_sil_pop_log(void* h, int64_t* t_ms, char* tag, int tag_len, char* msg, int msg_len);
// Direct access to the safety governor, for property-based tests.
DUET_API void* duet_gov_create(void);
DUET_API void duet_gov_destroy(void* g);
DUET_API int duet_gov_check(void* g, int64_t now_ms, int known, int power, int setpoint, float rh,
                   int sensor_ok, int user_hold, int requested);
DUET_API void duet_gov_record(void* g, int64_t now_ms);

DUET_API const char* duet_sil_verdict_name(int v);
DUET_API const char* duet_sil_mode_name(int m);
DUET_API const char* duet_sil_version(void);
// ABI check: Python compares these with ctypes.sizeof() before trusting the structs.
DUET_API int duet_sil_sizeof_inputs(void);
DUET_API int duet_sil_sizeof_outputs(void);

#ifdef __cplusplus
}
#endif
