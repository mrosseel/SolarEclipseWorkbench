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
#   ./run.sh <any python args>
set -euo pipefail
cd "$(dirname "$0")"

# NixOS nix-ld libs + Fuji SDK redistributables.  The Fuji SDK dlopen()s
# libusb-1.0.so by name, so the SDK lib dir must be on LD_LIBRARY_PATH.
_ld="/run/current-system/sw/share/nix-ld/lib"
_fuji_sdk="SDK/SDK13410/REDISTRIBUTABLES/Linux/Linux64PC"
[ -d "$_fuji_sdk" ] && _ld="$_ld:$PWD/$_fuji_sdk"
export LD_LIBRARY_PATH="$_ld${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

cmd="${1:-wizard}"
shift 2>/dev/null || true

case "$cmd" in
    wizard) exec .venv/bin/python -m solareclipseworkbench.wizard "$@" ;;
    gui)    exec .venv/bin/python -m solareclipseworkbench.gui "$@" ;;
    sew)    exec .venv/bin/python -m solareclipseworkbench.sew "$@" ;;
    *)      exec .venv/bin/python "$cmd" "$@" ;;
esac
