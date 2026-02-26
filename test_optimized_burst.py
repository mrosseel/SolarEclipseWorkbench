#!/usr/bin/env python3
"""Test all remaining optimizations for burst speed.

1. Read raw drive mode value (check if X-T4 reports 0x10F0 for CH)
2. Try SetDriveMode with model-specific values
3. Set PerformanceSettings to BOOST_FRAMERATE_PRIORITY
4. Set ShutterPriorityMode to RELEASE
5. Set CaptureDelay to OFF
6. Set LongExposureNR to OFF
7. Speed test with optimizations applied
"""

import ctypes
import os
import sys
import time
from pathlib import Path

SDK_PATH = str(Path(__file__).resolve().parent / "SDK/SDK13410/REDISTRIBUTABLES/Linux/Linux64PC")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fujixsdk import Camera, ensure_ld_library_path
from fujixsdk import _constants as C
from fujixsdk._errors import BusyError, XSDKError

# API codes from XAPIOpt.H
API_CODE_SetPerformanceSettings = 0x4262
API_CODE_GetPerformanceSettings = 0x4263
API_CODE_CapPerformanceSettings = 0x4266
API_CODE_SetShutterPriorityMode = 0x2217
API_CODE_GetShutterPriorityMode = 0x2218
API_CODE_SetCaptureDelay = 0x3021
API_CODE_GetCaptureDelay = 0x3022
API_CODE_SetLongExposureNR = 0x2145
API_CODE_GetLongExposureNR = 0x2146

# Constants
PERFORMANCE_NORMAL = 0x0001
PERFORMANCE_BOOST_FRAMERATE = 0x0005
AFPRIORITY_RELEASE = 0x0001
AFPRIORITY_FOCUS = 0x0002
ITEM_AFPRIORITY_AFS = 1
ITEM_AFPRIORITY_AFC = 2
CAPTUREDELAY_OFF = 0
LONGEXPOSURENR_ON = 0x0001
LONGEXPOSURENR_OFF = 0x0002

# X-T4 model-specific drive mode values
XT4_DRIVE_MODE_CH = 0x10F0
XT4_DRIVE_MODE_CL = 0x1000
XT4_DRIVE_MODE_BKT = 0x4000


def err_name(code):
    names = {
        0: "OK", 0x00001001: "Sequence", 0x00001002: "Param",
        0x00001005: "Unsupported", 0x00001006: "Busy",
        0x00001008: "ShootError", 0x00001009: "FrameFull",
        0x00002003: "Combination", 0x00001013: "ApiNotFound",
    }
    return names.get(code, f"0x{code:08X}")


def get_error(cam):
    _, err = cam.get_error()
    return err


def setup_camera(sdk_path):
    cameras = Camera.detect(sdk_path, C.IF_USB)
    if not cameras:
        print("No cameras found"); sys.exit(1)
    print(f"Camera: {cameras[0].product}")
    cam = Camera(sdk_path, cameras[0].device_name)
    for _ in range(20):
        try:
            cam.set_priority(C.PRIORITY_PC)
            break
        except BusyError:
            cam.drain_buffer(); time.sleep(0.5)
    time.sleep(0.3)
    cam.drain_buffer()
    time.sleep(0.5)
    return cam


def test_drive_mode(cam):
    """Read raw drive mode value and test setting it."""
    print(f"\n{'='*60}")
    print("  Drive Mode Investigation")
    print(f"{'='*60}")

    dm = cam.get_drive_mode()
    name = C.DRIVE_MODE_NAMES.get(dm, f"UNKNOWN")
    print(f"  Current drive mode: 0x{dm:04X} = {name}")

    # Check model-specific values
    if dm == XT4_DRIVE_MODE_CH:
        print(f"  → Matches X-T4 CH (0x{XT4_DRIVE_MODE_CH:04X})")
    elif dm == XT4_DRIVE_MODE_CL:
        print(f"  → Matches X-T4 CL (0x{XT4_DRIVE_MODE_CL:04X})")
    elif dm == C.DRIVE_MODE_CH:
        print(f"  → Matches generic CH (0x{C.DRIVE_MODE_CH:04X})")
    elif dm == C.DRIVE_MODE_S:
        print(f"  → Single shot mode")

    # Try CapDriveMode
    lib = cam._lib_inst
    num = ctypes.c_long()
    modes = (ctypes.c_long * 64)()
    rc = lib.XSDK_CapDriveMode(cam._handle, ctypes.byref(num), modes)
    if rc == C.COMPLETE:
        print(f"  CapDriveMode: {num.value} modes supported")
        for i in range(min(num.value, 64)):
            mname = C.DRIVE_MODE_NAMES.get(modes[i], "?")
            print(f"    [{i}] 0x{modes[i]:04X} = {mname}")
    else:
        err = get_error(cam)
        print(f"  CapDriveMode: {err_name(err)}")

    # Try setting drive mode to various CH values
    for name, val in [("Generic CH", C.DRIVE_MODE_CH),
                      ("X-T4 CH", XT4_DRIVE_MODE_CH),
                      ("X-T4 CL", XT4_DRIVE_MODE_CL)]:
        rc = lib.XSDK_SetDriveMode(cam._handle, ctypes.c_long(val))
        if rc == C.COMPLETE:
            print(f"  SetDriveMode({name} = 0x{val:04X}): OK!")
            dm2 = cam.get_drive_mode()
            print(f"    → Drive mode now: 0x{dm2:04X}")
        else:
            err = get_error(cam)
            print(f"  SetDriveMode({name} = 0x{val:04X}): {err_name(err)}")


def test_performance_settings(cam):
    """Read and set PerformanceSettings."""
    print(f"\n{'='*60}")
    print("  Performance Settings")
    print(f"{'='*60}")

    lib = cam._lib_inst
    val = ctypes.c_long()

    # Get current
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetPerformanceSettings),
                           ctypes.c_long(1),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        perf_names = {1: "NORMAL", 2: "ECONOMY", 3: "BOOST_LOWLIGHT",
                      4: "BOOST_RESOLUTION", 5: "BOOST_FRAMERATE"}
        print(f"  Current: 0x{val.value:04X} = {perf_names.get(val.value, '?')}")
    else:
        err = get_error(cam)
        print(f"  GetPerformanceSettings: {err_name(err)}")

    # Cap (enumerate)
    num = ctypes.c_long()
    modes = (ctypes.c_long * 16)()
    rc = lib.XSDK_CapProp(cam._handle,
                           ctypes.c_long(API_CODE_CapPerformanceSettings),
                           ctypes.c_long(1),
                           ctypes.byref(num), modes)
    if rc == C.COMPLETE:
        perf_names = {1: "NORMAL", 2: "ECONOMY", 3: "BOOST_LOWLIGHT",
                      4: "BOOST_RESOLUTION", 5: "BOOST_FRAMERATE"}
        print(f"  Available: {num.value} modes")
        for i in range(min(num.value, 16)):
            print(f"    [{i}] 0x{modes[i]:04X} = {perf_names.get(modes[i], '?')}")
    else:
        err = get_error(cam)
        print(f"  CapPerformanceSettings: {err_name(err)}")

    # Set BOOST_FRAMERATE_PRIORITY
    rc = lib.XSDK_SetProp(cam._handle,
                           ctypes.c_long(API_CODE_SetPerformanceSettings),
                           ctypes.c_long(1),
                           ctypes.c_long(PERFORMANCE_BOOST_FRAMERATE))
    if rc == C.COMPLETE:
        print(f"  Set BOOST_FRAMERATE_PRIORITY: OK!")
    else:
        err = get_error(cam)
        print(f"  Set BOOST_FRAMERATE: {err_name(err)}")

    # Verify
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetPerformanceSettings),
                           ctypes.c_long(1),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        perf_names = {1: "NORMAL", 2: "ECONOMY", 3: "BOOST_LOWLIGHT",
                      4: "BOOST_RESOLUTION", 5: "BOOST_FRAMERATE"}
        print(f"  After set: 0x{val.value:04X} = {perf_names.get(val.value, '?')}")


def test_shutter_priority(cam):
    """Set ShutterPriorityMode to RELEASE (skip AF wait)."""
    print(f"\n{'='*60}")
    print("  Shutter Priority Mode (Release/Focus)")
    print(f"{'='*60}")

    lib = cam._lib_inst

    # Get current for AFS
    val = ctypes.c_long()
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetShutterPriorityMode),
                           ctypes.c_long(2),
                           ctypes.c_long(ITEM_AFPRIORITY_AFS),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        pname = {1: "RELEASE", 2: "FOCUS"}.get(val.value, "?")
        print(f"  AFS priority: 0x{val.value:04X} = {pname}")
    else:
        err = get_error(cam)
        print(f"  Get AFS priority: {err_name(err)}")

    # Get current for AFC
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetShutterPriorityMode),
                           ctypes.c_long(2),
                           ctypes.c_long(ITEM_AFPRIORITY_AFC),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        pname = {1: "RELEASE", 2: "FOCUS"}.get(val.value, "?")
        print(f"  AFC priority: 0x{val.value:04X} = {pname}")
    else:
        err = get_error(cam)
        print(f"  Get AFC priority: {err_name(err)}")

    # Set both to RELEASE priority
    for item_name, item in [("AFS", ITEM_AFPRIORITY_AFS), ("AFC", ITEM_AFPRIORITY_AFC)]:
        rc = lib.XSDK_SetProp(cam._handle,
                               ctypes.c_long(API_CODE_SetShutterPriorityMode),
                               ctypes.c_long(2),
                               ctypes.c_long(item),
                               ctypes.c_long(AFPRIORITY_RELEASE))
        if rc == C.COMPLETE:
            print(f"  Set {item_name} → RELEASE: OK!")
        else:
            err = get_error(cam)
            print(f"  Set {item_name} → RELEASE: {err_name(err)}")


def test_capture_delay(cam):
    """Ensure CaptureDelay is OFF."""
    print(f"\n{'='*60}")
    print("  Capture Delay")
    print(f"{'='*60}")

    lib = cam._lib_inst
    val = ctypes.c_long()
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetCaptureDelay),
                           ctypes.c_long(1),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        delay_names = {0: "OFF", 2000: "2sec", 10000: "10sec"}
        print(f"  Current: {val.value} = {delay_names.get(val.value, '?')}")
    else:
        err = get_error(cam)
        print(f"  GetCaptureDelay: {err_name(err)}")

    # Set OFF
    rc = lib.XSDK_SetProp(cam._handle,
                           ctypes.c_long(API_CODE_SetCaptureDelay),
                           ctypes.c_long(1),
                           ctypes.c_long(CAPTUREDELAY_OFF))
    if rc == C.COMPLETE:
        print(f"  Set OFF: OK!")
    else:
        err = get_error(cam)
        print(f"  Set OFF: {err_name(err)}")


def test_long_exposure_nr(cam):
    """Ensure LongExposureNR is OFF."""
    print(f"\n{'='*60}")
    print("  Long Exposure NR")
    print(f"{'='*60}")

    lib = cam._lib_inst
    val = ctypes.c_long()
    rc = lib.XSDK_GetProp(cam._handle,
                           ctypes.c_long(API_CODE_GetLongExposureNR),
                           ctypes.c_long(1),
                           ctypes.byref(val))
    if rc == C.COMPLETE:
        nr_names = {1: "ON", 2: "OFF"}
        print(f"  Current: 0x{val.value:04X} = {nr_names.get(val.value, '?')}")
    else:
        err = get_error(cam)
        print(f"  GetLongExposureNR: {err_name(err)}")

    # Set OFF
    rc = lib.XSDK_SetProp(cam._handle,
                           ctypes.c_long(API_CODE_SetLongExposureNR),
                           ctypes.c_long(1),
                           ctypes.c_long(LONGEXPOSURENR_OFF))
    if rc == C.COMPLETE:
        print(f"  Set OFF: OK!")
    else:
        err = get_error(cam)
        print(f"  Set OFF: {err_name(err)}")


def speed_test(cam, count=20, s1_delay=0.05):
    """Speed test with minimal delays."""
    print(f"\n{'='*60}")
    print(f"  Speed test: {count} shots, S1 delay={s1_delay}s")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    lib = cam._lib_inst
    ok = 0
    fail = 0
    t0 = time.perf_counter()

    for i in range(count):
        # Check buffer not full
        cap, total = cam.get_buffer_capacity()
        if cap >= total - 2:
            print(f"  Buffer full at shot {i}")
            break

        # S1ON
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S1ON),
                         ctypes.byref(shot), ctypes.byref(af))
        time.sleep(s1_delay)

        # S2_S1OFF
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        rc = lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S2_S1OFF),
                              ctypes.byref(shot), ctypes.byref(af))
        if rc == C.COMPLETE:
            ok += 1
        else:
            fail += 1

    elapsed = time.perf_counter() - t0
    time.sleep(1.0)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2
    fps = exposures / elapsed if elapsed > 0 and exposures > 0 else 0

    print(f"  Time: {elapsed:.2f}s")
    print(f"  Release calls: OK={ok} Fail={fail}")
    print(f"  Buffer frames: {frames} → {exposures} exposures")
    print(f"  Rate: {fps:.2f} exposures/sec")
    return fps


def speed_test_no_s1_delay(cam, count=20):
    """Speed test with zero S1 delay (back-to-back S1ON+S2_S1OFF)."""
    print(f"\n{'='*60}")
    print(f"  Speed test NO S1 delay: {count} shots")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    lib = cam._lib_inst
    ok = 0
    fail = 0
    t0 = time.perf_counter()

    for i in range(count):
        cap, total = cam.get_buffer_capacity()
        if cap >= total - 2:
            print(f"  Buffer full at shot {i}")
            break

        # S1ON + S2_S1OFF back-to-back, no delay
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S1ON),
                         ctypes.byref(shot), ctypes.byref(af))
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        rc = lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S2_S1OFF),
                              ctypes.byref(shot), ctypes.byref(af))
        if rc == C.COMPLETE:
            ok += 1
        else:
            fail += 1
            # On failure, tiny sleep before retry
            time.sleep(0.05)

    elapsed = time.perf_counter() - t0
    time.sleep(1.0)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2
    fps = exposures / elapsed if elapsed > 0 and exposures > 0 else 0

    print(f"  Time: {elapsed:.2f}s")
    print(f"  Release calls: OK={ok} Fail={fail}")
    print(f"  Buffer frames: {frames} → {exposures} exposures")
    print(f"  Rate: {fps:.2f} exposures/sec")
    return fps


def speed_test_fire_and_forget(cam, count=16):
    """Fire S1ON+S2_S1OFF as fast as possible, don't check buffer between shots."""
    print(f"\n{'='*60}")
    print(f"  Fire-and-forget: {count} shots (no buffer check)")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    lib = cam._lib_inst
    ok = 0
    fail = 0
    t0 = time.perf_counter()

    for i in range(count):
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S1ON),
                         ctypes.byref(shot), ctypes.byref(af))
        # Minimal delay — just enough for S1 to register
        time.sleep(0.03)
        shot = ctypes.c_long(1)
        af = ctypes.c_long()
        rc = lib.XSDK_Release(cam._handle, ctypes.c_long(C.RELEASE_S2_S1OFF),
                              ctypes.byref(shot), ctypes.byref(af))
        if rc == C.COMPLETE:
            ok += 1
        else:
            fail += 1

    elapsed = time.perf_counter() - t0

    # Wait for camera to finish processing
    time.sleep(2.0)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2
    fps = exposures / elapsed if elapsed > 0 and exposures > 0 else 0

    print(f"  Time: {elapsed:.2f}s")
    print(f"  Release calls: OK={ok} Fail={fail}")
    print(f"  Buffer frames: {frames} → {exposures} exposures")
    print(f"  Rate: {fps:.2f} exposures/sec (including failed attempts)")


def main():
    if not ensure_ld_library_path(SDK_PATH):
        os.execvp(sys.executable, [sys.executable] + sys.argv)

    cam = setup_camera(SDK_PATH)
    try:
        # 1. Drive mode investigation
        test_drive_mode(cam)

        # 2. Performance settings
        test_performance_settings(cam)

        # 3. Shutter priority
        test_shutter_priority(cam)

        # 4. Capture delay
        test_capture_delay(cam)

        # 5. Long exposure NR
        test_long_exposure_nr(cam)

        # 6. Speed tests
        print(f"\n{'='*60}")
        print("  SPEED TESTS (with all optimizations)")
        print(f"{'='*60}")

        speed_test(cam, count=16, s1_delay=0.15)
        speed_test(cam, count=16, s1_delay=0.05)
        speed_test(cam, count=16, s1_delay=0.03)
        speed_test_no_s1_delay(cam, count=16)
        speed_test_fire_and_forget(cam, count=16)

    finally:
        try:
            cam.drain_buffer()
            cam.set_priority(C.PRIORITY_CAMERA)
            print("\nReturned priority to camera.")
        except XSDKError:
            pass
        cam.close()


if __name__ == "__main__":
    main()
