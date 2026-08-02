# Manual test scripts

Fuji SDK probes, run by hand with a body on USB.  `pytest` is configured to skip
this directory — they need hardware, and the SDK and Qt do not share a process
politely, so collecting them alongside the suite crashes the interpreter.  Run
one at a time:

    ./run.sh tests/manual/test_burst_speed.py

| script | what it answers |
|---|---|
| `test_burst_speed.py` | the SDK frame-rate ceiling — where the 1.8 fps figure comes from |
| `test_drive_modes.py` | which drive modes the body reports, and whether continuous can be set |
| `test_api_diagnostic.py` | why `SetProp` returns ApiNotFound |
| `test_model_api_info.py` | what the model-dependent library supports |
| `test_optimized_burst_v2.py` | the private `SDK_*` calls, including the `SetPerformanceSettings(5)` that BOOST rests on |

These diagnose a path the eclipse no longer uses: the bench closed SDK-triggered
shooting (17/17 `0x1008` with the drive dial on CH) and the relay does all the
triggering now.  Kept for the next time the SDK misbehaves, not because anything
depends on them.  See `xt4-relay-bench-2026-08-01.md`.

The script-style checks over the scheduling and exposure maths stay at the
repository root, where upstream keeps them.
