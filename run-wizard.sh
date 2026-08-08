#!/usr/bin/env bash
# The setup wizard, which builds an eclipse script from scratch.
#
# Its own launcher so it sits beside run-real.sh and run-sim.sh rather than
# hiding behind an argument.  It is a setup tool, not a way into a run: on a
# night when the schedule already exists you want ./run-real.sh, and a bare
# ./run.sh opens the workbench for the same reason.
set -euo pipefail
cd "$(dirname "$0")"
exec ./run.sh wizard "$@"
