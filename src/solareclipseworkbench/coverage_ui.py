"""What the loaded script will photograph, drawn on one timeline.

A script is a few hundred lines of times and exposures.  Read as text it is
impossible to see that an hour of partials is one frame every three minutes
while the whole of totality is a hundred seconds of everything at once - or
that a gap somebody meant to fill is still there.

One row, on a symmetric-log clock: the axis is compressed away from
mid-totality, so the hundred seconds that matter take half the width and the
hours of partials still fit on the ends.  Above it, the numbers that say
whether the plan is any good before a single frame is taken: how many frames,
and how much of totality the camera actually spends working.

The scheduler is the source, not the file: it holds what will actually run,
after the reference moments were resolved and any line that named a moment
this eclipse does not have was dropped.
"""

from __future__ import annotations

import datetime
import math
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QVBoxLayout,
                             QWidget)

from solareclipseworkbench.hardware_registry import JOB_COMMANDS

#: What each kind of command looks like.  Colours carry meaning: everything
#: that holds the shutter open for a stretch is warm, single frames are cool,
#: and anything that is not a photograph at all stays grey.
STYLES = {
    "take_picture": ("#2c7fb8", 1.0),
    "take_burst": ("#d95f0e", 3.0),
    "take_bracket": ("#e6550d", 6.5),
    "relay_burst": ("#c0392b", 4.0),
    "relay_shoot": ("#2c7fb8", 1.0),
    "sync_camera_time": ("#95a5a6", 0.0),
    "voice_prompt": ("#bdc3c7", 0.0),
}

#: Commands that photograph something.  The rest are prompts and housekeeping.
FRAME_COMMANDS = frozenset(
    name for name in STYLES if name not in ("voice_prompt", "sync_camera_time"))

#: Commands that hold the shutter (or the relay) for a stretch rather than
#: taking one frame; they get the upper lane and a span block.
HELD_COMMANDS = frozenset(("take_burst", "take_bracket", "relay_burst"))

_CONTACTS = ("C1", "C2", "MAX", "C3", "C4")

#: Seconds a bracket spends between rungs beyond the exposure itself: the new
#: speed goes over USB and the body has to finish writing.  Measured with the
#: ladders on 4 August, which ran 6.4-6.8 s for seven rungs.
_RUNG_OVERHEAD_S = 0.6

#: Frames a second under a held contact, measured on this body at CL 8 fps.
#: The burst commands carry a duration, not a frame count.
_BURST_FPS = 7.7

#: What a command costs the camera beyond the frames themselves - the same
#: footprints the totality validator prices jobs with.  A burst is its hold
#: plus the queue-tail drains; a bracket adds a drain after its ladder; a
#: single frame costs its slice of the schedule.
_BURST_TAIL_S = 4.5
_BRACKET_DRAIN_S = 2.0
_SINGLE_COST_S = 1.1

#: The symlog knee: inside this many seconds of the centre the axis is close
#: to linear, beyond it the hours compress.  Ten seconds gives a
#: hundred-second totality about a third of the width, with the hours of
#: partials folded into the ends - measured on the rendered panel, not
#: guessed.
_TAU_S = 10.0


def _exposure_seconds(text) -> float:
    """A shutter speed as written in a script, in seconds."""
    if text is None:
        return 0.0
    text = str(text).strip().rstrip('"')
    try:
        if text.startswith("1/"):
            return 1.0 / float(text[2:])
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _symlog(seconds: float, tau: float = _TAU_S) -> float:
    """Seconds from the centre onto the compressed axis, sign preserved."""
    return math.copysign(math.log1p(abs(seconds) / tau), seconds)


def frames_of(job, command: str) -> list:
    """Every frame a job will take, as (when, exposure seconds).

    A command is not a frame: a bracket is seven of them over six seconds and a
    bead burst is thirty over four.  Drawing one stripe per command shows when
    something happened; drawing one per frame shows what is actually
    photographed, and where nothing is.

    The times within a bracket are modelled, not measured - the body decides
    them - but the model is the same one the script generator sizes ladders
    with, and it matched the run to within a few tenths.
    """
    when = getattr(job, "next_run_time", None)
    if when is None:
        return []
    args = list(getattr(job, "args", None) or ())
    settings = next((a for a in args if hasattr(a, "shutter_speed")), None)
    exposure = _exposure_seconds(getattr(settings, "shutter_speed", None))

    if command in ("take_picture", "relay_shoot"):
        return [(when, exposure)]

    if command == "take_bracket":
        ladder = next((a for a in args if isinstance(a, str) and ";" in a), None)
        if not ladder:
            return [(when, exposure)]
        frames, offset = [], 0.0
        for rung in ladder.split(";"):
            seconds = _exposure_seconds(rung)
            frames.append((when + datetime.timedelta(seconds=offset), seconds))
            offset += seconds + _RUNG_OVERHEAD_S
        return frames

    if command in ("relay_burst", "take_burst"):
        held = next((float(a) for a in args
                     if isinstance(a, (int, float)) and 0.05 < float(a) < 120), 0.0)
        if held <= 0:
            return [(when, exposure)]
        count = max(int(held * _BURST_FPS), 1)
        step = held / count
        return [(when + datetime.timedelta(seconds=index * step), exposure)
                for index in range(count)]

    return [(when, exposure)]


def busy_seconds_of(command: str, frames: list) -> float:
    """How long a command keeps the camera working, from its frames.

    The same footprints the totality validator prices with: a burst holds for
    its span and then drains its queue tail, a bracket walks its ladder and
    drains, a single frame costs its slice of the schedule.
    """
    if not frames or command not in FRAME_COMMANDS:
        return 0.0
    span = (frames[-1][0] - frames[0][0]).total_seconds() + frames[-1][1]
    if command in ("relay_burst", "take_burst"):
        return span + _BURST_TAIL_S
    if command == "take_bracket":
        return span + _RUNG_OVERHEAD_S + _BRACKET_DRAIN_S
    return max(span, frames[0][1]) + _SINGLE_COST_S


def merged_intervals(intervals: list) -> list:
    """Overlapping (start, end) pairs merged, so busy time is counted once.

    Without this a burst and the bracket that follows into its drain would
    both claim the same seconds and utilisation could read past 100%.
    """
    merged: list = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def analyse(events: list, moments: dict) -> dict:
    """The numbers over the timeline, from the events as scheduled.

    ``utilization`` is the fraction of C2-C3 the camera spends working -
    exposures, holds and drains merged so overlap is not counted twice.
    """
    frames_total = sum(len(frames) for _, command, frames in events
                       if command in FRAME_COMMANDS)
    c2, c3 = moments.get("C2"), moments.get("C3")
    if c2 is None or c3 is None:
        return {"frames_total": frames_total, "frames_totality": None,
                "utilization": None, "totality_seconds": None}
    start, end = c2.time_utc, c3.time_utc
    totality = max((end - start).total_seconds(), 0.001)

    frames_totality = sum(
        1 for _, command, frames in events if command in FRAME_COMMANDS
        for at, _ in frames if start <= at <= end)

    intervals = []
    for _, command, frames in events:
        cost = busy_seconds_of(command, frames)
        if cost <= 0 or not frames:
            continue
        a = frames[0][0]
        b = a + datetime.timedelta(seconds=cost)
        if b < start or a > end:
            continue
        intervals.append((max(a, start), min(b, end)))
    busy = sum((b - a).total_seconds() for a, b in merged_intervals(intervals))

    return {"frames_total": frames_total,
            "frames_totality": frames_totality,
            "utilization": min(busy / totality, 1.0),
            "totality_seconds": totality}


class CoverageView(QWidget):
    """The scheduled frames on one symlog timeline.

    Two lanes - held commands above, single frames below - over an axis
    compressed away from mid-totality, with the contacts marked.  Totality is
    shaded, so the dense stretch and its edges read at a glance.
    """

    LANE = 16
    AXIS = 16
    TOP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: list = []      # (when, command, [(when, exposure)])
        self._moments: dict = {}
        self.setMinimumHeight(self.TOP + 2 * self.LANE + self.AXIS + 14)

    def set_schedule(self, jobs, moments: dict) -> None:
        """Take the jobs as they are now; the panel does not poll."""
        self._moments = moments or {}
        events = []
        for job in jobs or []:
            when = getattr(job, "next_run_time", None)
            if when is None:
                continue
            command = JOB_COMMANDS.get(getattr(job, "id", None), "")
            if command not in STYLES:
                continue
            events.append((when, command,
                           frames_of(job, command)
                           if command in FRAME_COMMANDS else []))
        self._events = sorted(events)
        self.update()

    # ---------------------------------------------------------------- geometry

    def _centre(self) -> Optional[datetime.datetime]:
        maximum = self._moments.get("MAX")
        if maximum is not None:
            return maximum.time_utc
        span = self._span_of("C2", "C3")
        if span:
            return span[0] + (span[1] - span[0]) / 2
        if self._events:
            return self._events[len(self._events) // 2][0]
        return None

    def _span_of(self, first: str, last: str):
        start, end = self._moments.get(first), self._moments.get(last)
        if start is None or end is None:
            return None
        return (start.time_utc, end.time_utc)

    def _totality_span(self):
        """C2 to C3, widened to take in the bead bursts either side.

        The C2 burst starts before second contact - the diamond ring is the
        last bead before totality - so a span that begins exactly at C2 leaves
        out the most important command in the script.
        """
        span = self._span_of("C2", "C3")
        if span is None:
            return None
        start, end = span
        margin = datetime.timedelta(seconds=8)
        beads_start = self._moments.get("BEADS_C2_START")
        beads_end = self._moments.get("BEADS_C3_END")
        if beads_start is not None:
            start = min(start, beads_start.time_utc)
        if beads_end is not None:
            end = max(end, beads_end.time_utc)
        return (start - margin, end + margin)

    # ------------------------------------------------------------------- paint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())

        centre = self._centre()
        if not self._events or centre is None:
            painter.setPen(QColor("#7f8c8d"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Load a script to see what it photographs")
            return

        first = min([self._events[0][0]]
                    + [m.time_utc for k, m in self._moments.items() if k == "C1"])
        last = max([self._events[-1][0]]
                   + [m.time_utc for k, m in self._moments.items() if k == "C4"])
        pad = datetime.timedelta(seconds=60)
        start, end = first - pad, last + pad

        left, right = 8, self.width() - 8
        width = max(right - left, 1)
        u_min = _symlog((start - centre).total_seconds())
        u_max = _symlog((end - centre).total_seconds())
        u_span = max(u_max - u_min, 1e-9)

        def x_of(when):
            u = _symlog((when - centre).total_seconds())
            return left + width * (u - u_min) / u_span

        # The lanes take whatever height the dock has: coverage is the whole
        # panel, not a strip above a blank.
        lane = max(self.LANE, (self.height() - self.TOP - self.AXIS - 10) // 2)
        held_top = self.TOP
        single_top = held_top + lane + 2
        axis_y = single_top + lane + 4

        small = QFont(self.font())
        small.setPointSizeF(max(7.5, small.pointSizeF() - 2.0))
        painter.setFont(small)

        # Totality shaded first, so everything else sits on top of it.
        shade = self._span_of("C2", "C3")
        if shade:
            x1, x2 = x_of(shade[0]), x_of(shade[1])
            painter.fillRect(QRectF(x1, held_top, max(x2 - x1, 1.5),
                                    axis_y - held_top),
                             QColor(230, 126, 34, 36))

        # The axis: a line, and ticks at round offsets from the centre.  On a
        # compressed clock the honest labels are offsets, not clock times -
        # "-30m" says what the distance is, where "17:59" pretends the axis
        # is linear.
        painter.setPen(QPen(QColor("#bdc3c7"), 1))
        painter.drawLine(left, axis_y, right, axis_y)
        painter.setPen(QColor("#95a5a6"))
        for offset in (-7200, -3600, -1800, -600, -300, -60, 0,
                       60, 300, 600, 1800, 3600, 7200):
            when = centre + datetime.timedelta(seconds=offset)
            if not (start <= when <= end):
                continue
            x = int(x_of(when))
            painter.drawLine(x, axis_y, x, axis_y + 3)
            if offset == 0:
                label = centre.astimezone().strftime("%H:%M")
            else:
                magnitude = abs(offset)
                label = ("%+dm" % (offset // 60) if magnitude < 3600
                         else "%+dh" % (offset // 3600))
            painter.drawText(
                x - painter.fontMetrics().horizontalAdvance(label) // 2,
                axis_y + self.AXIS, label)

        # Held commands as translucent span blocks, so a burst reads as the
        # stretch it occupies and not only as its comb of frames.
        for _, command, frames in self._events:
            if command not in HELD_COMMANDS or not frames:
                continue
            cost = busy_seconds_of(command, frames)
            x1 = x_of(frames[0][0])
            x2 = x_of(frames[0][0] + datetime.timedelta(seconds=cost))
            colour = QColor(STYLES[command][0])
            colour.setAlpha(60)
            painter.fillRect(QRectF(x1, held_top, max(x2 - x1, 2.0),
                                    lane - 2), colour)

        # One stripe per frame, as wide as the shutter is open - with a floor,
        # because 1/8000 on a two-hour axis is nothing at all.
        for _, command, frames in self._events:
            if command not in FRAME_COMMANDS:
                continue
            colour = QColor(STYLES[command][0])
            lane = held_top if command in HELD_COMMANDS else single_top
            for at, exposure in frames:
                x1 = x_of(at)
                x2 = x_of(at + datetime.timedelta(seconds=exposure))
                painter.fillRect(QRectF(x1, lane, max(x2 - x1, 1.2),
                                        lane - 2), colour)

        # Contacts on top, named where the names fit.
        painter.setPen(QPen(QColor("#2c3e50"), 1, Qt.PenStyle.DashLine))
        placed: list = []
        for name in _CONTACTS:
            moment = self._moments.get(name)
            if moment is None or not (start <= moment.time_utc <= end):
                continue
            x = int(x_of(moment.time_utc))
            painter.drawLine(x, held_top - 3, x, axis_y)
            room = painter.fontMetrics().horizontalAdvance(name) + 8
            if any(abs(x - other) < room for other in placed):
                continue
            placed.append(x)
            painter.setPen(QColor("#2c3e50"))
            text_width = painter.fontMetrics().horizontalAdvance(name)
            painter.drawText(min(x + 2, right - text_width), held_top - 4 + 10, name)
            painter.setPen(QPen(QColor("#2c3e50"), 1, Qt.PenStyle.DashLine))


def _ticks(start, end, count: int = 8) -> list:
    """Round-ish times across the span, so an axis reads as a clock."""
    seconds = max((end - start).total_seconds(), 1.0)
    for step in (10, 20, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if seconds / step <= count:
            break
    first = start + datetime.timedelta(
        seconds=(step - start.timestamp() % step) % step)
    ticks, when = [], first
    while when <= end:
        ticks.append(when)
        when += datetime.timedelta(seconds=step)
    return ticks


def _duration(seconds: float) -> str:
    if seconds < 120:
        return "%.0f s" % seconds
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return "%d m %02d s" % (minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "%d h %02d m" % (hours, minutes)


class _Stat(QWidget):
    """A number big enough to read from a tripod away, with its caption."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.setSpacing(0)
        self.value = QLabel("--")
        value_font = QFont(self.font())
        value_font.setPointSizeF(value_font.pointSizeF() + 8)
        value_font.setBold(True)
        self.value.setFont(value_font)
        self.value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label = QLabel(caption)
        label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.value)
        layout.addWidget(label)

    def show_value(self, text: str, colour: str = "") -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {colour};" if colour else "")


class CoverageDock(QDockWidget):
    """The numbers first, then the timeline they summarise."""

    def __init__(self, parent=None):
        super().__init__("Coverage", parent)
        self.setObjectName("coverage_dock")

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 2, 4, 4)

        stats = QHBoxLayout()
        stats.setSpacing(4)
        self.stat_frames = _Stat("frames total")
        self.stat_util = _Stat("C2-C3 camera use")
        self.stat_totality_frames = _Stat("frames in totality")
        self.stat_totality = _Stat("totality")
        for stat in (self.stat_frames, self.stat_util,
                     self.stat_totality_frames, self.stat_totality):
            stats.addWidget(stat)
        stats.addStretch(1)
        layout.addLayout(stats)

        self.view = CoverageView()
        layout.addWidget(self.view, 1)

        legend = QHBoxLayout()
        legend.setSpacing(10)
        for command, caption in (("take_picture", "single frame"),
                                 ("take_bracket", "bracket"),
                                 ("relay_burst", "burst")):
            swatch = QLabel()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet("background: %s; border-radius: 2px;"
                                 % STYLES[command][0])
            legend.addWidget(swatch)
            legend.addWidget(QLabel(caption))
        legend.addSpacing(14)
        self.summary = QLabel("No script loaded")
        legend.addWidget(self.summary)
        legend.addStretch(1)
        layout.addLayout(legend)
        self.setWidget(body)

    def set_schedule(self, scheduler, moments: dict) -> None:
        jobs = list(scheduler.get_jobs()) if scheduler is not None else []
        self.view.set_schedule(jobs, moments)
        self.summary.setText(self._summary(jobs, moments))

        numbers = analyse(self.view._events, moments or {})
        self.stat_frames.show_value(str(numbers["frames_total"]))
        if numbers["utilization"] is None:
            self.stat_util.show_value("--")
            self.stat_totality_frames.show_value("--")
            self.stat_totality.show_value("--")
            return
        utilization = numbers["utilization"]
        # Green is earned above two thirds - the measured ceiling of this
        # body's tether path is around seventy percent - amber below it, red
        # when totality is mostly camera silence.
        colour = ("#27ae60" if utilization >= 0.60
                  else "#e67e22" if utilization >= 0.35 else "#c0392b")
        self.stat_util.show_value("%d%%" % round(utilization * 100), colour)
        self.stat_totality_frames.show_value(str(numbers["frames_totality"]))
        self.stat_totality.show_value(_duration(numbers["totality_seconds"]))

    @staticmethod
    def _summary(jobs, moments: dict) -> str:
        if not jobs:
            return "No script loaded"
        counts: dict = {}
        for job in jobs:
            command = JOB_COMMANDS.get(getattr(job, "id", None), "")
            if command in FRAME_COMMANDS:
                counts[command] = counts.get(command, 0) + 1
        if not counts:
            return "%d commands, none of them frames" % len(jobs)
        parts = ["%d %s" % (count, name.replace("_", " "))
                 for name, count in sorted(counts.items())]
        return "  ".join(parts)
