"""Every production script has to fit the totality its name claims.

The corona ladders are laid out when the script is written, and a command that
cannot take the camera within 1.5s is dropped rather than delayed - so a script
whose ladders run long does not fail loudly.  It quietly loses frames, and keeps
exposing after C3 with the filter off.  This walks each script at its own
labelled duration and insists nothing is lost and nothing is still shooting.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO / "scripts" / "real" / "durations").glob("20260812_production_*s.txt"))

# The checker lives in scripts/, which is not a package, so it is loaded by path
# rather than imported - the alternative is putting scripts/ on sys.path, and
# that pulls every probe and campaign in this directory into the test run.
_spec = importlib.util.spec_from_file_location(
    "validate_totality", REPO / "scripts" / "validate_totality.py")
validate_totality = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_totality)


def labelled_duration(script: Path) -> float:
    """The seconds of totality in the filename: ..._090s.txt -> 90.0"""
    return float(script.stem.rsplit("_", 1)[1].rstrip("s"))


def test_there_is_a_script_to_choose_from():
    assert SCRIPTS, "no production scripts in scripts/real - run the generator"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_the_script_fits_the_totality_it_claims(script):
    duration = labelled_duration(script)
    ran, dropped, notes = validate_totality.simulate(script, duration, False)

    assert ran > 0, f"{script.name} schedules nothing"
    assert dropped == 0, f"{script.name} loses {dropped} command(s) at {duration:.0f}s"
    overrunning = [note for _, _, note in notes if "after C3" in note]
    assert not overrunning, f"{script.name} at {duration:.0f}s: {overrunning}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_the_script_says_what_it_fits(script):
    """Picking the wrong one is silent, so the file has to say so at the top."""
    head = script.read_text().splitlines()[:15]
    duration = labelled_duration(script)
    assert any(f"FILLS {duration:.0f} SECONDS" in line for line in head), \
        f"{script.name} does not state the totality it fills in its first lines"


def test_longer_totality_than_claimed_is_always_safe():
    """The rule is to take the largest script that fits, so every script has to
    survive any totality at or above its own label - that is what makes rounding
    down the safe direction."""
    shortest = SCRIPTS[0]
    for duration in (labelled_duration(shortest), 120.0, 160.0):
        ran, dropped, notes = validate_totality.simulate(shortest, duration, False)
        assert dropped == 0, f"{shortest.name} loses commands at {duration:.0f}s"
        assert not [note for _, _, note in notes if "after C3" in note]


def test_the_beads_load_and_arm_precede_the_burst_by_construction():
    """5 August: the exposure load fired half a second INTO the held C2 burst
    - a shutter-speed write on a body in continuous drive - and the burst
    died.  The load was anchored to a different moment than the burst, so
    widening the burst head reordered them.  All three now share one anchor
    and one base offset; this pins the order in the emitted script itself.
    """
    import pathlib
    import re

    for script in pathlib.Path("scripts/real/durations").glob("20260812_production_*s.txt"):
        text = script.read_text()
        offsets = {}
        for kind, pattern in (
                ("load", r'take_picture, C2, -, (\d+):(\d+):([\d.]+).*Load the beads'),
                ("arm", r'relay_arm, C2, -, (\d+):(\d+):([\d.]+)'),
                ("burst", r'relay_burst, C2, -, (\d+):(\d+):([\d.]+)')):
            m = re.search(pattern, text)
            assert m, "%s missing the %s" % (script.name, kind)
            offsets[kind] = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
        assert offsets["load"] > offsets["arm"] > offsets["burst"], \
            "%s: load/arm/burst out of order (bigger offset = earlier)" % script.name
        assert offsets["load"] - offsets["burst"] >= 2.0, \
            "%s: the load is too close to the burst" % script.name


def test_the_safety_release_fires_after_the_hold_lets_go():
    """A release inside the hold is not a safety net but a guillotine: it
    opens the contacts under a running burst.  Nearly shipped twice - once at
    C3 when the tail grew, once at C2 when the margins were reallocated - so
    it is pinned in the emitted artifact for both contacts, every duration.
    """
    import pathlib
    import re

    def offset(text, pattern):
        m = re.search(pattern, text)
        assert m, pattern
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    for script in pathlib.Path("scripts/real/durations").glob("20260812_production_*s.txt"):
        text = script.read_text()
        c2_hold = float(re.search(
            r'relay_burst, C2, -, \d+:\d+:[\d.]+, ([\d.]+)', text).group(1))
        c2_start = offset(text, r'relay_burst, C2, -, (\d+):(\d+):([\d.]+)')
        c2_release = offset(text, r'relay_release, C2, \+, (\d+):(\d+):([\d.]+)')
        assert c2_release > c2_hold - c2_start + 0.5, \
            "%s: the C2 release fires inside the hold" % script.name

        c3_hold = float(re.search(
            r'relay_burst, BEADS_C3_START, -, \d+:\d+:[\d.]+, ([\d.]+)', text).group(1))
        c3_start = offset(text, r'relay_burst, BEADS_C3_START, -, (\d+):(\d+):([\d.]+)')
        c3_release = offset(text, r'relay_release, BEADS_C3_START, \+, (\d+):(\d+):([\d.]+)')
        assert c3_release > c3_hold - c3_start + 0.5, \
            "%s: the C3 release fires inside the hold" % script.name
