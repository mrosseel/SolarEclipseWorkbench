# Fujifilm X-T4 — setup for the 12 August 2026 eclipse

Everything the body needs before `scripts/20260812_production.txt` is loaded. The
script drives shutter speed and ISO over the SDK and fires the contact bursts
through the relay; everything below is state the script *cannot* set for you.

Rig assumed: X-T4 on an 80/480 mm refractor (f/6, no electronic aperture), ND 5.0
solar film for the partial phases, relay trigger on the 2.5 mm remote jack.

> Dial *positions* are from the X-T4 layout as I understand it, not from an
> inspection of your body. Confirm the two marked **[verify]** against the camera
> before you rely on them — the rest are menu paths, which are unambiguous.

---

## Manual mode is not a dial position

The X-T4 has no PASM dial. The exposure mode is implied by where the shutter speed
dial and the lens aperture ring sit, so "set it to Manual" means moving those two,
not finding a menu item:

| Shutter dial | Aperture ring | AE mode the SDK reports |
|---|---|---|
| A | A | Program |
| A | a number | Aperture Priority |
| T or a number | A | Shutter Priority |
| **T or a number** | **a number** | **Manual** |

`validate_for_eclipse()` rejects anything but Manual, because in the other three
the body owns part of the exposure and the script's settings are ignored.

On a telescope there is no aperture ring at all, so the shutter dial off `A` is
enough. With an XF lens mounted for bench testing, the ring must be on a number
too — ring at `A` with the dial at `T` gives Shutter Priority, not Manual.

The focus mode selector on the front of the body is a **separate** control and is
checked separately. Confusing the two costs a run.

## Physical dials and switches

| Control | Position | Why |
|---|---|---|
| Shutter speed dial | **T** | Hands shutter speed to the command dial and to the SDK. At a numbered position the dial wins and every exposure in the script is ignored. |
| ISO dial | **C** | Same reasoning: ISO comes from the SDK, not the dial. **[verify]** the X-T4 labels this `C`. |
| Drive dial (collar under the ISO dial) | **CH** | The relay bursts depend on the body free-running continuous-high at ~15 fps. In `S` the relay gets you one frame per contact instead of 37. **[verify]** which collar carries the drive positions on the X-T4. |
| Stills/movie switch | **STILL** | |
| Focus mode switch (front of body) | **M** | There is no AF on a telescope. In S/C the SDK's S1ON returns `ShootError` and shots are dropped or delayed. |

### AF+MF and PRE-AF

`AF/MF SETTING` → **AF+MF: OFF** and **PRE-AF: OFF**.

The focus selector on `M` is not enough on its own: AF+MF leaves autofocus
running on the half-press even in manual focus, and every frame the SDK takes
begins with a half-press.

Turning AF+MF off roughly halved the cost of an individual release attempt on
the bench X-T4 (0.49 s to 0.25 s). It did **not** change sustained throughput
through `shoot_fast`, which measured 0.59 fps with it on and 0.52 fps with it
off - so turn it off, but do not expect it to buy frames.

None of this is checkable in software: `get_focus_mode()` returns
`0x1002 Invalid parameter` on this body, so the validator cannot read focus
state at all. The doc is the only defence.

A short mechanical sound before each shutter click is not necessarily focus.
The X-T4 has IBIS, and the sensor unit audibly engages on half-press and parks
afterwards, which sounds much the same. Set `IS MODE: OFF` on a mount.
| Exposure compensation dial | **0** | Applies on top of the SDK's manual exposure. |
| Aperture | n/a | The scope is fixed f/6. The `6.0` in the script's aperture column is a comment, not a command. |

Leave the shutter dial at **T** for everything you actually shoot. All three relay
commands that the production script and the main relay test use — `relay_shoot`
and `relay_burst` — work from T, because they just close the release contacts and
let the body do what its drive mode says.

`relay_bulb` is the single exception: `RelayTrigger.bulb()` holds the contacts and
expects the dial at **B**. Nothing in the production script uses it, and nothing
needs to — at T the X-T4 reaches multi-second exposures through the command dial,
so the SDK sets the 1 s and 1.6 s deep-corona frames itself. Bulb only buys holds
longer than the dial offers, which a 101 s totality never wants. It lives in
`scripts/testFujiXT4Bulb.txt` as a standalone pass so it never contradicts a
script that also sets exposures over USB.

---

## Menu settings

### Image quality — `IQ` menu

| Setting | Value | Why |
|---|---|---|
| Image Quality | **RAW** (no JPEG) | A JPEG per frame roughly doubles bytes written and eats buffer for a file you will never use. Costs frames in exactly the 2.5 s that matter. |
| RAW Recording | **Lossless Compressed** | Same detail as uncompressed at roughly half the size. Corona frames are mostly black sky, so they compress unusually well — this is what keeps a 2.5 s burst inside the buffer. |
| Long Exposure NR | **OFF** | Critical. It shoots a matching dark frame after every long exposure, so the 1 s and 1.6 s deep-corona frames would each take twice as long and blow their slot. |
| High ISO NR | −4 or OFF | Cosmetic only on RAW, but keeps in-camera processing off the critical path. |
| Film Simulation / WB | irrelevant on RAW | Set WB to Daylight anyway so the EVF preview is not misleading. |

### Drive and shutter

| Setting | Value | Why |
|---|---|---|
| Shutter Type | **MS** (mechanical) | 15 fps and no rolling-shutter artefacts. ES would give 20 fps but overruns the buffer in 2 s and adds banding risk. |
| CH speed | **15 fps** | The rate the frame budget assumes. |
| Pre-Shot ES | **OFF** | Fills the buffer before the burst even starts. |
| Self-timer | **OFF** | |
| IS Mode (IBIS) | **OFF** | The scope is on a mount; IBIS only adds drift on a static target. |

### Power and performance

| Setting | Value | Why |
|---|---|---|
| Power Management → Performance | **BOOST** | The X-T4 does not hold 15 fps in Normal. `test_optimized_burst_v2.py` sets `SDK_SetPerformanceSettings(5)` = `PERF_BOOST_FRAMERATE` for the same reason. |
| Auto Power Off | **OFF** | The script starts 20 min before C1 and runs ~2 h. |
| Auto Power Off (Temp.) | Standard | |

### Cards

| Setting | Value | Why |
|---|---|---|
| Card Slot Setting | **Sequential** | Not Backup. Backup writes every frame twice and halves sustained throughput, which is what the burst tail depends on. |
| Card | **UHS-II, V60 or better**, in slot 1 | Sustained write is what the burst falls back to once the ~38-frame buffer fills. |
| Format both cards | before the day | ~10 GB expected for 335 frames of compressed RAW; a 64 GB card is comfortable. |

### Connection

| Setting | Value | Why |
|---|---|---|
| Connection Setting → PC Connection Mode | **USB Tethering Shooting Auto** | The Fuji SDK will not see the body in any other mode. |
| USB Power Supply | **OFF** | Otherwise the body may run off the host instead of charging, and behaviour on disconnect gets murky. |

### Display

| Setting | Value | Why |
|---|---|---|
| Image Disp. | **OFF** | Review-after-shot costs time and keeps the buffer busy. |
| Preview Exp./WB in Manual Mode | **OFF** (Natural Live View on) | With it on, the EVF goes black at the partial-phase exposures and you cannot see to frame. |
| Touch Screen Setting | **OFF** | Stops a stray glove changing a setting mid-eclipse. |
| Face/Eye Detection | **OFF** | |

---

## Pre-flight checklist

Run through this once the rig is polar-aligned and before you load the script.

1. Focus mode switch **M**, shutter dial **T**, ISO dial **C**, drive **CH**.
2. **Shoot Without Lens: ON** — without it the body refuses to fire on a telescope.
3. RAW only, lossless compressed, Long Exposure NR **OFF**.
4. Performance **BOOST**, Auto Power Off **OFF**.
5. Card formatted, slot setting **Sequential**.
6. PC Connection Mode = USB tethering, then confirm the workbench lists the body as
   `Fuji Fujifilm X-T4` — the script matches on that exact string.
7. Relay connected and registered; run `scripts/testFujiXT4Relay.txt`, which fires a
   `relay_shoot` smoke test before anything else.
8. Fresh NP-W235 plus a spare. Tethered live view for two hours is not kind to a
   battery, and a swap mid-totality is not an option.
9. Focus on the solar limb at high EVF magnification through the filter, then tape
   the focuser. Refocus after the filter comes off is not in the timeline.

---

## Known unknowns

These are unresolved and each one changes what you get:

- **Remote jack while the SDK holds the session.** The relay assumes the body still
  answers its release contacts while the SDK is connected in PC priority. Untested.
  If it does not work, the X-T4 loses both contact bursts — 74 frames — and the
  `relay_*` lines log a warning while the rest of the script runs on.
- **Drive dial vs SDK bracketing.** `take_bracket` drives the SDK's
  `bracket_no_download` directly, which should be independent of the CH drive
  position, but the combination has not been bench-tested.
- **Buffer depth at 2.5 s.** The bursts are sized to exactly 38 frames. If your
  card and compression give 35 rather than 38, the last two or three frames of
  each burst arrive at card-write speed instead of 15 fps. Slower tail, not lost
  frames.

## Where the numbers come from

- ~1.8 fps SDK ceiling, 189–660 ms per frame: measured on this body, recorded in
  the project notes.
- ~15 fps relay bursts: X-T4 rated CH mechanical rate, with the host out of the
  loop because `relay_burst` with no interval holds the contacts closed
  (`relay_trigger.py:700`) rather than pulsing them.
- Frame counts, buffer peaks and per-body throughput: regenerate and re-check with
  `scripts/generate_20260812_production.py`.
