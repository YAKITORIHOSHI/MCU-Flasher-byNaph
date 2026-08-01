#include <WiFi.h>
#include <WiFiProv.h>

const char *pop = "042303";
const char *service_name = "PROV_ESP32S3_NAPH";
const char *host = "www.google.com";

// -------------------------------------------------------------
bool checkGoogle() {
  WiFiClient client;
  if (!client.connect(host, 80)) return false;

  client.print(String("GET / HTTP/1.1\r\n") +
               "Host: " + host + "\r\n" +
               "Connection: close\r\n\r\n");

  unsigned long timeout = millis() + 5000;
  while (!client.available()) {
    if (millis() > timeout) { client.stop(); return false; }
    delay(10);
  }

  client.readStringUntil('\n');  // consume response
  client.stop();
  return true;
}

// -------------------------------------------------------------
void SysProvEvent(arduino_event_t *sys_event) {
  if (sys_event->event_id == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.println("Got IP, checking internet...");
    if (checkGoogle()) {
      Serial.println("Internet available");
    } else {
      Serial.println("Internet unavailable → disconnecting");
      WiFi.setAutoReconnect(false);
      WiFi.disconnect();              
    }
  }
}

// -------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  WiFi.onEvent(SysProvEvent);

  WiFiProv.beginProvision(
    NETWORK_PROV_SCHEME_SOFTAP,
    NETWORK_PROV_SCHEME_HANDLER_NONE,
    NETWORK_PROV_SECURITY_1,
    pop,
    service_name
  );

  Serial.println("Provisioning started. Connect to AP '" + String(service_name) + "' to set Wi-Fi.");
}

// -------------------------------------------------------------
void loop() {}
