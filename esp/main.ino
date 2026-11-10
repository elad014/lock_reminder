#include <ArduinoOTA.h>
#include <ESP8266WiFi.h>

const int hallSensorPin = 4;

// Fill these in to join the home network. Leave the name empty and the
// module hosts its own Wi-Fi, named lock-reminder, for the upload.
const char WIFI_SSID[] = "";
const char WIFI_PASSWORD[] = "";
const char OTA_HOSTNAME[] = "lock-reminder";
const char OTA_PASSWORD[] = "lock-reminder";

const unsigned long SENSOR_PERIOD_MS = 200;
const unsigned long WIFI_JOIN_TIMEOUT_MS = 15000;

unsigned long lastReportMs = 0;

void startAccessPoint() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(OTA_HOSTNAME);
  Serial.print("Upload Wi-Fi: ");
  Serial.println(OTA_HOSTNAME);
  Serial.print("Upload address: ");
  Serial.println(WiFi.softAPIP());
}

void connectWifi() {
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);

  if (WIFI_SSID[0] == '\0') {
    startAccessPoint();
    return;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Joining Wi-Fi ");
  Serial.println(WIFI_SSID);

  const unsigned long startedMs = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startedMs < WIFI_JOIN_TIMEOUT_MS) {
    delay(200);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Home Wi-Fi did not connect. Starting upload Wi-Fi.");
    startAccessPoint();
    return;
  }

  Serial.print("Upload address: ");
  Serial.println(WiFi.localIP());
}

void onOtaStart() {
  Serial.println("OTA update started");
}

void onOtaEnd() {
  Serial.println("OTA update finished");
}

void onOtaError(ota_error_t error) {
  Serial.print("OTA error ");
  Serial.println(static_cast<int>(error));
}

void startOta() {
  ArduinoOTA.setHostname(OTA_HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart(onOtaStart);
  ArduinoOTA.onEnd(onOtaEnd);
  ArduinoOTA.onError(onOtaError);
  ArduinoOTA.begin();
  Serial.println("OTA ready. Hostname lock-reminder, password lock-reminder");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(hallSensorPin, INPUT);
  Serial.println("ESP-01 hall sensor");
  connectWifi();
  startOta();
  lastReportMs = millis();
}

void reportSensor() {
  const int sensorState = digitalRead(hallSensorPin);
  if (sensorState == LOW) {
    Serial.println("Magnet Field Detected!!!!!");
    return;
  }
  Serial.println("No Magnet Field!!!");
}

void loop() {
  ArduinoOTA.handle();

  const unsigned long nowMs = millis();
  if (nowMs - lastReportMs < SENSOR_PERIOD_MS) {
    return;
  }
  lastReportMs = nowMs;
  reportSensor();
}
