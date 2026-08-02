#!/usr/bin/env python3
"""Query XSDK_GetModelDependentAPIInfo to see what the model library supports."""

import ctypes
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

from fujixsdk import Camera
from fujixsdk import _constants as C

# Key API codes we want to check
API_CODES = {
    0x1401: "CapProp",
    0x1402: "SetProp",
    0x1403: "GetProp",
    0x2216: "CapShutterPriorityMode",
    0x2217: "SetShutterPriorityMode",
    0x2218: "GetShutterPriorityMode",
    0x4261: "CapPerformanceSettings",
    0x4262: "SetPerformanceSettings",
    0x4263: "GetPerformanceSettings",
    0x2144: "CapLongExposureNR",
    0x2145: "SetLongExposureNR",
    0x2146: "GetLongExposureNR",
    0x3020: "CapCaptureDelay",
    0x3021: "SetCaptureDelay",
    0x3022: "GetCaptureDelay",
}


def setup_camera():
    cameras = Camera.detect(SDK_PATH, C.IF_USB)
    if not cameras:
        print("No cameras found")
        sys.exit(1)
    print(f"Camera: {cameras[0].product}")
    cam = Camera(SDK_PATH, cameras[0].device_name)
    for _ in range(10):
        try:
            cam.set_priority(C.PRIORITY_PC)
            break
        except:
            cam.drain_buffer()
            time.sleep(0.5)
    return cam


def test_get_model_dependent_api_info(cam):
    """Call XSDK_GetModelDependentAPIInfo for various API codes."""
    print(f"\n{'='*60}")
    print("  XSDK_GetModelDependentAPIInfo Results")
    print(f"{'='*60}")

    lib = cam._lib_inst

    # XSDK_GetModelDependentAPIInfo signature (from nm):
    # Likely: (handle, api_code, out_param_count, out_api_name)
    # Let's try to figure it out

    # First, check if it's exported
    try:
        func = lib.XSDK_GetModelDependentAPIInfo
        func.restype = ctypes.c_long
        print("  Function found!")
    except AttributeError:
        print("  XSDK_GetModelDependentAPIInfo not found!")
        return

    # Try calling it for various API codes
    for code, name in sorted(API_CODES.items()):
        # Try different parameter combinations
        param_count = ctypes.c_long()
        api_name = ctypes.create_string_buffer(256)

        # Signature might be: (handle, api_code, &param_count, api_name_buf)
        rc = func(
            cam._handle,
            ctypes.c_long(code),
            ctypes.byref(param_count),
            api_name
        )

        if rc == 0:
            api_name_str = api_name.value.decode() if api_name.value else "(empty)"
            print(f"  0x{code:04X} {name:30s}: params={param_count.value}, name='{api_name_str}'")
        else:
            print(f"  0x{code:04X} {name:30s}: error {rc}")


def main():
    print("=" * 60)
    print("  Model Dependent API Info Test")
    print("=" * 60)

    cam = setup_camera()
    try:
        test_get_model_dependent_api_info(cam)
    finally:
        cam.set_priority(C.PRIORITY_CAMERA)
        cam.close()


if __name__ == "__main__":
    main()
