// Safety governor: the only path from the control core to the AC's IR transmitter.
// Enforces the five rules from the Duet abstract. Pure logic, unit- and property-tested.
#pragma once
#include "duet/types.h"

namespace duet {

struct GovernorLimits {
  int8_t min_setpoint_c = 24;
  int8_t max_setpoint_c = 28;
  int8_t max_step_c = 1;
  Millis min_interval = 20 * kMinute;  // at most 1 C per 20 min
  int max_changes_per_hour = 3;
  float max_rh_for_raise = 60.f;
};

struct GovernorContext {
  Millis now = 0;
  AcState current;        // what Duet believes the AC is set to
  float rh = 0.f;         // filtered humidity, %
  bool sensor_ok = true;  // climate sensor trusted
  bool user_hold = false; // the user's remote was used recently
};

class SafetyGovernor {
 public:
  explicit SafetyGovernor(const GovernorLimits& lim = GovernorLimits()) : lim_(lim) {}

  // Decide on a requested new setpoint. Does not record anything.
  Verdict check(const GovernorContext& ctx, int8_t requested_setpoint_c) const;
  // Record that an approved change was transmitted at time t.
  void record_change(Millis t);
  // Number of Duet-made changes in the 60 min before `now`.
  int changes_last_hour(Millis now) const;
  const GovernorLimits& limits() const { return lim_; }
  void reset();

 private:
  static constexpr int kHistory = 8;
  GovernorLimits lim_;
  Millis history_[kHistory] = {};
  int count_ = 0;   // total recorded (ring buffer index = count_ % kHistory)
};

}  // namespace duet
