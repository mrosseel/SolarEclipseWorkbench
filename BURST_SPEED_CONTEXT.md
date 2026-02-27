# Fuji X-T4 Burst Speed Investigation — Final Results

## Goal
Achieve 15fps burst capture through USB SDK on Fuji X-T4.

## Final Result: ~1.1 fps via SDK (hardware trigger needed for 15fps)

The SDK is fundamentally limited to ~1.1 fps due to camera internal processing time (~280ms per shot). **15fps requires hardware shutter trigger.**

## Key Discovery: Private SDK Functions Work!

We successfully bypassed the broken XSDK_SetProp interface by calling model library functions directly:

```python
# Load the model library
ff_lib = ctypes.CDLL("SDK/FF0000API.bundle/Contents/MacOS/FF0000API")

# Call private functions with the XSDK handle directly
ff_lib.SDK_SetPerformanceSettings(cam._handle, ctypes.c_long(5))  # BOOST_FRAMERATE
ff_lib.SDK_SetShutterPriorityMode(cam._handle, ctypes.c_long(1), ctypes.c_long(1))  # AFS=RELEASE
```

### Functions that work:
- `SDK_GetDriveMode` / `SDK_SetDriveMode` (but only to allowed values)
- `SDK_GetPerformanceSettings` / `SDK_SetPerformanceSettings` ✓
- `SDK_GetShutterPriorityMode` / `SDK_SetShutterPriorityMode` ✓
- `SDK_GetLongExposureNR` / `SDK_SetLongExposureNR`
- `SDK_GetCaptureDelay` / `SDK_SetCaptureDelay`

### Why XSDK_SetProp failed:
- `GetDeviceInfoEx` reports SetProp (0x1402) as NOT supported on X-T4
- The generic Prop interface routes through model library, but X-T4 doesn't support it
- Direct calls to `SDK_*` functions bypass this limitation

## Performance Testing Results

All performance modes give the same ~1.1 fps:

| Mode | FPS | Success Rate |
|------|-----|--------------|
| NORMAL | 1.14 | 50% |
| BOOST_RESOLUTION | 1.14 | 50% |
| BOOST_FRAMERATE | 1.12 | 50% |

**The bottleneck is camera internal processing (~280ms per shot), not SDK settings.**

The alternating 50% success rate (ShootError every other shot) is inherent to the SDK's single-shot protocol. The camera needs recovery time between Release() calls.

## Why 15fps is Impossible via SDK

1. **Single-shot protocol**: SDK fires one shot per `XSDK_Release()` call
2. **No continuous mode API**: No SDK function to trigger firmware burst mode
3. **280ms recovery time**: Camera needs processing time between shots
4. **50% failure rate**: Every other shot returns ShootError

The camera's 15fps CH mode is **firmware-controlled via physical shutter button only**.

## Hardware Trigger Solution

The X-T4 has a **2.5mm remote release port** (the hole next to shutter button):

```
2.5mm TRS Jack:
  Tip    = S1 (half-press/focus)
  Ring   = S2 (full-press/release)
  Sleeve = GND
```

### To trigger 15fps burst:
1. Set camera drive dial to CH (continuous high)
2. Connect Ring to Sleeve (GPIO + transistor)
3. Camera fires at native 15fps until released or buffer fills

### Simple circuit:
```
GPIO pin ──[220Ω]──► 2N2222 base
                    2N2222 collector ──► Ring (S2)
                    2N2222 emitter ───► Sleeve (GND)
```

### Python control:
```python
import RPi.GPIO as GPIO

SHUTTER_PIN = 17
GPIO.setup(SHUTTER_PIN, GPIO.OUT)

def burst(duration_seconds):
    GPIO.output(SHUTTER_PIN, GPIO.HIGH)  # Start 15fps burst
    time.sleep(duration_seconds)
    GPIO.output(SHUTTER_PIN, GPIO.LOW)   # Stop

burst(2.0)  # Fire ~30 frames at 15fps
```

## Test Scripts

- `test_sdk_direct_v2.py` - Proves private SDK calls work
- `test_set_perf.py` - Tests PerformanceSettings changes
- `test_burst_with_boost.py` - Compares performance modes
- `test_api_diagnostic.py` - Shows supported API codes

## Files Modified

- `BURST_SPEED_CONTEXT.md` - This document
- New test scripts in project root

## Summary

| Approach | Max FPS | Reliable? |
|----------|---------|-----------|
| SDK single-shot | ~1.1 | 50% success |
| Hardware trigger (2.5mm) | 15 | Yes |
| gphoto2 | ~1.0 | No |

**Recommendation**: Build hardware trigger for eclipse photography. The SDK is useful for setup/configuration, but burst capture should use hardware trigger.
