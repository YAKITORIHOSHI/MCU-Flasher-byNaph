#include <FastAccelStepper.h>
#include <ESP32Servo.h>

// ============================
// SERVO & ULTRASONIC
// ============================
#define SERVO_PIN 23
Servo servo;

#define TRIG 5
#define ECHO 4

#define BUZZER_PIN 25

#define DEFAULT_VIEW 80
#define SCAN_LEFT 130
#define SCAN_RIGHT 20

// ============================================================
// OBSTACLE CONFIGURATION
// ============================================================
#define OBSTACLE_DISTANCE 35.0f  // cm
#define MAX_BLOCKED_COUNT 10

// ============================
// TB6600 Connections
// ============================
const int STEP_PIN1 = 21;  // Right Motor STEP+
const int DIR_PIN1 = 22;   // Right Motor DIR+
const int STEP_PIN2 = 13;  // Left Motor STEP+
const int DIR_PIN2 = 12;   // Left Motor DIR+

// ============================
// DRIVER mode = STEP + DIR
// ============================
FastAccelStepperEngine engine = FastAccelStepperEngine();
FastAccelStepper *stepper1 = NULL;
FastAccelStepper *stepper2 = NULL;

// ============================
// Movement Configs
// ============================
const uint32_t MAX_SPEED = 8000;
const uint32_t ACCELERATION = 8000;

const uint32_t NUDGE_SPEED = 6000;
const int32_t FAR = 10000000;
const int STEP_VAL = 5000;

const uint32_t NUDGE_ACCEL = (uint32_t)(ACCELERATION * NUDGE_SPEED / MAX_SPEED);  // 750

static uint32_t lastPrintMs = 0, startMs = millis();

static bool nudgeDone1 = false, nudgeDone2 = false;
static bool forwardDone1 = false, forwardDone2 = false;

void buzzerTask(void *pvParameters) {
  digitalWrite(BUZZER_PIN, HIGH);
  vTaskDelay(pdMS_TO_TICKS(500));
  digitalWrite(BUZZER_PIN, LOW);
  vTaskDelay(pdMS_TO_TICKS(100));
  vTaskDelete(NULL);
}

// ────────────────────────────
// Ultrasonic reading WITHOUT moving the servo
// ────────────────────────────
float readDistanceRaw() {
  digitalWrite(TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG, LOW);
  long duration = pulseIn(ECHO, HIGH, 30000);
  if (duration == 0) return 999.0f;
  return duration * 0.0343f / 2.0f;
}

// Original getDistance (used in scanAngle)
float getDistance() {
  digitalWrite(TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG, LOW);
  long duration = pulseIn(ECHO, HIGH, 30000);
  if (duration == 0) return 999.0f;
  servo.write(DEFAULT_VIEW);
  return duration * 0.0343f / 2.0f;
}

float scanAngle(int angle) {
  servo.write(angle);
  delay(250);
  float d = getDistance();
  Serial.printf("SONIC %d° = %.1f cm\n", angle, d);
  servo.write(DEFAULT_VIEW);
  return d;
}

void turnRight(int32_t step = STEP_VAL) {
  stepper1->move(-step);
  stepper2->move(step);
  while (stepper1->isRunning() || stepper2->isRunning()) {
    delay(1);
  }
}

void turnLeft(int32_t step = STEP_VAL) {
  stepper1->move(step);
  stepper2->move(-step);
  while (stepper1->isRunning() || stepper2->isRunning()) {
    delay(1);
  }
}

void emergencyStopMotors() {
  stepper1->forceStopAndNewPosition(stepper1->getCurrentPosition());
  stepper2->forceStopAndNewPosition(stepper2->getCurrentPosition());
}

// --------------------------------------------------
//  CONTINUOUS MODE HELPERS
// --------------------------------------------------
void startStraight() {
  stepper1->setSpeedInHz(MAX_SPEED);
  stepper1->setAcceleration(ACCELERATION);
  stepper2->setSpeedInHz(MAX_SPEED);
  stepper2->setAcceleration(ACCELERATION);
  stepper1->runForward();
  stepper2->runForward();
}

void restoreStraight() {
  stepper1->setSpeedInHz(MAX_SPEED);
  stepper1->applySpeedAcceleration();
  stepper2->setSpeedInHz(MAX_SPEED);
  stepper2->applySpeedAcceleration();
}

void nudgeLeftContinuous(float delayMs) {
  // Reduce right motor speed → veer left
  stepper2->setSpeedInHz((uint32_t)(MAX_SPEED * 0.6));   // 60% of max = slower right wheel
  stepper2->applySpeedAcceleration();                     // <--- KEY: apply the new speed NOW

  Serial.println("<-- NUDGE LEFT");
  delay(delayMs);

  // Restore straight
  stepper2->setSpeedInHz(MAX_SPEED);
  stepper2->applySpeedAcceleration();
  Serial.println("^ STRAIGHT ^");
}

void nudgeRightContinuous(float delayMs) {
  // Reduce left motor speed → veer right
  stepper1->setSpeedInHz((uint32_t)(MAX_SPEED * 0.6));
  stepper1->applySpeedAcceleration();

  Serial.println("--> NUDGE RIGHT");
  delay(delayMs);

  stepper1->setSpeedInHz(MAX_SPEED);
  stepper1->applySpeedAcceleration();
  Serial.println("^ STRAIGHT ^");
}

// --------------------------------------------------
//  ABSOLUTE MOVE WITH OPTIONAL ADJ MODE
// --------------------------------------------------
void moveToTarget(long target1, long target2, bool isADJ = false) {

  if (isADJ) {
    // Fast simple move – no sweep, no obstacle detection, no serial commands
    stepper1->setSpeedInHz(MAX_SPEED);
    stepper1->setAcceleration(ACCELERATION);
    stepper2->setSpeedInHz(MAX_SPEED);
    stepper2->setAcceleration(ACCELERATION);
    stepper1->moveTo(target1);
    stepper2->moveTo(target2);
    while (stepper1->isRunning() || stepper2->isRunning()) {
      delay(1);
    }
    return;
  }

  // Normal mode – continuous forward, sweeping, obstacle handling, serial nudges
  startStraight();

  int servoAngle = DEFAULT_VIEW;
  int servoDir = 1;
  const int stepSize = 10;

  // Run until both motors have reached their position targets
  while (stepper1->getCurrentPosition() < target1 || stepper2->getCurrentPosition() < target2) {

    // ── Handle serial commands (nudges) without stopping ──
    while (Serial1.available()) {
      String msg = Serial1.readStringUntil('\n');
      msg.trim();
      if (msg.length() == 0) continue;
      Serial.println("[XIAO] : " + msg);

      String cmd = msg;
      float delayMs = 200.0;  // default nudge time

      int spaceIdx = msg.indexOf(' ');
      if (spaceIdx > 0) {
        cmd = msg.substring(0, spaceIdx);
        delayMs = msg.substring(spaceIdx + 1).toFloat();
        if (delayMs <= 0) delayMs = 150.0;
      }

      if (cmd == "NL") {
        nudgeLeftContinuous(delayMs);
      } else if (cmd == "NR") {
        nudgeRightContinuous(delayMs);
      } else if (cmd == "S") {
        // Force straight: stop and re-issue remaining distance
        emergencyStopMotors();
        long rem1 = target1 - stepper1->getCurrentPosition();
        long rem2 = target2 - stepper2->getCurrentPosition();
        if (rem1 > 0 && rem2 > 0) {
          stepper1->setSpeedInHz(MAX_SPEED);
          stepper1->setAcceleration(ACCELERATION);
          stepper2->setSpeedInHz(MAX_SPEED);
          stepper2->setAcceleration(ACCELERATION);
          stepper1->move(rem1);
          stepper2->move(rem2);
        }
      }
    }

    // ── Sweep servo left/right ──
    servoAngle += servoDir * stepSize;
    if (servoAngle >= SCAN_LEFT) {
      servoAngle = SCAN_LEFT;
      servoDir = -1;
    } else if (servoAngle <= SCAN_RIGHT) {
      servoAngle = SCAN_RIGHT;
      servoDir = 1;
    }
    servo.write(servoAngle);
    delay(15);  // servo settling time

    // ── Read ultrasonic at current angle ──
    float d = readDistanceRaw();

    // ── Obstacle detection ──
    if (d <= OBSTACLE_DISTANCE) {
      // Stop motors (only for the obstacle handling)
      emergencyStopMotors();

      // Beep
      digitalWrite(BUZZER_PIN, HIGH);
      delay(750);
      digitalWrite(BUZZER_PIN, LOW);

      // Reverse 200 steps (blocking)
      stepper1->move(-200);
      stepper2->move(-200);
      while (stepper1->isRunning() || stepper2->isRunning()) {
        delay(1);
      }

      // Wait until left, front, right are all clear
      bool allClear = false;
      while (!allClear) {
        delay(500);
        float leftDist = scanAngle(SCAN_LEFT);
        delay(200);
        float frontDist = scanAngle(DEFAULT_VIEW);
        delay(200);
        float rightDist = scanAngle(SCAN_RIGHT);
        delay(200);

        if (leftDist > OBSTACLE_DISTANCE && frontDist > OBSTACLE_DISTANCE && rightDist > OBSTACLE_DISTANCE) {
          allClear = true;
        } else {
          digitalWrite(BUZZER_PIN, HIGH);
          delay(500);
          digitalWrite(BUZZER_PIN, LOW);
          delay(2000);
        }
      }

      // Resume continuous forward run
      startStraight();
    }

    delay(1);  // yield
  }

  // Target reached – stop motors and return servo to default
  emergencyStopMotors();
  servo.write(DEFAULT_VIEW);
}

// Convenience: move a given number of steps forward
void moveDistance(int32_t steps = FAR, bool isADJ = false) {
  long t1 = stepper1->getCurrentPosition() + steps;
  long t2 = stepper2->getCurrentPosition() + steps;
  moveToTarget(t1, t2, isADJ);
}

void flushXiaoSerial() {
  while (Serial1.available()) Serial1.read();
}

void returnToPointB() {

  // Adj
  for(int i = 0; i < 5; i++){
    moveDistance(100, true);
    delay(1);
  }

  delay(100);

  moveDistance(13000);

  delay(100);

  turnLeft(4900);

  delay(100);

  // Adj
  for(int i = 0; i < 5; i++){
    moveDistance(100);
    delay(1);
  }

  delay(100);

  // Move Forward
  moveDistance(25500);

  delay(100);

  turnLeft(4900);

  delay(100);

  for(int i = 0; i < 5; i++){
    moveDistance(100);
    delay(1);
  }

  delay(100);

  moveDistance(6000);

  delay(100);

  turnRight(4900);

  delay(100);

  for(int i = 0; i < 5; i++){
    moveDistance(100);
    delay(1);
  }

  moveDistance(471000);
  
  delay(100);

  turnRight(4800);

  delay(100);

  // Adj
  for(int i = 0; i < 5; i++){
    moveDistance(100);
    delay(1);
  }

  delay(100);

  // Move Forward
  moveDistance(7000);

  delay(100);

  turnLeft(9800);

  delay(100);

}

void runStart() {
  // Adj
  //for (int i = 0; i < 5; i++) {
    //moveDistance(100, true);  // ← ADJ mode
    //delay(1);
  //}

  //delay(100);

  //moveDistance(5000);

  //delay(100);

  //turnLeft(4900);

  //delay(100);

  // Adj
  for (int i = 0; i < 5; i++) {
    moveDistance(100, true);
    delay(1);
  }

  //delay(100);

  moveDistance(470000);

  //delay(100);

  turnLeft(4900);

  delay(100);

  // Adj
  for (int i = 0; i < 5; i++) {
    moveDistance(100, true);
    delay(1);
  }

  delay(100);

  moveDistance(3000);

  delay(100);

  turnRight(4900);

  delay(100);

  // Adj
  for (int i = 0; i < 5; i++) {
    moveDistance(100, true);
    delay(1);
  }

  delay(100);

  moveDistance(20000);
  
  delay(100);

  turnRight(4900);

  delay(100);

  // Adj
  for (int i = 0; i < 5; i++) {
    moveDistance(100, true);
    delay(1);
  }

  delay(100);

  moveDistance(8500);

  delay(100);

  turnLeft(9800);

}

float frontDistance = 0.0, leftDistance = 0.0, rightDistance = 0.0;

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial1.begin(9600, SERIAL_8N1, /*RX=*/18, /*TX=*/19);
  Serial1.setTimeout(2000);

  flushXiaoSerial();
  delay(100);

  // ── Stepper Motor Setup ──────────────────────────────────
  engine.init();
  stepper1 = engine.stepperConnectToPin(STEP_PIN1);
  stepper2 = engine.stepperConnectToPin(STEP_PIN2);

  if (stepper1) {
    stepper1->setDirectionPin(DIR_PIN1, false);
    stepper1->setSpeedInHz(MAX_SPEED);
    stepper1->setAcceleration(ACCELERATION);
  }

  if (stepper2) {
    stepper2->setDirectionPin(DIR_PIN2, true);
    stepper2->setSpeedInHz(MAX_SPEED);
    stepper2->setAcceleration(ACCELERATION);
  }

  // ── Servo Setup ──────────────────────────────────────────
  servo.setPeriodHertz(50);
  servo.attach(SERVO_PIN, 500, 2500);
  delay(100);
  servo.write(DEFAULT_VIEW);

  // ── Sensor / Buzzer Pin Setup ────────────────────────────
  pinMode(TRIG, OUTPUT);
  pinMode(ECHO, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);

  delay(1000);
  startMs = millis();

  xTaskCreate(buzzerTask, "BuzzerTask", 1024, NULL, 1, NULL);
  runStart();
  delay(3000);
  returnToPointB();

}

bool isConnected = false, isBlocked = false;

void loop() {
  // Your future loop code can go here
}