// Shared value types for the Duet firmware. No heap, no STL containers.
#pragma once
#include <stdint.h>

namespace duet {

using Millis = int64_t;  // time from IClock, in milliseconds

constexpr Millis kSecond = 1000;
constexpr Millis kMinute = 60 * kSecond;
constexpr Millis kHour = 60 * kMinute;

enum class AcMode : uint8_t { Cool = 0, Dry = 1, Fan = 2, Auto = 3 };

// Full state carried by one split-AC IR frame (state-based protocols resend everything).
struct AcState {
  bool known = false;   // false until Duet has seen or sent a frame
  bool power = false;
  AcMode mode = AcMode::Cool;
  int8_t setpoint_c = 24;
};

struct ClimateReading {
  float temp_c = 0.f;
  float rh = 0.f;
  bool crc_ok = true;
  bool present = true;  // false if the bus did not answer
};

// What the IR receiver decoded from the user's own remote.
enum class RemoteKind : uint8_t { None = 0, Ac = 1, Fan = 2 };
struct RemoteEvent {
  RemoteKind kind = RemoteKind::None;
  AcState ac;          // valid when kind == Ac
  int8_t fan_step = 0; // valid when kind == Fan
};

enum class Mode : uint8_t { Auto = 0, UserHold = 1, Safe = 2, Vacant = 3, Idle = 4 };

enum class Profile : uint8_t { Evening = 0, Sleep = 1 };

// Why the governor said no (or yes). Logged and shown on the console.
enum class Verdict : uint8_t {
  Approved = 0,
  OutOfRange,      // setpoint outside 24..28
  StepTooLarge,    // more than 1 C in one command
  TooSoon,         // less than 20 min since the last change
  HourlyLimit,     // already 3 changes in the last 60 min
  HumidityBlock,   // raise requested while RH > 60 %
  SensorFault,     // climate sensor not trusted
  UserHold,        // the user's remote wins
  AcOff,           // Duet never powers the AC on or off
  Unknown,         // AC state never seen
};

const char* verdict_name(Verdict v);
const char* mode_name(Mode m);

}  // namespace duet
