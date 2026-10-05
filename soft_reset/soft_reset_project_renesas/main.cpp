#include <Arduino.h>
void setup() {
  Serial.begin(115200);
  unsigned long start = millis();
  while (!Serial && (millis() - start < 1500)) {
    delay(10);
  }
  Serial.println(">>> ----- <<<");
}

void loop() {
  delay(1000);
}
