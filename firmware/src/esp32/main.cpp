// Duet on an ESP32-C3: the same control core as the simulator, real drivers underneath.
#ifdef ARDUINO
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "drivers.h"
#include "duet/controller.h"

#ifndef DUET_WIFI_SSID
#define DUET_WIFI_SSID ""
#define DUET_WIFI_PASS ""
#endif
#ifndef DUET_MQTT_HOST
#define DUET_MQTT_HOST "192.168.1.10"
#endif
#ifndef DUET_ROOM
#define DUET_ROOM "bedroom"
#endif
#ifndef DUET_AC_PROTOCOL
#define DUET_AC_PROTOCOL VOLTAS
#endif
#ifndef DUET_NATIVE_FAN
#define DUET_NATIVE_FAN 0  // 1 = Duet Native (wired to the Atomberg controller), 0 = Duet Clip (IR)
#endif

using namespace duet;

static esp32::Clock g_clock;
static esp32::Sht40 g_sht;
static esp32::Ld2410c g_radar(Serial1);
static esp32::IrAcTx g_irtx(decode_type_t::DUET_AC_PROTOCOL);
static esp32::IrRemoteRx g_irrx(decode_type_t::DUET_AC_PROTOCOL);
#if DUET_NATIVE_FAN
static esp32::NativeFan g_fan(Serial0);
#else
static esp32::IrFan g_fan(g_irrx);
#endif
static esp32::SerialLog g_log;
static Controller* g_ctl = nullptr;

static WiFiClient g_net;
static PubSubClient g_mqtt(g_net);
static uint32_t g_last_tick = 0, g_last_pub = 0;

static void on_mqtt(char* topic, uint8_t* payload, unsigned len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  if (doc["preference"].is<float>()) g_ctl->set_preference(doc["preference"].as<float>());
}

static void mqtt_publish() {
  if (!g_mqtt.connected()) return;
  const Telemetry& t = g_ctl->telemetry();
  JsonDocument d;
  d["mode"] = mode_name(t.mode);
  d["t_sensor"] = t.t_sensor;
  d["rh_sensor"] = t.rh_sensor;
  d["t_occ"] = t.t_occ;
  d["pmv_est"] = t.pmv_est;
  d["fan_step"] = t.fan_step;
  d["believed_sp"] = t.ac.setpoint_c;
  d["ac_power"] = t.ac.power;
  d["present"] = t.occupied;
  d["verdict"] = verdict_name(t.last_verdict);
  char buf[384];
  const size_t n = serializeJson(d, buf);
  g_mqtt.publish("duet/" DUET_ROOM "/state", reinterpret_cast<uint8_t*>(buf), n);
}

void setup() {
  Serial.begin(115200);
  g_sht.begin();
  g_radar.begin();
  g_irrx.begin();
  WiFi.mode(WIFI_STA);
  if (strlen(DUET_WIFI_SSID)) WiFi.begin(DUET_WIFI_SSID, DUET_WIFI_PASS);
  configTime(19800, 0, "pool.ntp.org");  // IST
  g_mqtt.setServer(DUET_MQTT_HOST, 1883);
  g_mqtt.setCallback(on_mqtt);
  static Controller ctl(Hal{&g_clock, &g_sht, &g_radar, &g_irtx, &g_irrx, &g_fan, &g_log});
  g_ctl = &ctl;
  g_ctl->begin();
}

void loop() {
  // Control never waits for the network: Wi-Fi or broker outages do not stop Duet.
  if (millis() - g_last_tick >= 1000) {
    g_last_tick = millis();
    g_ctl->tick();
  }
  if (WiFi.status() == WL_CONNECTED) {
    if (!g_mqtt.connected() && g_mqtt.connect("duet-" DUET_ROOM)) g_mqtt.subscribe("duet/" DUET_ROOM "/cmd");
    g_mqtt.loop();
    if (millis() - g_last_pub >= 10000) { g_last_pub = millis(); mqtt_publish(); }
  }
  delay(5);
}
#endif  // ARDUINO
