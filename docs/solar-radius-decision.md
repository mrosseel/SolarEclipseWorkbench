# Solar radius: 959.63″ or 959.95″?

`constants.py` sets `SOLAR_RADIUS = 696221300` m, which is **959.94″ at 1 au**.
Published eclipse predictions use **959.63″**. Since we generate our own
Besselian elements, this constant sets our contact times.

## Effect

Total eclipse of 2026-08-12, at 41.7639°, −2.9294°, 1180 m:

| | 959.94″ | 959.63″ | eclipse-chaser-log |
|---|---|---|---|
| C2 | 18:29:13.15 | 18:29:12.63 | 18:29:12.66 |
| C3 | 18:30:56.06 | 18:30:56.57 | 18:30:56.63 |
| totality | 102.91 s | 103.93 s | 103.97 s |

A larger Sun shortens totality. The same signature appears against Jubier's
2015-03-20 Longyearbyen sheet and NASA/TP-1999-209484 for Lusaka 2001: in all
three the contact midpoint agrees to under 0.1 s and the entire disagreement is
duration, so no other term in the solve is implicated.

Magnitude: ~1 s on ~100 s of totality, one-signed. The sensitivity rises sharply
towards the path limits, where reported near-limit durations change by a factor
of two, and the umbral edge moves by of order 600 m.

## 959.63″

Auwers (1891), adopted as the standard for eclipse prediction and used by
Espenak's canon, Solar Eclipse Maestro's default, and eclipse-chaser-log. It is
a conventional reference value rather than a current measurement; its merit is
that all published predictions and all three of our validation references share
it, so our results remain directly comparable and any residual is attributable.

## 959.95″

Derived from eclipse observations rather than adopted. Quaglia et al. (2021) give
959.95″ ± 0.05″ from flash-spectrum video at the 2017 southern limit; IOTA's
bead timings give 959.99″ ± 0.06″; Jubier suggests 959.98″. Our 959.94″ lies
inside that cluster and 959.63″ lies well outside it, so the standard value is
inconsistent with modern determinations at high significance. The physical
distinction is that eclipse contacts measure the radius of complete photospheric
extinction, which is not the same as the radius from other techniques, and is
the quantity contact-time prediction actually needs.

## Question

Which value should be the default, and should the other be selectable? The
choice is between comparability with the published literature and consistency
with modern eclipse-derived determinations. It should not remain an
undocumented constant: it is currently the largest systematic in our contact
times, exceeding the lunar limb correction.

## Sources

- Quaglia, Irwin, Emmanouilidis, Pessi, arXiv:2107.09416
- NASA/TP-1999-209484, *Lunar Limb Profile*
- eclipse-chaser-log.com, per-location circumstances, 2026-08-12
