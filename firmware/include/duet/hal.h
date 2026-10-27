// Hardware abstraction layer. The control core only ever talks to these.
// Backends: src/esp32/* (real board) and src/sil/* (simulation, laptop build).
#pragma once
#include "duet/types.h"

namespace duet {

class IClock {
 public:
  virtual ~IClock() = default;
  virtual Millis now() const = 0;
};

class IClimateSensor {  // SHT40 on I2C 0x44
 public:
  virtual ~IClimateSensor() = default;
  virtual ClimateReading read() = 0;
};

class IPresence {  // LD2410C on UART, 256000 baud
 public:
  virtual ~IPresence() = default;
  virtual bool occupied() = 0;
};

class IIrTx {  // 940 nm LED on the RMT peripheral, 38 kHz carrier
 public:
  virtual ~IIrTx() = default;
  // Sends one full-state AC frame. Returns false only on a local driver error.
  virtual bool send_ac(const AcState& s) = 0;
};

class IIrRx {  // 38 kHz receiver; frames from the user's own remotes
 public:
  virtual ~IIrRx() = default;
  virtual RemoteEvent poll() = 0;
};

// Fan actuation. Duet Native: wired command to the Atomberg controller.
// Duet Clip: IR "speed N" codes. Both take an absolute step, so resends are safe.
class IFanDriver {
 public:
  virtual ~IFanDriver() = default;
  virtual bool set_step(int8_t step) = 0;
};

class ILog {
 public:
  virtual ~ILog() = default;
  virtual void event(Millis t, const char* tag, const char* msg) = 0;
};

struct Hal {
  IClock* clock;
  IClimateSensor* climate;
  IPresence* presence;
  IIrTx* ir_tx;
  IIrRx* ir_rx;
  IFanDriver* fan;
  ILog* log;  // may be null
};

}  // namespace duet
