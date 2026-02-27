# Fuji X-T4 SDK Burst Speed Investigation - Final Report

## Executive Summary

**Conclusion: The Fujifilm SDK cannot achieve burst speeds above ~1.3 fps. Hardware trigger is required for 15fps.**

## What Was Tested

### Private SDK Functions (via FF0000API.bundle)
| Function | Result |
|----------|--------|
| SDK_SetPerformanceSettings(BOOST_FRAMERATE) | ✓ Works, no speed improvement |
| SDK_SetShutterPriorityMode(RELEASE) | ✓ Works, no speed improvement |
| SDK_SetDriveMode(CH/CL) | ✗ Returns -1, not allowed |
| SDK_SetBKT | ✗ Returns -1, not available |
| SDK_SetBKTFrame | ✗ Returns -1, not available |
| SDK_SetBurstNumber | ✗ Returns -1, not available |
| SDK_SetBurstInterval | ✗ Returns -1, not available |
| SDK_Shoot | ✗ Blocks indefinitely |
| SDK_ShootS1/S2 | ✗ Blocks indefinitely |

### PTP Layer Functions (via FTLPTP.dylib)
| Function | Result |
|----------|--------|
| FTL_PTP_InitiateCapture | ✗ Returns -1, wrong handle type |
| FTL_PTP_InitiateOpenCapture | ✗ Returns -1, wrong handle type |
| FTL_PTP_TerminateOpenCapture | Not applicable |

### XSDK Functions
| Function | Result |
|----------|--------|
| XSDK_Release(S1ON + S2_S1OFF) | ✓ Works, ~280ms per shot = 3.5fps theoretical, ~1.3fps actual |
| XSDK_Release(S2 only, hold S1) | ✗ Returns -1 |
| XSDK_CapDriveMode | Returns 0 modes available |
| XSDK_SetDriveMode(CH/CL) | ✗ Returns -1 |

## Root Cause

The X-T4 SDK is designed for **tethered shooting** (remote control photography from a computer), not high-speed capture. The camera enforces these limitations:

1. **Drive Mode Lock**: When connected via SDK, the camera disallows drive mode changes from PC control
2. **Single-Shot Protocol**: Each XSDK_Release call initiates a full capture cycle (~280ms minimum)
3. **No Burst Queuing**: Cannot queue multiple shots - each must complete before the next
4. **Hardware Timing**: The 280ms includes sensor readout + USB transfer notification

## Physical Burst Mode

The X-T4's mechanical shutter can achieve:
- **CH (Continuous High)**: 15 fps
- **CL (Continuous Low)**: 3-8 fps configurable
- **Electronic shutter**: 30 fps

These require the physical drive mode dial to be set, which the SDK cannot control.

## Solution: Hardware Trigger

The X-T4 has a 2.5mm remote release port (in the rubber cover on the right side):

```
Pin Configuration:
  Tip    = S1 (Focus/Half-press)
  Ring   = S2 (Shutter/Full-press)
  Sleeve = GND (Ground)
```

### Simple Hardware Trigger Circuit:

```
                    ┌─────────────┐
GPIO (S1) ──────────┤ 220Ω       ├──────── 2.5mm Tip
                    └─────────────┘
                    ┌─────────────┐
GPIO (S2) ──────────┤ 220Ω       ├──────── 2.5mm Ring
                    └─────────────┘
GND ────────────────────────────────────── 2.5mm Sleeve
```

### Advantages:
1. Full 15fps burst using camera's native CH mode
2. No USB latency or SDK overhead
3. Can be controlled via Raspberry Pi GPIO or USB-serial adapter
4. Timing accuracy down to microseconds

### Implementation Options:

1. **Raspberry Pi**: Direct GPIO control, ~1µs precision
2. **Arduino**: USB-serial controlled, <1ms precision
3. **USB Relay Board**: Simple on/off, ~10ms precision

## Recommended Implementation

For the Solar Eclipse Workbench:

1. Set camera to **CH mode** physically (drive mode dial)
2. Set desired shutter speed and exposure manually
3. Use hardware trigger to:
   - Hold S1 continuously (keeps camera in focus-ready state)
   - Pulse S2 for each burst sequence
4. The camera will shoot at its native 15fps while S2 is held

Python example for Raspberry Pi:
```python
import RPi.GPIO as GPIO
import time

S1_PIN = 17  # Focus
S2_PIN = 27  # Shutter

GPIO.setmode(GPIO.BCM)
GPIO.setup(S1_PIN, GPIO.OUT, initial=GPIO.HIGH)  # Active low
GPIO.setup(S2_PIN, GPIO.OUT, initial=GPIO.HIGH)

def burst_capture(duration_seconds):
    """Fire burst for specified duration at camera's native fps."""
    GPIO.output(S1_PIN, GPIO.LOW)  # Half-press (focus)
    time.sleep(0.05)               # Focus lock time
    GPIO.output(S2_PIN, GPIO.LOW)  # Full-press (shutter)
    time.sleep(duration_seconds)   # Burst duration
    GPIO.output(S2_PIN, GPIO.HIGH) # Release shutter
    GPIO.output(S1_PIN, GPIO.HIGH) # Release focus
```

## Files Created During Investigation

- `test_sdk_shoot.py` - SDK_Shoot function tests
- `test_s2_burst.py` - Rapid S2 while holding S1
- `test_drive_modes.py` - Drive mode capability check
- `test_async_shots.py` - Threading and async approaches
- `test_overlap_s1.py` - S1 overlap optimization
- `test_bracketing.py` - BKT mode exploration
- `test_bkt_enable.py` - BKT enable attempts
- `test_ptp_burst.py` - PTP layer access
- `test_session_handle.py` - Internal handle extraction

All return the same conclusion: SDK is limited to ~1.3 fps maximum.
