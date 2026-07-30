#!/usr/bin/env bash
# Launch Solar Eclipse Workbench with the library paths needed by the native
# Fujifilm Shooting SDK.
#
# Running via `python -m` from the repo root puts the repo root on sys.path so
# the top-level `fujixsdk` package (and the SDK libraries under SDK/) are found.
#
# Usage:
#   ./run.sh gui [-s] [--virtual-camera] [-lon X] [-lat Y] [-alt Z] [-d DATE]
#   ./run.sh wizard
#   ./run.sh hardware [--simulate]      # relay + mount bench console
#   ./run.sh <any python args>
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in
    Darwin)
        # macOS loads the SDK's .bundle files through @rpath/dlopen, so there is
        # no library path to set.  DYLD_* would be stripped by SIP on exec
        # anyway, which is why the SDK ships bundles rather than plain dylibs.
        #
        # ptpcamerad grabs USB cameras the moment they appear; the Fuji and
        # gphoto2 paths both kill it when they need the device.
        ;;
    *)
        # NixOS nix-ld libs + Fuji SDK redistributables.  The Fuji SDK dlopen()s
        # libusb-1.0.so by name, so the SDK lib dir must be on LD_LIBRARY_PATH.
        _ld="/run/current-system/sw/share/nix-ld/lib"
        _fuji_sdk="SDK/SDK13410/REDISTRIBUTABLES/Linux/Linux64PC"
        [ -d "$_fuji_sdk" ] && _ld="$_ld:$PWD/$_fuji_sdk"
        export LD_LIBRARY_PATH="$_ld${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        ;;
esac

cmd="${1:-wizard}"
shift 2>/dev/null || true

case "$cmd" in
    wizard)   exec .venv/bin/python -m solareclipseworkbench.wizard "$@" ;;
    gui)      exec .venv/bin/python -m solareclipseworkbench.gui "$@" ;;
    sew)      exec .venv/bin/python -m solareclipseworkbench.sew "$@" ;;
    hardware) exec .venv/bin/python -m solareclipseworkbench.hardware_console "$@" ;;
    *)        exec .venv/bin/python "$cmd" "$@" ;;
esac
