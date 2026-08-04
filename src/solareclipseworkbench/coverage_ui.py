"""What the loaded script will photograph, drawn on a timeline.

A script is a few hundred lines of times and exposures.  Read as text it is
impossible to see that an hour of partials is one frame every three minutes
while the whole of totality is a hundred seconds of everything at once - or
that a gap somebody meant to fill is still there.

Two rows, because the eclipse and totality are three orders of magnitude
apart: the whole eclipse, C1 to C4, and totality on its own scale.  A tick for
a frame, a block for anything that holds the shutter, and the contacts marked.

The scheduler is the source, not the file: it holds what will actually run,
after the reference moments were resolved and any line that named a moment
this eclipse does not have was dropped.
"""

from __future__ import annotations

import datetime
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

_CONTACTS = ("C1", "C2", "MAX", "C3", "C4")


class CoverageView(QWidget):
    """Two timelines of the scheduled frames, with an axis and a key.

    A row is a span of time drawn left to right: a clock along the bottom, a
    lane of single frames, and above it a lane of everything that holds the
    shutter for a stretch.  Separating the two matters - a bracket and a frame
    are both "a command" and one of them is seven frames over six seconds.
    """

    LANE = 13                 # a bar's height
    HEADER = 15               # the row's title line
    AXIS = 13                 # the clock under it
    GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: list = []          # (when, command, seconds)
        self._moments: dict = {}
        self.setMinimumHeight(2 * (self.HEADER + 2 * self.LANE + self.AXIS + self.GAP))

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
            events.append((when, command, STYLES[command][1]))
        self._events = sorted(events)
        self.update()

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())

        if not self._events:
            painter.setPen(QColor("#7f8c8d"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Load a script to see what it photographs")
            return

        rows = [("Whole eclipse", self._span_of("C1", "C4"))]
        totality = self._totality_span()
        if totality:
            rows.append(("Totality and the beads", totality))

        height = self.height() // len(rows)
        for index, (title, span) in enumerate(rows):
            self._draw_row(painter, title, span, index * height, height)

    def _totality_span(self):
        """C2 to C3, widened to take in the bead bursts either side.

        The C2 burst starts before second contact - the diamond ring is the
        last bead before totality - so a row that begins exactly at C2 leaves
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

    def _span_of(self, first: str, last: str):
        start, end = self._moments.get(first), self._moments.get(last)
        if start is None or end is None:
            return None
        return (start.time_utc, end.time_utc)

    def _draw_row(self, painter, title, span, top, height) -> None:
        left, right = 8, self.width() - 8
        width = max(right - left, 1)

        if span is None:
            span = (self._events[0][0], self._events[-1][0])
        start, end = span
        seconds = max((end - start).total_seconds(), 1.0)
        inside = [e for e in self._events if start <= e[0] <= end]

        small = QFont(self.font())
        small.setPointSizeF(max(7.5, small.pointSizeF() - 2.0))
        painter.setFont(small)

        def x_of(when):
            return left + width * ((when - start).total_seconds() / seconds)

        held_top = top + self.HEADER
        single_top = held_top + self.LANE + 2
        axis_y = single_top + self.LANE + 4

        # Header: what this row is, and what is on it.
        frames = sum(1 for _, command, _ in inside if command in FRAME_COMMANDS)
        held = sum(1 for _, command, _ in inside
                   if command in FRAME_COMMANDS and STYLES[command][1] > 1.0)
        painter.setPen(QColor("#2c3e50"))
        bold = QFont(small)
        bold.setBold(True)
        painter.setFont(bold)
        painter.drawText(left, top + self.HEADER - 3, title)
        # Measured in the font it was drawn in.  Measuring the bold title with
        # the regular metrics understates it, and the line that follows lands on
        # top of the last few letters.
        title_width = painter.fontMetrics().horizontalAdvance(title)
        painter.setFont(small)
        painter.setPen(QColor("#7f8c8d"))
        painter.drawText(left + title_width + 12,
                         top + self.HEADER - 3,
                         "%s   %d commands, %d of them bursts or brackets"
                         % (_duration(seconds), frames, held))

        # Totality shaded behind everything, so the dense stretch is findable
        # on a row two hours wide.
        shade = self._span_of("C2", "C3")
        if shade and shade != span:
            x1, x2 = x_of(shade[0]), x_of(shade[1])
            painter.fillRect(QRectF(x1, held_top, max(x2 - x1, 1.5),
                                    axis_y - held_top),
                             QColor(230, 126, 34, 40))

        # The axis, with real clock times.
        painter.setPen(QPen(QColor("#bdc3c7"), 1))
        painter.drawLine(left, axis_y, right, axis_y)
        painter.setPen(QColor("#95a5a6"))
        for when in _ticks(start, end):
            x = int(x_of(when))
            painter.drawLine(x, axis_y, x, axis_y + 3)
            label = when.astimezone().strftime("%H:%M:%S" if seconds < 600 else "%H:%M")
            painter.drawText(x - painter.fontMetrics().horizontalAdvance(label) // 2,
                             axis_y + self.AXIS, label)

        # The frames themselves, in two lanes.
        for when, command, hold in inside:
            colour, _ = STYLES[command]
            x = x_of(when)
            if command not in FRAME_COMMANDS:
                continue
            if hold > 1.0:
                block = max(width * (hold / seconds), 2.0)
                painter.fillRect(QRectF(x, held_top, block, self.LANE - 2),
                                 QColor(colour))
            else:
                painter.fillRect(QRectF(x, single_top, 2.0, self.LANE - 2),
                                 QColor(colour))

        # Contacts on top, named where the names fit.
        painter.setPen(QPen(QColor("#2c3e50"), 1, Qt.PenStyle.DashLine))
        placed = []
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
            # Inside the edge: a name drawn past the right of the widget is
            # clipped to its first letter, which is how C3 and C4 both read "C".
            text_width = painter.fontMetrics().horizontalAdvance(name)
            painter.drawText(min(x + 2, right - text_width), held_top - 4, name)
            painter.setPen(QPen(QColor("#2c3e50"), 1, Qt.PenStyle.DashLine))


def _ticks(start, end, count: int = 8) -> list:
    """Round-ish times across the span, so the axis reads as a clock."""
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


class CoverageDock(QDockWidget):
    """The timeline, and the counts that go with it."""

    def __init__(self, parent=None):
        super().__init__("Coverage", parent)
        self.setObjectName("coverage_dock")

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 2, 4, 4)
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
