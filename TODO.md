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
- [x] Baseline discrepancy explained, and it was not dT.  Catalogue dT is 67.64 s
      against his 67.73 s, worth 0.09 s -- negligible.  The cause is the solar
      radius: `constants.py` SOLAR_RADIUS = 696221300 m is **959.94"**, Jubier's own
      suggested value, while Solar Eclipse Maestro *defaults to the standard
      959.63"* and his published sheet used the default.  A larger Sun shortens
      totality.  Re-running at 959.63" (`--solar-radius-arcsec`) takes our
      uncorrected duration from 146.2 s to **147.4 s against his 147.6 s**, and the
      remaining contact offsets become a near-pure -0.55 s epoch shift, which is
      exactly dUT1.  So our mean-limb geometry agrees with his to 0.2 s in duration
      and 0.1 s in epoch.  Note the elements come from our own
      `BesselianElementGenerator`, not the CSV -- the CSV is only a fallback -- so
      SOLAR_RADIUS really does drive this.
- [x] Our arc method is the documented standard.  NASA/TP-1999-209484: "For any
      given position angle, there will be a high mountain (annular) or a low valley
      (total) *in the vicinity* that ultimately determines the true instant of
      contact", and Herald's procedure slides the solar limb "until it is tangent to
      the lowest lunar profile feature in the vicinity".  Their epicyclic
      approximation h = S(m-1)(1-cos C) is the small-angle form of our exact
      `solar_limb_reach()`.
- [x] Second reference case, NASA/TP-1999-209484 for Lusaka at TSE 2001-06-21
      (`--case lusaka2001`).  Three independent confirmations fell out of it:
      - contact position angles come out at **118.08 and 246.82 deg** against NASA's
        published P2 = 118 and P3 = 247.  That validates the atan2(-u, -v) sign.
      - uncorrected duration **193.5 s against their 193.5 s** exactly, with what is
        left a pure +0.85 s epoch shift on both contacts.
      - the arc treatment is vindicated where the single-angle one fails outright:
        single angle gives C2 **-1.67 s, the wrong sign**, while the arc gives
        **+3.15 s** against their +4.0 s.

      | case                  | single PA | whole arc | reference |
      |-----------------------|-----------|-----------|-----------|
      | Svalbard 2015 C2      | +0.98s    | +1.82s    | +0.40s    |
      | Svalbard 2015 C3      | -3.08s    | -3.25s    | -2.80s    |
      | Lusaka 2001 C2        | -1.67s    | +3.15s    | +4.00s    |
      | Lusaka 2001 C3        | -0.20s    | -2.75s    | -1.20s    |

- [ ] Residuals are 0.45 to 1.55 s and do not sit one way, so this is scatter rather
      than a bias to chase.  Three things could each account for a second, and they
      have to be separated before any of it is called an error:
      - the two references use different limb data.  Svalbard is LRO/Kaguya, Lusaka
        is Watts, whose errors reach 0.4 arcsec -- about a second of time -- and
        Jubier's own note is that LRO and Kaguya are "much more accurate than the
        Watts even after correction".
      - NASA's Lusaka figures were read off a graph, and our Lusaka coordinates are
        their city-database entry to the arcminute, so about +/-1 km.  Near the limb
        that changes which valley governs.
      - the 1-pixel registration ambiguity in the LDEM label, worth 237 m.
      A real test needs a case with precise coordinates and a published LRO-based
      correction, or better, an observed contact timing.
- [ ] Second validation case: TSE 2024-04-08 near the northern limit, C2 +1.3s,
      C3 -31.6s -- an extreme test of the arc treatment above
- [ ] Ship the blob as a release asset with its SHA256, downloaded on first use
      (sha256 5e5ba2ed...b40640); it is gitignored

## Phase 4 — the limits he admits to

- [ ] Read his honest-limitations section and check each against us: battery death, card
      full, host clock drift.  Free space and battery are polled — verify whether any
      threshold actually warns the user.

## Phase 3c — put the burst on the diamond ring

What the limb profile is actually *for*, in this application: a burst centred on the
diamond ring rather than on a nominal contact.  `bead_window()` walks out from the
corrected contact until the sunlight still showing past the limb spans more than 20
degrees of position angle -- the moment the beads merge back into a crescent.

Two results that matter for scheduling:

| case          | C2 window | centre vs C2 | C3 window | centre vs C3 |
|---------------|-----------|--------------|-----------|--------------|
| Svalbard 2015 | 4.6 s     | -2.3 s       | 4.3 s     | +2.1 s       |
| Lusaka 2001   | 7.8 s     | -3.9 s       | 4.2 s     | +2.1 s       |

- The window is **one-sided**: it ends at C2 and starts several seconds earlier.  A
  burst centred on C2 spends half its frames on an empty corona.  Centre it on
  C2 minus half the window instead.
- The length is **site specific** -- 4.6 s against 7.8 s here -- so a fixed plus or
  minus N seconds is wrong somewhere.  For reference Jubier's Svalbard sheet quotes
  "Baily's Beads: +/-3.0s", against our 4.6 s at a 20 degree arc threshold.

- [ ] Expose the window to the script layer, so `take_burst` can be scheduled against
      the bead window rather than against C2 with a hand-guessed offset
- [ ] Pick the arc threshold against real frames rather than by eye; 20 degrees is a
      first guess, and it sets how much crescent is allowed into the first frame
- [ ] Sanity-check that our 1 s absolute accuracy is comfortable here: it is a fifth
      of a bead window, so centring is safe, but the window *edges* are where it
      would show

## Third reference: eclipse-chaser-log.com, 2026-08-12

Their interactive map gives per-location circumstances including a limb correction,
so it is a third independent check.  At 41.7639, -2.9294, 1180 m:

| quantity      | ours       | site        | delta   |
|---------------|------------|-------------|---------|
| Delta T       | 69.1087 s  | 69.10 s     | exact   |
| C2 mean limb  | 18:29:13.15| 18:29:12.66 | +0.49 s |
| C3 mean limb  | 18:30:56.06| 18:30:56.63 | -0.57 s |
| duration      | 102.91 s   | 103.97 s    | -1.06 s |
| C2 correction | -0.27 s    | -1.01 s     | 0.74 s  |
| C3 correction | -2.87 s    | -1.26 s     | 1.61 s  |

Two things this settles:

- **The solar radius signature repeats.**  The mean contacts straddle theirs almost
  symmetrically (+0.49, -0.57), so the midpoint agrees to about 0.04 s and the whole
  disagreement is 1.06 s of duration -- the same shape as the Svalbard case, and the
  same cause: our 959.94" against the standard 959.63".  Third independent
  confirmation, and the sign says our totality is short because our Sun is big.
- **The correction scatter is not converging.**  0.74 s and 1.61 s here, against
  1.42/0.45 at Svalbard and 0.85/1.55 at Lusaka.  Three references, three different
  limb datasets, no consistent bias -- our corrections sit within about 1.6 s of all
  of them and match none.  At a 2 to 8 s bead window that is fine for centring a
  burst, and it is not good enough to claim sub-second contact times.

- [ ] Decide what to do about SOLAR_RADIUS.  959.94" is Jubier's suggested value and
      matches Quaglia's 959.95 +/- 0.05 from 2017 flash spectra, so it is arguably the
      better constant -- but every reference we check against uses 959.63", so we will
      always look 1 s short.  Either keep it and document why, or make it a setting.

### Same solar radius, same answer

Re-run at 959.63" against the same site and location: C2 -0.02 s, C3 -0.06 s,
duration -0.04 s.  Our mean-limb geometry is right to better than a tenth of a
second, and the whole 1 s disagreement was SOLAR_RADIUS and nothing else.

- [ ] Refraction.  At 7.6 deg altitude refraction is about 416 arcsec, but both
      limbs are lifted almost equally, so what matters is the differential across
      the roughly 20 arcsec between the centres at internal contact: about
      0.27 arcsec, which at 0.4 arcsec/s is **0.7 s of timing**.  Same order as
      our limb-correction scatter, so it is worth having for a low-sun eclipse
      like 2026-08-12.  Note we match the reference to 0.02 s *without* it, and
      its own footnote says refraction is applied to the altitude column, so it
      probably does not model this in contact times either -- meaning no
      reference we have can validate a refraction correction.  Estimate first,
      then decide.

### Refraction, quantified

Done properly for 2026-08-12 at 41.7639, -2.9294, 1180 m, projecting the centre
separation onto the vertical via the parallactic angle rather than assuming it:

| | altitude | separation | vertical fraction | dR/dh | shift |
|---|---|---|---|---|---|
| C2 | 7.51 deg | 31.3" | 0.42 | -0.0138 | 0.18" = **0.30 s** |
| C3 | 7.20 deg | 31.2" | 0.29 | -0.0148 | 0.14" = **0.22 s** |

Both contacts move outward -- refraction compresses the sky vertically, so the
centres appear closer, the Moon covers more, totality starts earlier and ends
later.  Net **+0.5 s on duration**, one-signed.

Smaller than the +/-0.7 s hand estimate, because the contacts happen 60 to 70
degrees off the vertical here so only a third to a half of the separation is
subject to the gradient.

Assessment: real, one-signed, and second-order.  It is below our limb-correction
scatter of about 1 s, and well below the 2 to 8 s bead window, so it does not
affect burst centring.  It would matter to a sub-second contact-time claim.

- [x] Resolved, and the answer is that there is nothing to apply.  Refraction maps
      h to h + R(h), which locally is an affine map -- a uniform vertical scaling
      by 1 + dR/dh -- and affine maps preserve tangency.  Squash both discs and
      the separation between their centres by the same factor and the instant the
      limbs touch does not move.  The 0.30 s and 0.22 s measured above came from
      squashing the separation while leaving the discs round, which is not a
      physical model.  What survives is second order, through the variation of
      dR/dh across the half-arcminute between the centres, and is negligible.
      Documented in limb_correction.py so it is not re-added.
- [x] The wizard's sun altitudes are now refracted (`calculate_sun_altitude_at_time`).
      They key the extinction table, and near the horizon geometric and apparent
      altitude diverge sharply -- at 0.5 deg geometric the Sun is really about a
      degree up, most of a stop of airmass -- and they also decide whether a
      low-sun shot is worth taking.  For 2026-08-12 in Spain the last brackets now
      read 2.23, 0.62 and -0.28 deg.

---

# Mount (MLAstro SAL-33 / OnStepX) — planned 4 August, not started

Findings verified against the code; the epoch error was recomputed here rather
than taken from the review that raised it.

## P1 — `goto_sun` sends the wrong coordinate epoch

`mounts/base.py:309` calls Skyfield's `radec()` with no epoch, which returns
ICRF/J2000. OnStepX works in the current epoch (`MOUNT_COORDS TOPOCENTRIC`).

Computed for 12 Aug 2026 18:30 UTC with our own DE421:

    J2000  : RA 9.4738 h   Dec +14.9087
    of date: RA 9.4984 h   Dec +14.7920
    offset : 0.375 deg = 22.5'   (dRA 21.4', dDec -7.0')

That is **1.4 solar radii** — the Sun falls entirely outside where it was
aimed. 40% of the half-frame at 480 mm on APS-C, 67% at 800 mm.

Fix: `radec(epoch='date')`. Optionally observe from `wgs84.latlon(...)` since
the workbench knows the location — but that is cosmetic: solar parallax is
~8.8", under 1% of the Sun's radius. The error is precession, not parallax.

Do **not** add refraction. Skyfield's `apparent()` covers light-time and
aberration but not refraction, and OnStepX applies its own. Double-correcting
would be worst near the horizon, which is where this sunset eclipse happens.

Only two call sites, both through `sun_radec`: `goto_sun` and the simulator.

## P1 — the Track button does not select solar rate

`gui.py:2809` calls `tracking_on` directly, so it resumes whatever rate the
mount held, usually sidereal.  `mounts/__init__.py:185 mount_track_sun` already
does it properly — checks `capabilities.tracking_rates`, sends `:TS#`, then
tracks.  The button should call that, so panel and script cannot diverge.

## P2 — refusals are silent

`gui.py:2791 _guard` catches exceptions but discards the returned `bool`.
`tracking_on`, `park` and `unpark` return `False` on refusal and the user sees
only a status that quietly snaps back.

## P2 — mount time and site are never synchronised

No `:St` / `:Sg` / `:SL` / `:SG` anywhere in the driver.  Add `set_site` and
`set_time` plus an explicit "Sync mount to workbench" button — not a silent
write on connect.

**Verify before writing:** LX200 `:Sg` traditionally takes longitude
**west-positive**, the opposite of this codebase's convention.  Getting it
backwards puts the mount out by twice the longitude.  Check against the OnStepX
source and pin the sign in a test.

## P2 — the panel is USB-only — deferred

`mounts/onstepx.py:383` enumerates serial ports only; there is no host/IP
input.  Real, but USB works and a new network path is a new failure mode.  Not
before the eclipse.

## Testing

- Epoch asserted against equinox-of-date, plus a regression that the
  J2000-vs-date separation on eclipse day exceeds a solar radius, so it cannot
  silently revert.
- Fake transport asserting `:TS#` precedes tracking-on from the button.
- `_guard` reports when an action returns `False`.
- On hardware: read RA/Dec and compare against the date-epoch expectation, then
  `goto_sun` and compare reported against commanded.  **The epoch bug is
  invisible against the simulator**, which shares the same wrong function —
  likely how it survived.

# Still unverified after 4 August

- **A full rehearsal through the scheduler producing a readable frame-timing
  log.**  Everything so far is component-level.
- Whether the relaxed live view rule holds up on hardware through a whole run.
- `GetMediaCapacity` is listed as supported by the body's own module yet refuses
  every parameter 0-8 and every slot.  Unexplained; reported as unavailable
  rather than worked around.
- Battery cannot be read at all on the X-T4 — `GetDeviceInfoEx` does not list
  `0x4055`.  **Battery is a manual pre-flight check**: fresh battery in, spare
  in a pocket.
