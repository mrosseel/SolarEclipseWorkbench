#!/usr/bin/env bash
# Launch Solar Eclipse Workbench with NixOS library paths
#
# Usage:
#   ./run.sh gui [-s] [--virtual-camera] [-lon X] [-lat Y] [-alt Z] [-d DATE]
#   ./run.sh wizard
#   ./run.sh <any python args>
set -euo pipefail
cd "$(dirname "$0")"

export LD_LIBRARY_PATH="/run/current-system/sw/share/nix-ld/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Auto-detect Fuji SDK library path
fuji_sdk_lib=$(find SDK -name 'XAPI.so' -o -name 'libXAPI.so' 2>/dev/null | head -1)
if [[ -n "$fuji_sdk_lib" ]]; then
    export LD_LIBRARY_PATH="$(dirname "$fuji_sdk_lib"):$LD_LIBRARY_PATH"
fi

cmd="${1:-wizard}"
shift 2>/dev/null || true

case "$cmd" in
    wizard) exec .venv/bin/python -m solareclipseworkbench.wizard "$@" ;;
    gui)    exec .venv/bin/python -m solareclipseworkbench.gui "$@" ;;
    *)      exec .venv/bin/python "$cmd" "$@" ;;
esac
