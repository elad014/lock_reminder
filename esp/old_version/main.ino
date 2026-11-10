#include <ArduinoOTA.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <stdio.h>
#include <string.h>

#include "config.h"

const int sensorPin = 2;
const char OTA_HOSTNAME[] = "lock-reminder";
const char OTA_PASSWORD[] = "lock-reminder";

const unsigned long EDGE_STABLE_MS = 50;
const unsigned long WIFI_JOIN_TIMEOUT_MS = 15000;
const uint16_t NEON_TIMEOUT_MS = 15000;
const int NEON_URL_CAPACITY = 80;

const char STATUS_MELLON_SQL[] =
    "WITH updated AS ("
    " UPDATE state"
    " SET status = 'Mellon'"
    " WHERE ctid = (SELECT ctid FROM state LIMIT 1)"
    " RETURNING status"
    ") INSERT INTO state (status)"
    " SELECT 'Mellon'"
    " WHERE NOT EXISTS (SELECT 1 FROM updated)";

const char STATUS_AUTOMATE_SQL[] =
    "WITH updated AS ("
    " UPDATE state"
    " SET status = 'Automate'"
    " WHERE ctid = (SELECT ctid FROM state LIMIT 1)"
    " RETURNING status"
    ") INSERT INTO state (status)"
    " SELECT 'Automate'"
    " WHERE NOT EXISTS (SELECT 1 FROM updated)";

// Public Let's Encrypt root that signs the Neon host.
static const char ISRG_ROOT_X1[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
)EOF";

int lastLevel = -1;

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

bool postNeonStatus(const char* sql) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Neon skipped, Wi-Fi is down");
    return false;
  }

  char url[NEON_URL_CAPACITY];
  const int urlLength = snprintf(url, sizeof(url), "https://%s/sql", NEON_HOST);
  if (urlLength < 0 || static_cast<size_t>(urlLength) >= sizeof(url)) {
    Serial.println("Neon URL does not fit");
    return false;
  }

  String body;
  body.reserve(strlen(sql) + 24);
  body += "{\"query\":\"";
  body += sql;
  body += "\",\"params\":[]}";

  WiFi.setSleepMode(WIFI_NONE_SLEEP);

  BearSSL::WiFiClientSecure client;
  BearSSL::X509List trustAnchors(ISRG_ROOT_X1);
  client.setBufferSizes(6144, 1024);
  client.setTrustAnchors(&trustAnchors);

  HTTPClient http;
  bool sent = false;
  if (!http.begin(client, url)) {
    Serial.println("Neon connection did not start");
  } else {
    http.setTimeout(NEON_TIMEOUT_MS);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Neon-Connection-String", NEON_CONNECTION_STRING);
    const int statusCode = http.POST(body);
    Serial.print("Neon HTTP ");
    Serial.println(statusCode);
    if (statusCode >= 200 && statusCode < 300) {
      sent = true;
    } else {
      Serial.println(http.getString());
    }
    http.end();
  }

  WiFi.setSleepMode(WIFI_LIGHT_SLEEP);
  return sent;
}

void watchSensor() {
  const int level = digitalRead(sensorPin);
  if (level == lastLevel) {
    return;
  }

  delay(EDGE_STABLE_MS);
  const int confirmed = digitalRead(sensorPin);
  if (confirmed == lastLevel || confirmed != level) {
    return;
  }

  lastLevel = confirmed;
  if (confirmed == HIGH) {
    Serial.println("GPIO 2 low to high");
    postNeonStatus(STATUS_MELLON_SQL);
    return;
  }
  Serial.println("GPIO 2 high to low");
  postNeonStatus(STATUS_AUTOMATE_SQL);
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(sensorPin, INPUT);
  Serial.println("ESP GPIO 2");
  connectWifi();
  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleepMode(WIFI_LIGHT_SLEEP);
    Serial.println("Wi-Fi light sleep");
  }
  startOta();
  lastLevel = digitalRead(sensorPin);
}

void loop() {
  ArduinoOTA.handle();
  watchSensor();
}
