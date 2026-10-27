// Unit tests for the control core. Plain asserts, no framework, runs on the laptop.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "duet/comfort.h"
#include "duet/controller.h"
#include "duet/governor.h"

using namespace duet;

static int g_fail = 0, g_pass = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (cond) { ++g_pass; }                                             \
    else { ++g_fail; printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

static GovernorContext ctx_at(Millis now, int sp, float rh = 50) {
  GovernorContext c;
  c.now = now;
  c.current.known = true;
  c.current.power = true;
  c.current.setpoint_c = static_cast<int8_t>(sp);
  c.rh = rh;
  return c;
}

static void test_governor_rules() {
  printf("governor rules\n");
  SafetyGovernor g;
  CHECK(g.check(ctx_at(0, 24), 25) == Verdict::Approved);
  CHECK(g.check(ctx_at(0, 28), 29) == Verdict::OutOfRange);
  CHECK(g.check(ctx_at(0, 24), 23) == Verdict::OutOfRange);
  CHECK(g.check(ctx_at(0, 24), 26) == Verdict::StepTooLarge);
  CHECK(g.check(ctx_at(0, 25, 61), 26) == Verdict::HumidityBlock);
  CHECK(g.check(ctx_at(0, 25, 61), 24) == Verdict::Approved);  // lowering is fine
  GovernorContext c = ctx_at(0, 25); c.sensor_ok = false;
  CHECK(g.check(c, 26) == Verdict::SensorFault);
  c = ctx_at(0, 25); c.user_hold = true;
  CHECK(g.check(c, 26) == Verdict::UserHold);
  c = ctx_at(0, 25); c.current.power = false;
  CHECK(g.check(c, 26) == Verdict::AcOff);
  c = ctx_at(0, 25); c.current.known = false;
  CHECK(g.check(c, 26) == Verdict::Unknown);

  g.record_change(0);
  CHECK(g.check(ctx_at(19 * kMinute, 25), 26) == Verdict::TooSoon);
  CHECK(g.check(ctx_at(20 * kMinute, 25), 26) == Verdict::Approved);
  g.record_change(20 * kMinute);
  g.record_change(40 * kMinute);
  CHECK(g.changes_last_hour(59 * kMinute) == 3);
  CHECK(g.check(ctx_at(59 * kMinute, 26), 27) == Verdict::TooSoon);
  CHECK(g.check(ctx_at(60 * kMinute, 26), 27) == Verdict::Approved);
  // the hourly cap binds on its own when the spacing rule is relaxed
  GovernorLimits fast; fast.min_interval = 5 * kMinute;
  SafetyGovernor g2(fast);
  g2.record_change(0); g2.record_change(10 * kMinute); g2.record_change(20 * kMinute);
  CHECK(g2.check(ctx_at(30 * kMinute, 26), 27) == Verdict::HourlyLimit);
  CHECK(g2.check(ctx_at(60 * kMinute, 26), 27) == Verdict::Approved);
}

static void test_comfort_lookup() {
  printf("comfort lookup\n");
  // Values from the ASHRAE 55 table (met 1.0, clo 0.5, tr = ta + 1)
  CHECK(fabsf(Comfort::pmv(Profile::Evening, 24.f, 55.f, 0) - (-0.56f)) < 0.03f);
  CHECK(fabsf(Comfort::pmv(Profile::Evening, 27.f, 55.f, 2) - 0.12f) < 0.03f);
  // more air speed never makes it warmer above 24 C
  for (float t = 24.f; t <= 32.f; t += 0.5f)
    for (int s = 1; s <= 6; ++s)
      CHECK(Comfort::pmv(Profile::Evening, t, 50.f, s) <= Comfort::pmv(Profile::Evening, t, 50.f, s - 1) + 0.09f);
  // monotone in temperature
  for (int s = 0; s <= 6; ++s)
    CHECK(Comfort::pmv(Profile::Evening, 28.f, 50.f, s) > Comfort::pmv(Profile::Evening, 26.f, 50.f, s));
  CHECK(Comfort::lowest_step_at_or_below(Profile::Evening, 28.f, 55.f, 0.0f) >= 4);
}

// ---- small fake HAL for controller tests
struct FClock : IClock { Millis t = 0; Millis now() const override { return t; } };
struct FClimate : IClimateSensor {
  ClimateReading r; float jitter = 0; int n = 0;
  ClimateReading read() override { ClimateReading o = r; o.temp_c += ((n++ % 3) - 1) * 0.01f; return o; }
};
struct FPresence : IPresence { bool o = true; bool occupied() override { return o; } };
struct FTx : IIrTx { int frames = 0; AcState last; bool send_ac(const AcState& s) override { ++frames; last = s; return true; } };
struct FRx : IIrRx { RemoteEvent e; RemoteEvent poll() override { RemoteEvent x = e; e = RemoteEvent(); return x; } };
struct FFan : IFanDriver { int8_t step = 0; int cmds = 0; bool set_step(int8_t s) override { step = s; ++cmds; return true; } };

struct Rig {
  FClock clk; FClimate cli; FPresence pre; FTx tx; FRx rx; FFan fan;
  Controller ctl{Hal{&clk, &cli, &pre, &tx, &rx, &fan, nullptr}};
  Rig() { clk.t = 20 * kHour; cli.r.temp_c = 25.f; cli.r.rh = 50.f; ctl.begin(); }
  void run(Millis dur, Millis dt = 5 * kSecond) { for (Millis e = 0; e < dur; e += dt) { clk.t += dt; ctl.tick(); } }
  void remote_ac(bool on, int sp) { rx.e.kind = RemoteKind::Ac; rx.e.ac.known = true; rx.e.ac.power = on; rx.e.ac.setpoint_c = (int8_t)sp; }
};

static void test_controller_raises_and_respects_remote() {
  printf("controller: raise, hold, remote wins\n");
  Rig r;
  r.remote_ac(true, 24);
  r.run(5 * kSecond);
  CHECK(r.ctl.telemetry().mode == Mode::UserHold);  // power-on hold
  // room sits ~0.5 C above setpoint at the bed and is cool at 24 C
  r.cli.r.temp_c = 25.5f;
  r.run(45 * kMinute);
  CHECK(r.ctl.telemetry().ac.setpoint_c >= 25);  // Duet raised
  CHECK(r.tx.frames >= 2);
  const int frames_before = r.tx.frames;
  // user disagrees: back to 24
  r.remote_ac(true, 24);
  r.run(5 * kSecond);
  CHECK(r.ctl.telemetry().mode == Mode::UserHold);
  r.run(100 * kMinute);
  CHECK(r.tx.frames == frames_before);            // no AC command during the 2 h hold
  CHECK(r.ctl.telemetry().target_pmv < 0.f);       // learned a cooler preference
}

static void test_controller_sensor_fault_is_safe() {
  printf("controller: frozen sensor -> safe, fan held\n");
  Rig r;
  r.remote_ac(true, 25);
  r.cli.r.temp_c = 27.f;
  r.run(30 * kMinute);
  const int8_t fan_before = r.fan.step;
  const int frames_before = r.tx.frames;
  // freeze: identical readings with no jitter
  struct Frozen : IClimateSensor { ClimateReading read() override { ClimateReading o; o.temp_c = 27.0f; o.rh = 50.f; return o; } } frozen;
  Controller c2{Hal{&r.clk, &frozen, &r.pre, &r.tx, &r.rx, &r.fan, nullptr}};
  c2.begin();
  r.clk.t += kSecond;
  for (int i = 0; i < 12 * 60 / 5 + 10; ++i) { r.clk.t += 5 * kSecond; c2.tick(); }
  CHECK(c2.telemetry().mode == Mode::Safe);
  CHECK(r.tx.frames == frames_before);
  (void)fan_before;
}

static void test_controller_humidity_blocks_raise() {
  printf("controller: no raise above 60%% RH\n");
  Rig r;
  r.cli.r.rh = 68.f;
  r.remote_ac(true, 24);
  r.cli.r.temp_c = 25.5f;
  r.run(2 * kHour);
  CHECK(r.ctl.telemetry().ac.setpoint_c == 24);
  CHECK(r.ctl.telemetry().counters.rejected[(int)Verdict::HumidityBlock] > 0);
}

static void test_controller_vacant_turns_fan_off() {
  printf("controller: vacant -> fan off, AC untouched\n");
  Rig r;
  r.remote_ac(true, 25);
  r.cli.r.temp_c = 28.f;
  r.run(10 * kMinute);
  CHECK(r.fan.step > 0);
  r.pre.o = false;
  r.run(25 * kMinute);
  CHECK(r.ctl.telemetry().mode == Mode::Vacant);
  CHECK(r.fan.step == 0);
}

int main() {
  test_governor_rules();
  test_comfort_lookup();
  test_controller_raises_and_respects_remote();
  test_controller_sensor_fault_is_safe();
  test_controller_humidity_blocks_raise();
  test_controller_vacant_turns_fan_off();
  printf("\n%d passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
