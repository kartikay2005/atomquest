#include "duet/governor.h"

namespace duet {

Verdict SafetyGovernor::check(const GovernorContext& ctx, int8_t req) const {
  if (!ctx.current.known) return Verdict::Unknown;
  if (!ctx.current.power) return Verdict::AcOff;
  if (ctx.user_hold) return Verdict::UserHold;
  if (!ctx.sensor_ok) return Verdict::SensorFault;
  if (req < lim_.min_setpoint_c || req > lim_.max_setpoint_c) return Verdict::OutOfRange;
  const int delta = static_cast<int>(req) - static_cast<int>(ctx.current.setpoint_c);
  if (delta == 0) return Verdict::Approved;  // no-op resend is always safe
  if (delta > lim_.max_step_c || delta < -lim_.max_step_c) return Verdict::StepTooLarge;
  if (delta > 0 && ctx.rh > lim_.max_rh_for_raise) return Verdict::HumidityBlock;
  if (count_ > 0) {
    const Millis last = history_[(count_ - 1) % kHistory];
    if (ctx.now - last < lim_.min_interval) return Verdict::TooSoon;
  }
  if (changes_last_hour(ctx.now) >= lim_.max_changes_per_hour) return Verdict::HourlyLimit;
  return Verdict::Approved;
}

void SafetyGovernor::record_change(Millis t) {
  history_[count_ % kHistory] = t;
  ++count_;
}

int SafetyGovernor::changes_last_hour(Millis now) const {
  int n = 0;
  const int stored = count_ < kHistory ? count_ : kHistory;
  for (int i = 0; i < stored; ++i) {
    const Millis t = history_[(count_ - 1 - i) % kHistory];
    if (now - t < kHour) ++n;
  }
  return n;
}

void SafetyGovernor::reset() { count_ = 0; }

const char* verdict_name(Verdict v) {
  switch (v) {
    case Verdict::Approved: return "approved";
    case Verdict::OutOfRange: return "out_of_range";
    case Verdict::StepTooLarge: return "step_too_large";
    case Verdict::TooSoon: return "too_soon";
    case Verdict::HourlyLimit: return "hourly_limit";
    case Verdict::HumidityBlock: return "humidity_block";
    case Verdict::SensorFault: return "sensor_fault";
    case Verdict::UserHold: return "user_hold";
    case Verdict::AcOff: return "ac_off";
    case Verdict::Unknown: return "ac_unknown";
  }
  return "?";
}

const char* mode_name(Mode m) {
  switch (m) {
    case Mode::Auto: return "auto";
    case Mode::UserHold: return "user_hold";
    case Mode::Safe: return "safe";
    case Mode::Vacant: return "vacant";
    case Mode::Idle: return "idle";
  }
  return "?";
}

}  // namespace duet
