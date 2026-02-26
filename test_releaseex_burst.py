#!/usr/bin/env python3
"""Test ReleaseEx for native burst in Camera Priority mode.

Previous tests used XSDK_Release (PC Priority API) in Camera Priority.
The CORRECT approach is XSDK_ReleaseEx with EX constants in Camera Priority.

Also fixes CapRelease/CapReleaseEx to use array parameters (not single values).

Key test: ReleaseEx S1_ON → S2_ON (hold for burst) → S2_OFF → S1_OFF
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


def setup_camera(sdk_path, priority=C.PRIORITY_PC):
    cameras = Camera.detect(sdk_path, C.IF_USB)
    if not cameras:
        print("No cameras found"); sys.exit(1)
    print(f"Camera: {cameras[0].product}")
    cam = Camera(sdk_path, cameras[0].device_name)

    for _ in range(20):
        try:
            cam.set_priority(priority)
            break
        except BusyError:
            cam.drain_buffer(); time.sleep(0.5)
    time.sleep(0.3)
    cam.drain_buffer()
    time.sleep(0.5)
    return cam


def err_name(code):
    names = {
        0: "OK", 0x00001001: "Sequence", 0x00001002: "Param",
        0x00001005: "Unsupported", 0x00001006: "Busy",
        0x00001008: "ShootError", 0x00001009: "FrameFull",
        0x00002003: "Combination", 0x00001013: "ApiNotFound",
    }
    return names.get(code, f"0x{code:08X}")


def test_cap_release_fixed(cam):
    """CapRelease with ARRAY parameter (not single value)."""
    print(f"\n{'='*60}")
    print("  CapRelease (fixed: array output)")
    print(f"{'='*60}")

    lib = cam._lib_inst
    num = ctypes.c_long()
    modes = (ctypes.c_long * 64)()

    # First call: get count
    rc = lib.XSDK_CapRelease(cam._handle, ctypes.byref(num), modes)
    if rc != C.COMPLETE:
        _, err = cam.get_error()
        print(f"  CapRelease FAILED: {err_name(err)}")
        return

    print(f"  Supported release modes: {num.value}")
    if num.value > 0:
        release_names = {
            0x0104: "SHOOT_S1OFF", 0x0200: "S1ON", 0x0004: "N_S1OFF",
            0x0304: "S2_S1OFF", 0x0500: "BULBS2_ON", 0x000C: "N_BULBS1OFF",
            0x000F: "CANCEL", 0x0100: "SHOOT", 0x0300: "S2",
            0x0400: "BULB_ON", 0x0008: "N_BULBOFF",
            0x0301: "S2_AFOFF", 0x0302: "S2_AEOFF", 0x0303: "S2_AFAEOFF",
            0x0101: "SHOOT_AFOFF", 0x0102: "SHOOT_AEOFF",
            0x8000: "CUSWB", 0x9000: "AEON", 0x9100: "AFON",
            0x9200: "AFAEON", 0x9300: "AF", 0xA000: "INSTANTAF",
        }
        for i in range(num.value):
            name = release_names.get(modes[i], f"?")
            print(f"    [{i:2d}] 0x{modes[i]:04X} = {name}")
    else:
        print("  (no modes reported)")


def test_cap_release_ex_fixed(cam):
    """CapReleaseEx with ARRAY parameter."""
    print(f"\n{'='*60}")
    print("  CapReleaseEx (fixed: array output)")
    print(f"{'='*60}")

    lib = cam._lib_inst
    num = ctypes.c_long()
    modes = (ctypes.c_long * 64)()

    rc = lib.XSDK_CapReleaseEx(cam._handle, ctypes.byref(num), modes)
    if rc != C.COMPLETE:
        _, err = cam.get_error()
        print(f"  CapReleaseEx FAILED: {err_name(err)}")
        return

    print(f"  Supported ReleaseEx modes: {num.value}")
    ex_names = {
        C.RELEASE_EX_S1_ON: "S1_ON", C.RELEASE_EX_S2_ON: "S2_ON",
        C.RELEASE_EX_S1_OFF: "S1_OFF", C.RELEASE_EX_S2_OFF: "S2_OFF",
        C.RELEASE_EX_REC_START: "REC_START", C.RELEASE_EX_REC_STOP: "REC_STOP",
        C.RELEASE_EX_CUSWB_ON: "CUSWB_ON", C.RELEASE_EX_CUSWB_OFF: "CUSWB_OFF",
        C.RELEASE_EX_CANCEL: "CANCEL",
        C.RELEASE_EX_INSTANTAF_ON: "INSTANTAF_ON",
        C.RELEASE_EX_AEL_ON: "AEL_ON", C.RELEASE_EX_AEL_OFF: "AEL_OFF",
        C.RELEASE_EX_AFL_ON: "AFL_ON", C.RELEASE_EX_AFL_OFF: "AFL_OFF",
        C.RELEASE_EX_AFON_ON: "AFON_ON", C.RELEASE_EX_AFON_OFF: "AFON_OFF",
        C.RELEASE_EX_WBL_ON: "WBL_ON", C.RELEASE_EX_WBL_OFF: "WBL_OFF",
        C.RELEASE_EX_GRAB: "GRAB",
        C.RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF: "S1_ON_S2_ON_S2_OFF_S1_OFF",
        C.RELEASE_EX_S2_ON_S2_OFF_S1_OFF: "S2_ON_S2_OFF_S1_OFF",
        C.RELEASE_EX_S2_OFF_S1_OFF: "S2_OFF_S1_OFF",
    }
    if num.value > 0:
        for i in range(num.value):
            name = ex_names.get(modes[i], f"?")
            print(f"    [{i:2d}] 0x{modes[i]:08X} = {name}")
    else:
        print("  (no modes reported)")


def raw_release_ex(cam, mode, shot_count=1):
    """Call XSDK_ReleaseEx. Returns (rc, shot_opt_out, af_status, err_code)."""
    lib = cam._lib_inst
    shot_opt = ctypes.c_long(shot_count)
    af_status = ctypes.c_long()
    rc = lib.XSDK_ReleaseEx(
        cam._handle, ctypes.c_long(mode),
        ctypes.byref(shot_opt), ctypes.byref(af_status))
    err = 0
    if rc != C.COMPLETE:
        _, err = cam.get_error()
    return rc, shot_opt.value, af_status.value, err


def raw_release(cam, mode, shot_count=1):
    """Call XSDK_Release. Returns (rc, shot_opt_out, af_status, err_code)."""
    lib = cam._lib_inst
    shot_opt = ctypes.c_long(shot_count)
    af_status = ctypes.c_long()
    rc = lib.XSDK_Release(
        cam._handle, ctypes.c_long(mode),
        ctypes.byref(shot_opt), ctypes.byref(af_status))
    err = 0
    if rc != C.COMPLETE:
        _, err = cam.get_error()
    return rc, shot_opt.value, af_status.value, err


def test_releaseex_probe(cam):
    """Try each ReleaseEx mode individually to see what's supported."""
    print(f"\n{'='*60}")
    print("  ReleaseEx mode probe (Camera Priority)")
    print(f"{'='*60}")

    modes_to_test = [
        ("GRAB", C.RELEASE_EX_GRAB),
        ("S1_ON", C.RELEASE_EX_S1_ON),
        ("S2_ON", C.RELEASE_EX_S2_ON),
        ("S2_OFF", C.RELEASE_EX_S2_OFF),
        ("S1_OFF", C.RELEASE_EX_S1_OFF),
        ("CANCEL", C.RELEASE_EX_CANCEL),
        ("S1+S2+S2OFF+S1OFF", C.RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF),
        ("S2+S2OFF+S1OFF", C.RELEASE_EX_S2_ON_S2_OFF_S1_OFF),
    ]

    for name, mode in modes_to_test:
        rc, shot_out, af_out, err = raw_release_ex(cam, mode)
        result = "OK" if rc == 0 else err_name(err)
        print(f"  ReleaseEx({name:25s}): {result:15s} shot_out={shot_out} af={af_out}")

        # Cancel between tests
        if rc == 0:
            time.sleep(0.3)
            raw_release_ex(cam, C.RELEASE_EX_CANCEL)
            time.sleep(0.3)


def test_releaseex_burst(cam, hold_time=2.0, shot_opt=20):
    """ReleaseEx S1_ON → S2_ON (hold for burst) → S2_OFF → S1_OFF."""
    print(f"\n{'='*60}")
    print(f"  ReleaseEx burst: S1→S2 hold {hold_time}s (plShotOpt={shot_opt})")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    # S1_ON (half press)
    rc1, _, _, err1 = raw_release_ex(cam, C.RELEASE_EX_S1_ON)
    print(f"  S1_ON: {err_name(err1) if rc1 != 0 else 'OK'}")

    time.sleep(0.15)

    # S2_ON (full press — start continuous burst)
    t0 = time.perf_counter()
    rc2, shot_out, af_out, err2 = raw_release_ex(cam, C.RELEASE_EX_S2_ON, shot_count=shot_opt)
    print(f"  S2_ON: {err_name(err2) if rc2 != 0 else 'OK'} shot_out={shot_out}")

    if rc2 != 0:
        raw_release_ex(cam, C.RELEASE_EX_CANCEL)
        return

    # Hold — poll release status for SHOOTING bit
    shoot_seen = False
    poll_count = 0
    while time.perf_counter() - t0 < hold_time:
        try:
            status = cam.get_release_status()
            if status & C.RELEASE_STATUS_SHOOTING and not shoot_seen:
                print(f"  SHOOTING bit at {(time.perf_counter()-t0)*1000:.0f}ms! status=0x{status:04X}")
                shoot_seen = True
            poll_count += 1
        except XSDKError:
            pass
        time.sleep(0.02)

    # S2_OFF (stop burst)
    rc3, _, _, err3 = raw_release_ex(cam, C.RELEASE_EX_S2_OFF)
    print(f"  S2_OFF: {err_name(err3) if rc3 != 0 else 'OK'}")

    # S1_OFF (release half press)
    rc4, _, _, err4 = raw_release_ex(cam, C.RELEASE_EX_S1_OFF)
    print(f"  S1_OFF: {err_name(err4) if rc4 != 0 else 'OK'}")

    time.sleep(1.0)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2  # RAW + JPEG per exposure
    fps = exposures / hold_time if hold_time > 0 and exposures > 0 else 0
    print(f"  Frames: {frames} ({exposures} exposures) in {hold_time}s = {fps:.1f} fps")
    print(f"  Polled {poll_count} times, SHOOTING seen: {shoot_seen}")


def test_releaseex_grab(cam, count=10):
    """Test RELEASE_EX_GRAB — "Still Image Capture" in Camera Priority."""
    print(f"\n{'='*60}")
    print(f"  ReleaseEx GRAB test ({count} shots)")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    taken = 0
    errors = 0
    t0 = time.perf_counter()

    for i in range(count):
        rc, shot_out, af_out, err = raw_release_ex(cam, C.RELEASE_EX_GRAB)
        if rc == 0:
            taken += 1
        else:
            if i == 0:
                print(f"  GRAB failed: {err_name(err)}")
                if err == C.ERRCODE_UNSUPPORTED:
                    print("  GRAB is not supported on this camera")
                    return
            errors += 1
            time.sleep(0.2)

    elapsed = time.perf_counter() - t0
    time.sleep(0.5)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2
    fps = exposures / elapsed if elapsed > 0 and exposures > 0 else 0
    print(f"  OK: {taken} Failed: {errors}")
    print(f"  Exposures: {exposures} in {elapsed:.2f}s = {fps:.1f} fps")


def test_releaseex_single_shot_cycle(cam, count=10):
    """ReleaseEx S1_ON_S2_ON_S2_OFF_S1_OFF atomic cycle."""
    print(f"\n{'='*60}")
    print(f"  ReleaseEx atomic cycle ({count} shots)")
    print(f"{'='*60}")

    cam.drain_buffer()
    time.sleep(0.3)
    cap_before, total = cam.get_buffer_capacity()
    print(f"  Buffer: {cap_before}/{total}")

    taken = 0
    errors = 0
    t0 = time.perf_counter()

    for i in range(count):
        rc, shot_out, af_out, err = raw_release_ex(
            cam, C.RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF)
        if rc == 0:
            taken += 1
        else:
            if i == 0:
                print(f"  Atomic cycle failed: {err_name(err)}")
                if err == C.ERRCODE_UNSUPPORTED:
                    return
            errors += 1
            time.sleep(0.2)

    elapsed = time.perf_counter() - t0
    time.sleep(0.5)
    cap_after, _ = cam.get_buffer_capacity()
    frames = cap_after - cap_before
    exposures = frames // 2
    fps = exposures / elapsed if elapsed > 0 and exposures > 0 else 0
    print(f"  OK: {taken} Failed: {errors}")
    print(f"  Exposures: {exposures} in {elapsed:.2f}s = {fps:.1f} fps")


def test_in_both_priorities(cam):
    """Test CapRelease and CapReleaseEx in both priority modes."""

    # PC Priority
    print("\n" + "="*60)
    print("  IN PC PRIORITY:")
    print("="*60)
    try:
        cam.drain_buffer()
        cam.set_priority(C.PRIORITY_PC)
        time.sleep(0.3)
    except XSDKError as e:
        print(f"  Could not set PC priority: {e}")
        return

    test_cap_release_fixed(cam)
    test_cap_release_ex_fixed(cam)

    # Camera Priority
    print("\n" + "="*60)
    print("  IN CAMERA PRIORITY:")
    print("="*60)
    try:
        cam.drain_buffer()
        cam.set_priority(C.PRIORITY_CAMERA)
        time.sleep(0.3)
    except XSDKError as e:
        print(f"  Could not set CAMERA priority: {e}")
        return

    test_cap_release_fixed(cam)
    test_cap_release_ex_fixed(cam)


def main():
    if not ensure_ld_library_path(SDK_PATH):
        os.execvp(sys.executable, [sys.executable] + sys.argv)

    cam = setup_camera(SDK_PATH, priority=C.PRIORITY_PC)
    try:
        dm = cam.get_drive_mode()
        print(f"Drive: {C.DRIVE_MODE_NAMES.get(dm, f'0x{dm:04X}')}")

        # 1. Test CapRelease/CapReleaseEx with correct array params in both priorities
        test_in_both_priorities(cam)

        # 2. Switch to CAMERA priority for ReleaseEx tests
        print(f"\n{'='*60}")
        print("  Switching to CAMERA priority for ReleaseEx tests")
        print(f"{'='*60}")
        try:
            cam.drain_buffer()
            cam.set_priority(C.PRIORITY_CAMERA)
            time.sleep(0.3)
        except XSDKError as e:
            print(f"  ERROR: {e}")
            return

        # 3. Probe all ReleaseEx modes
        test_releaseex_probe(cam)

        # 4. Test GRAB
        test_releaseex_grab(cam, count=5)

        # 5. Test atomic single-shot cycle
        test_releaseex_single_shot_cycle(cam, count=5)

        # 6. Test burst with S2 hold
        test_releaseex_burst(cam, hold_time=2.0, shot_opt=20)

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
