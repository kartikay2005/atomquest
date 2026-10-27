// ESP32-C3 HAL backend for Duet (Arduino framework). Built only by PlatformIO env:esp32c3.
#pragma once
#ifdef ARDUINO
#include <Arduino.h>
#include <IRac.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>
#include <Wire.h>

#include "duet/hal.h"

namespace duet {
namespace esp32 {

// ---------------------------------------------------------------- pins (ESP32-C3 SuperMini)
constexpr int kPinSda = 8;
constexpr int kPinScl = 9;
constexpr int kPinRadarRx = 20;  // LD2410C TX -> ESP RX
constexpr int kPinRadarTx = 21;  // ESP TX -> LD2410C RX
constexpr int kPinIrLed = 3;     // via 2N7002 MOSFET, 2x 940 nm LEDs
constexpr int kPinIrRecv = 10;   // TSOP38238 output (active low)

class Clock : public IClock {
 public:
  // Local wall time in ms when NTP has synced (IST = UTC+05:30), else uptime.
  Millis now() const override;
};

class Sht40 : public IClimateSensor {
 public:
  explicit Sht40(TwoWire& w = Wire, uint8_t addr = 0x44) : w_(w), addr_(addr) {}
  bool begin();
  ClimateReading read() override;  // high-precision measurement, CRC-checked

 private:
  static uint8_t crc8(const uint8_t* d, int n);
  TwoWire& w_;
  uint8_t addr_;
};

class Ld2410c : public IPresence {
 public:
  explicit Ld2410c(HardwareSerial& s) : s_(s) {}
  void begin();
  bool occupied() override;  // drains UART, parses report frames

 private:
  HardwareSerial& s_;
  uint8_t buf_[64];
  int n_ = 0;
  bool occ_ = false;
  uint32_t last_frame_ms_ = 0;
};

class IrAcTx : public IIrTx {
 public:
  explicit IrAcTx(decode_type_t protocol, int16_t model = -1)
      : ac_(kPinIrLed), protocol_(protocol), model_(model) {}
  bool send_ac(const AcState& s) override;

 private:
  IRac ac_;
  decode_type_t protocol_;
  int16_t model_;
};

class IrRemoteRx : public IIrRx {
 public:
  IrRemoteRx(decode_type_t ac_protocol) : rx_(kPinIrRecv, 1024, 50, true), ac_protocol_(ac_protocol) {}
  void begin() { rx_.enableIRIn(); }
  RemoteEvent poll() override;
  // Learned fan codes (Duet Clip): value received for fan steps 0..6, set during pairing.
  uint64_t fan_codes[7] = {};
  decode_type_t fan_protocol = decode_type_t::NEC;

 private:
  IRrecv rx_;
  decode_results res_;
  decode_type_t ac_protocol_;
};

// Duet Clip: fan driven by learned IR "speed N" codes (absolute, so resends are safe).
class IrFan : public IFanDriver {
 public:
  explicit IrFan(const IrRemoteRx& codes) : send_(kPinIrLed), codes_(codes) { send_.begin(); }
  bool set_step(int8_t step) override;

 private:
  IRsend send_;
  const IrRemoteRx& codes_;
};

// Duet Native: wired UART command to the Atomberg fan controller in the canopy.
// The command frame is a placeholder until Atomberg's controller protocol is available.
class NativeFan : public IFanDriver {
 public:
  explicit NativeFan(HardwareSerial& s) : s_(s) {}
  bool set_step(int8_t step) override;

 private:
  HardwareSerial& s_;
};

class SerialLog : public ILog {
 public:
  void event(Millis t, const char* tag, const char* msg) override;
};

}  // namespace esp32
}  // namespace duet
#endif  // ARDUINO
