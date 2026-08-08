"""Why does the SDK backup shutter path fail with 0x1008?

The relay fires the shutter through the remote jack.  When no relay is
connected, ``FujiCamera.capture`` uses the SDK instead: ``shoot_no_af()``.
That path is the only backup if the relay fails during totality.

On 8 August the backup path failed.  Two partial frames failed with
XSDK error 0x00001008 "Shooting error".  Each failure did a reconnect.  The
reconnect reported success.  The next shot failed again.  A bracket took all
3 frames 36 seconds later.

Two explanations fit that log.  This probe separates them:

  1. The drive mode.  The body must be on CL for the relay bursts.  A single
     SDK release in a continuous drive mode can behave differently.
  2. The settle time.  ``shoot_no_af`` waits 0.15 s between S1 and S2.  The
     code comment calls 0.15 s "the sweet spot" but records no drive mode.

The probe also shoots immediately after connect.  The failures came 14 s and
26 s after the camera registered, so a cold session is a third candidate.

No relay is necessary.  Disconnect the relay, or leave it: the probe does not
use it.  Close the workbench first - the SDK permits one session.

    .venv/bin/python scripts/sdk_shoot_probe.py --dial CL
    .venv/bin/python scripts/sdk_shoot_probe.py --dial single

Run it once for each drive dial position.  Compare the error counts.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import (
    detect_fuji_cameras,
    find_fuji_sdk_path,
    maybe_reexec_for_fuji_sdk,
)

maybe_reexec_for_fuji_sdk()

import ctypes

from fujixsdk import _constants as C
from fujixsdk._errors import XSDKError

from bench_log import tee_console

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

#: Frames per phase.  Enough to show a rate, short enough to keep the queue
#: below the 32-slot limit between drains.
SHOTS = 10


def sdk_of(camera):
    return getattr(camera, "_sdk", None) or getattr(camera, "_sdk_cam", None)


def release(sdk, mode, shots=1):
    """One raw XSDK_Release.  Returns None on success, or the error code."""
    shot_opt = ctypes.c_long(shots)
    af_status = ctypes.c_long()
    rc = sdk._lib_inst.XSDK_Release(
        sdk._handle, ctypes.c_long(mode),
        ctypes.byref(shot_opt), ctypes.byref(af_status))
    if rc == C.COMPLETE:
        return None
    _api, err = sdk.get_error()
    return err


def shoot_with_settle(sdk, settle_s):
    """The production sequence, with the settle under our control.

    This is what ``shoot_no_af`` does: S1ON, wait, then S2_S1OFF.  S1ON is
    allowed to fail - it reports AF failure when the lens is manual - but it
    still moves the body into the S1 state that S2 needs.
    """
    release(sdk, C.RELEASE_S1ON)
    time.sleep(settle_s)
    return release(sdk, C.RELEASE_S2_S1OFF)


def phase(name, sdk, camera, action, shots=SHOTS, gap_s=1.0):
    """Fire ``shots`` frames and count the failures."""
    print(f"\n{name}")
    errors, codes, times = 0, {}, []
    for n in range(1, shots + 1):
        started = time.monotonic()
        try:
            err = action()
        except XSDKError as exc:
            err = getattr(exc, "code", -1)
        except Exception as exc:                       # noqa: BLE001
            print(f"   shot {n:2d}  {RED}exception {exc}{RESET}")
            err = -1
        ms = (time.monotonic() - started) * 1000
        times.append(ms)
        if err is None:
            print(f"   shot {n:2d}  {GREEN}ok{RESET}      {ms:6.0f} ms")
        else:
            errors += 1
            key = f"0x{err:08X}" if isinstance(err, int) and err >= 0 else str(err)
            codes[key] = codes.get(key, 0) + 1
            mark = "SHOOT ERROR" if err == C.ERRCODE_SHOOT_ERROR else "FAILED"
            print(f"   shot {n:2d}  {RED}{mark}{RESET} {ms:6.0f} ms  {key}")
        time.sleep(gap_s)
        if n % 5 == 0:
            # Keep the transfer queue clear; a full queue stops the body and
            # would be read here as a shooting error it is not.
            try:
                camera.drain()
            except Exception:                          # noqa: BLE001
                pass
    ok = shots - errors
    colour = GREEN if errors == 0 else RED
    print(f"   -> {colour}{ok}/{shots} succeeded{RESET}"
          + (f", codes {codes}" if codes else ""))
    return {"phase": name, "shots": shots, "ok": ok, "errors": errors,
            "codes": codes, "median_ms": round(sorted(times)[len(times) // 2])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dial", required=True,
                    help="drive dial position you have set, for the record: CL or single")
    ap.add_argument("--shots", type=int, default=SHOTS)
    args = ap.parse_args()

    tee_console("sdk_shoot_probe")
    print(f"Drive dial: {args.dial}.  Shutter dial on T.  Workbench closed.")
    print(f"The shutter fires about {args.shots * 4} times.  No relay is used.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}The SDK sees no camera.  Power cycle it with the cable in.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk = sdk_of(camera)
    print(f"{GREEN}Connected:{RESET} {name}")

    out = []
    try:
        # Cold, exactly as the failing run was: the first frame came 14 s after
        # the camera registered.
        out.append(phase("1. cold session, production settle 0.15 s",
                         sdk, camera, lambda: shoot_with_settle(sdk, 0.15),
                         args.shots))

        print("\n   warming up for 30 s ...")
        time.sleep(30.0)

        out.append(phase("2. warm session, production settle 0.15 s",
                         sdk, camera, lambda: shoot_with_settle(sdk, 0.15),
                         args.shots))
        out.append(phase("3. warm session, longer settle 0.40 s",
                         sdk, camera, lambda: shoot_with_settle(sdk, 0.40),
                         args.shots))
        out.append(phase("4. warm session, the production call itself",
                         sdk, camera, lambda: sdk.shoot_no_af() and None,
                         args.shots))
    finally:
        try:
            camera.drain()
        except Exception:                              # noqa: BLE001
            pass
        camera.disconnect()

    path = Path.cwd() / f"sdk_shoot_probe_{args.dial}_{int(time.time())}.json"
    path.write_text(json.dumps({"dial": args.dial, "phases": out}, indent=2))

    print("\n" + "=" * 60)
    for r in out:
        colour = GREEN if r["errors"] == 0 else RED
        print(f"{colour}{r['ok']}/{r['shots']}{RESET}  {r['phase']}  "
              f"({r['median_ms']} ms median)")

    cold, warm, longer = out[0], out[1], out[2]
    print()
    if cold["errors"] and not warm["errors"]:
        print(f"{YELLOW}The cold session fails and the warm session does not."
              f"{RESET}  Give the camera time after it registers, before the "
              f"first frame.")
    elif warm["errors"] and not longer["errors"]:
        print(f"{YELLOW}0.15 s is too short and 0.40 s is not.{RESET}  Increase "
              f"the settle in shoot_no_af.")
    elif not any(r["errors"] for r in out):
        print(f"{GREEN}No failure on the {args.dial} dial.{RESET}  Run the probe "
              f"again on the other dial position and compare.")
    else:
        print(f"{RED}Failures in more than one phase.{RESET}  Compare this file "
              f"with the run on the other dial position.")
    print(f"\n{path}")


if __name__ == "__main__":
    main()
