#!/usr/bin/env python3
"""Quick integration test — connect to X-T4, read info, take a photo."""

import os
import sys
from pathlib import Path

SDK_LIBS = Path(__file__).resolve().parent / "SDK/SDK13410/REDISTRIBUTABLES/Linux/Linux64PC"
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The SDK needs LD_LIBRARY_PATH set before the process starts.
# If it's missing, set it and re-exec.
from fujixsdk import ensure_ld_library_path
if not ensure_ld_library_path(str(SDK_LIBS)):
    os.execvp(sys.executable, [sys.executable] + sys.argv)

from fujixsdk import Camera, SHUTTER_SPEED_NAMES
from fujixsdk import (
    IF_USB, PRIORITY_PC, PRIORITY_CAMERA, AE_OFF,
    SHUTTER_1_250, ISO_100, WB_DAYLIGHT, DRIVE_MODE_S,
)

print("=== Fujifilm X SDK Test ===\n")

# Step 1: Detect cameras
print(f"SDK path: {SDK_LIBS}")
print("Detecting cameras on USB...")
cameras = Camera.detect(str(SDK_LIBS), IF_USB)

if not cameras:
    print("No cameras found. Is the X-T4 connected via USB and in the right mode?")
    print("  - Camera should be ON")
    print("  - USB mode should be set to 'USB TETHER SHOOTING AUTO'")
    sys.exit(1)

for i, cam in enumerate(cameras):
    print(f"  [{i}] {cam.product} (S/N: {cam.serial_no}) via {cam.framework}")
    print(f"      device_name: {cam.device_name!r}")

# Step 2: Connect
print(f"\nConnecting to {cameras[0].product}...")
with Camera(str(SDK_LIBS), cameras[0].device_name) as cam:
    # Check camera mode
    mode = cam.camera_mode
    MODE_TETHER = 0x0001
    MODE_STILL = 0x0002
    MODE_MOV = 0x0004
    caps = []
    if mode & MODE_TETHER: caps.append("Tether")
    if mode & MODE_STILL: caps.append("Still")
    if mode & MODE_MOV: caps.append("Movie")
    print(f"  Mode:     {mode:#06x} ({', '.join(caps) if caps else 'none'})")

    if not (mode & MODE_TETHER):
        print("\n*** Camera is NOT in tether shooting mode! ***")
        print("On the X-T4, go to: Menu > Setup > Connection Setting > USB Mode")
        print("  Set to: USB TETHER SHOOTING AUTO")
        print("Then reconnect the USB cable and re-run this test.")
        sys.exit(1)

    # Device info
    info = cam.device_info
    print(f"  Model:    {info.product}")
    print(f"  Serial:   {info.serial_no}")
    try:
        print(f"  Firmware: {cam.firmware_version}")
    except Exception as e:
        print(f"  Firmware: (not available: {e})")
    print(f"  SDK ver:  {cam.get_sdk_version()}")

    # Lens info
    try:
        lens = cam.lens_info
        print(f"  Lens:     {lens.product_name} ({lens.model})")
    except Exception as e:
        print(f"  Lens:     (not available: {e})")

    import time
    from fujixsdk._errors import BusyError, XSDKError

    # Drain any pending images from previous sessions
    shoot_frames, total_frames = cam.get_buffer_capacity()
    if shoot_frames < total_frames:
        pending = total_frames - shoot_frames
        print(f"\n  Draining {pending} pending image(s) from buffer...")
        for _ in range(pending):
            try:
                cam.read_image_info()
                cam.delete_image()
            except XSDKError:
                break

    # Take control (retry on BUSY — camera may need time after open)
    print("\nSetting PC priority...")
    for attempt in range(10):
        try:
            cam.set_priority(PRIORITY_PC)
            break
        except BusyError:
            print(f"  Camera busy, retrying ({attempt+1}/10)...")
            time.sleep(1)
    print(f"  Priority: {'PC' if cam.get_priority() == PRIORITY_PC else 'Camera'}")

    time.sleep(0.5)

    # Configure exposure
    print("\nConfiguring exposure:")
    for attempt in range(5):
        try:
            cam.set_ae_mode(AE_OFF)
            break
        except BusyError:
            time.sleep(1)
    cam.set_shutter_speed(SHUTTER_1_250)
    cam.set_iso(ISO_100)
    cam.set_wb_mode(WB_DAYLIGHT)
    cam.set_drive_mode(DRIVE_MODE_S)

    speed, bulb = cam.get_shutter_speed()
    speed_name = SHUTTER_SPEED_NAMES.get(speed, f"? ({speed})")
    print(f"  AE mode:  Manual")
    print(f"  Shutter:  {speed_name}")
    print(f"  ISO:      {cam.get_iso()}")
    print(f"  Aperture: f/{cam.get_aperture() / 100:.1f}")

    # Buffer capacity
    shoot_frames, total_frames = cam.get_buffer_capacity()
    print(f"  Buffer:   {shoot_frames}/{total_frames} frames")

    # Take a test shot
    print("\nTaking test photo (no AF)...")
    shot_opt, af_status = cam.shoot_no_af()
    print(f"  Shot result: opt={shot_opt}, af={af_status}")

    # Wait for image to be ready in the buffer
    print("Waiting for image...")
    for wait in range(30):
        time.sleep(0.5)
        try:
            img_info = cam.read_image_info()
            if img_info.data_size > 0:
                break
        except XSDKError:
            pass
    else:
        print("  Timed out waiting for image!")
        sys.exit(1)

    print("Reading image info...")
    print(f"  File:     {img_info.internal_name}")
    print(f"  Format:   {img_info.format}")
    print(f"  Size:     {img_info.data_size} bytes ({img_info.width}x{img_info.height})")

    out_path = Path(__file__).parent / f"test_{img_info.internal_name}"
    print(f"  Downloading to {out_path}...")
    cam.download_image(out_path)
    cam.delete_image()
    print(f"  Saved! ({out_path.stat().st_size} bytes)")

    # Return control
    cam.set_priority(PRIORITY_CAMERA)
    print("\nReturned priority to camera.")

print("\nDone!")
