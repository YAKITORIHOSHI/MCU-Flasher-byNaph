#pragma once
// ============================================================
// LIBRARIES
// ============================================================
#include <ESP32Servo.h>
#include <FastAccelStepper.h>
#include <HardwareSerial.h>
#include <HX711.h>
#include <Arduino.h>

// ============================================================
// LOAD CELL
// ============================================================
#define LOADCELL_DOUT_PIN  33
#define LOADCELL_SCK_PIN   32
const float  MAX_LOAD_APPRX      = 1.0;        // kg
const float  CALIBRATION_FACTOR  = 100000.0f;

// ============================================================
// ESP32 WROOM (LiDAR BLE bridge)
// ============================================================
#define ESP_RX  18
#define ESP_TX  19

// ============================================================
// SIM MODULE — Air780E
// ============================================================
#define AIR_RX      16
#define AIR_TX      17
#define PWRKEY_PIN  26

// ============================================================
// SERVO
// ============================================================
#define SERVO_PIN    23
#define SCAN_LEFT    145
#define SCAN_RIGHT   0
#define DEFAULT_VIEW 80

// ============================================================
// BUZZER
// ============================================================
#define BUZZER_PIN   25

// ============================================================
// ULTRASONIC SENSOR
// ============================================================
#define TRIG_PIN  5
#define ECHO_PIN  4
const float  ULTRASONIC_TRASH_LEVEL = 65.0f;

// ============================================================
// STEPPER MOTORS (FastAccelStepper)
// ============================================================
#define STEP_PIN1  21
#define DIR_PIN1   22
#define STEP_PIN2  13
#define DIR_PIN2   12

const uint32_t MAX_SPEED    = 10000;
const uint32_t ACCELERATION = 10000;
const uint32_t NUDGE_SPEED  = 7500;
const int32_t  FAR          = 10000000;
const int      STEP_VAL     = 3000;
const uint32_t NUDGE_ACCEL  = (uint32_t)(ACCELERATION * NUDGE_SPEED / MAX_SPEED);

extern uint32_t lastPrintMs;
extern uint32_t startMs;
extern bool     nudgeDone1,   nudgeDone2;
extern bool     forwardDone1, forwardDone2;

// ============================================================
// WHEEL GEOMETRY
// ============================================================
#define STEPS_PER_REV      1600
#define WHEEL_DIAMETER_MM  65.0f
#define WHEELBASE_MM       150.0f

// ============================================================
// OBSTACLE CONFIGURATION
// ============================================================
#define OBSTACLE_DISTANCE  45.0f   // cm  (front servo ultrasonic)
#define MAX_BLOCKED_COUNT  10

// ============================================================
// CONTACT NUMBER
// ============================================================
#define CONTACT_NUMBER  "+639242473078"

// ============================================================
// NUDGE CONFIGURATION (kept for fallback, but we now use continuous)
// ============================================================
#define SIDES_NUDGE_THRESHOLD_CM  30.0f
#define NUDGE_DEAD_ZONE_CM  5.0f
#define NUDGE_HYSTERESIS_CM 8.0f
#define NUDGE_DEBOUNCE_COUNT 3
#define NUDGE_DURATION_MS  300UL

// ============================================================
// ACK PROTOCOL
// ============================================================
#define ACK_MSG  "[ESP RECEIVED]"

// ============================================================
// PARSE DATAS
// ============================================================
class ParseData {
  public:
    enum class PathState { BLOCKED, CLEAR };
    enum class SideState { NUDGE_LEFT, NUDGE_RIGHT, STABLE };

  private:
    struct FrontPath {
      PathState FRONT       = PathState::CLEAR;
      PathState FRONT_LEFT  = PathState::CLEAR;
      PathState FRONT_RIGHT = PathState::CLEAR;
    };
    struct BackPath {
      PathState BACK       = PathState::CLEAR;
      PathState BACK_LEFT  = PathState::CLEAR;
      PathState BACK_RIGHT = PathState::CLEAR;
    };
    struct SidePath {
      SideState LEFT  = SideState::STABLE;
      SideState RIGHT = SideState::STABLE;
    };
    FrontPath front;
    BackPath  back;
    SidePath  side;
    // ---- NEW for continuous control ----
    bool   continuousMode = false;   // true if we have LEFT=xx|RIGHT=yy
    float  leftDist  = 0.0f;
    float  rightDist = 0.0f;

  public:
    void setFrontPath(PathState state);
    void setBackPath (PathState state);
    void setSidePath (SideState state);
    void setFrontField(const String& field, PathState state);
    void setBackField (const String& field, PathState state);

    // ---- NEW getters for continuous mode ----
    bool  isContinuousMode() const { return continuousMode; }
    float getLeftDist()     const { return leftDist; }
    float getRightDist()    const { return rightDist; }

    PathState getFront()      { return front.FRONT;       }
    PathState getFrontLeft()  { return front.FRONT_LEFT;  }
    PathState getFrontRight() { return front.FRONT_RIGHT; }
    PathState getBack()       { return back.BACK;         }
    PathState getBackLeft()   { return back.BACK_LEFT;    }
    PathState getBackRight()  { return back.BACK_RIGHT;   }
    SideState getSideLeft()   { return side.LEFT;         }
    SideState getSideRight()  { return side.RIGHT;        }

    bool parse(const String& raw);
    void printAllState();
};

class ReceivedDatas {
  public:
    enum class UltrasonicStatus { EMPTY, HALFWAY, FULL };
    enum class MQ4Status        { NORMAL, WARNING, DANGER };
    enum class MQ135Status      { CLEAN, MODERATE, POOR, VERY_POOR };
    enum class MQ137Status      { NORMAL, WARNING, DANGER };

  private:
    struct Ultrasonic_Data { int value = 0; UltrasonicStatus status = UltrasonicStatus::EMPTY; };
    struct MQ4_Data        { int value = 0; MQ4Status        status = MQ4Status::NORMAL;       };
    struct MQ135_Data      { int value = 0; MQ135Status      status = MQ135Status::CLEAN;      };
    struct MQ137_Data      { int value = 0; MQ137Status      status = MQ137Status::NORMAL;     };

    Ultrasonic_Data us;
    MQ4_Data        mq4;
    MQ135_Data      mq135;
    MQ137_Data      mq137;

  public:
    void setUS   (int val);
    void setMQ4  (int val);
    void setMQ135(int val);
    void setMQ137(int val);

    bool parse(const String& raw);

    int              getUSValue();    UltrasonicStatus getUSStatus();
    int              getMQ4Value();   MQ4Status        getMQ4Status();
    int              getMQ135Value(); MQ135Status      getMQ135Status();
    int              getMQ137Value(); MQ137Status      getMQ137Status();

    void printAll();
};

bool pathReadDecisionMaker  (ParseData&     p);
bool sensorReadDecisionMaker(ReceivedDatas& d);

extern ParseData     path;
extern ReceivedDatas data;

// ============================================================
// LIDAR ZONE STRUCT
// ============================================================
struct LidarZones {
  float front      = 999.0f;
  float frontLeft  = 999.0f;
  float frontRight = 999.0f;
  float left       = 999.0f;
  float right      = 999.0f;
  float back       = 999.0f;
  float backLeft   = 999.0f;
  float backRight  = 999.0f;
};

// ============================================================
// ROBOT STATE MACHINE
// ============================================================
enum class GarbyState { IDLE, RUNNING, RETURNING };
extern GarbyState garbyState;
extern bool       resetQueued;

// ============================================================
// NON-BLOCKING NUDGE STATE (fallback, mostly unused now)
// ============================================================
enum class NudgeDir { NONE, LEFT, RIGHT };
extern NudgeDir      activeNudge;
extern unsigned long nudgeStartMs;
extern unsigned long nudgeDurationMs;
extern int nudgeLeftCount;
extern int nudgeRightCount;
extern bool nudgeWasStable;

// ============================================================
// GLOBAL OBJECT DECLARATIONS
// ============================================================
extern HX711             scale;
extern HardwareSerial    ESP_Serial;
extern HardwareSerial    Air780;
extern Servo             servo;
extern FastAccelStepperEngine engine;
extern FastAccelStepper* stepper1;
extern FastAccelStepper* stepper2;

// ============================================================
// GLOBAL STATE DECLARATIONS
// ============================================================
extern LidarZones    zones;
extern bool          lidarBlockedActive;
extern bool          lidarControlled;
extern unsigned long lidarBlockedStart;
extern unsigned long lidarLastPeriodicSMS;
extern unsigned long lidarLastLRScan;
extern int           blockedCount;
extern bool          idleMode;
extern bool          lastConnected;
extern bool          movingForward;
extern bool          blockedSMSSent;
extern bool          loadcellSMSSent;
extern bool          buzzerState;
extern unsigned long lastBeepTime;
extern float frontDistance;
extern float leftDistance;
extern float rightDistance;
extern bool isTrashbinFull;
extern bool shouldStop;
extern bool sensorTripped;

extern unsigned long lastIdlePrintMs;

// ============================================================
// FUNCTION PROTOTYPES
// ============================================================
// Motor control
void turnRight(int32_t step = STEP_VAL);
void turnLeft (int32_t step = STEP_VAL);
void emergencyStopMotors();
void startStraight();
void restoreStraight();

// ---- NEW: request status from RasPi ----
void requestStatus();

void nudgeLeftContinuous (float delayMs);
void nudgeRightContinuous(float delayMs);
void updateNudge();
void pollAndApplySideNudge();

void moveToTarget(long target1, long target2, bool isADJ = false);
void moveDistance(int32_t steps = FAR, bool isADJ = false);

// Sensor
float readDistanceRaw();
float getDistance();
float scanAngle(int angle);

// SMS / Air780E
String sendAT(const String& cmd, unsigned long timeout = 3000);
void   powerOnAir780();
bool   waitForModule (int maxAttempts = 20);
bool   waitForNetwork(int maxAttempts = 20);
void   sendSMS(const String& phoneNumber, const String& message);

// Path / sensor handlers
bool handlePath();
bool handleSensor();

// State machine helpers
void pollESP();
void haltAndWait(const String& reason);
void movementGate();

void safeMoveDistance(int32_t steps, bool isADJ = false, bool isNudgeEnabled = true);
void safeTurnLeft (int32_t step = STEP_VAL);
void safeTurnRight(int32_t step = STEP_VAL);
void fullReset();
void printIdleUptime();

// Utility
void flushESPSerial();
void buzzerTask(void *pvParameters);
bool checkLoad(float threshold = MAX_LOAD_APPRX);