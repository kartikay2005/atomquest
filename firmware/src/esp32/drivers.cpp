#ifdef ARDUINO
#include "drivers.h"

#include <time.h>

namespace duet {
namespace esp32 {

// ---------------------------------------------------------------- clock
Millis Clock::now() const {
  time_t t = time(nullptr);
  if (t > 1700000000) {  // NTP synced; configTime already applied the IST offset
    struct tm lt;
    localtime_r(&t, &lt);
    const Millis day_ms = (Millis(lt.tm_hour) * 3600 + lt.tm_min * 60 + lt.tm_sec) * 1000;
    const Millis days = Millis(t / 86400);
    return days * 86400000LL + day_ms + (millis() % 1000);
  }
  return Millis(millis());
}

// ---------------------------------------------------------------- SHT40 (I2C 0x44)
uint8_t Sht40::crc8(const uint8_t* d, int n) {
  uint8_t crc = 0xFF;  // poly 0x31, init 0xFF (Sensirion)
  for (int i = 0; i < n; ++i) {
    crc ^= d[i];
    for (int b = 0; b < 8; ++b) crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
  }
  return crc;
}

bool Sht40::begin() {
  w_.begin(kPinSda, kPinScl, 400000);
  w_.beginTransmission(addr_);
  w_.write(0x94);  // soft reset
  return w_.endTransmission() == 0;
}

ClimateReading Sht40::read() {
  ClimateReading r;
  w_.beginTransmission(addr_);
  w_.write(0xFD);  // measure T and RH, high precision
  if (w_.endTransmission() != 0) { r.present = false; return r; }
  delay(10);
  if (w_.requestFrom(int(addr_), 6) != 6) { r.present = false; return r; }
  uint8_t b[6];
  for (int i = 0; i < 6; ++i) b[i] = w_.read();
  r.crc_ok = crc8(b, 2) == b[2] && crc8(b + 3, 2) == b[5];
  const uint16_t rt = (uint16_t(b[0]) << 8) | b[1];
  const uint16_t rh = (uint16_t(b[3]) << 8) | b[4];
  r.temp_c = -45.f + 175.f * rt / 65535.f;
  r.rh = -6.f + 125.f * rh / 65535.f;
  if (r.rh < 0) r.rh = 0;
  if (r.rh > 100) r.rh = 100;
  return r;
}

// ---------------------------------------------------------------- LD2410C (UART 256000)
void Ld2410c::begin() { s_.begin(256000, SERIAL_8N1, kPinRadarRx, kPinRadarTx); }

bool Ld2410c::occupied() {
  // Report frame: F4 F3 F2 F1 | len(2, LE) | 0x01/0x02 0xAA state ... 0x55 0x00 | F8 F7 F6 F5
  while (s_.available()) {
    const uint8_t c = s_.read();
    if (n_ < 4) {
      static const uint8_t head[4] = {0xF4, 0xF3, 0xF2, 0xF1};
      n_ = (c == head[n_]) ? n_ + 1 : (c == 0xF4 ? 1 : 0);
      if (n_) buf_[n_ - 1] = c;
      continue;
    }
    if (n_ < int(sizeof buf_)) buf_[n_++] = c;
    if (n_ >= 6) {
      const int len = buf_[4] | (buf_[5] << 8);
      if (len > int(sizeof buf_) - 10) { n_ = 0; continue; }
      if (n_ == 6 + len + 4) {
        const uint8_t* d = buf_ + 6;
        if ((d[0] == 0x01 || d[0] == 0x02) && d[1] == 0xAA) {
          occ_ = d[2] != 0x00;  // 0 none, 1 moving, 2 stationary, 3 both
          last_frame_ms_ = millis();
        }
        n_ = 0;
      }
    }
  }
  // No frames for 5 s: treat the radar as absent -> report occupied (fail safe: never
  // switch the fan off because a cable came loose).
  if (millis() - last_frame_ms_ > 5000) return true;
  return occ_;
}

// ---------------------------------------------------------------- IR to the AC
bool IrAcTx::send_ac(const AcState& s) {
  stdAc::state_t st;
  st.protocol = protocol_;
  st.model = model_;
  st.power = s.power;
  st.mode = s.mode == AcMode::Cool ? stdAc::opmode_t::kCool
          : s.mode == AcMode::Dry ? stdAc::opmode_t::kDry
          : s.mode == AcMode::Fan ? stdAc::opmode_t::kFan : stdAc::opmode_t::kAuto;
  st.celsius = true;
  st.degrees = s.setpoint_c;
  st.fanspeed = stdAc::fanspeed_t::kAuto;
  st.swingv = stdAc::swingv_t::kAuto;
  return ac_.sendAc(st, nullptr);  // full-state frame for the configured brand
}

// ---------------------------------------------------------------- IR from the user's remotes
RemoteEvent IrRemoteRx::poll() {
  RemoteEvent ev;
  if (!rx_.decode(&res_)) return ev;
  if (res_.decode_type == ac_protocol_) {
    stdAc::state_t st;
    if (IRAcUtils::decodeToState(&res_, &st, nullptr)) {
      ev.kind = RemoteKind::Ac;
      ev.ac.known = true;
      ev.ac.power = st.power;
      ev.ac.setpoint_c = static_cast<int8_t>(st.degrees);
      ev.ac.mode = st.mode == stdAc::opmode_t::kDry ? AcMode::Dry : AcMode::Cool;
    }
  } else if (res_.decode_type == fan_protocol) {
    for (int s = 0; s < 7; ++s)
      if (fan_codes[s] && res_.value == fan_codes[s]) { ev.kind = RemoteKind::Fan; ev.fan_step = s; }
  }
  rx_.resume();
  return ev;
}

bool IrFan::set_step(int8_t step) {
  const uint64_t code = codes_.fan_codes[step];
  if (!code) return false;
  send_.send(codes_.fan_protocol, code, 32);
  return true;
}

bool NativeFan::set_step(int8_t step) {
  // Placeholder frame: 0xA5 'S' step checksum. Replace with Atomberg's controller protocol.
  const uint8_t f[4] = {0xA5, 'S', uint8_t(step), uint8_t(0xA5 ^ 'S' ^ step)};
  return s_.write(f, 4) == 4;
}

void SerialLog::event(Millis t, const char* tag, const char* msg) {
  Serial.printf("[%lld] %-8s %s\n", (long long)(t / 1000), tag, msg);
}

}  // namespace esp32
}  // namespace duet
#endif  // ARDUINO
