#include "NAPHTALI_CODE_V2.h"

// ============================================================
// SETUP
// ============================================================
void setup() {
  setCpuFrequencyMhz(240);

  Serial.begin(115200);
  Air780.begin(115200, SERIAL_8N1, AIR_RX, AIR_TX);
  ESP_Serial.begin(9600, SERIAL_8N1, ESP_RX, ESP_TX);
  delay(1000);
  flushESPSerial();

  powerOnAir780();
  if (!waitForModule(20)) {
    Serial.println("[Air780E] FATAL: Module not responding.");
    while (true) delay(1000);
  }
  sendAT("AT+CGSN");
  sendAT("AT+CSQ");
  sendAT("AT+CIMI");
  sendAT("AT+COPS?");
  sendAT("AT+CFUN=1", 5000);
  delay(1000);
  if (!waitForNetwork(20)) {
    Serial.println("[Air780E] Proceeding anyway — SMS may fail.");
  }
  sendAT("AT+CSQ");
  delay(100);

  // ── Stepper Motor Setup ────────────────────────────────────
  engine.init();
  stepper1 = engine.stepperConnectToPin(STEP_PIN1);
  stepper2 = engine.stepperConnectToPin(STEP_PIN2);
  if (stepper1) {
    stepper1->setDirectionPin(DIR_PIN1, false);
    stepper1->setSpeedInHz(MAX_SPEED);
    stepper1->setAcceleration(ACCELERATION);
  }
  if (stepper2) {
    stepper2->setDirectionPin(DIR_PIN2);
    stepper2->setSpeedInHz(MAX_SPEED);
    stepper2->setAcceleration(ACCELERATION);
  }
  Serial.println("[Motor] Setup Done.");
  delay(100);

  // ── Servo Setup ───────────────────────────────────────────
  servo.setPeriodHertz(50);
  servo.attach(SERVO_PIN, 500, 2500);

  // ── Sensor / Buzzer Pin Setup ─────────────────────────────
  pinMode(TRIG_PIN,   OUTPUT);
  pinMode(ECHO_PIN,   INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  delay(100);

  // ── Load Cell Setup ───────────────────────────────────────
  scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
  long ZERO_FACTOR = scale.read_average();
  scale.set_offset(ZERO_FACTOR);
  scale.set_scale(CALIBRATION_FACTOR);
  Serial.printf("[DEBUG] Zero factor: %ld\n", ZERO_FACTOR);

  servo.write(DEFAULT_VIEW);
  delay(5000);
  scale.tare();
  delay(100);

  // ── Wait for BLE bridge to signal connection ───────────────
  // The BLE bridge (GarbyESP32) sends "[BLE CONNECTION ESTABLISHED]"
  // over UART once the Raspi successfully connects to it.
  // We block here so the robot never starts without a live BLE link.
  Serial.println("[BOOT] Waiting for [BLE CONNECTION ESTABLISHED]...");
  bool bleReady = false;
  while (!bleReady) {
    while (Air780.available()) Serial.write(Air780.read()); // keep Air780 alive
    if (ESP_Serial.available()) {
      String msg = ESP_Serial.readStringUntil('\n');
      msg.trim();
      Serial.println("[BOOT] ESP: " + msg);
      if (msg == "[BLE CONNECTION ESTABLISHED]") {
        bleReady = true;
        Serial.println("[BOOT] BLE ready — entering loop.");
        // Brief confirmation beep
        digitalWrite(BUZZER_PIN, HIGH);
        delay(200);
        digitalWrite(BUZZER_PIN, LOW);
      }
    }
    delay(50);
  }

  sendSMS(CONTACT_NUMBER, "[GARBY] Restarted and ready.");
  delay(100);

  xTaskCreate(buzzerTask, "BuzzerTask", 1024, NULL, 1, NULL);
  delay(1000);
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  // Always drain ESP and Air780 pass-through
  pollESP();
  updateNudge();
  while (Air780.available()) Serial.write(Air780.read());
  while (Serial.available()) Air780.write(Serial.read());

  // ── IDLE ──────────────────────────────────────────────────
  if (garbyState == GarbyState::IDLE) {
    printIdleUptime();

    float w = scale.get_units(10) - 0.010f;
    w = (w > -0.05f && w < 0.05f) ? 0.0f : w;
    Serial.printf("[IDLE] Load: %.1f kg\n", w);

    bool loadReady = (w >= MAX_LOAD_APPRX);
    bool gasReady  = sensorTripped;

    if (loadReady || gasReady) {
      if (loadReady && !loadcellSMSSent) {
        sendSMS(CONTACT_NUMBER, "[GARBY] Load threshold reached. Moving to Area B.");
        loadcellSMSSent = true;
      }
      if (gasReady && !loadcellSMSSent) {
        sendSMS(CONTACT_NUMBER, "[GARBY] Dangerous gas detected. Moving to Area B.");
        loadcellSMSSent = true;
      }
      garbyState = GarbyState::RUNNING;
      Serial.println("[STATE] IDLE -> RUNNING");
    } else {
      vTaskDelay(pdMS_TO_TICKS(200));
    }

  // ── RUNNING ────────────────────────────────────────────────
  } else if (garbyState == GarbyState::RUNNING) {
    Serial.println("[RUN] Starting runStart()");
    runStart();
    Serial.println("[RUN] runStart() complete");

    if (resetQueued) {
      resetQueued = false;
      garbyState  = GarbyState::RETURNING;
      Serial.println("[STATE] RUNNING -> RETURNING (queued reset)");
    } else {
      Serial.println("[RUN] Waiting for [RESET]...");
      emergencyStopMotors();
      vTaskDelay(pdMS_TO_TICKS(200));
    }

  // ── RETURNING ─────────────────────────────────────────────
  } else if (garbyState == GarbyState::RETURNING) {
    Serial.println("[RETURN] Starting returnToPointB()");
    returnToPointB();
    Serial.println("[RETURN] Done.");
    fullReset();
  }
}
