"""Minimal Fuji SDK detection probe for macOS.

Answers one question with no campaign wrapped around it: can XSDK_Detect see
the X-T4 from THIS process?  Run it locally in Terminal on the Mac (not over
SSH — the SDK's PTP transport rides ImageCaptureCore, which may need the GUI
session), with the camera on USB TETHER SHOOTING FIXED, and watch the screen
for any macOS permission prompt while it runs.

    .venv/bin/python scripts/sdk_probe.py
"""

import ctypes
import platform
import subprocess
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/sdk_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import find_fuji_sdk_path
from fujixsdk.camera import Camera
from fujixsdk import _constants as C


def usb_entry() -> str:
    result = subprocess.run(["system_profiler", "SPUSBDataType"],
                            capture_output=True, text=True, timeout=20)
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if "fuji" in line.lower() or "x-t" in line.lower():
            return "\n".join(lines[max(0, index - 6):index + 6])
    return "(no Fuji device on the USB bus)"


def main() -> None:
    print("=== USB bus ===")
    print(usb_entry())

    print("\n=== ptpcamerad ===")
    alive = subprocess.run(["pgrep", "-l", "ptpcamerad"],
                           capture_output=True, text=True).stdout.strip()
    print(alive or "not running (it respawns when a camera talks ICA)")

    sdk_root = find_fuji_sdk_path()
    print(f"\n=== SDK ===\n{sdk_root}")
    if platform.system() == "Darwin":
        bundle_dir = Path(sdk_root)
        hits = sorted(bundle_dir.rglob("FTLPTP.dylib"))
        if hits:
            for name in ("FTLPTP.dylib", "FTLPTPIP.dylib"):
                path = hits[0].parent / name
                ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
                print(f"preloaded {path.name}")

    print("\n=== detect, ptpcamerad left alone ===")
    for attempt in range(1, 4):
        try:
            cameras = Camera.detect(sdk_root, interface=C.IF_USB)
        except Exception as exc:
            print(f"attempt {attempt}: raised {exc!r}")
            time.sleep(2.0)
            continue
        print(f"attempt {attempt}: {len(cameras)} camera(s)")
        for info in cameras:
            print("   ", info)
        if cameras:
            print("\nSUCCESS — the SDK sees the body from this context.")
            return
        time.sleep(2.0)

    print("\nStill zero.  While this window was up, did macOS show ANY permission")
    print("prompt (accessing files on a connected camera / removable device)?")
    print("Also confirm on the camera: wrench menu > CONNECTION SETTING >")
    print("PC CONNECTION MODE = USB TETHER SHOOTING FIXED (not AUTO, not card")
    print("reader), then power the camera off and on with the cable in.")


if __name__ == "__main__":
    main()
