# Which solar radius should Solar Eclipse Workbench use?

`src/solareclipseworkbench/constants.py` currently has

```python
SUN_RADIUS = SOLAR_RADIUS = 696221300   # metres
```

which is **959.94″ at 1 au**. Nearly every published eclipse prediction uses
**959.63″**. This note sets out what that difference does, and the case for each,
so the choice can be made deliberately rather than inherited.

## What we measured

Our Besselian elements are generated in-house by `BesselianElementGenerator`
(the CSV catalogue is only a fallback), so this constant really does drive our
contact times.

Checking the total eclipse of 2026-08-12 at 41.7639°, −2.9294°, 1180 m against
[eclipse-chaser-log.com](https://www.eclipse-chaser-log.com/solar-eclipses/2026-08-12),
which publishes per-location circumstances:

| | ours at 959.94″ | ours at 959.63″ | site |
|---|---|---|---|
| C2 | 18:29:13.15 | 18:29:12.63 | 18:29:12.66 |
| C3 | 18:30:56.06 | 18:30:56.57 | 18:30:56.63 |
| totality | 102.91 s | 103.93 s | 103.97 s |

At the standard radius we agree to **0.06 s**. At ours we are **1.06 s short**.

The same signature appears against two other independent references — Jubier's
Solar Eclipse Maestro sheet for 2015-03-20 at Longyearbyen (1.40 s short) and
NASA/TP-1999-209484 for Lusaka at 2001-06-21. In every case the contact
*midpoint* agrees to well under 0.1 s and the whole disagreement is duration.
Nothing else in the contact solve is implicated.

**A larger Sun shortens totality**, and 959.94″ is larger than 959.63″.

## The case for 959.63″ (Auwers 1891)

- **It is what every source we can check against uses.** Espenak and NASA's
  canon, Solar Eclipse Maestro's default, eclipse-chaser-log, and every
  published eclipse map and table. Keeping our own value guarantees that our
  times disagree with every other tool a user consults, by about a second,
  forever — and the user has no way to know which is wrong.
- **It makes our predictions checkable.** All three of our validation references
  are built on it. With 959.94″ we cannot tell a real regression from the
  constant, because everything is a second off before we start. That cost us
  real time on this work: a 1.4 s discrepancy was chased through ΔT, position
  angles and the limb profile before the constant turned out to be the cause.
- **It is a convention, not a measurement.** For prediction purposes the number
  functions as an agreed reference; comparability across tools has value
  independent of which value is closer to the physical Sun.

## The case for 959.94″ (the modern eclipse value)

- **It is closer to what is actually observed at eclipses.** Quaglia, Irwin et
  al. (2021) derived **959.95″ ± 0.05″** from flash-spectrum video at the
  southern limit of the 2017 eclipse. IOTA's bead-timing value is 959.99″ ±
  0.06″. Jubier's own suggested figure is 959.98″. Our 959.94″ sits inside that
  cluster; 959.63″ sits about 6σ outside it.
- **The standard value is 19th-century.** Auwers 1891, adopted before
  photoelectric photometry, and retained for continuity rather than because it
  survived re-measurement.
- **The difference is not academic at the path limits.** Published work puts the
  edge-of-path error from the standard radius at roughly 600 m, and a
  near-the-limit duration can change by a factor of two or more (a documented
  2017 example: 34 s becomes 13 s). Anyone shooting near the edge — which is
  where the most interesting bead work happens — is materially misled by the
  legacy value.
- **We are a capture tool, not a catalogue.** What we owe the user is a shutter
  that fires when the sky does, not agreement with a table. Our bead windows are
  2–8 s long; a systematic 1 s error in the same direction every time is a real
  bias in where the burst sits.

## What is actually at stake

- **Timing:** about 1 s on a 100 s totality, always in the same direction —
  our totality is short.
- **Bead windows:** the correction is differential, so the window *length* is
  barely affected; it is the placement that shifts.
- **Path limits:** the sensitivity grows sharply near the edge, where a second
  of duration can be a large fraction of the whole.
- **Support burden:** with 959.94″, a user comparing us against any online
  source sees a discrepancy and reports it as a bug. That has to be answerable.

## Recommendation

Make it a setting rather than a constant, so the choice is visible and
reversible, and label it in the interface — something like *Solar radius:
959.63″ (standard) / 959.95″ (eclipse-derived)*.

On the default, we are genuinely torn and think it is the maintainer's call:

- Default **959.63″** if the priority is that the software agrees with the maps,
  tables and other tools the user will check it against. This makes us
  verifiable and makes future regressions detectable.
- Default **959.94″** if the priority is the best available physics, on the
  grounds that a capture tool should fire when the sky does. This needs a clear
  note in the interface explaining why our times differ from published ones,
  otherwise it reads as a bug.

Either way, the value should not stay an undocumented constant: it is currently
the single largest systematic in our contact times, and it is larger than the
lunar limb correction we went to considerable lengths to add.

## Sources

- Quaglia, Irwin, Emmanouilidis, Pessi, *Estimation of the Eclipse Solar Radius
  by Flash Spectrum Video Analysis*, arXiv:2107.09416
- NASA/TP-1999-209484, *Lunar Limb Profile*
- Xavier Jubier, Solar Eclipse Maestro documentation
- eclipse-chaser-log.com, per-location circumstances for 2026-08-12
