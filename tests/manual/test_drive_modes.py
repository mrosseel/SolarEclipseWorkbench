#!/usr/bin/env python3
"""Check available drive modes and try to set continuous high."""

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

cameras = Camera.detect(str(SDK_PATH), C.IF_USB)
if not cameras:
    print("No cameras found")
    sys.exit(1)

print(f"Found: {cameras[0].product}")
cam = Camera(str(SDK_PATH), cameras[0].device_name)

for _ in range(10):
    try:
        cam.set_priority(C.PRIORITY_PC)
        break
    except:
        cam.drain_buffer()
        time.sleep(0.5)

cam.drain_buffer()
time.sleep(0.3)

handle = cam._handle
lib = cam._lib_inst

# XSDK_GetDriveMode
print("\n" + "="*60)
print("  Drive Mode Analysis")
print("="*60)

# Get current
val = ctypes.c_long()
rc = lib.XSDK_GetDriveMode(handle, ctypes.byref(val))
print(f"Current drive mode: 0x{val.value:04X} (rc={rc})")

# Get capabilities
print("\nCapabilities:")
# Define the function signature properly
lib.XSDK_CapDriveMode.restype = ctypes.c_long
lib.XSDK_CapDriveMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long), ctypes.c_void_p]

num = ctypes.c_long()
modes = (ctypes.c_long * 64)()
rc = lib.XSDK_CapDriveMode(handle, ctypes.byref(num), modes)
print(f"  rc={rc}, count={num.value}")

DRIVE_MODE_NAMES = {
    0x0001: "SINGLE",
    0x0002: "CH (Continuous High)",
    0x0003: "CL (Continuous Low)",
    0x0004: "SELF_TIMER",
    0x0005: "BRACKETING",
    0x0006: "MULTI_EXPOSURE",
    0x0007: "REMOTE",
    0x0100: "MOVIE",
    0x1000: "CL (Model-specific)",
    0x10F0: "CH (Model-specific)",
}

if rc == 0 and num.value > 0:
    for i in range(num.value):
        mode = modes[i]
        name = DRIVE_MODE_NAMES.get(mode, "UNKNOWN")
        print(f"    0x{mode:04X} = {name}")

# Try setting to CH
print("\n" + "="*60)
print("  Trying to Set Continuous Modes")
print("="*60)

# Try standard CH (0x0002)
for mode in [0x0002, 0x0003, 0x10F0, 0x1000]:
    name = DRIVE_MODE_NAMES.get(mode, "UNKNOWN")
    print(f"Setting 0x{mode:04X} ({name})...")
    rc = lib.XSDK_SetDriveMode(handle, ctypes.c_long(mode))
    lib.XSDK_GetDriveMode(handle, ctypes.byref(val))
    print(f"  rc={rc}, now=0x{val.value:04X}")

# If any mode succeeded, test shooting
lib.XSDK_GetDriveMode(handle, ctypes.byref(val))
current = val.value
print(f"\nFinal drive mode: 0x{current:04X}")

if current in [0x0002, 0x0003, 0x10F0, 0x1000]:
    print("\n" + "="*60)
    print("  Continuous Mode Shooting Test")
    print("="*60)

    cam.set_ae_mode(C.AE_OFF)
    cam.set_shutter_speed(C.SHUTTER_1_1000)

    avail_before, total = cam.get_buffer_capacity()
    print(f"Buffer before: {total - avail_before}")

    # S1ON and hold
    shot_opt = ctypes.c_long()
    af_status = ctypes.c_long()

    lib.XSDK_Release(handle, ctypes.c_long(C.RELEASE_S1ON), ctypes.c_long(1),
                     ctypes.byref(shot_opt), ctypes.byref(af_status))
    time.sleep(0.1)

    # Multiple S2
    shots = 0
    start = time.time()
    for i in range(5):
        rc = lib.XSDK_Release(handle, ctypes.c_long(C.RELEASE_S2), ctypes.c_long(1),
                              ctypes.byref(shot_opt), ctypes.byref(af_status))
        if rc == 0:
            shots += 1
        time.sleep(0.05)

    lib.XSDK_Release(handle, ctypes.c_long(C.RELEASE_N_S1OFF), ctypes.c_long(1),
                     ctypes.byref(shot_opt), ctypes.byref(af_status))

    elapsed = time.time() - start
    time.sleep(0.5)
    avail_after, total = cam.get_buffer_capacity()

    print(f"Shots: {shots}/5 in {elapsed:.2f}s")
    print(f"Buffer: {total - avail_after}")

# Cleanup
cam.drain_buffer()
cam.set_priority(C.PRIORITY_CAMERA)
cam.close()
print("\nDone")
