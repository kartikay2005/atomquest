#include "duet/controller.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>

#include "duet/comfort.h"

namespace duet {
namespace {

float median5(const float* v, int n) {
  float a[5] = {0, 0, 0, 0, 0};
  for (int i = 0; i < n; ++i) a[i] = v[i];
  for (int i = 1; i < n; ++i)  // insertion sort, n <= 5
    for (int j = i; j > 0 && a[j - 1] > a[j]; --j) {
      float t = a[j]; a[j] = a[j - 1]; a[j - 1] = t;
    }
  return a[n / 2];
}

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

}  // namespace

Controller::Controller(const Hal& hal, const ControllerConfig& cfg)
    : hal_(hal), cfg_(cfg), gov_(cfg.limits) {}

void Controller::begin() {
  const Millis now = hal_.clock->now();
  next_fan_ = now;
  next_ac_ = now + cfg_.ac_period;
  tel_ = Telemetry();
  tel_.now = now;
  log(now, "boot", "duet controller started");
}

void Controller::set_preference(float p) { cfg_.preference = clampf(p, -0.3f, 0.3f); }

float Controller::target() const {
  return cfg_.target_pmv + cfg_.preference + learned_offset_;
}

Profile Controller::profile_at(Millis now) const {
  const int tod = static_cast<int>((now / kSecond) % 86400);
  const bool sleep = cfg_.sleep_start_s > cfg_.sleep_end_s
                         ? (tod >= cfg_.sleep_start_s || tod < cfg_.sleep_end_s)
                         : (tod >= cfg_.sleep_start_s && tod < cfg_.sleep_end_s);
  return sleep ? Profile::Sleep : Profile::Evening;
}

void Controller::tick() {
  const Millis now = hal_.clock->now();
  tel_.now = now;
  tel_.profile = profile_at(now);
  sense(now);
  tel_.occupied = hal_.presence->occupied();
  if (tel_.occupied) last_seen_ = now;
  handle_remote(now);
  update_mode(now);
  if (now >= next_fan_) { fan_loop(now); next_fan_ = now + cfg_.fan_period; }
  if (now >= next_ac_) { ac_loop(now); next_ac_ = now + cfg_.ac_period; }
  verify(now);
  watch_trend(now);

  // telemetry
  const int s = tel_.fan_step;
  tel_.t_occ = tel_.t_sensor - cfg_.stratification_c[s < 0 ? 0 : (s > 6 ? 6 : s)];
  tel_.pmv_est = Comfort::pmv(tel_.profile, tel_.t_occ, tel_.rh_sensor, s);
  tel_.target_pmv = target();
  tel_.bias_c = bias_;
  tel_.sensor_ok = !fault_;
}

// ---------------------------------------------------------------- sensing
void Controller::sense(Millis now) {
  const ClimateReading r = hal_.climate->read();
  bool bad = !r.present || !r.crc_ok || r.temp_c < -10.f || r.temp_c > 60.f || r.rh < 0.f ||
             r.rh > 100.f;
  if (!bad && last_raw_time_ >= 0) {
    const float dt_min = (now - last_raw_time_) / float(kMinute);
    if (dt_min > 0 && fabsf(r.temp_c - last_raw_t_) / dt_min > cfg_.max_jump_c_per_min &&
        fabsf(r.temp_c - last_raw_t_) > 0.5f)
      bad = true;
    // A healthy SHT40 never repeats to the last digit for 10 minutes.
    if (r.temp_c == last_raw_t_ && r.rh == last_raw_rh_) {
      if (now - same_since_ >= cfg_.stuck_after) bad = true;
    } else {
      same_since_ = now;
    }
  } else if (last_raw_time_ < 0) {
    same_since_ = now;
  }
  if (r.present && r.crc_ok) { last_raw_t_ = r.temp_c; last_raw_rh_ = r.rh; last_raw_time_ = now; }

  if (bad) {
    good_since_ = -1;
    if (!fault_) {
      fault_ = true;
      tel_.counters.sensor_faults++;
      log(now, "fault", "climate sensor untrusted (t=%.2f rh=%.1f crc=%d)", r.temp_c, r.rh,
          r.crc_ok);
    }
    return;
  }
  if (fault_) {
    if (good_since_ < 0) good_since_ = now;
    if (now - good_since_ >= cfg_.fault_clear_after) {
      fault_ = false;
      n_samples_ = 0;
      log(now, "fault", "climate sensor recovered");
    }
  }
  // median filter
  for (int i = kMed - 1; i > 0; --i) { t_buf_[i] = t_buf_[i - 1]; rh_buf_[i] = rh_buf_[i - 1]; }
  t_buf_[0] = r.temp_c;
  rh_buf_[0] = r.rh;
  if (n_samples_ < kMed) ++n_samples_;
  tel_.t_sensor = median5(t_buf_, n_samples_);
  tel_.rh_sensor = median5(rh_buf_, n_samples_);
}

// ---------------------------------------------------------------- user remote
void Controller::handle_remote(Millis now) {
  for (int guard = 0; guard < 4; ++guard) {
    const RemoteEvent ev = hal_.ir_rx->poll();
    if (ev.kind == RemoteKind::None) return;
    if (ev.kind == RemoteKind::Ac) {
      tel_.counters.user_ac_presses++;
      const AcState prev = tel_.ac;
      tel_.ac = ev.ac;
      tel_.ac.known = true;
      verify_pending_ = false;
      last_dir_ = 0;
      mismatch_since_ = -1;
      belief_confirmed_ = false;
      drift_since_ = -1;
      tracking_trend_ = false;
      if (ev.ac.power && (!prev.known || !prev.power)) {
        ac_hold_until_ = now + cfg_.hold_after_power_on;
        last_ac_change_ = now;
        bias_learned_ = false;
        stable_count_ = 0;
        duet_raised_since_user_ = false;
        log(now, "remote", "AC switched on at %d C; Duet waits %d min", ev.ac.setpoint_c,
            int(cfg_.hold_after_power_on / kMinute));
      } else if (ev.ac.power && prev.known && ev.ac.setpoint_c != prev.setpoint_c) {
        ac_hold_until_ = now + cfg_.hold_after_user_change;
        last_ac_change_ = now;
        // The user disagreed with the room: learn a small, bounded offset.
        if (ev.ac.setpoint_c < prev.setpoint_c && duet_raised_since_user_)
          learned_offset_ = clampf(learned_offset_ - 0.1f, -0.5f, 0.5f);
        else if (ev.ac.setpoint_c > prev.setpoint_c)
          learned_offset_ = clampf(learned_offset_ + 0.1f, -0.5f, 0.5f);
        duet_raised_since_user_ = false;
        log(now, "remote", "user set AC %d -> %d C; Duet holds 2 h (offset %.1f)",
            prev.setpoint_c, ev.ac.setpoint_c, learned_offset_);
      } else if (!ev.ac.power) {
        ac_hold_until_ = -1;
        log(now, "remote", "user switched the AC off");
      }
    } else if (ev.kind == RemoteKind::Fan) {
      tel_.counters.user_fan_presses++;
      tel_.fan_step = ev.fan_step;
      fan_hold_until_ = now + cfg_.fan_hold_after_user;
      log(now, "remote", "user set fan to %d; fan hold 1 h", ev.fan_step);
    }
  }
}

// ---------------------------------------------------------------- modes
void Controller::update_mode(Millis now) {
  Mode m;
  const bool present_recent = last_seen_ >= 0 && now - last_seen_ < cfg_.presence_hold;
  const Millis vac = tel_.profile == Profile::Sleep ? cfg_.vacancy_timeout_sleep : cfg_.vacancy_timeout;
  const bool vacant = last_seen_ < 0 || now - last_seen_ >= vac;
  if (fault_) m = Mode::Safe;
  else if (vacant && !present_recent) m = Mode::Vacant;
  else if (ac_hold_until_ >= 0 && now < ac_hold_until_) m = Mode::UserHold;
  else if (!tel_.ac.known || !tel_.ac.power) m = Mode::Idle;
  else m = Mode::Auto;

  if (m != tel_.mode) {
    log(now, "mode", "%s -> %s", mode_name(tel_.mode), mode_name(m));
    if (m == Mode::Vacant) vacant_fan_off_sent_ = false;
    tel_.mode = m;
  }
  if (m == Mode::Vacant && !vacant_fan_off_sent_) {
    if (tel_.fan_step != 0) set_fan(0);
    vacant_fan_off_sent_ = true;
  }
}

// ---------------------------------------------------------------- fan loop (30 s)
void Controller::fan_loop(Millis now) {
  if (tel_.mode == Mode::Safe || tel_.mode == Mode::Vacant) return;  // hold / off
  if (fan_hold_until_ >= 0 && now < fan_hold_until_) return;          // remote wins
  if (n_samples_ == 0) return;
  const Profile p = tel_.profile;
  const float rh = tel_.rh_sensor;
  const float tgt = target();
  const int cur = tel_.fan_step;
  // Bed temperature depends on the step we pick (mixing), so evaluate per step.
  auto pmv_at = [&](int s) {
    return Comfort::pmv(p, tel_.t_sensor - cfg_.stratification_c[s], rh, s);
  };
  int desired = Comfort::kMaxStep;
  for (int s = 0; s <= Comfort::kMaxStep; ++s)
    if (pmv_at(s) <= tgt + cfg_.fan_deadband) { desired = s; break; }
  int next = cur;
  if (desired > cur) next = cur + 1;
  else if (desired < cur && pmv_at(cur - 1) <= tgt) next = cur - 1;  // hysteresis
  if (next != cur) set_fan(static_cast<int8_t>(next));
}

// ---------------------------------------------------------------- AC loop (5 min)
void Controller::ac_loop(Millis now) {
  if (tel_.mode != Mode::Auto) return;
  const Profile p = tel_.profile;
  const float rh = tel_.rh_sensor;
  const float tgt = target();
  const int8_t sp = tel_.ac.setpoint_c;
  const float t_occ = tel_.t_occ;

  // Settled = enough time since the last setpoint change AND the room has stopped
  // drifting (pull-down after switch-on can take 30+ minutes).
  const bool stable_now = prev_t_occ_valid_ && fabsf(t_occ - prev_t_occ_) <= 0.2f;
  stable_count_ = stable_now ? stable_count_ + 1 : 0;
  prev_t_occ_ = t_occ;
  prev_t_occ_valid_ = true;
  const bool stable = stable_count_ >= 2;  // 10 minutes without drift
  const bool settled =
      stable && (last_ac_change_ < 0 || now - last_ac_change_ >= cfg_.settle_time);
  // Learn how far the bed sits from the setpoint while the AC is regulating. The first
  // settled period after the user switched on is ground truth (the user chose that
  // setpoint); afterwards only small corrections, so a missed user change is never
  // absorbed into the offset.
  if (settled && !bias_learned_) {
    bias_ = t_occ - sp;
    bias_learned_ = true;
    bias_learned_at_ = now;
    belief_confirmed_ = true;
    log(now, "learn", "room settled at %d C: bed sits %+.1f C from the setpoint", sp, bias_);
  }
  const float gap = t_occ - (sp + bias_);
  if (unexplained_drift(now, gap)) return;
  if (settled && fabsf(gap) < 0.6f) {
    bias_ += 0.2f * ((t_occ - sp) - bias_);
    belief_confirmed_ = true;
  }

  auto best_pmv = [&](float t, float h, int max_step) {
    float best = 99.f;
    for (int s = 0; s <= max_step; ++s) {
      const float v = Comfort::pmv(p, t, h, s);  // t is already the bed temperature
      if (v < best) best = v;
    }
    return best;
  };

  if (reconcile(now, stable)) return;

  int8_t request = sp;
  // Too warm even at full fan: give a degree back.
  if (best_pmv(t_occ, rh, Comfort::kMaxStep) > tgt + cfg_.ac_lower_margin) {
    request = static_cast<int8_t>(sp - 1);
  } else if (settled && bias_learned_ && now - bias_learned_at_ >= cfg_.ac_period &&
             gap >= -0.3f && gap <= 0.6f) {  // one more window confirms the learned offset
    // Would one degree warmer still be comfortable with fan in reserve?
    const float t_pred = sp + 1 + bias_;
    const float rh_pred = rh + 2.0f;  // inverter dehumidifies less when warmer
    if (best_pmv(t_pred, rh_pred, cfg_.max_auto_fan_step) <= tgt + 0.05f)
      request = static_cast<int8_t>(sp + 1);
  }
  if (request == sp) return;

  GovernorContext ctx;
  ctx.now = now;
  ctx.current = tel_.ac;
  ctx.rh = rh;
  ctx.sensor_ok = !fault_;
  ctx.user_hold = ac_hold_until_ >= 0 && now < ac_hold_until_;
  const Verdict v = gov_.check(ctx, request);
  tel_.last_verdict = v;
  tel_.last_request = request;
  if (v != Verdict::Approved) {
    tel_.counters.rejected[static_cast<int>(v)]++;
    if (v != last_logged_verdict_ || request != last_logged_request_)
      log(now, "governor", "request %d C rejected: %s", request, verdict_name(v));
    last_logged_verdict_ = v;
    last_logged_request_ = request;
    return;
  }
  last_logged_verdict_ = Verdict::Approved;
  gov_.record_change(now);
  last_ac_change_ = now;
  tel_.counters.ac_commands++;
  if (request > sp) duet_raised_since_user_ = true;
  last_dir_ = request > sp ? 1 : -1;
  mismatch_since_ = -1;
  const bool belief_confirmed_before = belief_confirmed_;
  belief_confirmed_ = false;
  drift_since_ = -1;
  tracking_trend_ = belief_confirmed_before;
  extreme_since_change_ = t_occ;
  // arm the effect check
  verify_pending_ = settled && fabsf(t_occ - (sp + bias_)) <= 0.6f;
  verify_resent_ = false;
  verify_at_ = now + cfg_.verify_after;
  verify_t0_ = tel_.t_sensor;
  verify_dir_ = request > sp ? 1 : -1;
  log(now, "ac", "setpoint %d -> %d C (rh %.0f%%, pmv %.2f, target %.2f)", sp, request, rh,
      tel_.pmv_est, tgt);
  send_ac(now, request);
}

// IR is one-way. If the room keeps sitting where the *previous* setpoint would put it
// (colder than expected after a raise, warmer after a lower), the frame probably never
// landed. Full-state frames make a resend harmless, so resend what Duet believes.
bool Controller::reconcile(Millis now, bool stable) {
  // Only while Duet's own last command was never confirmed: once the room has settled at
  // the belief, a later disagreement is the user's doing (see unexplained_drift).
  if (last_dir_ == 0 || !stable || belief_confirmed_) { mismatch_since_ = -1; return false; }
  const float gap = tel_.t_occ - (tel_.ac.setpoint_c + bias_);
  const bool disagrees = last_dir_ > 0 ? gap < -cfg_.reconcile_gap_c : gap > cfg_.reconcile_gap_c;
  if (!disagrees) { mismatch_since_ = -1; return false; }
  if (mismatch_since_ < 0) mismatch_since_ = now;
  if (now - mismatch_since_ < cfg_.reconcile_after) return false;
  if (last_reconcile_ >= 0 && now - last_reconcile_ < cfg_.reconcile_after) return false;
  GovernorContext ctx;
  ctx.now = now;
  ctx.current = tel_.ac;
  ctx.rh = tel_.rh_sensor;
  ctx.sensor_ok = !fault_;
  ctx.user_hold = ac_hold_until_ >= 0 && now < ac_hold_until_;
  if (gov_.check(ctx, tel_.ac.setpoint_c) != Verdict::Approved) return false;  // same value: no-op check
  last_reconcile_ = now;
  tel_.counters.reconciles++;
  log(now, "ir", "room still %.1f C off what %d C should give; resending full state", gap,
      tel_.ac.setpoint_c);
  send_ac(now, tel_.ac.setpoint_c);
  return true;
}

// The receiver can miss a user's remote press (it sits in the canopy, the user points at
// the AC). If the room had settled at Duet's belief and then drifts away without Duet
// having changed anything, assume the user changed the AC and stand back for 2 hours.
bool Controller::unexplained_drift(Millis now, float gap) {
  if (!belief_confirmed_) { drift_since_ = -1; return false; }
  const bool drift = gap < -cfg_.unexplained_cold_c || gap > cfg_.unexplained_warm_c;
  if (!drift) { drift_since_ = -1; return false; }
  if (drift_since_ < 0) drift_since_ = now;
  if (now - drift_since_ < cfg_.unexplained_after) return false;
  assume_user_change(now, gap < 0 ? "room colder than the setpoint explains"
                                   : "room warmer than the setpoint explains");
  return true;
}

// After Duet raises, the room warms and stays; after it lowers, the room cools and stays.
// A clear reversal with no new Duet command means someone else changed the AC.
void Controller::watch_trend(Millis now) {
  if (!tracking_trend_ || tel_.mode != Mode::Auto) return;
  // Inverter cycling swings the room about +-0.5 C around setpoint + offset, so a reversal
  // only counts if the room also leaves that band, and stays out for 5 minutes.
  const float t = tel_.t_occ;
  const float gap = t - (tel_.ac.setpoint_c + bias_);
  bool reversed = false;
  if (last_dir_ > 0) {
    if (t > extreme_since_change_) extreme_since_change_ = t;
    reversed = extreme_since_change_ - t >= 0.7f && gap < -0.7f;
  } else if (last_dir_ < 0) {
    if (t < extreme_since_change_) extreme_since_change_ = t;
    reversed = t - extreme_since_change_ >= 0.9f && gap > 0.9f;
  }
  if (!reversed) { reversal_since_ = -1; return; }
  if (reversal_since_ < 0) reversal_since_ = now;
  if (now - reversal_since_ >= 5 * kMinute)
    assume_user_change(now, last_dir_ > 0 ? "room cooled well below Duet's raised setpoint"
                                          : "room warmed well above Duet's lowered setpoint");
}

void Controller::assume_user_change(Millis now, const char* why) {
  ac_hold_until_ = now + cfg_.hold_after_user_change;
  belief_confirmed_ = false;
  drift_since_ = -1;
  last_dir_ = 0;
  tracking_trend_ = false;
  reversal_since_ = -1;
  verify_pending_ = false;
  tel_.counters.missed_remote++;
  log(now, "remote", "%s; assuming the user changed the AC, holding 2 h", why);
}

void Controller::send_ac(Millis now, int8_t setpoint) {
  AcState s = tel_.ac;
  s.setpoint_c = setpoint;
  // State-based protocol: two identical frames, ~300 ms apart; repeats are harmless.
  hal_.ir_tx->send_ac(s);
  hal_.ir_tx->send_ac(s);
  tel_.counters.ac_frames += 2;
  tel_.ac = s;
  (void)now;
}

// ---------------------------------------------------------------- IR effect check
void Controller::verify(Millis now) {
  if (!verify_pending_ || now < verify_at_) return;
  if (tel_.mode != Mode::Auto) { verify_pending_ = false; return; }
  const float dt = tel_.t_sensor - verify_t0_;
  const bool ok = verify_dir_ > 0 ? dt >= 0.15f : dt <= -0.15f;
  if (ok) { verify_pending_ = false; belief_confirmed_ = true; return; }
  if (!verify_resent_) {
    tel_.counters.ac_resends++;
    log(now, "ir", "no room response after %d min (dT %.2f); resending full state",
        int(cfg_.verify_after / kMinute), dt);
    send_ac(now, tel_.ac.setpoint_c);
    verify_resent_ = true;
    verify_at_ = now + cfg_.verify_after;
    verify_t0_ = tel_.t_sensor;
  } else {
    verify_pending_ = false;
    log(now, "ir", "still no response; giving up until the next decision");
  }
}

void Controller::set_fan(int8_t step) {
  if (step < 0) step = 0;
  if (step > Comfort::kMaxStep) step = Comfort::kMaxStep;
  hal_.fan->set_step(step);
  tel_.fan_step = step;
  tel_.counters.fan_commands++;
}

void Controller::log(Millis now, const char* tag, const char* fmt, ...) {
  if (!hal_.log) return;
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(log_buf_, sizeof log_buf_, fmt, ap);
  va_end(ap);
  hal_.log->event(now, tag, log_buf_);
}

}  // namespace duet
