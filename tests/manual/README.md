# Manual test scripts

Run by hand, never by the suite.  `pytest` is configured to skip this directory:
some of these need a camera or a relay board on USB, and the ones that do not
still crash the interpreter when collected together (Qt and the SDK do not share
a process politely).  Run one at a time:

    .venv/bin/python tests/manual/test_burst_speed.py

Two kinds live here.

**Needs hardware.** A body on USB, and for the burst scripts the drive dial on
CH with the relay wired to the release jack.

| script | what it answers |
|---|---|
| `test_burst_speed.py` | the SDK frame-rate ceiling — where the 1.8 fps figure comes from |
| `test_drive_modes.py` | which drive modes the body reports, and whether continuous can be set |
| `test_api_diagnostic.py` | why `SetProp` returns ApiNotFound |
| `test_model_api_info.py` | what the model-dependent library actually supports |
| `test_optimized_burst_v2.py` | the private `SDK_*` calls, including the `SetPerformanceSettings(5)` that BOOST rests on |

These are diagnostics for a path the eclipse no longer uses: the bench closed
SDK-triggered shooting (17/17 `0x1008` with the dial on CH) and the relay does
all the triggering now.  They are kept for the next time the SDK misbehaves, not
because anything depends on them.  See `xt4-relay-bench-2026-08-01.md`.

**Needs nothing.** Script-style checks over the scheduling and exposure maths,
printing rather than asserting: `test_exposure_calculator.py`,
`test_geocoding.py`, `test_partial_phase_generation.py`,
`test_full_partial_generation.py`, `test_realistic_shutters.py`,
`test_script_integration.py`.
