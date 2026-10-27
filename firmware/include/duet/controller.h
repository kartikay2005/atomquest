// Duet control core: sensing, mode state machine, two-loop cascade, IR verification.
#pragma once
#include "duet/governor.h"
#include "duet/hal.h"
#include "duet/types.h"

namespace duet {

struct ControllerConfig {
  Millis fan_period = 30 * kSecond;
  Millis ac_period = 5 * kMinute;
  float target_pmv = 0.0f;       // neutral
  float preference = 0.0f;       // app slider: -0.3 (cooler) .. +0.3 (saver)
  float fan_deadband = 0.2f;     // fan may let PMV sit this far above target
  float ac_lower_margin = 0.3f;  // lower the AC if even max fan cannot reach target + this
  int8_t max_auto_fan_step = 5;  // keep step 6 in reserve when planning an AC raise
  Millis hold_after_power_on = 15 * kMinute;
  Millis hold_after_user_change = 2 * kHour;
  Millis fan_hold_after_user = 1 * kHour;
  Millis presence_hold = 2 * kMinute;
  Millis vacancy_timeout = 20 * kMinute;
  // A still sleeper can drop out of the radar's view; be slower to call the room empty at night.
  Millis vacancy_timeout_sleep = 60 * kMinute;
  Millis settle_time = 20 * kMinute;   // no raise until the room has settled
  Millis verify_after = 10 * kMinute;  // IR effect check
  Millis reconcile_after = 15 * kMinute;  // resend belief if the room disagrees this long
  float reconcile_gap_c = 0.8f;
  // Once the room has settled at Duet's believed setpoint, a drift this large that Duet
  // did not cause means someone used the AC remote and Duet missed the frame.
  float unexplained_cold_c = 0.9f;
  float unexplained_warm_c = 1.2f;
  Millis unexplained_after = 10 * kMinute;
  int sleep_start_s = 23 * 3600;       // Sleep comfort profile window (local time)
  int sleep_end_s = 6 * 3600 + 1800;
  // Ceiling-to-bed temperature offset per fan step (canopy pod sees warmer air
  // when the room is stratified). Calibrate in a real room.
  float stratification_c[7] = {1.0f, 0.6f, 0.4f, 0.3f, 0.2f, 0.2f, 0.1f};
  // Sensor plausibility
  float max_jump_c_per_min = 2.0f;
  Millis stuck_after = 10 * kMinute;
  Millis fault_clear_after = 5 * kMinute;
  GovernorLimits limits;
};

struct Counters {
  uint32_t ac_commands = 0;   // approved setpoint changes Duet decided
  uint32_t ac_frames = 0;     // IR frames transmitted (incl. repeats)
  uint32_t ac_resends = 0;    // resends after a failed effect check
  uint32_t reconciles = 0;    // full-state resends because the room disagreed with Duet's belief
  uint32_t missed_remote = 0; // inferred user changes Duet did not receive
  uint32_t fan_commands = 0;
  uint32_t user_ac_presses = 0;
  uint32_t user_fan_presses = 0;
  uint32_t rejected[10] = {}; // by Verdict
  uint32_t sensor_faults = 0;
};

struct Telemetry {
  Millis now = 0;
  Mode mode = Mode::Idle;
  Profile profile = Profile::Evening;
  float t_sensor = 0, rh_sensor = 0;  // filtered sensor values
  float t_occ = 0;                    // estimated temperature at the bed
  float pmv_est = 0;                  // Duet's estimate at the current fan step
  float target_pmv = 0;
  float bias_c = 0;                   // learned room temp minus setpoint
  int8_t fan_step = 0;
  AcState ac;                         // what Duet believes the AC is set to
  bool occupied = false;
  bool sensor_ok = true;
  Verdict last_verdict = Verdict::Approved;
  int8_t last_request = 0;
  Counters counters;
};

class Controller {
 public:
  Controller(const Hal& hal, const ControllerConfig& cfg = ControllerConfig());
  void begin();
  // Call at a steady rate of 1 Hz or slower (the SIL build uses 1/5 Hz).
  void tick();
  const Telemetry& telemetry() const { return tel_; }
  ControllerConfig& config() { return cfg_; }
  void set_preference(float p);

 private:
  void sense(Millis now);
  void handle_remote(Millis now);
  void update_mode(Millis now);
  void fan_loop(Millis now);
  void ac_loop(Millis now);
  void verify(Millis now);
  bool reconcile(Millis now, bool stable);
  bool unexplained_drift(Millis now, float gap);
  void watch_trend(Millis now);
  void assume_user_change(Millis now, const char* why);
  void send_ac(Millis now, int8_t setpoint);
  void set_fan(int8_t step);
  Profile profile_at(Millis now) const;
  float target() const;
  void log(Millis now, const char* tag, const char* fmt, ...);

  Hal hal_;
  ControllerConfig cfg_;
  SafetyGovernor gov_;
  Telemetry tel_;

  // filtering
  static constexpr int kMed = 5;
  float t_buf_[kMed] = {}, rh_buf_[kMed] = {};
  int n_samples_ = 0;
  // fault detection
  float last_raw_t_ = 0, last_raw_rh_ = 0;
  Millis last_raw_time_ = -1;
  Millis same_since_ = 0;
  Millis bad_since_ = -1, good_since_ = -1;
  bool fault_ = false;
  // presence
  Millis last_seen_ = -1;
  // holds
  Millis ac_hold_until_ = -1;
  Millis fan_hold_until_ = -1;
  // loops
  Millis next_fan_ = 0, next_ac_ = 0;
  Millis last_ac_change_ = -1;
  // learned
  float bias_ = 0.0f;
  float learned_offset_ = 0.f;
  bool duet_raised_since_user_ = false;
  // verification
  bool verify_pending_ = false;
  bool verify_resent_ = false;
  Millis verify_at_ = 0;
  float verify_t0_ = 0;
  int verify_dir_ = 0;
  int last_dir_ = 0;            // direction of Duet's last own change
  Millis mismatch_since_ = -1;
  Millis last_reconcile_ = -1;
  bool belief_confirmed_ = false;  // room was seen settled at the believed setpoint
  Millis drift_since_ = -1;
  float extreme_since_change_ = 0;  // peak after a raise, trough after a lower
  bool tracking_trend_ = false;
  Millis reversal_since_ = -1;
  bool vacant_fan_off_sent_ = false;
  float prev_t_occ_ = 0;
  bool prev_t_occ_valid_ = false;
  int stable_count_ = 0;          // consecutive stable 5-min windows
  bool bias_learned_ = false;     // learned from the first settled period after switch-on
  Millis bias_learned_at_ = -1;
  Verdict last_logged_verdict_ = Verdict::Approved;
  int8_t last_logged_request_ = 0;
};

}  // namespace duet
