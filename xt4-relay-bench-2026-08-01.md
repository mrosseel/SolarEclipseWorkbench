# X-T4 through the relay — bench results, 1 August 2026

What the camera actually does when the relay closes its release-jack contacts, measured
so the production eclipse script can be built on numbers rather than assumptions.

## Rig

Walfront 2-channel HID relay (dcttech `16c0:05df`) on the release jack of an X-T4:
CH1 to S1 (red / ring), CH2 to S2 (white / tip), both COM terminals to the twisted
greens (sleeve). Camera on a tripod aimed at a millisecond clock rendered full-screen
on the MacBook, so every frame carries an absolute timestamp from the same clock the
driving script logs against. RAW only, f/2, ISO 320, manual focus, mechanical shutter
except where noted.

`scripts/campaign_relay.py` drove it; 396 frames came off the card afterwards.

**Correlation was exact.** Fuji records `Image Count`, an absolute shutter-actuation
number: 8318 to 8713 across 396 files with **zero gaps**. Every actuation the camera made
reached the card, and no frame is unaccounted for.

## Trigger latency: ~170 ms, and 120 ms of it is ours

Command timestamp against the clock photographed in the frame:

| frame | pulse width | commanded | photographed | latency |
|---|---|---|---|---|
| `_DSF7083` | 80 ms | 18:29:59.149 | .329 | 180 ms |
| `_DSF7085` | 40 ms | 18:30:11.381 | .545 | 164 ms |
| `_DSF7089` | 100 ms | 18:30:24.286 | .446 | 160 ms |
| `_DSF7091` | 200 ms | 18:30:30.971 | 31.145 | 174 ms |

Mean ~170 ms, spread ~20 ms, and **pulse width makes no difference** — 40 ms is as good
as 200 ms.

The number decomposes. `RelayTrigger.shoot()` closes S1, sleeps `DEFAULT_SETTLE_S`
(120 ms), then closes S2. Relay round trip is 2.2 ms. So the **camera's own release lag
is only ~46 ms**; the rest is our pre-arm.

Implication: assert S1 early and hold it through the critical phases, rather than paying
the settle on every frame. The dual-channel wiring exists for exactly this.

## Frame rate is limited by the card, not the shutter

CH at 1/1000, frames per second through a 20-second hold:

```
10 10 10 10  8 6 4 5 4 4 4 3 3 4 3 3 3 3 3 3
```

Full rate for about 4 seconds — roughly a 28-frame buffer — then a floor of **3 fps**.

| hold | frames | mean rate |
|---|---|---|
| 5 s | 48 | 9.6 fps |
| 10 s | 72 | 7.2 fps |
| 20 s | 103 | 5.15 fps |

3 fps x 20.9 MB per compressed RAW = **63 MB/s, the card's sustained write speed**.

The proof that it is the card and not the shutter: **electronic and mechanical shutter
returned identical 9.6 fps**. CH was configured to 10 fps for this run; the body offers
15 fps mechanical and 20 fps electronic, so the burst window can be made denser, but the
floor after it cannot.

**A faster card is the highest-leverage upgrade, with a known ceiling.** The tail wobbled
3–4 fps, so the card's real figure is 63–84 MB/s — UHS-I class. Slot 1 of the X-T4 is
UHS-II: a V90 card (~260 MB/s sustained) would lift the floor to roughly 12 fps. Nothing
reaches the 313 MB/s that 15 fps × 20.9 MB would need, so the burst window never becomes
permanent — but 3 fps versus 12 fps is the difference between one corona ladder per 7 s
and four. Worth checking which card was in the slot before buying anything. Also note
EXIF's `AutoBracketing` tag read "On" for 385 of 396 frames including every CH burst — it
reflects a menu setting, not activity, and must never be used to identify bracket frames;
the ladder signature in `ExposureTime` + `SequenceNumber` is the reliable marker.

Rate also collapses once exposures get long, as they will during totality:

| shutter | rate |
|---|---|
| 1/1000 | 9.6 fps |
| 1/15 | 7.8 fps |
| 1/2 | 1.9 fps |

## Drive mode governs a held contact absolutely

In Single drive, a 2-second hold produced **exactly one frame**, twice. Contact duration
does not override the dial.

## Bracketing: one tap runs the whole sequence

A single 40 ms pulse ran a **complete 9-frame AE bracket**. A 3-second hold also returned
exactly 9 frames — the sequence **runs to completion even after the contact opens**.
Bracketing is therefore tapped, not held.

**9 frames x 3 EV clamps and wastes frames.** From base 1/500 the ladder came out:

| seq | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| | 1/500 | 8s | 1s | 1/8 | 1/60 | 1/4000 | 1/8000 | 1/8000 | 1/8000 |

Three duplicates piled up against the 1/8000 wall: 9 x 3 EV spans 24 EV and the body only
has about 18 EV between 1/8000 and 8 s.

**9 frames x 2 EV from base 1/30 fits exactly**, and lands close to the classic corona
ladder:

```
1/8000  1/2000  1/500  1/125  1/30  1/8  1/2  2s  8s
```

Note that a 2 EV ladder needs roughly 11 seconds per sequence, because of its 8-second
frame.

## Spending the frame budget

The card fixes the total: about 3 frames per second, whatever else is done. The only real
decision is what those frames contain.

Clustering does not beat a continuous hold on count. Over the same 37.5 s, holding
throughout gives roughly 40 frames from the buffer window plus 3 fps for the remaining
33.5 s, about **140 frames**; three cycles of a 2.5 s burst and a 10 s drain give about
**111**. A pause idles the sensor while the card is still draining, so it costs about 20%.

Nor is the 3 fps tail as coarse as it sounds — 0.33 s between frames, and the corona does
not move in a third of a second, so those frames stack perfectly well. Tight clusters only
matter for subjects that change fast, meaning beads and the diamond ring, and those live
inside the buffer window where full rate is already available.

The waste in a long hold is therefore not timing but **redundancy**: 140 near-identical
corona frames. The same budget spent on bracketed exposures returns the same count of
far more useful images. Block 6 of the second campaign measures the duty cycle rather
than trusting this arithmetic.

## Change made as a result

`RelayTrigger.pressed()` now skips the settle when S1 is already closed, and leaves S1
closed on the way out. Pre-arming with `half_press()` before a sequence therefore drops
per-frame trigger latency from ~170 ms to the body's own ~46 ms, without gambling on
whether a cold body wakes in time. Verified on the simulated backend: 144 ms cold,
15 ms pre-armed.

## A held contact does not repeat the bracket

Read off the clock digits in frame, the 15-second hold went:

| | time |
|---|---|
| S2 closed | 18:40:28.066 |
| first frame (1/500) | 18:40:28.059 |
| last frame (1/8000) | 18:40:37.928 |
| S2 released | 18:40:43.073 |

The sequence took **9.87 s** and then **5.1 s of held contact produced nothing**. One
closure runs one sequence and stops.

The sum of that ladder's own exposures is about 9.13 s, so the body adds only ~0.7 s of
overhead across nine frames. That gives a usable model:

> sequence duration = sum of the ladder's exposures + ~0.7 s

Which prices the candidates: the 9 x 2 EV corona ladder needs ~11.4 s per sequence
because of its 8-second frame, while 7 frames x 2 EV from 1/125 needs only ~1.4 s and can
therefore repeat throughout totality.

The first frame *appears* to land within ~10 ms of S2 closing — but treat that as an
anomaly, not a result. The photographed digits read 7 ms *before* the command, which is
impossible; the display's 0–16 ms render lag just about explains it, but an error band
containing impossible values is noise, and it disagrees with the ~46 ms single-shot figure
for no known reason. Block 5 of the second campaign re-measures pre-armed latency with
proper samples.

## Accuracy of the clock rig

The page redraws under `requestAnimationFrame`, so the displayed time lags reality by up
to one screen refresh, 0 to 16 ms. Photographed readings are therefore early, and every
latency figure here is an **under**-estimate by up to 16 ms: 170 ms means 170 to 186 ms.
Long bracket frames are unreadable — at 1 s and 8 s the screen is blown out completely —
so only exposures between roughly 1/8000 and 1/125 can be timed this way.

## Still open

**Does the release jack still fire while the SDK holds a USB session?** Untested here;
blocks 6 to 8 of the SDK campaign. This decides whether exposures can be ramped over USB
while the relay drives the frame rate, or whether in-camera bracketing is the only option.
