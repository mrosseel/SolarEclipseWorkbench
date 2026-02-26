#!/usr/bin/env python3
"""Burst speed test — fire 100 shots at 1/8000 as fast as possible.

Tests both the Fuji SDK path (burst_no_download) and the gphoto2 path
(trigger_capture without download) to measure real frame rates.

Usage:
    python test_burst_speed.py              # auto-detect backend
    python test_burst_speed.py --fuji       # force Fuji SDK
    python test_burst_speed.py --gphoto2    # force gphoto2
    python test_burst_speed.py --count 50   # fewer shots
"""

import argparse
import os
import sys
import time
from pathlib import Path

SHOT_COUNT = 100

# ---------------------------------------------------------------------------
# Fuji SDK backend
# ---------------------------------------------------------------------------

def test_fuji_sdk(count: int, sdk_path: str):
    from fujixsdk import (
        Camera, EclipseShooter,
        IF_USB, PRIORITY_PC, PRIORITY_CAMERA, AE_OFF,
        SHUTTER_1_8000, ISO_100, WB_DAYLIGHT, DRIVE_MODE_CH,
        SHUTTER_SPEED_NAMES,
    )
    from fujixsdk._errors import BusyError, XSDKError

    print(f"\n{'='*60}")
    print("  FUJI SDK BURST SPEED TEST")
    print(f"{'='*60}")

    # Detect
    cameras = Camera.detect(sdk_path, IF_USB)
    if not cameras:
        print("No Fuji cameras found.")
        return
    print(f"Camera: {cameras[0].product} (S/N: {cameras[0].serial_no})")

    with Camera(sdk_path, cameras[0].device_name) as cam:
        # Drain pending images
        shoot_frames, total_frames = cam.get_buffer_capacity()
        if shoot_frames < total_frames:
            pending = total_frames - shoot_frames
            print(f"Draining {pending} pending image(s)...")
            for _ in range(pending):
                try:
                    cam.read_image_info()
                    cam.delete_image()
                except XSDKError:
                    break

        # Take control
        for attempt in range(10):
            try:
                cam.set_priority(PRIORITY_PC)
                break
            except BusyError:
                time.sleep(0.5)

        time.sleep(0.3)

        # Configure
        for attempt in range(5):
            try:
                cam.set_ae_mode(AE_OFF)
                break
            except BusyError:
                time.sleep(0.5)

        cam.set_shutter_speed(SHUTTER_1_8000)
        cam.set_iso(ISO_100)
        cam.set_wb_mode(WB_DAYLIGHT)
        cam.set_drive_mode(DRIVE_MODE_CH)

        speed, _ = cam.get_shutter_speed()
        speed_name = SHUTTER_SPEED_NAMES.get(speed, f"? ({speed})")
        shoot_frames, total_frames = cam.get_buffer_capacity()
        print(f"Shutter: {speed_name}  ISO: {cam.get_iso()}  "
              f"Aperture: f/{cam.get_aperture() / 100:.1f}")
        print(f"Drive: CH  Buffer: {shoot_frames}/{total_frames} frames")
        print()

        if shoot_frames < count:
            print(f"WARNING: buffer only has room for {shoot_frames} shots, "
                  f"reducing count from {count}")
            count = shoot_frames

        # --- Benchmark: burst_no_download ---
        shooter = EclipseShooter(cam)
        print(f"Firing {count} shots (burst_no_download, no interval)...")
        t0 = time.perf_counter()
        taken = shooter.burst_no_download(count, min_interval_ms=0)
        elapsed = time.perf_counter() - t0
        fps = taken / elapsed if elapsed > 0 else 0
        print(f"  Shots taken: {taken}")
        print(f"  Elapsed:     {elapsed:.3f}s")
        print(f"  Rate:        {fps:.1f} fps")
        print(f"  Per frame:   {elapsed/taken*1000:.1f}ms" if taken else "")

        # --- Benchmark: individual shoot_no_af loop ---
        # Drain buffer first
        time.sleep(0.5)
        shoot_frames, total_frames = cam.get_buffer_capacity()
        if shoot_frames < total_frames:
            drained = 0
            for _ in range(total_frames - shoot_frames):
                try:
                    cam.read_image_info()
                    cam.delete_image()
                    drained += 1
                except XSDKError:
                    break
            print(f"\nDrained {drained} images from buffer")
            time.sleep(0.3)

        shoot_frames, _ = cam.get_buffer_capacity()
        count2 = min(count, shoot_frames)
        print(f"\nFiring {count2} shots (raw shoot_no_af loop)...")
        t0 = time.perf_counter()
        taken2 = 0
        lap_times = []
        for i in range(count2):
            t_shot = time.perf_counter()
            sf, _ = cam.get_buffer_capacity()
            if sf == 0:
                print(f"  Buffer full at shot {i}")
                break
            cam.shoot_no_af()
            lap_times.append(time.perf_counter() - t_shot)
            taken2 += 1
        elapsed2 = time.perf_counter() - t0
        fps2 = taken2 / elapsed2 if elapsed2 > 0 else 0
        print(f"  Shots taken: {taken2}")
        print(f"  Elapsed:     {elapsed2:.3f}s")
        print(f"  Rate:        {fps2:.1f} fps")
        if lap_times:
            avg = sum(lap_times) / len(lap_times) * 1000
            mn = min(lap_times) * 1000
            mx = max(lap_times) * 1000
            print(f"  Per frame:   avg {avg:.1f}ms  min {mn:.1f}ms  max {mx:.1f}ms")

        # Return control
        cam.set_priority(PRIORITY_CAMERA)
        print("\nReturned priority to camera.")


# ---------------------------------------------------------------------------
# gphoto2 backend
# ---------------------------------------------------------------------------

def test_gphoto2(count: int):
    import gphoto2 as gp

    print(f"\n{'='*60}")
    print("  GPHOTO2 BURST SPEED TEST")
    print(f"{'='*60}")

    ctx = gp.gp_context_new()
    camera = gp.Camera()
    camera.init(ctx)

    # Get camera name
    abilities = camera.get_abilities()
    print(f"Camera: {abilities.model}")

    config = camera.get_config(ctx)

    # Set shutter to 1/8000
    try:
        ss = gp.check_result(gp.gp_widget_get_child_by_name(config, 'shutterspeed'))
        gp.gp_widget_set_value(ss, '1/8000')
        camera.set_config(config, ctx)
        print("Shutter: 1/8000")
    except gp.GPhoto2Error as e:
        print(f"Could not set shutter speed: {e}")

    # --- Test 1: gp_camera_capture (standard, waits for file) ---
    print(f"\nFiring {count} shots (gp_camera_capture — standard path)...")
    lap_times = []
    t0 = time.perf_counter()
    for i in range(count):
        t_shot = time.perf_counter()
        try:
            file_path = camera.capture(gp.GP_CAPTURE_IMAGE, ctx)
        except gp.GPhoto2Error as e:
            print(f"  Capture failed at shot {i}: {e}")
            break
        lap_times.append(time.perf_counter() - t_shot)
    elapsed = time.perf_counter() - t0
    taken = len(lap_times)
    fps = taken / elapsed if elapsed > 0 else 0
    print(f"  Shots taken: {taken}")
    print(f"  Elapsed:     {elapsed:.3f}s")
    print(f"  Rate:        {fps:.1f} fps")
    if lap_times:
        avg = sum(lap_times) / len(lap_times) * 1000
        mn = min(lap_times) * 1000
        mx = max(lap_times) * 1000
        print(f"  Per frame:   avg {avg:.1f}ms  min {mn:.1f}ms  max {mx:.1f}ms")

    # --- Test 2: gp_camera_trigger_capture (async, no download wait) ---
    print(f"\nFiring {count} shots (gp_camera_trigger_capture — async, no download)...")
    lap_times2 = []
    t0 = time.perf_counter()
    for i in range(count):
        t_shot = time.perf_counter()
        try:
            gp.check_result(gp.gp_camera_trigger_capture(camera, ctx))
            # Drain events to prevent queue backup
            while True:
                event_type, event_data = camera.wait_for_event(10, ctx)
                if event_type == gp.GP_EVENT_TIMEOUT:
                    break
        except gp.GPhoto2Error as e:
            print(f"  Trigger failed at shot {i}: {e}")
            break
        lap_times2.append(time.perf_counter() - t_shot)
    elapsed2 = time.perf_counter() - t0
    taken2 = len(lap_times2)
    fps2 = taken2 / elapsed2 if elapsed2 > 0 else 0
    print(f"  Shots taken: {taken2}")
    print(f"  Elapsed:     {elapsed2:.3f}s")
    print(f"  Rate:        {fps2:.1f} fps")
    if lap_times2:
        avg = sum(lap_times2) / len(lap_times2) * 1000
        mn = min(lap_times2) * 1000
        mx = max(lap_times2) * 1000
        print(f"  Per frame:   avg {avg:.1f}ms  min {mn:.1f}ms  max {mx:.1f}ms")

    # --- Test 3: trigger_capture with deferred event drain ---
    print(f"\nFiring {count} shots (trigger only, drain events after all shots)...")
    t0 = time.perf_counter()
    taken3 = 0
    for i in range(count):
        try:
            gp.check_result(gp.gp_camera_trigger_capture(camera, ctx))
            taken3 += 1
        except gp.GPhoto2Error as e:
            print(f"  Trigger failed at shot {i}: {e}")
            break
    fire_elapsed = time.perf_counter() - t0

    # Now drain all events
    drained = 0
    while drained < taken3:
        event_type, event_data = camera.wait_for_event(3000, ctx)
        if event_type == gp.GP_EVENT_TIMEOUT:
            break
        if event_type == gp.GP_EVENT_FILE_ADDED:
            drained += 1
    total_elapsed = time.perf_counter() - t0
    fps3 = taken3 / fire_elapsed if fire_elapsed > 0 else 0
    print(f"  Shots fired: {taken3}")
    print(f"  Fire time:   {fire_elapsed:.3f}s ({fps3:.1f} trigger/s)")
    print(f"  Total time:  {total_elapsed:.3f}s (including event drain)")

    camera.exit(ctx)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Camera burst speed test")
    parser.add_argument("--fuji", action="store_true", help="Test Fuji SDK only")
    parser.add_argument("--gphoto2", action="store_true", help="Test gphoto2 only")
    parser.add_argument("--count", type=int, default=SHOT_COUNT, help="Number of shots")
    args = parser.parse_args()

    # Default SDK path
    sdk_path = str(Path(__file__).resolve().parent / "SDK/SDK13410/REDISTRIBUTABLES/Linux/Linux64PC")

    # If neither flag, try both
    run_fuji = args.fuji or (not args.fuji and not args.gphoto2)
    run_gp = args.gphoto2 or (not args.fuji and not args.gphoto2)

    if run_fuji:
        # Ensure LD_LIBRARY_PATH for Fuji SDK
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from fujixsdk import ensure_ld_library_path
            if not ensure_ld_library_path(sdk_path):
                os.execvp(sys.executable, [sys.executable] + sys.argv)
            test_fuji_sdk(args.count, sdk_path)
        except ImportError:
            print("Fuji SDK not available, skipping")
        except Exception as e:
            print(f"Fuji SDK test failed: {e}")

    if run_gp:
        try:
            test_gphoto2(args.count)
        except ImportError:
            print("gphoto2 not available, skipping")
        except Exception as e:
            print(f"gphoto2 test failed: {e}")

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    print("Fuji SDK burst_no_download: fires shoot_no_af() in a tight loop")
    print("  - No config round-trips per frame (settings applied once)")
    print("  - No download wait (images stay on card)")
    print("  - Should hit mechanical shutter rate (~15 fps on X-T4 CH)")
    print()
    print("gphoto2 gp_camera_capture: full capture + file path return")
    print("  - USB round-trip per frame for file notification")
    print("  - ~1 fps typical (each capture blocks on PTP transaction)")
    print()
    print("gphoto2 trigger_capture: fire-and-forget trigger")
    print("  - Faster than capture but still PTP-limited")
    print("  - Deferred drain avoids per-shot event wait")


if __name__ == "__main__":
    main()
