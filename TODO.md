# SolarEclipseWorkbench — Robustness Hardening

Eclipse-day reliability. Only get one chance.

## Critical — Will lose shots

- [x] Per-camera threading lock in capture functions
  - gphoto2 is not thread-safe; APScheduler fires jobs from thread pool
  - concurrent PTP transactions corrupt USB session
  - add `threading.Lock` per camera, acquire before any capture/config call
  - [x] Fuji path: `FujiCamera._lock` wraps `configure()` and `capture()`
  - [x] gphoto2 path: `GPhotoCameraAdapter` lock + take_picture/burst/bracket wrapped

- [x] Cache settings in `__adapt_camera_settings()` (gphoto2 path)
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

- [x] APScheduler job error listener + GUI notification
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
