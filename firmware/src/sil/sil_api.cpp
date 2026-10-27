// SIL backend: HAL implementations fed from DuetSilInputs, plus the C ABI.
#include "duet/sil_api.h"

#include <string.h>

#include <new>

#include "duet/controller.h"

using namespace duet;

namespace {

struct SilClock : IClock {
  Millis t = 0;
  Millis now() const override { return t; }
};

struct SilClimate : IClimateSensor {
  ClimateReading r;
  ClimateReading read() override { return r; }
};

struct SilPresence : IPresence {
  bool occ = false;
  bool occupied() override { return occ; }
};

struct SilIrTx : IIrTx {
  int frames = 0;
  AcState last;
  bool send_ac(const AcState& s) override { ++frames; last = s; return true; }
};

struct SilIrRx : IIrRx {
  RemoteEvent pending;
  RemoteEvent poll() override { RemoteEvent e = pending; pending = RemoteEvent(); return e; }
};

struct SilFan : IFanDriver {
  bool sent = false;
  int8_t step = 0;
  bool set_step(int8_t s) override { sent = true; step = s; return true; }
};

struct LogLine { Millis t; char tag[16]; char msg[160]; };

struct SilLog : ILog {
  static constexpr int kN = 256;
  LogLine lines[kN];
  int head = 0, count = 0;
  void event(Millis t, const char* tag, const char* msg) override {
    LogLine& l = lines[(head + count) % kN];
    l.t = t;
    strncpy(l.tag, tag, sizeof l.tag - 1); l.tag[sizeof l.tag - 1] = 0;
    strncpy(l.msg, msg, sizeof l.msg - 1); l.msg[sizeof l.msg - 1] = 0;
    if (count < kN) ++count; else head = (head + 1) % kN;
  }
};

struct Sil {
  SilClock clock;
  SilClimate climate;
  SilPresence presence;
  SilIrTx tx;
  SilIrRx rx;
  SilFan fan;
  SilLog log;
  ControllerConfig cfg;
  Controller* ctl = nullptr;
  alignas(Controller) unsigned char storage[sizeof(Controller)];
};

}  // namespace

extern "C" {

void* duet_sil_create(void) { return new Sil(); }

void duet_sil_destroy(void* h) {
  Sil* s = static_cast<Sil*>(h);
  if (s->ctl) s->ctl->~Controller();
  delete s;
}

int duet_sil_set(void* h, const char* key, double v) {
  Sil* s = static_cast<Sil*>(h);
  // Safety limits are copied into the governor at begin(); changing them later would be
  // silently ignored, so refuse instead.
  if (s->ctl && (!strncmp(key, "min_", 4) || !strncmp(key, "max_s", 5) || !strncmp(key, "max_r", 5) ||
                 !strncmp(key, "max_c", 5)))
    return 0;
  ControllerConfig& c = s->ctl ? s->ctl->config() : s->cfg;
  const Millis m = static_cast<Millis>(v * kMinute);
  if (!strcmp(key, "preference")) c.preference = static_cast<float>(v);
  else if (!strcmp(key, "target_pmv")) c.target_pmv = static_cast<float>(v);
  else if (!strcmp(key, "fan_deadband")) c.fan_deadband = static_cast<float>(v);
  else if (!strcmp(key, "max_auto_fan_step")) c.max_auto_fan_step = static_cast<int8_t>(v);
  else if (!strcmp(key, "hold_after_user_change_min")) c.hold_after_user_change = m;
  else if (!strcmp(key, "hold_after_power_on_min")) c.hold_after_power_on = m;
  else if (!strcmp(key, "min_setpoint")) c.limits.min_setpoint_c = static_cast<int8_t>(v);
  else if (!strcmp(key, "max_setpoint")) c.limits.max_setpoint_c = static_cast<int8_t>(v);
  else if (!strcmp(key, "max_rh_for_raise")) c.limits.max_rh_for_raise = static_cast<float>(v);
  else if (!strcmp(key, "min_interval_min")) c.limits.min_interval = m;
  else if (!strcmp(key, "max_changes_per_hour")) c.limits.max_changes_per_hour = static_cast<int>(v);
  else if (!strncmp(key, "strat", 5) && key[5] >= '0' && key[5] <= '6')
    c.stratification_c[key[5] - '0'] = static_cast<float>(v);
  else return 0;
  return 1;
}

void duet_sil_begin(void* h, int64_t now_ms) {
  Sil* s = static_cast<Sil*>(h);
  if (s->ctl) { s->ctl->~Controller(); s->ctl = nullptr; }  // restart cleanly
  s->clock.t = now_ms;
  Hal hal{&s->clock, &s->climate, &s->presence, &s->tx, &s->rx, &s->fan, &s->log};
  s->ctl = new (s->storage) Controller(hal, s->cfg);
  s->ctl->begin();
}

void duet_sil_step(void* h, const DuetSilInputs* in, DuetSilOutputs* out) {
  Sil* s = static_cast<Sil*>(h);
  if (!s->ctl) { memset(out, 0, sizeof *out); return; }  // step before begin: no-op
  s->clock.t = in->now_ms;
  s->climate.r.temp_c = in->temp_c;
  s->climate.r.rh = in->rh;
  s->climate.r.crc_ok = in->crc_ok != 0;
  s->climate.r.present = in->sensor_present != 0;
  s->presence.occ = in->occupied != 0;
  if (in->remote_kind == 1) {
    s->rx.pending.kind = RemoteKind::Ac;
    s->rx.pending.ac.known = true;
    s->rx.pending.ac.power = in->remote_power != 0;
    s->rx.pending.ac.setpoint_c = in->remote_setpoint;
  } else if (in->remote_kind == 2) {
    s->rx.pending.kind = RemoteKind::Fan;
    s->rx.pending.fan_step = in->remote_fan_step;
  }
  s->tx.frames = 0;
  s->fan.sent = false;

  s->ctl->tick();

  const Telemetry& t = s->ctl->telemetry();
  memset(out, 0, sizeof *out);
  out->ac_frames = static_cast<uint8_t>(s->tx.frames);
  out->ac_power = s->tx.last.power;
  out->ac_setpoint = s->tx.last.setpoint_c;
  out->fan_cmd = s->fan.sent;
  out->fan_step = t.fan_step;
  out->mode = static_cast<uint8_t>(t.mode);
  out->profile = static_cast<uint8_t>(t.profile);
  out->sensor_ok = t.sensor_ok;
  out->occupied = t.occupied;
  out->believed_setpoint = t.ac.setpoint_c;
  out->believed_power = t.ac.power;
  out->last_verdict = static_cast<uint8_t>(t.last_verdict);
  out->t_sensor = t.t_sensor;
  out->rh_sensor = t.rh_sensor;
  out->t_occ = t.t_occ;
  out->pmv_est = t.pmv_est;
  out->target_pmv = t.target_pmv;
  out->bias_c = t.bias_c;
  out->ac_commands = t.counters.ac_commands;
  out->ac_frames_total = t.counters.ac_frames;
  out->ac_resends = t.counters.ac_resends;
  out->fan_commands = t.counters.fan_commands;
  out->sensor_faults = t.counters.sensor_faults;
  out->user_ac_presses = t.counters.user_ac_presses;
  out->user_fan_presses = t.counters.user_fan_presses;
  out->reconciles = t.counters.reconciles;
  out->missed_remote = t.counters.missed_remote;
  for (int i = 0; i < 10; ++i) out->rejected[i] = t.counters.rejected[i];
}

int duet_sil_pop_log(void* h, int64_t* t_ms, char* tag, int tag_len, char* msg, int msg_len) {
  Sil* s = static_cast<Sil*>(h);
  if (s->log.count == 0) return 0;
  const LogLine& l = s->log.lines[s->log.head];
  *t_ms = l.t;
  strncpy(tag, l.tag, tag_len - 1); tag[tag_len - 1] = 0;
  strncpy(msg, l.msg, msg_len - 1); msg[msg_len - 1] = 0;
  s->log.head = (s->log.head + 1) % SilLog::kN;
  --s->log.count;
  return 1;
}

void* duet_gov_create(void) { return new SafetyGovernor(); }
void duet_gov_destroy(void* g) { delete static_cast<SafetyGovernor*>(g); }
int duet_gov_check(void* g, int64_t now_ms, int known, int power, int setpoint, float rh,
                   int sensor_ok, int user_hold, int requested) {
  GovernorContext c;
  c.now = now_ms;
  c.current.known = known != 0;
  c.current.power = power != 0;
  c.current.setpoint_c = static_cast<int8_t>(setpoint);
  c.rh = rh;
  c.sensor_ok = sensor_ok != 0;
  c.user_hold = user_hold != 0;
  return static_cast<int>(static_cast<SafetyGovernor*>(g)->check(c, static_cast<int8_t>(requested)));
}
void duet_gov_record(void* g, int64_t now_ms) { static_cast<SafetyGovernor*>(g)->record_change(now_ms); }

const char* duet_sil_verdict_name(int v) { return verdict_name(static_cast<Verdict>(v)); }
const char* duet_sil_mode_name(int m) { return mode_name(static_cast<Mode>(m)); }
const char* duet_sil_version(void) { return "duet-fw 0.2.0 (sil)"; }
int duet_sil_sizeof_inputs(void) { return static_cast<int>(sizeof(DuetSilInputs)); }
int duet_sil_sizeof_outputs(void) { return static_cast<int>(sizeof(DuetSilOutputs)); }

}  // extern "C"
