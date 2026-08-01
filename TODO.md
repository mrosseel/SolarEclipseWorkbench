# SolarEclipseWorkbench — Robustness Hardening

Eclipse-day reliability. Only get one chance.

## Critical — Will lose shots

- [x] Per-camera threading lock in capture functions
  - gphoto2 is not thread-safe; APScheduler fires jobs from thread pool
  - concurrent PTP transactions corrupt USB session
  - add `threading.Lock` per camera, acquire before any capture/config call
  - [x] Fuji path: `FujiCamera._lock` wraps `configure()` and `capture()`
  - [x] gphoto2 path: `GPhotoCameraAdapter` lock + take_picture/burst/bracket wrapped

- [ ] Cache settings in `__adapt_camera_settings()` (gphoto2 path)
  - REGRESSED: done on `fujisdk` (`camera/capture.py:67`, `camera/gphoto.py:21`), lost when
    Fuji was re-applied onto flat `main` — no `_last_settings` exists on `fuji-on-main`
  - currently reconfigures ISO + aperture + shutter + 3x `sleep(0.1)` on EVERY shot
  - ~400ms overhead per frame even when settings haven't changed
  - cache last-applied `CameraSettings` per camera, skip config if unchanged
  - Fuji SDK path already does this correctly via `EclipseShooter._configure()`

- [x] try/except in `observer.py:notify_observers()`
  - one observer exception kills entire notification chain
  - jobs table, camera overview stop updating
  - wrap each `observer.update()` call in try/except, log error

- [x] Silent `KeyError` in `schedule_command()` (utils.py line 216)
  - `except KeyError: return` — drops commands with no log
  - reference moment missing for partial eclipse (no C2/C3) silently loses commands
  - log the KeyError with command details

## High — May cause failures under real conditions

- [ ] APScheduler job error listener + GUI notification
  - REGRESSED: done on `fujisdk` (`ui/controller.py`), lost in the re-apply onto `main` —
    no `EVENT_JOB_ERROR` / `add_listener` anywhere on `fuji-on-main`
  - if `take_picture()` throws, job is lost silently
  - no retry, no fallback, no user-visible warning
  - add `EVENT_JOB_ERROR` listener, show in jobs table, optional single retry

- [x] Camera health check at script load time
  - cameras dict accepted as-is; disconnected camera → every capture throws
  - quick `get_battery_level()` check before scheduling
  - re-check periodically via sync_cameras

- [x] Wrap `voice_prompt` calls in try/except
  - missing WAV file or busy audio device throws in scheduler thread
  - kills the scheduled job; voice prompts are not worth losing shots over

- [x] Fuji SDK `_init_count` not thread-safe
  - added `threading.Lock` around `_ensure_lib()` and `_release_lib()` in `fujixsdk/camera.py`

## Medium — Usability and correctness

- [x] Camera reconnection logic (Fuji)
  - `FujiCamera.capture()` retries once after `_reconnect()` (close + reopen SDK connection)
  - gphoto2 reconnection still TODO

- [x] `commands.py` uses `shell=True`
  - `execute_command()` passes script strings to `subprocess.run(shell=True)`
  - use `shell=False` with `shlex.split()`

- [x] Ephemeris file startup check
  - `de440s.bsp` or `eclipse_besselian.csv` missing → `FileNotFoundError` with no helpful message
  - check at startup, log warning telling user what's missing

- [x] Scheduler cleanup after eclipse
  - hundreds of expired one-shot CronTrigger jobs sit in scheduler
  - expired jobs (next_run_time=None) removed in update_jobs_countdown

## Low — Code quality

- [x] Circular import between `gui.py` and `utils.py`
  - moved `SolarEclipseController` import to `TYPE_CHECKING` block
  - `gui.py` still imports `observe_solar_eclipse` inside method (lazy, fine)

- [x] `scripts.py` silently drops unknown commands
  - now logs warning for unsupported MAESTRO commands

- [ ] No test coverage for error paths
  - no tests for: camera failures, scheduler errors, thread races, malformed scripts

---

# Resilience plan — measured against eclipseClick's claims

Prompted by the eclipseClick author's write-up on capture resilience
(https://eclipseclick.com/blog/eclipse-capture-resilience).  His thesis: what ruins an
eclipse capture is never astronomical — it is a nudged cable, a dead battery, a sulking
driver — so the only job of the software is to make each interruption cost exactly the
frames that physically fell inside it, and not one more.

## Where we already stand

- **Absolute scheduling — we already do this.**  `schedule_command()` (utils.py:154)
  computes each job's absolute UTC instant from its reference moment and adds an
  independent trigger (utils.py:307).  Losing frame N cannot shift frame N+1.
- **Late frames are dropped, not fired late — deliberate.**  `_serialised_on_camera`
  (camera.py:663) drops a shot if the USB lock is not free within `_MAX_LOCK_WAIT_S`
  (1.5 s); APScheduler keeps its default 1 s misfire grace (utils.py:114).
- **GPS-vs-computer clock offset is already compensated** (`gps_time_offset`,
  utils.py:295) — arguably ahead of him here.
- **Per-camera isolation exists in principle** (per-camera locks, per-camera jobs) but is
  not visible anywhere in the UI.

## Phase 0 — reconcile what the rebase lost

- [ ] Audit the four items ticked above that may not exist on `fuji-on-main`, and port
      what survives the flat layout: settings cache, `EVENT_JOB_ERROR` listener,
      gphoto2 reconnect, script-load health check.  Confirmed missing: the first two.
- [ ] Fix the stale comment at camera.py:1078 claiming `misfire_grace_time` is increased
      for EOS R bodies — utils.py:114 says the opposite.

## Phase 1 — measure before adding mechanism

- [ ] Record `intended_utc` on each job at schedule time and `actual_utc` around the
      shutter call; keep per-frame offset and outcome (fired / dropped-late / errored).
- [ ] Surface it: per-camera live counters in the GUI (OK / dropped / failed) plus a
      post-run CSV and summary.  Today a dropped shot only reaches `logging.warning`,
      not the GUI and not `hardware_problems`.
- [ ] Rehearsal harness: run a script against the 2026-08-12 contact times with the
      simulator plus one real body, pulling the USB mid-run, and measure what we lose.

## Phase 2 — sub-second accuracy

- [ ] utils.py:301 builds a `CronTrigger` from `year…second`, so the fractional part of
      the script's `hh:mm:ss.ss` is silently truncated and jobs fire on whole-second
      boundaries.  Switch to `DateTrigger(run_date=execution_time)` and verify against
      the Phase 1 offset log.

## Phase 3 — recovery ladder (gphoto2)

- [ ] Per-camera consecutive-failure counter that escalates: retry → gphoto2 reinit →
      full close and re-detect by port (`get_camera_by_port` already exists).  No modal
      dialogs; report through `hardware_problems`.
- [ ] After any reconnect, invalidate the cached settings (once Phase 0 restores the
      cache) so the next frame rewrites shutter/aperture/ISO from scratch — otherwise we
      reproduce exactly the failure he warns about: reconnected, and quietly shooting at
      the wrong exposure.
- [ ] Bound recovery in time: a rebuild must not hold the USB lock past its own frame's
      window, and a camera in recovery drops its own frames rather than delaying the
      other camera.

## Phase 3b — lunar limb profile (limb-corrected C2/C3)

Not from eclipseClick — from Jubier's approach.  Our contact solver uses a single
mean lunar radius (`l1`/`l2` from `eclipse_besselian.csv`), so C2/C3 ignore lunar
topography entirely.  Jubier corrects them with an LRO/Kaguya limb profile.

- [x] Precompute the marginal-zone blob — `tools/build_limb_blob.py`
  - source: LOLA LDEM_128 (128 px/deg, 236.9 m), DE421 mean Earth/polar axis frame
  - band of +/-11.99 deg around the mean limb great circle: every point that can
    ever be on the limb, given max libration (~10.4 deg) plus parallax (~1.0 deg)
  - native resolution, no downsampling: 46080 x 3072 cells
  - int16 quantised to 4 m, delta along phi, LZMA2 tiles (512 x 256) with an index
  - **72.3 MB**, 3.9x packed, stdlib-only decode (no new runtime dependency)
  - verified against the source DEM: max error 2.0 m = the quantisation bound
- [x] Reader — `src/solareclipseworkbench/lunar_limb.py` (`LimbBand`, tile LRU cache)
- [x] Libration + orientation at runtime — `src/solareclipseworkbench/limb_correction.py`
      Skyfield `PlanetaryConstants` + `moon_pa_de421_1900-2050.bpc` (1.8 MB) and
      `moon_080317.tf`, frame `MOON_ME_DE421` (the frame LOLA is gridded in).
      Position angles are taken in the true equator and equinox of date, to match
      the Besselian elements rather than ICRF.
      Validated on TSE 2015-03-20 Longyearbyen: axis angle c = 335.08 deg exactly
      as Jubier, libration l = +0.877 vs his +0.880, b = -0.285 vs his -0.260,
      diameter ratio 1.0431 exactly once k = 0.2725076 (1738.091 km) is used.
- [x] PA-dependent `L2'` — the contact tangent point is at atan2(-u, -v), since the
      solver's (u, v) runs observer-to-axis.  That puts C2 on the Sun's east limb
      and C3 on its west, as the eastward relative motion requires.
      Independent confirmation of k2: the mean limb height over the whole profile
      comes out at +0.015 km above k2 * R_earth = 1736.635 km.
- [x] Arc treatment: totality holds only while the Sun's limb is inside the true limb
      at *every* position angle.  `sunlight_margin()` gives that per angle,
      `solve_limb_contact()` bisects for the moment it last goes negative, and
      `beads()` returns the lit arcs.  Physically well behaved: 1 s before C2 it
      finds 7 small beads at PA 80-82 deg and none 1 s after; 1 s after C3, three
      beads spanning 239-249 deg, a broad valley.
- [ ] Reconcile with Jubier -- the arc result moved *away* from his figures:

      |            | single PA | whole arc | Jubier |
      |------------|-----------|-----------|--------|
      | C2         | +0.98s    | +1.83s    | +0.40s |
      | C3         | -3.08s    | -3.25s    | -2.80s |
      | duration   | -4.06s    | -5.08s    | -3.20s |

      Leading hypothesis: his C2'/C3' are not the last-bead instants.  Our arc
      result sits *outside* his on both sides -- later C2, earlier C3, totality
      1.9 s shorter -- which is what you get if he quotes a contact defined nearer
      the mean limb over the contact region while we quote the extreme bead.  His
      own sheet carries a separate "Baily's Beads: +/-3.0s" figure, the same order
      as the gap.  Decide which definition we want before tuning anything.
- [ ] Our uncorrected C3 is already 1.23 s from Jubier's before any limb data is
      involved (C2 is +0.14 s).  Check the catalogue dt against his 67.73 s and
      dUT1 -0.55 s -- part of the residual is not a limb problem at all.
- [ ] Second validation case: TSE 2024-04-08 near the northern limit, C2 +1.3s,
      C3 -31.6s -- an extreme test of the arc treatment above
- [ ] Ship the blob as a release asset with its SHA256, downloaded on first use
      (sha256 5e5ba2ed...b40640); it is gitignored

## Phase 4 — the limits he admits to

- [ ] Read his honest-limitations section and check each against us: battery death, card
      full, host clock drift.  Free space and battery are polled — verify whether any
      threshold actually warns the user.
