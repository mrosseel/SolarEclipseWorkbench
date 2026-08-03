"""What each scheduled command was meant to do, and what it actually did.

A command knows its camera and its settings but not the time it was supposed to
run, so nothing could tell a frame that fired on its mark from one that fired
four seconds late - and during totality four seconds is a different corona.  A
frame the camera lock dropped left only a line in the log, joined to nothing.

Every scheduled command is wrapped so it records one row:

    intended_utc, actual_utc, delta_s, command, outcome, detail

which makes a rehearsal scoreable (worst delta, how many dropped) and the real
run reconstructable against EXIF afterwards.

Nothing here may raise into the capture path: a frame is worth more than its
own bookkeeping.
"""

from __future__ import annotations

import csv
import logging
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Where the rows go.  Set `frame_log.PATH` before the run to put it elsewhere.
PATH: Path = Path(tempfile.gettempdir()) / "sew_frame_timing.csv"

FIELDS = ("intended_utc", "actual_utc", "delta_s", "command", "outcome", "detail")

# The command currently running on this thread.  APScheduler fires each job in
# its own thread, so this cannot be a plain module global.
_current = threading.local()

_write_lock = threading.Lock()
_header_written = False


def begin(command: str, intended: Optional[datetime]) -> None:
    """Note what this thread is about to run, and when it should have run."""
    _current.command = command
    _current.intended = intended
    _current.recorded = False


def record(outcome: str, detail: str = "") -> None:
    """Write the row for whatever this thread is running.

    Called once per command.  A second call is ignored, so a path that reports
    its own outcome - a frame the lock dropped, say - wins over the generic one
    the wrapper writes on the way out.
    """
    if getattr(_current, "recorded", True):
        return
    _current.recorded = True

    intended = getattr(_current, "intended", None)
    command = getattr(_current, "command", "?")
    actual = datetime.now(timezone.utc)
    delta = (actual - intended).total_seconds() if intended else ""

    try:
        _append({
            "intended_utc": intended.isoformat() if intended else "",
            "actual_utc": actual.isoformat(),
            "delta_s": f"{delta:.3f}" if delta != "" else "",
            "command": command,
            "outcome": outcome,
            "detail": detail,
        })
    except Exception:
        # Bookkeeping must never cost a frame.
        logging.debug("Could not write the frame timing row", exc_info=True)


def _append(row: dict) -> None:
    global _header_written
    with _write_lock:
        empty = not PATH.exists() or PATH.stat().st_size == 0
        with open(PATH, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if empty:
                writer.writeheader()
            writer.writerow(row)
        _header_written = True


def reset(path: Optional[Path] = None) -> None:
    """Start a fresh file.  Called when a script is loaded, not per frame."""
    global _header_written
    if path is not None:
        globals()["PATH"] = Path(path)
    _header_written = False
    try:
        if PATH.exists():
            PATH.unlink()
    except OSError:
        logging.debug("Could not clear %s", PATH, exc_info=True)


def summarise(path: Optional[Path] = None) -> dict:
    """Read a run back: how many fired, how late, how many were lost.

    This is what makes a rehearsal answer "is the schedule tight enough" with a
    measurement rather than an estimate.
    """
    target = Path(path) if path is not None else PATH
    rows = []
    try:
        with open(target, newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return {"rows": 0}

    deltas = [float(r["delta_s"]) for r in rows if r.get("delta_s")]
    lost = [r for r in rows if r["outcome"] not in ("ok",)]
    return {
        "rows": len(rows),
        "ok": len(rows) - len(lost),
        "lost": len(lost),
        "worst_delta_s": max(deltas, default=0.0),
        "mean_delta_s": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "outcomes": {o: sum(1 for r in rows if r["outcome"] == o)
                     for o in sorted({r["outcome"] for r in rows})},
    }
