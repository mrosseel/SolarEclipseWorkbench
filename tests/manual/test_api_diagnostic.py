#!/usr/bin/env python3
"""Diagnostic script to understand why SetProp fails with ApiNotFound.

This traces:
1. What GetDeviceInfoEx reports as supported APIs
2. Which model library is being loaded
3. What happens when we call SetProp vs direct APIs
"""

import ctypes
import os
import platform
import sys
import time
from pathlib import Path

_base = Path(__file__).resolve().parent / "SDK/SDK13410/REDISTRIBUTABLES"
if platform.system() == "Darwin":
    SDK_PATH = str(_base / "macOS/SDK_13400")
else:
    SDK_PATH = str(_base / "Linux/Linux64PC")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fujixsdk import Camera, ensure_ld_library_path
from fujixsdk import _constants as C
from fujixsdk._errors import XSDKError

# Ensure library path
if platform.system() != "Darwin" and not ensure_ld_library_path(SDK_PATH):
    os.execvp(sys.executable, [sys.executable] + sys.argv)

# API codes from headers
API_CODE_GetDeviceInfoEx = 0x1047
API_CODE_SetProp = 0x1402
API_CODE_GetProp = 0x1403
API_CODE_SetShutterPriorityMode = 0x2217
API_CODE_GetShutterPriorityMode = 0x2218
API_CODE_SetPerformanceSettings = 0x4262
API_CODE_GetPerformanceSettings = 0x4263

# Error codes
ERR_NAMES = {
    0: "OK",
    0x00001001: "Sequence",
    0x00001002: "Param",
    0x00001003: "InvalidCamera",
    0x00001004: "LoadLib",
    0x00001005: "Unsupported",
    0x00001006: "Busy",
    0x00001012: "NoModelModule",
    0x00001013: "ApiNotFound",
    0x00001014: "ApiMismatch",
}


def err_name(code):
    return ERR_NAMES.get(code, f"0x{code:08X}")


def get_error(cam):
    _, err = cam.get_error()
    return err


def setup_camera():
    cameras = Camera.detect(SDK_PATH, C.IF_USB)
    if not cameras:
        print("No cameras found")
        sys.exit(1)
    print(f"Camera: {cameras[0].product}")
    cam = Camera(SDK_PATH, cameras[0].device_name)
    # Get to PC priority
    for _ in range(20):
        try:
            cam.set_priority(C.PRIORITY_PC)
            break
        except:
            cam.drain_buffer()
            time.sleep(0.5)
    time.sleep(0.3)
    return cam


def test_get_device_info_ex(cam):
    """Check what APIs GetDeviceInfoEx reports as supported."""
    print(f"\n{'='*60}")
    print("  GetDeviceInfoEx - Supported API codes")
    print(f"{'='*60}")

    lib = cam._lib_inst

    # Import DeviceInformation structure
    from fujixsdk._structures import DeviceInformation

    # GetDeviceInfoEx signature: (handle, pDevInfo, plNumAPICode, plAPICode)
    dev_info = DeviceInformation()
    num_codes = ctypes.c_long()
    api_codes = (ctypes.c_long * 512)()

    rc = lib.XSDK_GetDeviceInfoEx(
        cam._handle,
        ctypes.byref(dev_info),
        ctypes.byref(num_codes),
        api_codes
    )
    if rc != C.COMPLETE:
        print(f"  GetDeviceInfoEx failed: {err_name(get_error(cam))}")
        return []

    print(f"  Supported API count: {num_codes.value}")
    codes = [api_codes[i] for i in range(num_codes.value)]

    # Check for key codes
    key_codes = {
        0x1402: "SetProp",
        0x1403: "GetProp",
        0x1401: "CapProp",
        0x2217: "SetShutterPriorityMode",
        0x2218: "GetShutterPriorityMode",
        0x4262: "SetPerformanceSettings",
        0x4263: "GetPerformanceSettings",
        0x2145: "SetLongExposureNR",
        0x2146: "GetLongExposureNR",
        0x3021: "SetCaptureDelay",
        0x3022: "GetCaptureDelay",
        0x1377: "SetDriveMode",
        0x1378: "GetDriveMode",
        0x1379: "CapDriveMode",
    }

    print("\n  Key API codes:")
    for code, name in sorted(key_codes.items()):
        status = "YES" if code in codes else "NO"
        print(f"    0x{code:04X} {name:30s}: {status}")

    return codes


def test_direct_api_calls(cam):
    """Test calling APIs directly (not through SetProp)."""
    print(f"\n{'='*60}")
    print("  Direct API calls (XSDK_SetDriveMode, etc.)")
    print(f"{'='*60}")

    lib = cam._lib_inst

    # Test GetDriveMode
    val = ctypes.c_long()
    rc = lib.XSDK_GetDriveMode(cam._handle, ctypes.byref(val))
    if rc == C.COMPLETE:
        print(f"  GetDriveMode: OK, value=0x{val.value:04X}")
    else:
        print(f"  GetDriveMode: {err_name(get_error(cam))}")

    # Test SetDriveMode with Single
    rc = lib.XSDK_SetDriveMode(cam._handle, ctypes.c_long(C.DRIVE_MODE_S))
    if rc == C.COMPLETE:
        print(f"  SetDriveMode(S): OK")
    else:
        print(f"  SetDriveMode(S): {err_name(get_error(cam))}")


def test_setprop_calls(cam):
    """Test calling APIs through SetProp interface."""
    print(f"\n{'='*60}")
    print("  SetProp/GetProp interface calls")
    print(f"{'='*60}")

    lib = cam._lib_inst

    # Test GetProp for PerformanceSettings
    val = ctypes.c_long()
    rc = lib.XSDK_GetProp(
        cam._handle,
        ctypes.c_long(API_CODE_GetPerformanceSettings),
        ctypes.c_long(1),  # API_PARAM = 1 for X-T4
        ctypes.byref(val)
    )
    if rc == C.COMPLETE:
        print(f"  GetProp(PerformanceSettings): OK, value=0x{val.value:04X}")
    else:
        err = get_error(cam)
        print(f"  GetProp(PerformanceSettings): {err_name(err)}")

    # Test GetProp for ShutterPriorityMode
    rc = lib.XSDK_GetProp(
        cam._handle,
        ctypes.c_long(API_CODE_GetShutterPriorityMode),
        ctypes.c_long(2),  # API_PARAM = 2 for X-T4
        ctypes.c_long(1),  # item = AFS
        ctypes.byref(val)
    )
    if rc == C.COMPLETE:
        print(f"  GetProp(ShutterPriorityMode): OK, value=0x{val.value:04X}")
    else:
        err = get_error(cam)
        print(f"  GetProp(ShutterPriorityMode): {err_name(err)}")


def test_model_library_detection(cam):
    """Try to figure out which model library is being used."""
    print(f"\n{'='*60}")
    print("  Model Library Detection")
    print(f"{'='*60}")

    # Get device info
    info = cam.device_info
    print(f"  Model: {info.strProduct.decode()}")
    print(f"  Serial: {info.strSerialNo.decode()}")
    print(f"  Device ID: {info.bDeviceId}")
    print(f"  Device Name: {info.strDeviceName.decode()}")

    # The device_id might indicate which FF*API to use
    # Let's also check what bundles exist
    if platform.system() == "Darwin":
        bundle_path = Path(SDK_PATH)
        bundles = sorted(bundle_path.glob("FF*.bundle"))
        print(f"\n  Available FF bundles: {len(bundles)}")
        for b in bundles[:5]:
            print(f"    {b.name}")
        print(f"    ... and {len(bundles) - 5} more")


def main():
    print("=" * 60)
    print("  Fujifilm SDK SetProp Diagnostic")
    print("=" * 60)

    cam = setup_camera()
    try:
        test_model_library_detection(cam)
        supported_codes = test_get_device_info_ex(cam)
        test_direct_api_calls(cam)
        test_setprop_calls(cam)

        print(f"\n{'='*60}")
        print("  Conclusion")
        print(f"{'='*60}")

        if 0x1402 not in supported_codes:
            print("  → SetProp (0x1402) NOT in supported API list!")
            print("  → This camera may not support generic SetProp interface")
        if 0x2217 in supported_codes:
            print("  → SetShutterPriorityMode (0x2217) IS supported")
            print("  → But must be called through SetProp interface")
            print("  → If SetProp fails, SDK/model library may have bug")

    finally:
        cam.set_priority(C.PRIORITY_CAMERA)
        cam.close()


if __name__ == "__main__":
    main()
