// .ino (Extension)
// ============================
// RUN DRAFT END-TO-END
// ============================

// safeMoveDistance(steps, isADJ, isNudgeEnabled)
//   isNudgeEnabled=false on the short 5-step calibration bumps
//   so they don't trigger nudge logic on tiny distances.

void runStart() {
  Serial.println("[RUNSTART] Entered runStart()");   // confirms this function is actually reached

  // Request initial LiDAR/sensor status
  requestStatus();
  delay(100);

  // Short calibration bumps — nudge disabled
  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(8000, false, false);
  delay(100);
  safeTurnLeft(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(595500, false, true);
  delay(100);
  safeTurnLeft(5000);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(6000, false, false);
  delay(100);
  safeTurnRight(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(25500, false, false);
  delay(100);
  safeTurnRight(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(10600, false, false);
  delay(100);
  safeTurnLeft(9800);

  Serial.println("[RUNSTART] runStart() finished all moves");
}

void returnToPointB() {
  Serial.println("[RETURNTOB] Entered returnToPointB()");

  // Request initial status for the return trip
  requestStatus();
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, true, false); delay(1); }
  delay(100);

  safeMoveDistance(9500, false, false);
  delay(100);
  safeTurnLeft(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, false, false); delay(1); }
  delay(100);

  safeMoveDistance(25500, false, false);
  delay(100);
  safeTurnLeft(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, false, false); delay(1); }
  delay(100);

  safeMoveDistance(6000, false, false);
  delay(100);
  safeTurnRight(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, false, false); delay(1); }
  safeMoveDistance(595700, false, true);
  delay(100);
  safeTurnRight(4900);
  delay(100);

  for (int i = 0; i < 5; i++) { safeMoveDistance(100, false, false); delay(1); }
  delay(100);

  safeMoveDistance(7000, false, false);
  delay(100);
  safeTurnLeft(9800);
  delay(100);

  Serial.println("[RETURNTOB] returnToPointB() finished all moves");
}
