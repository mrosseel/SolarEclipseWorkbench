# What the hardware actually does

Measured results from bench sessions with the real rig, so none of it has to
be measured twice. Every entry names the probe that produced it and the date
it was run. Where a belief was overturned, the overturned belief is kept:
knowing which plausible theory is wrong is most of the value.

**The rig.** Fujifilm X-T4 on the Fuji X SDK over USB, fired through a
dual-channel USB relay on the 2.5 mm remote jack (tip = S1 half press,
ring = S2 full press, sleeve = ground). Drive dial on **CL**, shutter dial on
**T**, shutter type **MS+ES**. A parked Canon EOS 800D and an MLAstro SAL-33
(OnStepX) mount share the bus.

---

## 1. The SDK cannot drive a burst — the relay exists for a reason

**Investigated:** July 2026, before the relay was built.
**Verdict: settled. Do not revisit; there is no SDK path to a fast burst.**

The X-T4's SDK is built for tethered single-shot work. Every route to a
host-driven burst was tried and closed:

| Route | Result |
|---|---|
| `XSDK_Release(S1ON + S2_S1OFF)` | works, ~280 ms/shot — **~1.3 fps actual** |
| `XSDK_Release(S2 only, S1 held)` | refused, returns −1 |
| `XSDK_SetDriveMode(CH/CL)` | refused — the body will not take drive mode from the PC |
| `XSDK_CapDriveMode` | answers **0 modes available** |
| `SDK_SetPerformanceSettings(BOOST_FRAMERATE)` | accepted, no speed change |
| `SDK_SetShutterPriorityMode(RELEASE)` | accepted, no speed change |
| `SDK_SetBKT`, `SetBKTFrame`, `SetBurstNumber`, `SetBurstInterval` | all refused, −1 |
| `SDK_Shoot`, `SDK_ShootS1/S2` | block indefinitely |
| `FTL_PTP_InitiateCapture` / `InitiateOpenCapture` (PTP layer) | refused, wrong handle type |

The body's own drive modes (CH 15 fps, CL 3–8 fps, ES 30 fps) need the
physical dial, which the SDK cannot reach. Hence the relay: hold the contact
and the camera free-runs at its dial rate with no USB in the loop.

An optocoupler-isolated Raspberry Pi Pico trigger was designed as an
alternative and **never built** — see `docs/PICO_TRIGGER_DESIGN.md`. The USB
relay does the job; the Pico design is kept only in case the relay dies.

---

## 2. Nine shutter speeds this body refuses

**Probe:** `scripts/shutter_value_probe.py` — 5 August.
**Verdict: settled. The dropdown is filtered; do not re-add them.**

`CapShutterSpeed` answers **empty** on this body, so the live-view dropdown was
cut from the SDK's name table — a mixed grid containing half-stop values other
bodies use. Nine of the 64 are refused here with `0x2003` "invalid parameter
combination":

> 1/6000, 1/3000, 1/1500, 1/750, 1/350, 1/180, 1/90, 1/45, 1/1.5

**A first run also reported 1/8000 refused. That was wrong** — a transient
`0x1006` busy recorded as a permanent refusal. The probe now retries; 1/8000 is
set on every corona ladder rung and works. Any single-attempt "refusal" from
this body should be assumed transient until retried.

---

## 3. The ISO *can* be changed with the transfer queue loaded

**Probe:** `scripts/iso_switch_probe.py` — 7 August.
**Verdict: settled, and it contradicted a comment in our own source.**

`fuji_camera` claimed `set_iso` needed an empty queue. Measured across three
conditions, ISO wrote and read back correctly every time:

| Condition | Pending frames | Landed |
|---|---|---|
| queue empty (control) | 0 | 6/6 |
| queue part full, as between ladders | 5 | 6/6 |
| queue loaded, as during totality | 18 | 6/6 |

So a split ISO through totality is affordable. The stale comment is corrected.

---

## 4. A held S1 makes the body refuse every setting change

**Probe:** `scripts/ladder_write_probe.py` — 7 August, five runs.
**Verdict: settled, and it was the corona ladder bug.**

This is the big one. Holding S1 across a ladder keeps the **CL drive running**,
so the body is permanently mid-exposure and refuses shutter writes:

| Phase | Speeds landed | Frames fired |
|---|---|---|
| 1. contacts open (control) | **7/7** | 7 |
| 2. S1 held for the whole ladder (what it did) | **1/7** | **32** — the whole queue |
| 3. S1 released around each write (my proposed fix) | 2–3/7 | 12–15 |

Phase 2 is the rehearsal failure exactly: one speed of seven, and the body
firing continuously until the 32-slot queue hard-stopped it.

**My proposed fix was also wrong** (phase 3, 2–3 of 7). The cure is not to
release S1 around each write — it is to *not hold S1 at all* across a ladder.
`fuji_camera.py` now calls `relay.release_all()` before the rungs.

**Write latency, 40 samples:** median 178–185 ms, p90 ~205 ms, worst 257 ms.
That is what `LADDER_PER_RUNG_USB_S = 0.5` pays for.

---

## 5. The tap length, and why rungs sometimes fire twice

**Probe:** `scripts/tap_reliability_probe.py` — 7 and 8 August.
**Verdict: settled. `TAP_S = 0.03`. The doubles are harmless — leave them.**

A 0.03 s tap fired **at least one frame per rung in all 18 ladders tested**
(10 rounds on 7 August, 8 more on 8 August). It never fired short.

Some rungs fire *twice*. Two theories were proposed and **both are wrong**:

- **Not a first-tap effect** — position 1 doubled once in eight ladders.
- **Not a fastest-rung effect** — 1/125 doubled 7 times against 1/8000's 4.

The trap: the ladder is symmetric, so 1/125 sits at position 4 *in both
directions*, and a position tally reads as a position effect that isn't there.
Running the ladder **backwards** is what made it decidable — forward, 1/8000 is
both the first rung and the fastest, so the card could never separate them.

What it actually tracks is **exposure length** (0.03 s tap, 56 rungs):

| rung | 1/8000 | 1/2000 | 1/500 | 1/125 | 1/30 | 1/8 | 0.5 |
|---|---|---|---|---|---|---|---|
| doubles | 4 | 4 | 1 | 7 | 2 | **0** | **0** |

Everything at 1/30 and faster doubles; nothing at 1/8 or slower ever does.
Halving the tap to 0.015 s halved the doubles (18 → 10) with still no misses —
but 1/8000 doubles *less* than 1/125, which no simple model predicts, so the
mechanism is **not pinned**.

**Do not shorten `TAP_S` to chase this.** 0.03 has 18 ladders behind it against
0.015's 8. A duplicate costs one buffer slot; a missed corona rung cannot be
retaken. About 2 extra frames per ladder is the right side of the trade.

---

## 6. The 32-slot transfer queue and the drain

**Probes:** `scripts/burst_queue_probe.py`, bench work 5 and 8 August.
**Verdict: settled, after two wrong models and one actively harmful "fix".**

With an SDK session open, **every frame parks a copy in a 32-slot queue until
the PC deletes it** — card writes do not free the slots, and at 32 the body
hard-stops a held burst. This silently truncated every scripted burst before
5 August; "C2 stopped too soon" was always this.

Draining *during* the hold works and does not drop the session (the old warning
to the contrary was stale). But three further things had to be measured:

**a. The sag is not queue saturation.** In the working configuration the queue
peaks at 16–30 of 32 and comes back down. An earlier model derived from card
EXIF — "fills at 4.5 s, then the body throttles" — is **wrong**. Frames and
deletes contend for the bus throughout; the rate is that steady state, not a
cliff. A plan to "ease into" the burst with a paced head, to postpone a
saturation point, was therefore abandoned before being built.

**b. An unbounded drain became the burst.** `_drain_pass` was handed a deadline
and never checked it, so a pass deleted everything the buffer reported however
long that took: ~0.15 s an image, **15 s for a call budgeted 2 s**. Since the
contact cannot open while a drain runs, an 8 s burst held **18.7 s at 5.35 fps**
— overrunning into totality and blocking the corona ladders behind it. Fixed by
honouring the budget and draining mid-burst with one round, no settle, and never
longer than the hold has left.

| | before | after |
|---|---|---|
| contact held, 8 s asked | 18.7 s | **8.04 / 8.16 / 8.23 s** |
| rate on the contact | 5.35 fps | **7.59 / 7.65 / 7.71 fps** |

7.6 fps is what `XT4_RELAY_FPS = 7.7` in the generator had assumed all along.

**c. Never move the drain to a thread.** The obvious repair — drain on a worker
so the hold can be timed exactly — was tried and costs two thirds of the burst:
**95 frames inline against exactly 32 threaded**, the queue full, the body
hard-stopped, and the contact clicking against a shutter that would not fire.
The drain only keeps pace while it owns the thread. A test in
`tests/test_relay_trigger.py` fails if anyone threads it again.

**Judge any burst change by frame count on the contact, never by wall clock.**
The wall clock said the threaded version was better; it was 66% worse.

**Tail drain:** after the contact opens, emptying the queue takes 2.97–5.04 s
and holds the camera's USB lock. `BURST_TAIL_DRAIN_S = 5.0` in the generator
prices this, and is why the first corona ladder waits until C2+11.2.

---

## 7. Trigger latency depends on the path

**Bench, 1 August.** Host-to-contact plus body release lag:

| Path | Latency |
|---|---|
| S1 pre-armed and held, S2 closes | **43–48 ms** |
| S2 alone, S1 never asserted | ~130 ms |
| bare `shoot()` | ~170 ms (120 ms of it our own settle) |

Every scripted burst pre-arms with `relay_arm` and takes the fast path, which
both shrinks the lead and stops it depending on how the release cable is wired.
`RELAY_LATENCY_S = 0.045`.

---

## 8. The two serial adapters are indistinguishable by name

**Verdict: settled. Identify by probe, never by saved name.**

The mount and the relay are twin CP2102 chips, both reporting serial `0001`.
Two macOS drivers (Apple `usbserial-*`, SiLabs `SLAB_USBtoUART*`) front each
chip, so **two devices produce four nodes**, and the names swap on replug. Only
`location` distinguishes them.

Consequences learned the hard way: a saved relay port became the mount and
every AT command went silently into an OnStepX (6 August); collapsing the four
nodes to two orphaned a saved port name so the relay could not be connected to
at all (7 August). `serial_ports.resolve_port()` now maps a stale saved name to
the currently-offered node on the same physical port, and the DSD backend
refuses a port that does not answer `AT` — the SH-UR firmware echoes OK for
every command, so a silent port is provably not a relay board.

Identify by behaviour: **LX200 replies = mount, AT-OK = relay.**

---

## Still open

- **Why the fastest rung doubles less than a middling one** (§5). Harmless, so
  not worth hardware time before the eclipse.

## Probes worth keeping

| Script | Answers |
|---|---|
| `scripts/burst_queue_probe.py` | queue depth and drain rate through a real burst; held vs paced |
| `scripts/tap_reliability_probe.py` | frames per rung, forward and reversed ladders |
| `scripts/ladder_write_probe.py` | which contact state lets settings be written |
| `scripts/iso_switch_probe.py` | whether ISO moves with the queue loaded |
| `scripts/shutter_value_probe.py` | which dropdown speeds the body really takes |

All need the camera and relay on the bus **with the workbench closed** — the
SDK allows one session. All log to `bench_log`, and retry transient `0x1006`
rather than recording it as a refusal.
