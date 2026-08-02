#!/usr/bin/env python3
"""Apply all optimizations via private SDK_* calls and test burst speed."""

import ctypes
import platform
import sys
import time
from pathlib import Path

_base = Path(__file__).resolve().parent / "SDK/SDK13410/REDISTRIBUTABLES"
if platform.system() == "Darwin":
    SDK_PATH = _base / "macOS/SDK_13400"
else:
    SDK_PATH = _base / "Linux/Linux64PC"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fujixsdk import Camera
from fujixsdk import _constants as C
from fujixsdk._errors import XSDKError

# Constants
SHUTTER_PRIORITY_RELEASE = 0x0001
SHUTTER_PRIORITY_FOCUS = 0x0002
PERF_NORMAL = 0x0001
PERF_BOOST_FRAMERATE = 0x0005
ITEM_AFS = 1
ITEM_AFC = 2


def setup_camera():
    cameras = Camera.detect(str(SDK_PATH), C.IF_USB)
    if not cameras:
        print("No cameras found")
        sys.exit(1)
    print(f"Camera: {cameras[0].product}")
    cam = Camera(str(SDK_PATH), cameras[0].device_name)
    for _ in range(20):
        try:
            cam.set_priority(C.PRIORITY_PC)
            break
        except:
            cam.drain_buffer()
            time.sleep(0.5)
    cam.drain_buffer()
    time.sleep(0.3)
    return cam


def load_ff_lib():
    """Load FF0000API model library."""
    bundle_path = SDK_PATH / "FF0000API.bundle" / "Contents" / "MacOS" / "FF0000API"
    return ctypes.CDLL(str(bundle_path))


def apply_optimizations(cam, ff_lib):
    """Apply all available optimizations via private SDK calls."""
    print(f"\n{'='*60}")
    print("  Applying Optimizations via Private SDK Calls")
    print(f"{'='*60}")

    handle = cam._handle

    # 1. Set ShutterPriorityMode to RELEASE for both AFS and AFC
    print("\n  ShutterPriorityMode:")
    set_sp = ff_lib.SDK_SetShutterPriorityMode
    set_sp.restype = ctypes.c_long
    set_sp.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_long]

    get_sp = ff_lib.SDK_GetShutterPriorityMode
    get_sp.restype = ctypes.c_long
    get_sp.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(ctypes.c_long)]

    for item, name in [(ITEM_AFS, "AFS"), (ITEM_AFC, "AFC")]:
        # Get current
        val = ctypes.c_long()
        get_sp(handle, ctypes.c_long(item), ctypes.byref(val))
        old = "RELEASE" if val.value == SHUTTER_PRIORITY_RELEASE else "FOCUS"

        # Set to RELEASE
        rc = set_sp(handle, ctypes.c_long(item), ctypes.c_long(SHUTTER_PRIORITY_RELEASE))

        # Verify
        get_sp(handle, ctypes.c_long(item), ctypes.byref(val))
        new = "RELEASE" if val.value == SHUTTER_PRIORITY_RELEASE else "FOCUS"
        print(f"    {name}: {old} → {new} (rc={rc})")

    # 2. Set PerformanceSettings to BOOST_FRAMERATE
    print("\n  PerformanceSettings:")
    set_perf = ff_lib.SDK_SetPerformanceSettings
    set_perf.restype = ctypes.c_long
    set_perf.argtypes = [ctypes.c_void_p, ctypes.c_long]

    get_perf = ff_lib.SDK_GetPerformanceSettings
    get_perf.restype = ctypes.c_long
    get_perf.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)]

    perf_names = {1: "NORMAL", 2: "ECONOMY", 3: "BOOST_LOWLIGHT",
                  4: "BOOST_RESOLUTION", 5: "BOOST_FRAMERATE"}

    val = ctypes.c_long()
    get_perf(handle, ctypes.byref(val))
    old = perf_names.get(val.value, f"0x{val.value:04X}")

    rc = set_perf(handle, ctypes.c_long(PERF_BOOST_FRAMERATE))

    get_perf(handle, ctypes.byref(val))
    new = perf_names.get(val.value, f"0x{val.value:04X}")
    print(f"    {old} → {new} (rc={rc})")

    # 3. Try LongExposureNR (might not be available)
    print("\n  LongExposureNR:")
    try:
        set_nr = ff_lib.SDK_SetLongExposureNR
        set_nr.restype = ctypes.c_long
        set_nr.argtypes = [ctypes.c_void_p, ctypes.c_long]

        get_nr = ff_lib.SDK_GetLongExposureNR
        get_nr.restype = ctypes.c_long
        get_nr.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)]

        val = ctypes.c_long()
        rc = get_nr(handle, ctypes.byref(val))
        if rc == 0:
            old_val = val.value
            # Set to OFF (0x0002)
            rc = set_nr(handle, ctypes.c_long(0x0002))
            get_nr(handle, ctypes.byref(val))
            print(f"    0x{old_val:04X} → 0x{val.value:04X} (rc={rc})")
        else:
            print(f"    Not available (rc={rc})")
    except AttributeError:
        print("    Function not found")

    # 4. CaptureDelay
    print("\n  CaptureDelay:")
    try:
        set_cd = ff_lib.SDK_SetCaptureDelay
        set_cd.restype = ctypes.c_long
        set_cd.argtypes = [ctypes.c_void_p, ctypes.c_long]

        get_cd = ff_lib.SDK_GetCaptureDelay
        get_cd.restype = ctypes.c_long
        get_cd.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)]

        val = ctypes.c_long()
        rc = get_cd(handle, ctypes.byref(val))
        if rc == 0:
            old_val = val.value
            # Set to 0 (OFF)
            rc = set_cd(handle, ctypes.c_long(0))
            get_cd(handle, ctypes.byref(val))
            print(f"    {old_val} → {val.value} (rc={rc})")
        else:
            print(f"    Not available (rc={rc})")
    except AttributeError:
        print("    Function not found")

    print("\n  ✓ Optimizations applied!")


def burst_test(cam, shots=10, delay=0.05):
    """Run burst capture test."""
    print(f"\n{'='*60}")
    print(f"  Burst Test: {shots} shots, {delay*1000:.0f}ms delay")
    print(f"{'='*60}")

    # Configure exposure
    cam.set_ae_mode(C.AE_OFF)
    cam.set_shutter_speed(C.SHUTTER_1_1000)
    cam.set_iso(C.ISO_100)

    # Clear buffer
    cam.drain_buffer()
    time.sleep(0.3)

    successes = 0
    failures = 0
    times = []

    print("\n  Shooting...")
    start_time = time.time()

    for i in range(shots):
        shot_start = time.time()
        try:
            cam.shoot_no_af()
            successes += 1
            shot_time = time.time() - shot_start
            times.append(shot_time)
            print(f"    Shot {i+1}: {shot_time*1000:.0f}ms ✓")
        except XSDKError as e:
            failures += 1
            print(f"    Shot {i+1}: FAIL ({e})")

        if delay > 0:
            time.sleep(delay)

    total_time = time.time() - start_time
    actual_shots = successes

    print(f"\n  Results:")
    print(f"    Success: {successes}/{shots}")
    print(f"    Failures: {failures}")
    print(f"    Total time: {total_time:.2f}s")
    if actual_shots > 1:
        fps = actual_shots / total_time
        print(f"    Effective FPS: {fps:.2f}")
        if times:
            avg_shot = sum(times) / len(times)
            print(f"    Avg shot time: {avg_shot*1000:.0f}ms")

    return successes, total_time


def main():
    print("=" * 60)
    print("  Optimized Burst Test via Private SDK Calls")
    print("=" * 60)

    ff_lib = load_ff_lib()
    cam = setup_camera()

    try:
        # Baseline test
        print("\n" + "="*60)
        print("  BASELINE (before optimizations)")
        print("="*60)
        baseline_shots, baseline_time = burst_test(cam, shots=5, delay=0.15)

        # Apply optimizations
        apply_optimizations(cam, ff_lib)

        # Optimized test
        print("\n" + "="*60)
        print("  OPTIMIZED (after applying settings)")
        print("="*60)
        opt_shots, opt_time = burst_test(cam, shots=5, delay=0.15)

        # Aggressive test with minimal delay
        print("\n" + "="*60)
        print("  AGGRESSIVE (minimal delay)")
        print("="*60)
        cam.drain_buffer()
        time.sleep(0.5)
        agg_shots, agg_time = burst_test(cam, shots=10, delay=0.03)

        # Summary
        print(f"\n{'='*60}")
        print("  SUMMARY")
        print(f"{'='*60}")
        if baseline_shots > 1:
            print(f"  Baseline FPS:   {baseline_shots/baseline_time:.2f}")
        if opt_shots > 1:
            print(f"  Optimized FPS:  {opt_shots/opt_time:.2f}")
        if agg_shots > 1:
            print(f"  Aggressive FPS: {agg_shots/agg_time:.2f}")

    finally:
        cam.drain_buffer()
        cam.set_priority(C.PRIORITY_CAMERA)
        cam.close()


if __name__ == "__main__":
    main()
