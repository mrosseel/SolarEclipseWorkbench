#!/usr/bin/env bash
# Solar Eclipse Workbench against the real hardware, on a machine kept awake.
#
# This is the launcher for a rehearsal or for eclipse day.  Sleep suspends the
# USB bus and neither device survives it: the relay's next write fails with
# "[Errno 6] Device not configured" and the camera's SDK handle is dead, with
# no warning either way (6 August, mid-run).  caffeinate wraps the app, so the
# machine sleeps normally again as soon as it exits.
#
# Any extra arguments are passed to the GUI:
#   ./run-real.sh -lon 4.35 -lat 50.85 -alt 60 -d 2026-08-12
set -euo pipefail
cd "$(dirname "$0")"
exec ./run.sh gui "$@"
