---
name: garby-constants-audit
description: "Audit GARBY thesis documentation (SYSTEM_ARCHITECTURE.md) against actual source code (BLE_Receiver-Final.ino, NAPHTALI_CODE_V2, RasPi script) to identify mismatched tuning constants, thresholds, UUIDs, or watchdog timeouts."
argument-hint: "Optional target variant: 'dual', 'single', or leave empty to audit both"
user-invocable: true
---

# GARBY Code vs. Docs Constants Audit Workflow

Use this skill when preparing thesis documentation or after tweaking firmware constants to ensure `SYSTEM_ARCHITECTURE.md` perfectly matches the source code across both `DUAL-TASK-BOTH-CORES` and `SINGLE-TASK-DEFAULT` variants.

## Step-by-step Audit Procedure

### 1. Identify Target Scope
Check the user input or argument:
- If `"dual"` is passed, scope to `DUAL-TASK-BOTH-CORES/`
- If `"single"` is passed, scope to `SINGLE-TASK-DEFAULT/`
- If omitted or empty, audit **both variants** sequentially and compare their consistency.

---

### 2. Check BLE Bridge Tuning (`BLE_Receiver-Final/BLE_Receiver-Final.ino`)
Read the BLE bridge source code and extract the live values for:

| Constant | Symbol / Logic | Expected Baseline |
|----------|---------------|-------------------|
| Watchdog Timeout | `DATA_TIMEOUT_MS` | `45000` (45 s) |
| EMA Alpha | `alpha` in EMA calculation | `0.65` |
| Lateral Weight | Weight multiplier on `LEFT - RIGHT` | `0.55` |
| Heading Weight | Weight multiplier on `FRONT_LEFT - FRONT_RIGHT` | `0.65` |
| Dead-zone Threshold | Centered error threshold | `8.5` cm |
| Hysteresis Band | Direction reversal offset | `5.0` cm (reversal threshold = `13.5` cm) |
| Direction Confirmation | Required consecutive packet count | `4` packets |
| Nudge Cooldown | Cooldown delay | `650` ms |
| Nudge Duration Range | Minimum / Maximum tap duration | `35` ms to `100` ms |
| Nudge Intensity Range | Minimum / Maximum speed cut | `12%` to `35%` |
| Wall Protrusion Threshold | Baseline width drop threshold | `12.0` cm (attenuates error by `0.25×`) |
| Front Suppression Zones | Full suppression / Linear scaling thresholds | Full ≤ `60` cm, Linear `60`–`120` cm |
| Startup Grace & Ramp | Grace packet count / Ramp packet count | `15` packets grace, `10` packets ramp |
| NimBLE Service UUID | Service UUID string | `4fafc201-1fb5-459e-8fcc-c5c9c331914b` |
| Write Characteristic UUID | Write UUID string | `beb5483e-36e1-4688-b7f5-ea07361b26a8` |
| Notify Characteristic UUID | Notify UUID string | `beb5483e-36e1-4688-b7f5-ea07361b26a9` |

---

### 3. Check Main MCU Controller Tuning (`NAPHTALI_CODE_V2/`)
Read `NAPHTALI_CODE_V2.h`, `NAPHTALI_CODE_V2.cpp`, and `NAPHTALI_CODE_V2.ino` for live values:

| Constant | Symbol | Expected Baseline |
|----------|--------|-------------------|
| Motor Max Speed | `MAX_SPEED` | `6500` Hz |
| Motor Acceleration | `ACCELERATION` | (Check header definition) |
| Left Boost Multiplier | Left nudge multiplier | `1.25×` |
| Right Cut Cap | Maximum right speed cut | `50%` |
| Left Cut Cap | Maximum left speed cut | `60%` |
| Max Hold Cap | `NUDGE_MAX_HOLD_MS` | `150` ms |
| Post-Nudge Settle | Settle guard duration | `300` ms |
| Local Ultrasonic Threshold | Safety stop distance | `45` cm |
| Fast Safety Cadence | `FAST_STOP_CHECK_MS` | `40` ms |
| Servo Sweep Angles | Sweep steps | `0°` to `145°` in `10°` steps |
| Scan Angle Settle | `scanAngle()` calculation | `2 ms/degree`, max `300 ms` |
| 3-Point Scan Angles | Obstacle scan angles | `145°`, `80°`, `0°` |
| Status Poll Cadence | `requestStatus()` in scan loop | `150` ms |
| Load Cell Threshold | Payload weight threshold | `1.0` kg |

---

### 4. Check Raspberry Pi ROS 2 LiDAR Node (`RasPi/final_w_serial.py`)
Read the Pi script for live values:

| Constant | Logic / Variable | Expected Baseline |
|----------|-----------------|-------------------|
| LiDAR Cones | Number & angle width | `8` cones, `22°` width each |
| Median History | Filter depth | `6` frames |
| Outlier Streak | Confirmation depth | `2` consecutive frames |
| Front Path Block | Threshold | `50.0` cm |
| Back Path Block | Threshold | `25.0` cm |
| Serial Baud | `/dev/ttyAMA0` rate | `9600` baud |
| LiDAR Watchdog | ROS 2 scan timeout | `5` seconds |

---

### 5. Compare with `SYSTEM_ARCHITECTURE.md`
Read the corresponding `SYSTEM_ARCHITECTURE.md` file(s). Cross-reference every value extracted in steps 2–4 with the prose and tables in `SYSTEM_ARCHITECTURE.md`.

---

### 6. Generate Discrepancy Report
Produce a markdown table summarizing the audit findings:

```markdown
## GARBY Constants Audit Report

| Component | Parameter | Code Value | Docs Value | Status |
|-----------|-----------|------------|------------|--------|
| Bridge    | `DATA_TIMEOUT_MS` | 45000 (45s) | 45s | ✅ Match |
| MCU       | `MAX_SPEED` | 6500 Hz | 10000 Hz | ❌ MISMATCH |

### Action Items
- [ ] Update `SYSTEM_ARCHITECTURE.md` line XX: change `MAX_SPEED` from 10000 Hz to 6500 Hz
```

If mismatches are found, offer to automatically fix `SYSTEM_ARCHITECTURE.md` using `replace_string_in_file`.
