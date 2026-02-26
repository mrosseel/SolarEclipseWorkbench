# Fuji X-T4 Burst Speed Investigation — Context for Continuation

## Goal
Achieve 15fps burst capture through USB SDK on Fuji X-T4. Currently stuck at ~1.8 fps.

## What's Been Tested and Confirmed

### Working
- `XSDK_Release(S1ON)` → 0.15s delay → `XSDK_Release(S2_S1OFF)` in PC Priority = ~1.8 fps
- Buffer: each exposure = 2 entries (RAW 13MB + JPEG 754KB), capacity 32 = 16 exposures max
- `_cleanup_stale_state()` flush-shot recovery on init works reliably

### Dead Ends (all confirmed on X-T4)
- **ReleaseEx**: completely unsupported — every mode returns "Unsupported" (0x00001005)
- **CapRelease/CapReleaseEx**: return 0 modes in both PC and Camera priority (even with correct array params)
- **RELEASE_SHOOT (0x0100)**: ParamError
- **RELEASE_SHOOT_S1OFF (0x0104)**: ShootError always
- **S2 without S1OFF** (any variant): "Invalid combination"
- **plShotOpt > 1**: ignored, always fires exactly 1 exposure
- **CAMERA priority**: all Release modes return ShootError
- **SetDriveMode**: CapDriveMode returns 0 modes, SetDriveMode fails with ParamError
- **SetImageQuality/GetImageQuality**: ParamError
- **gphoto2 trigger_capture**: even slower (~1 fps)

### Camera's 15fps CH burst
Only works via physical shutter button. Camera firmware controlled, not accessible through any SDK API path we've found.

## What's Ready to Test Next

`test_optimized_burst.py` — NOT YET RUN (camera disconnected before we could run it)

### Optimizations to test (all supported on X-T4 per model header):

1. **PerformanceSettings → BOOST_FRAMERATE_PRIORITY (0x0005)**
   - API: `SetProp(handle, 0x4262, 1, 0x0005)`
   - X-T4 header confirms API_PARAM = 1 (supported)
   - May reduce internal ~300ms post-processing delay

2. **ShutterPriorityMode → RELEASE (0x0001)**
   - API: `SetProp(handle, 0x2217, 2, item, 0x0001)` where item = AFS(1) or AFC(2)
   - X-T4 header confirms API_PARAM = 2 (supported)
   - Fires immediately without waiting for AF confirmation

3. **CaptureDelay → OFF (0)**
   - API: `SetProp(handle, 0x3021, 1, 0)`
   - Ensure self-timer is off

4. **LongExposureNR → OFF (0x0002)**
   - API: `SetProp(handle, 0x2145, 1, 0x0002)`
   - Eliminate post-shot NR processing

5. **Drive mode raw value investigation**
   - GetDriveMode returns 0x0004 ("Single") even when physical dial is on CH
   - X-T4 model-specific values: CH=0x10F0, CL=0x1000 (NOT the generic 0x0002/0x0003)
   - Test SetDriveMode with model-specific 0x10F0

6. **Speed tests with varying S1 delays**
   - 150ms, 50ms, 30ms, zero, and fire-and-forget (no buffer check between shots)

### Run command (adjust paths for macOS):
```bash
LD_LIBRARY_PATH="<libusb-path>:<sdk-lib-path>" python test_optimized_burst.py
```

## Key SDK Architecture Facts

- SDK is single-shot per Release() call — no "start continuous" API exists
- Every shot: S1ON (3ms) + settle delay + S2_S1OFF (30ms) + camera processing (~300ms)
- Camera's internal processing is the bottleneck, not USB bandwidth
- The 50% alternating ShootError pattern is inherent — camera needs recovery time
- No callback/event mechanism — everything is polling-based
- SetProp/GetProp/CapProp is the variadic interface for model-dependent features

## Key Files

- `fujixsdk/camera.py` — Camera class with shoot_no_af(), drain_buffer(), cleanup
- `fujixsdk/_constants.py` — All SDK constants (missing PerformanceSettings constants)
- `fujixsdk/_library.py` — ctypes bindings
- `fujixsdk/eclipse.py` — Eclipse shooter with shoot_fast() retry logic
- `test_optimized_burst.py` — **THE NEXT TEST TO RUN**
- `test_releaseex_burst.py` — ReleaseEx diagnostic (already confirmed: unsupported)
- `SDK/SDK13410/HEADERS/X-T4.h` — Model-specific supported features
- `SDK/SDK13410/HEADERS/XAPIOpt.H` — Extended API constants
- `SDK/SDK13410/HEADERS/XAPI.H` — Core API

## X-T4 Header: Key Supported/Unsupported Features

| Feature | API_PARAM | Status |
|---------|-----------|--------|
| SetPerformanceSettings | 1 | SUPPORTED |
| SetShutterPriorityMode | 2 | SUPPORTED |
| SetCaptureDelay | 1 | SUPPORTED |
| SetLongExposureNR | 1 | SUPPORTED |
| SetPreviewTime | -1 | NOT SUPPORTED |
| SetImageQuality | 1 | SUPPORTED (but returns ParamError??) |
| SetSilentMode | -1 | NOT SUPPORTED |

## If Optimizations Don't Help

The hard truth may be: ~1.8 fps is the USB PTP protocol limit for this camera. The SDK has no API to trigger camera-firmware burst mode. Alternatives:
- Hardware servo on physical shutter button
- Accept 1.8fps and optimize download pipeline (shoot first, download after)
- WiFi PTP/IP (likely slower due to latency)
