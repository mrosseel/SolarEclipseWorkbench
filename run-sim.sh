#!/usr/bin/env bash
# Solar Eclipse Workbench in simulator mode, on a machine kept awake.
#
# Same caffeinate wrapper as the real launcher: a rehearsal that runs the
# clock forward is exactly as long as a real run, and a sleeping Mac ends it
# the same way - the relay gone from USB, the camera's SDK session dead.
#
# Any extra arguments are passed to the GUI:
#   ./run-sim.sh --virtual-camera
set -euo pipefail
cd "$(dirname "$0")"
exec ./run.sh gui --sim "$@"
