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
SCRIPTS = sorted((REPO / "scripts" / "real").glob("20260812_production_*s.txt"))

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
