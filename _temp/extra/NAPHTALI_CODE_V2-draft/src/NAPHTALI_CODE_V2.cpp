#include "NAPHTALI_CODE_V2.h"

// ============================================================
// GLOBAL OBJECT DEFINITIONS
// ============================================================
HX711             scale;
HardwareSerial    ESP_Serial(1);
HardwareSerial    Air780(2);
Servo             servo;
FastAccelStepperEngine engine   = FastAccelStepperEngine();
FastAccelStepper*      stepper1 = NULL;
FastAccelStepper*      stepper2 = NULL;

// ============================================================
// STATIC / MISC VARS
// ============================================================
uint32_t lastPrintMs  = 0;
uint32_t startMs      = 0;
bool     nudgeDone1   = false, nudgeDone2   = false;
bool     forwardDone1 = false, forwardDone2 = false;

// ============================================================
// GLOBAL STATE DEFINITIONS
// ============================================================
LidarZones zones;
bool          lidarBlockedActive   = false;
bool          lidarControlled      = false;
unsigned long lidarBlockedStart    = 0;
unsigned long lidarLastPeriodicSMS = 0;
unsigned long lidarLastLRScan      = 0;
int           blockedCount         = 0;
bool          idleMode             = false;
bool          lastConnected        = false;
bool          movingForward        = false;
bool          blockedSMSSent       = false;
bool          loadcellSMSSent      = false;
bool          buzzerState          = false;
unsigned long lastBeepTime         = 0;
float frontDistance = 0.0f;
float leftDistance  = 0.0f;
float rightDistance = 0.0f;
bool isTrashbinFull = false;
bool shouldStop     = false;
bool sensorTripped  = false;

ParseData     path;
ReceivedDatas data;

GarbyState garbyState  = GarbyState::IDLE;
bool       resetQueued = false;

// ── Non-blocking nudge ───────────────────────────────────────
NudgeDir      activeNudge     = NudgeDir::NONE;
unsigned long nudgeStartMs    = 0;
unsigned long nudgeDurationMs = 0;

// Debounce / hysteresis counters
int  nudgeLeftCount  = 0;
int  nudgeRightCount = 0;
bool nudgeWasStable  = true;

unsigned long lastIdlePrintMs    = 0;
#define IDLE_PRINT_INTERVAL_MS   5000UL

// ============================================================
// NEW: requestStatus() – sends request to RasPi via BLE bridge
// ============================================================
void requestStatus() {
    ESP_Serial.println("[REQUEST-STATUS]");
    Serial.println("[REQ] Sent [REQUEST-STATUS] to BLE bridge");
}

// ============================================================
// PARSE DATA — PathState
// ============================================================
void ParseData::setFrontPath(PathState state) {
  front.FRONT = front.FRONT_LEFT = front.FRONT_RIGHT = state;
}
void ParseData::setBackPath(PathState state) {
  back.BACK = back.BACK_LEFT = back.BACK_RIGHT = state;
}
void ParseData::setSidePath(SideState state) {
  side.LEFT = side.RIGHT = state;
}
void ParseData::setFrontField(const String& field, PathState state) {
  if      (field == "FRONT")       front.FRONT       = state;
  else if (field == "FRONT_LEFT")  front.FRONT_LEFT  = state;
  else if (field == "FRONT_RIGHT") front.FRONT_RIGHT = state;
}
void ParseData::setBackField(const String& field, PathState state) {
  if      (field == "BACK")       back.BACK       = state;
  else if (field == "BACK_LEFT")  back.BACK_LEFT  = state;
  else if (field == "BACK_RIGHT") back.BACK_RIGHT = state;
}

static void applyFieldList(const String& fieldStr, ParseData::PathState state,
                           ParseData& pd, bool isFront) {
  if (fieldStr == "ALL") {
    if (isFront) pd.setFrontPath(state);
    else         pd.setBackPath(state);
    return;
  }
  int start = 0;
  while (true) {
    int    comma = fieldStr.indexOf(',', start);
    String token = (comma == -1) ? fieldStr.substring(start)
                                 : fieldStr.substring(start, comma);
    token.trim();
    if (isFront) pd.setFrontField(token, state);
    else         pd.setBackField(token, state);
    if (comma == -1) break;
    start = comma + 1;
  }
}

bool ParseData::parse(const String& raw) {
  String input = raw;
  input.trim();

  // --- 1) Parse PATH (front) ---
  int pathIdx = input.indexOf("PATH:");
  if (pathIdx == -1) return false;
  int pathEnd = input.indexOf('|', pathIdx);
  String pathVal = (pathEnd == -1) ? input.substring(pathIdx + 5)
                                   : input.substring(pathIdx + 5, pathEnd);
  pathVal.trim();

  // Parse the PATH value (CLEAR / BLOCKED / BLOCKED{...})
  if (pathVal == "CLEAR") {
    setFrontPath(PathState::CLEAR);
  } else if (pathVal == "BLOCKED") {
    setFrontPath(PathState::BLOCKED);
  } else if (pathVal.startsWith("BLOCKED{")) {
    int braceClose = pathVal.indexOf('}');
    if (braceClose == -1) return false;
    String fieldStr = pathVal.substring(8, braceClose);  // after "BLOCKED{"
    fieldStr.trim();
    // First set all to CLEAR, then mark specified fields as BLOCKED
    setFrontPath(PathState::CLEAR);
    applyFieldList(fieldStr, PathState::BLOCKED, *this, true);
  } else {
    return false;
  }

  // --- 2) Parse BACK_PATH ---
  int backIdx = input.indexOf("BACK_PATH:");
  if (backIdx == -1) return false;
  int backEnd = input.indexOf('|', backIdx);
  String backVal = (backEnd == -1) ? input.substring(backIdx + 10)
                                   : input.substring(backIdx + 10, backEnd);
  backVal.trim();

  // Parse the BACK_PATH value (same logic as PATH)
  if (backVal == "CLEAR") {
    setBackPath(PathState::CLEAR);
  } else if (backVal == "BLOCKED") {
    setBackPath(PathState::BLOCKED);
  } else if (backVal.startsWith("BLOCKED{")) {
    int braceClose = backVal.indexOf('}');
    if (braceClose == -1) return false;
    String fieldStr = backVal.substring(8, braceClose);
    fieldStr.trim();
    setBackPath(PathState::CLEAR);
    applyFieldList(fieldStr, PathState::BLOCKED, *this, false);
  } else {
    return false;
  }

  // --- 3) Parse SIDES ---
  int sidesIdx = input.indexOf("SIDES:");
  if (sidesIdx != -1) {
    // Everything after "SIDES:" is the sides value
    String sidesVal = input.substring(sidesIdx + 6);
    sidesVal.trim();

    // Determine if it's continuous (contains '=') or discrete
    if (sidesVal.indexOf('=') != -1) {
      // Continuous mode: LEFT=xxx|RIGHT=yyy
      continuousMode = true;
      int leftEq = sidesVal.indexOf('=');
      int pipe2  = sidesVal.indexOf('|');
      int rightEq = sidesVal.indexOf('=', pipe2);
      if (leftEq != -1 && pipe2 != -1 && rightEq != -1) {
        leftDist  = sidesVal.substring(leftEq + 1, pipe2).toFloat();
        rightDist = sidesVal.substring(rightEq + 1).toFloat();
        // Clamp to avoid extreme corrections
        if (leftDist  < 5.0)  leftDist  = 5.0;
        if (rightDist < 5.0)  rightDist = 5.0;
        if (leftDist  > 200.0) leftDist  = 200.0;
        if (rightDist > 200.0) rightDist = 200.0;
      }
    } else {
      // Discrete mode: STABLE / NUDGE_LEFT / NUDGE_RIGHT
      continuousMode = false;
      sidesVal.replace('-', '_');
      sidesVal.toUpperCase();
      if (sidesVal == "STABLE") {
        side.LEFT = side.RIGHT = SideState::STABLE;
      } else if (sidesVal == "NUDGE_LEFT") {
        side.LEFT  = SideState::NUDGE_LEFT;
      } else if (sidesVal == "NUDGE_RIGHT") {
        side.RIGHT = SideState::NUDGE_RIGHT;
      } else {
        return false;
      }
    }
  }
  // (If SIDES is missing, we simply leave previous state unchanged)

  return true;
}

void ParseData::printAllState() {
  auto ps = [](PathState s) { return s == PathState::BLOCKED ? "BLOCKED" : "CLEAR"; };
  auto ss = [](SideState s) -> const char* {
    switch (s) {
      case SideState::NUDGE_LEFT:  return "NUDGE_LEFT";
      case SideState::NUDGE_RIGHT: return "NUDGE_RIGHT";
      default:                     return "STABLE";
    }
  };
  Serial.println("=== ParseData ===");
  Serial.printf("  FRONT:       %s\n", ps(front.FRONT));
  Serial.printf("  FRONT_LEFT:  %s\n", ps(front.FRONT_LEFT));
  Serial.printf("  FRONT_RIGHT: %s\n", ps(front.FRONT_RIGHT));
  Serial.printf("  BACK:        %s\n", ps(back.BACK));
  Serial.printf("  BACK_LEFT:   %s\n", ps(back.BACK_LEFT));
  Serial.printf("  BACK_RIGHT:  %s\n", ps(back.BACK_RIGHT));
  if (continuousMode) {
    Serial.printf("  SIDE_CONT:  LEFT=%.1f  RIGHT=%.1f\n", leftDist, rightDist);
  } else {
    Serial.printf("  SIDE_LEFT:   %s\n", ss(side.LEFT));
    Serial.printf("  SIDE_RIGHT:  %s\n", ss(side.RIGHT));
  }
}

// ============================================================
// PARSE DATA — ReceivedDatas (unchanged)
// ============================================================
void ReceivedDatas::setUS(int val) {
  us.value = val;
  if      (val < 20) us.status = UltrasonicStatus::EMPTY;
  else if (val < 40) us.status = UltrasonicStatus::HALFWAY;
  else               us.status = UltrasonicStatus::FULL;
}
void ReceivedDatas::setMQ4(int val) {
  mq4.value = val;
  if      (val < 400) mq4.status = MQ4Status::NORMAL;
  else if (val < 700) mq4.status = MQ4Status::WARNING;
  else                mq4.status = MQ4Status::DANGER;
}
void ReceivedDatas::setMQ135(int val) {
  mq135.value = val;
  if      (val < 300) mq135.status = MQ135Status::CLEAN;
  else if (val < 500) mq135.status = MQ135Status::MODERATE;
  else if (val < 700) mq135.status = MQ135Status::POOR;
  else                mq135.status = MQ135Status::VERY_POOR;
}
void ReceivedDatas::setMQ137(int val) {
  mq137.value = val;
  if      (val < 400) mq137.status = MQ137Status::NORMAL;
  else if (val < 700) mq137.status = MQ137Status::WARNING;
  else                mq137.status = MQ137Status::DANGER;
}
bool ReceivedDatas::parse(const String& raw) {
  if (!raw.startsWith("SENSOR:")) return false;
  String body = raw.substring(7);
  body.trim();
  int segStart = 0;
  while (segStart < (int)body.length()) {
    int    pipe = body.indexOf('|', segStart);
    String seg  = (pipe == -1) ? body.substring(segStart)
                               : body.substring(segStart, pipe);
    seg.trim();
    segStart = (pipe == -1) ? (int)body.length() : pipe + 1;
    int eq = seg.indexOf('=');
    if (eq == -1) return false;
    String key = seg.substring(0, eq);
    int    val = seg.substring(eq + 1).toInt();
    key.trim();
    if      (key == "US")    setUS(val);
    else if (key == "MQ4")   setMQ4(val);
    else if (key == "MQ135") setMQ135(val);
    else if (key == "MQ137") setMQ137(val);
  }
  return true;
}
int ReceivedDatas::getUSValue()    { return us.value;    }
int ReceivedDatas::getMQ4Value()   { return mq4.value;   }
int ReceivedDatas::getMQ135Value() { return mq135.value; }
int ReceivedDatas::getMQ137Value() { return mq137.value; }
ReceivedDatas::UltrasonicStatus ReceivedDatas::getUSStatus()    { return us.status;    }
ReceivedDatas::MQ4Status        ReceivedDatas::getMQ4Status()   { return mq4.status;   }
ReceivedDatas::MQ135Status      ReceivedDatas::getMQ135Status() { return mq135.status; }
ReceivedDatas::MQ137Status      ReceivedDatas::getMQ137Status() { return mq137.status; }
void ReceivedDatas::printAll() {
  Serial.println("=== ReceivedDatas ===");
  Serial.printf("  US:    val=%d  status=%d\n", us.value,    (int)us.status);
  Serial.printf("  MQ4:   val=%d  status=%d\n", mq4.value,   (int)mq4.status);
  Serial.printf("  MQ135: val=%d  status=%d\n", mq135.value, (int)mq135.status);
  Serial.printf("  MQ137: val=%d  status=%d\n", mq137.value, (int)mq137.status);
}

bool pathReadDecisionMaker(ParseData& p) {
  bool frontBlocked = (p.getFront()      == ParseData::PathState::BLOCKED)
                   || (p.getFrontLeft()  == ParseData::PathState::BLOCKED)
                   || (p.getFrontRight() == ParseData::PathState::BLOCKED);
  bool backBlocked  = (p.getBack()       == ParseData::PathState::BLOCKED)
                   || (p.getBackLeft()   == ParseData::PathState::BLOCKED)
                   || (p.getBackRight()  == ParseData::PathState::BLOCKED);
  return (frontBlocked || backBlocked);
}
bool sensorReadDecisionMaker(ReceivedDatas& d) {
  if (d.getMQ4Status()   == ReceivedDatas::MQ4Status::DANGER)      return true;
  if (d.getMQ135Status() == ReceivedDatas::MQ135Status::VERY_POOR) return true;
  if (d.getMQ137Status() == ReceivedDatas::MQ137Status::DANGER)     return true;
  int secondHighCount = 0;
  if (d.getMQ4Status()   == ReceivedDatas::MQ4Status::WARNING)      secondHighCount++;
  if (d.getMQ135Status() == ReceivedDatas::MQ135Status::POOR)       secondHighCount++;
  if (d.getMQ137Status() == ReceivedDatas::MQ137Status::WARNING)    secondHighCount++;
  return (secondHighCount >= 3);
}

// ============================================================
// STATE MACHINE HELPERS
// ============================================================

// ── pollESP ──────────────────────────────────────────────────
// Reads all pending messages from ESP_Serial, updates global
// path/sensor/reset state, and immediately sends ACK_MSG back
// so the BLE bridge can forward "[ESP RECEIVED]" to the Raspi.
void pollESP() {
  while (ESP_Serial.available()) {
    String espMsg = ESP_Serial.readStringUntil('\n');
    espMsg.trim();
    if (espMsg.length() == 0) continue;

    Serial.println("[ESP_IN] " + espMsg);

    // ── RESET command ──────────────────────────────────────
    if (espMsg == "[RESET]") {
      if (garbyState == GarbyState::RUNNING) {
        resetQueued = true;
        Serial.println("[RESET] Queued");
      } else if (garbyState == GarbyState::IDLE) {
        garbyState = GarbyState::RETURNING;
        Serial.println("[RESET] -> RETURNING");
      }
      ESP_Serial.println(ACK_MSG);
      continue;
    }

    // ── PATH / BACK_PATH message or combined ──────────────
    if (espMsg.indexOf("PATH:") != -1 || espMsg.indexOf("BACK_PATH:") != -1 || espMsg.indexOf("SIDES:") != -1) {
      bool ok = path.parse(espMsg);
      if (ok) {
        shouldStop = pathReadDecisionMaker(path);
        Serial.printf("[COMBINED] parsed OK — shouldStop=%d\n", (int)shouldStop);
      } else {
        Serial.println("[WARN] Bad combined message: " + espMsg);
      }
      ESP_Serial.println(ACK_MSG);
      continue;
    }

    // ── SENSOR message ────────────────────────────────────
    if (espMsg.startsWith("SENSOR")) {
      bool ok = data.parse(espMsg);
      if (ok) {
        sensorTripped = sensorReadDecisionMaker(data);
        Serial.printf("[SENSOR] parsed OK — sensorTripped=%d\n", (int)sensorTripped);
      } else {
        Serial.println("[WARN] Bad SENSOR message: " + espMsg);
      }
      ESP_Serial.println(ACK_MSG);
      continue;
    }

    // ── Anything else — log it ────────────────────────────
    Serial.println(">>> RECEIVED: " + espMsg + " <<<");
  }
}

// ── pollAndApplySideNudge (fallback discrete; now mostly unused) ──
// We keep it for non‑continuous mode, but moveToTarget now uses continuous.
void pollAndApplySideNudge() {
  if (path.isContinuousMode()) return;   // disable discrete nudges

  ParseData::SideState sLeft  = path.getSideLeft();
  ParseData::SideState sRight = path.getSideRight();

  bool wantLeft  = (sLeft  == ParseData::SideState::NUDGE_LEFT);
  bool wantRight = (sRight == ParseData::SideState::NUDGE_RIGHT);

  if (!wantLeft && !wantRight) {
    nudgeLeftCount  = 0;
    nudgeRightCount = 0;
    nudgeWasStable  = true;
    return;
  }

  if (activeNudge != NudgeDir::NONE) return;

  if (wantLeft) {
    nudgeLeftCount++;
    nudgeRightCount = 0;
    if (nudgeLeftCount >= NUDGE_DEBOUNCE_COUNT) {
      nudgeLeftContinuous((float)NUDGE_DURATION_MS);
      nudgeLeftCount = 0;
    }
  } else if (wantRight) {
    nudgeRightCount++;
    nudgeLeftCount = 0;
    if (nudgeRightCount >= NUDGE_DEBOUNCE_COUNT) {
      nudgeRightContinuous((float)NUDGE_DURATION_MS);
      nudgeRightCount = 0;
    }
  }
}

// ── haltAndWait ───────────────────────────────────────────────
void haltAndWait(const String& reason) {
  emergencyStopMotors();
  movingForward = false;
  if (!blockedSMSSent) {
    sendSMS(CONTACT_NUMBER, "[GARBY] Blocked: " + reason);
    blockedSMSSent = true;
  }
  unsigned long lastSMS  = millis();
  unsigned long lastBeep = millis();
  unsigned long lastRequestTime = millis();
  const unsigned long REQUEST_INTERVAL_MS = 400;   // ask for new status every 500 ms

  Serial.println("[HALT] Waiting for path to clear...");
  while (true) {
    // ── Ask RasPi for fresh LiDAR data ──────────────────────────
    if (millis() - lastRequestTime >= REQUEST_INTERVAL_MS) {
      requestStatus();               // sends "[REQUEST-STATUS]" to BLE bridge
      lastRequestTime = millis();
    }

    // ── Read any responses from RasPi via BLE bridge ────────────
    pollESP();   // updates shouldStop based on incoming PATH messages

    // ── Check if LiDAR path is clear ────────────────────────────
    if (reason.startsWith("PATH") && !shouldStop) {
      Serial.println("[HALT] LiDAR path cleared — resuming");
      blockedSMSSent = false;
      return;
    }

    // ── Check if local ultrasonic obstacle is clear ────────────
    if (reason.startsWith("SONIC")) {
      float localDist = getDistance();
      if (localDist >= OBSTACLE_DISTANCE) {
        Serial.println("[HALT] Sonic cleared — resuming");
        blockedSMSSent = false;
        return;
      }
    }

    // ── Buzzer and SMS reminders ──────────────────────────────
    if (millis() - lastBeep >= 5000) {
      lastBeep = millis();
      digitalWrite(BUZZER_PIN, HIGH);
      delay(200);
      digitalWrite(BUZZER_PIN, LOW);
    }
    if (millis() - lastSMS >= 30000) {
      lastSMS = millis();
      sendSMS(CONTACT_NUMBER, "[GARBY] Still blocked: " + reason);
    }

    // ── Pass through Air780 serial ──────────────────────────────
    while (Air780.available()) Serial.write(Air780.read());
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

// ── movementGate ──────────────────────────────────────────────
// Called before every discrete move (turn, short distance).
// Checks shouldStop FIRST so front-blocked always halts.
void movementGate(bool isNudgeEnabled) {
  pollESP();

  if (shouldStop) {
    Serial.println("[GATE] PATH blocked — halting");
    haltAndWait("PATH:BLOCKED from LiDAR");
  }

  // Only apply discrete nudge if enabled AND not in continuous mode
  if (isNudgeEnabled && !path.isContinuousMode()) {
    pollAndApplySideNudge();
  }

  float localDist = getDistance();
  if (localDist < OBSTACLE_DISTANCE) {
    Serial.printf("[GATE] Sonic: %.1f cm — halting\n", localDist);
    haltAndWait("SONIC:BLOCKED by local ultrasonic");
  }
}

// ── safeMoveDistance ──────────────────────────────────────────
void safeMoveDistance(int32_t steps, bool isADJ, bool isNudgeEnabled) {
  movementGate(isNudgeEnabled);
  moveDistance(steps, isADJ);
}

void safeTurnLeft(int32_t step) {
  movementGate(false);
  turnLeft(step);
}

void safeTurnRight(int32_t step) {
  movementGate(false);
  turnRight(step);
}

// ── fullReset ─────────────────────────────────────────────────
void fullReset() {
  shouldStop      = false;
  sensorTripped   = false;
  movingForward   = false;
  blockedSMSSent  = false;
  loadcellSMSSent = false;
  resetQueued     = false;
  activeNudge     = NudgeDir::NONE;
  nudgeLeftCount  = 0;
  nudgeRightCount = 0;
  nudgeWasStable  = true;
  garbyState      = GarbyState::IDLE;
  Serial.println("[RESET] All flags cleared — back to IDLE");
  sendSMS(CONTACT_NUMBER, "[GARBY] Returned to base. Ready for next cycle.");
}

// ── printIdleUptime ───────────────────────────────────────────
void printIdleUptime() {
  unsigned long now = millis();
  if (now - lastIdlePrintMs < IDLE_PRINT_INTERVAL_MS) return;
  lastIdlePrintMs = now;
  unsigned long uptimeSec = now / 1000UL;
  unsigned long h = uptimeSec / 3600;
  unsigned long m = (uptimeSec % 3600) / 60;
  unsigned long s = uptimeSec % 60;
  Serial.printf("[IDLE] Up %02lu:%02lu:%02lu — waiting for trigger...\n", h, m, s);
}

// ============================================================
// MOTOR MOVEMENT FUNCTIONS
// ============================================================
void turnRight(int32_t step) {
  stepper1->move(-step);
  stepper2->move(step);
  while (stepper1->isRunning() || stepper2->isRunning()) delay(1);
}
void turnLeft(int32_t step) {
  stepper1->move(step);
  stepper2->move(-step);
  while (stepper1->isRunning() || stepper2->isRunning()) delay(1);
}
void emergencyStopMotors() {
  stepper1->forceStopAndNewPosition(stepper1->getCurrentPosition());
  stepper2->forceStopAndNewPosition(stepper2->getCurrentPosition());
  activeNudge     = NudgeDir::NONE;
  nudgeLeftCount  = 0;
  nudgeRightCount = 0;
}
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

// ── nudgeLeftContinuous (fallback) ───────────────────────────
void nudgeLeftContinuous(float delayMs) {
  if (activeNudge == NudgeDir::LEFT) return;
  stepper2->setSpeedInHz((uint32_t)(MAX_SPEED * 0.85));
  stepper2->applySpeedAcceleration();
  Serial.println("<-- NUDGE LEFT");
  activeNudge     = NudgeDir::LEFT;
  nudgeStartMs    = millis();
  nudgeDurationMs = (unsigned long)delayMs;
}

// ── nudgeRightContinuous (fallback) ──────────────────────────
void nudgeRightContinuous(float delayMs) {
  if (activeNudge == NudgeDir::RIGHT) return;
  stepper1->setSpeedInHz((uint32_t)(MAX_SPEED * 0.85));
  stepper1->applySpeedAcceleration();
  Serial.println("--> NUDGE RIGHT");
  activeNudge     = NudgeDir::RIGHT;
  nudgeStartMs    = millis();
  nudgeDurationMs = (unsigned long)delayMs;
}

// ── updateNudge (fallback) ───────────────────────────────────
void updateNudge() {
  if (activeNudge == NudgeDir::NONE) return;
  if (millis() - nudgeStartMs >= nudgeDurationMs) {
    if (activeNudge == NudgeDir::LEFT) {
      stepper2->setSpeedInHz(MAX_SPEED);
      stepper2->applySpeedAcceleration();
    } else if (activeNudge == NudgeDir::RIGHT) {
      stepper1->setSpeedInHz(MAX_SPEED);
      stepper1->applySpeedAcceleration();
    }
    Serial.println("^ STRAIGHT ^");
    activeNudge    = NudgeDir::NONE;
    nudgeWasStable = true;
  }
}

// ── moveToTarget ──────────────────────────────────────────────
//
// FIX (was causing "twitch then stop, motor never actually moves"):
// The previous version unconditionally called
// forceStopAndNewPosition() on BOTH steppers on EVERY loop
// iteration (every ~31ms: 30ms servo settle + sensor read), then
// tried to restart them with startStraight(). Steppers were being
// killed before they could ramp up to speed, so the robot just sat
// there twitching instead of driving forward.
//
// Fix: keep the motors running continuously in velocity mode.
// Only the ultrasonic distance read needs a brief quiet period to
// avoid pulseIn() being corrupted by step-pulse interrupt jitter —
// and even that isn't done every iteration anymore. The servo sweep
// and ultrasonic poll now run on their own slower cadence
// (SENSOR_POLL_INTERVAL_MS) instead of every pass through the loop,
// and the stepper is only ever stopped when there's an ACTUAL reason
// to stop (target reached, obstacle detected, LiDAR block).
void moveToTarget(long target1, long target2, bool isADJ) {
  if (isADJ) {
    stepper1->setSpeedInHz(MAX_SPEED);
    stepper1->setAcceleration(ACCELERATION);
    stepper2->setSpeedInHz(MAX_SPEED);
    stepper2->setAcceleration(ACCELERATION);
    stepper1->moveTo(target1);
    stepper2->moveTo(target2);
    while (stepper1->isRunning() || stepper2->isRunning()) delay(1);
    return;
  }

  startStraight();
  Serial.println("[MOVE] startStraight() called — motors should be running");

  int servoAngle = DEFAULT_VIEW;
  int servoDir   = 1;
  const int stepSize = 10;

  unsigned long lastRequestTime = 0;
  const unsigned long REQUEST_INTERVAL_MS = 400;   // ← slower to avoid BLE congestion

  unsigned long lastSensorPoll = 0;
  const unsigned long SENSOR_POLL_INTERVAL_MS = 150;  // servo+ultrasonic cadence (was every loop / ~31ms)

  while (stepper1->getCurrentPosition() < target1 ||
         stepper2->getCurrentPosition() < target2) {

    // ── Request fresh LiDAR/sensor data periodically ──
    if (millis() - lastRequestTime >= REQUEST_INTERVAL_MS) {
        requestStatus();
        lastRequestTime = millis();
    }

    // ── Read incoming messages (updates path, shouldStop) ──
    pollESP();

    // ── Emergency stop if front/back blocked ──────────────
    if (shouldStop) {
      emergencyStopMotors();
      haltAndWait("PATH:BLOCKED from LiDAR (during move)");
      long rem1 = target1 - stepper1->getCurrentPosition();
      long rem2 = target2 - stepper2->getCurrentPosition();
      if (rem1 > 0 || rem2 > 0) startStraight();
      lastRequestTime = millis();
    }

    // ── Continuous lane‑centering (if we have distance data) ──
    if (path.isContinuousMode()) {
        float error = path.getLeftDist() - path.getRightDist();
        float Kp = 0.025;
        float correction = constrain(error * Kp, -0.2, 0.2);

        uint32_t speedL = (uint32_t)(MAX_SPEED * (1.0 + correction));
        uint32_t speedR = (uint32_t)(MAX_SPEED * (1.0 - correction));

        stepper1->setSpeedInHz(speedL);
        stepper2->setSpeedInHz(speedR);
        stepper1->applySpeedAcceleration();
        stepper2->applySpeedAcceleration();
    } else {
        pollAndApplySideNudge();
        updateNudge();
    }

    // ── Servo sweep + ultrasonic obstacle detection ──────────────
    // Only run this on its own slower cadence — NOT every loop pass.
    // The motors keep running (velocity mode) the rest of the time.
    if (millis() - lastSensorPoll >= SENSOR_POLL_INTERVAL_MS) {
      lastSensorPoll = millis();

      servoAngle += servoDir * stepSize;
      if (servoAngle >= SCAN_LEFT)  { servoAngle = SCAN_LEFT;  servoDir = -1; }
      else if (servoAngle <= SCAN_RIGHT) { servoAngle = SCAN_RIGHT; servoDir =  1; }
      servo.write(servoAngle);

      float d = readDistanceRaw();

      if (d <= OBSTACLE_DISTANCE) {
        emergencyStopMotors();
        digitalWrite(BUZZER_PIN, HIGH);
        delay(750);
        digitalWrite(BUZZER_PIN, LOW);
        stepper1->move(-200);
        stepper2->move(-200);
        while (stepper1->isRunning() || stepper2->isRunning()) delay(1);
        bool allClear = false;
        while (!allClear) {
          delay(500);
          float leftDist  = scanAngle(SCAN_LEFT);
          delay(200);
          float frontDist = scanAngle(DEFAULT_VIEW);
          delay(200);
          float rightDist = scanAngle(SCAN_RIGHT);
          delay(200);
          if (leftDist > OBSTACLE_DISTANCE &&
              frontDist > OBSTACLE_DISTANCE &&
              rightDist > OBSTACLE_DISTANCE) {
            allClear = true;
          } else {
            digitalWrite(BUZZER_PIN, HIGH);
            delay(500);
            digitalWrite(BUZZER_PIN, LOW);
            delay(2000);
          }
        }
        startStraight();
        lastRequestTime = millis();
      }
    }

    delay(1);
  }

  emergencyStopMotors();
  servo.write(DEFAULT_VIEW);
}

void moveDistance(int32_t steps, bool isADJ) {
  long t1 = stepper1->getCurrentPosition() + steps;
  long t2 = stepper2->getCurrentPosition() + steps;
  moveToTarget(t1, t2, isADJ);
}

// ============================================================
// SERVO + ULTRASONIC (unchanged)
// ============================================================
float readDistanceRaw() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 60000);
  if (duration == 0) return 999.0f;
  return duration * 0.0343f / 2.0f;
}
float getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
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

// ============================================================
// SMS / AIR780E (unchanged)
// ============================================================
String sendAT(const String& cmd, unsigned long timeout) {
  while (Air780.available()) Air780.read();
  Air780.println(cmd);
  Serial.print(">> " + cmd + " ");
  String response = "";
  unsigned long start = millis();
  while (millis() - start < timeout) {
    while (Air780.available()) {
      char c = Air780.read();
      response += c;
    }
    if (response.indexOf("OK") != -1 || response.indexOf("ERROR") != -1) break;
  }
  Serial.println("<< " + response);
  return response;
}
void powerOnAir780() {
  Serial.println("[Air780E] Checking if alive...");
  pinMode(PWRKEY_PIN, OUTPUT);
  digitalWrite(PWRKEY_PIN, HIGH);
  for (int i = 0; i < 5; i++) {
    String r = sendAT("AT", 1000);
    if (r.indexOf("OK") != -1) {
      Serial.println("[Air780E] Already on.");
      return;
    }
    delay(500);
  }
  Serial.println("[Air780E] Sending power-on pulse...");
  digitalWrite(PWRKEY_PIN, LOW);
  delay(1500);
  digitalWrite(PWRKEY_PIN, HIGH);
  Serial.println("[Air780E] Waiting for boot...");
  delay(8000);
  Serial.println("[Air780E] Power-on done.");
}
bool waitForModule(int maxAttempts) {
  for (int i = 0; i < maxAttempts; i++) {
    String r = sendAT("AT", 2000);
    if (r.indexOf("OK") != -1) {
      Serial.println("[Air780E] Module responsive!");
      return true;
    }
    Serial.printf("[Air780E] Attempt %d/%d\n", i + 1, maxAttempts);
    delay(1000);
  }
  Serial.println("[Air780E] ERROR: No response.");
  return false;
}
bool waitForNetwork(int maxAttempts) {
  for (int i = 0; i < maxAttempts; i++) {
    String resp = sendAT("AT+CREG?", 3000);
    if (resp.indexOf(",1") != -1 || resp.indexOf(",5") != -1) {
      Serial.println("[Air780E] Registered!");
      return true;
    }
    Serial.printf("[Air780E] Network attempt %d/%d\n", i + 1, maxAttempts);
    delay(2000);
  }
  Serial.println("[Air780E] WARNING: Not registered.");
  return false;
}
void sendSMS(const String& phoneNumber, const String& message) {
  Serial.println("[SMS] Sending to " + phoneNumber);
  String resp = sendAT("AT+CMGF=1", 3000);
  if (resp.indexOf("OK") == -1) { Serial.println("[SMS] Text mode failed."); return; }
  while (Air780.available()) Air780.read();
  Air780.println("AT+CMGS=\"" + phoneNumber + "\"");
  String prompt = "";
  unsigned long start = millis();
  while (millis() - start < 5000) {
    while (Air780.available()) { char c = Air780.read(); prompt += c; }
    if (prompt.indexOf(">") != -1) break;
  }
  if (prompt.indexOf(">") == -1) {
    Serial.println("[SMS] No '>' prompt. Aborting.");
    Air780.write(0x1B);
    return;
  }
  Air780.print(message);
  Air780.write(0x1A);
  String result = "";
  start = millis();
  while (millis() - start < 15000) {
    while (Air780.available()) { char c = Air780.read(); result += c; }
    if (result.indexOf("+CMGS") != -1 || result.indexOf("ERROR") != -1) break;
  }
  if (result.indexOf("+CMGS") != -1) Serial.println("[SMS] Sent!");
  else                                Serial.println("[SMS] Failed: " + result);
}

// ============================================================
// SENSOR DECISION HANDLERS (unchanged)
// ============================================================
bool handlePath() {
  if (shouldStop) { Serial.println("[STOP] Path blocked"); return true; }
  return false;
}
bool handleSensor() {
  if (sensorTripped) { Serial.println("[ALERT] Gas danger!"); return true; }
  switch (data.getUSStatus()) {
    case ReceivedDatas::UltrasonicStatus::FULL:
      Serial.println("[BIN] Full — return to base");
      return true;
    case ReceivedDatas::UltrasonicStatus::HALFWAY:
      Serial.println("[BIN] Halfway");
      break;
    default: break;
  }
  return false;
}

// ============================================================
// UTILITY (unchanged)
// ============================================================
void flushESPSerial() {
  while (ESP_Serial.available()) ESP_Serial.read();
}
void buzzerTask(void *pvParameters) {
  digitalWrite(BUZZER_PIN, HIGH);
  vTaskDelay(pdMS_TO_TICKS(500));
  digitalWrite(BUZZER_PIN, LOW);
  vTaskDelay(pdMS_TO_TICKS(100));
  vTaskDelete(NULL);
}
bool checkLoad(float threshold) {
  float w = scale.get_units(10) - 0.010f;
  return (w >= threshold);
}