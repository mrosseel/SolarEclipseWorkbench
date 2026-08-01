"""Baily's beads, drawn under the eclipse geometry.

The true lunar limb against the Sun's limb, with the arcs where sunlight still
gets through filled in, and a slider over the seconds either side of a contact
so the beads can be watched forming and going out.  At the corrected contact the
solar limb sits tangent to the deepest valley and nothing is lit, which is the
arc calculation made visible.

The panel also states how large the correction is and whether it is being
applied, so an implausible one at an untested location is visible before it is
trusted.  The toolbar button is what turns it on and off.
"""

import logging

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QSlider,
                             QVBoxLayout, QWidget)

from solareclipseworkbench.limb_correction import K2, EARTH_RADIUS_KM, is_enabled, solve_limb

# How far either side of a contact the slider reaches.
SLIDER_RANGE_S = 8.0
SLIDER_STEPS = 320

PROFILE_COLOUR = QColor(150, 120, 200)
SUN_COLOUR = QColor(240, 170, 60)
BEAD_COLOUR = QColor(255, 220, 90)
MEAN_COLOUR = QColor(130, 130, 130)


def beads_icon(size=32):
    """A diamond ring, drawn rather than shipped: no icon in img/ suits this."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = size * 0.12
    diameter = size - 2 * margin

    # The corona, then the Moon covering it, offset just enough to leave a bead.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(SUN_COLOUR)
    painter.drawEllipse(QPointF(size / 2, size / 2), diameter / 2, diameter / 2)

    painter.setBrush(QColor(30, 30, 36))
    painter.drawEllipse(QPointF(size / 2 - size * 0.04, size / 2),
                        diameter / 2 * 0.94, diameter / 2 * 0.94)

    painter.setBrush(BEAD_COLOUR)
    painter.drawEllipse(QPointF(size / 2 + diameter / 2 * 0.86, size / 2 - size * 0.06),
                        size * 0.11, size * 0.11)
    painter.end()
    return QIcon(pixmap)


class BeadsView(QWidget):
    """The profile itself: position angle across, limb height up."""

    def __init__(self, solution):
        super().__init__()
        self.solution = solution
        self.contact = "C2"
        self.offset_s = 0.0
        self.setMinimumSize(320, 140)

    def set_contact(self, contact):
        self.contact = contact
        self.offset_s = 0.0
        self.update()

    def set_offset(self, offset_s):
        self.offset_s = offset_s
        self.update()

    def moment_hours(self):
        base = (self.solution.c2_limb if self.contact == "C2" else self.solution.c3_limb)
        return base + self.offset_s / 3600.0

    def visible_angles(self, span_deg=40.0):
        """The arc worth drawing: centred on where the contact happens."""
        from solareclipseworkbench.limb_correction import contact_position_angle
        centre = contact_position_angle(self.solution.evaluate(self.moment_hours()))
        return centre - span_deg / 2.0, centre + span_deg / 2.0

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(18, 18, 22))

        solution = self.solution
        if solution is None:
            return
        hours = self.moment_hours()
        elements = solution.evaluate(hours)

        low, high = self.visible_angles()
        angles = solution.angles
        # Work on the unwrapped window, so an arc straddling north still draws.
        shifted = (angles - low) % 360.0
        inside = shifted <= (high - low)
        if not inside.any():
            return

        window_angles = shifted[inside]
        order = np.argsort(window_angles)
        window_angles = window_angles[order]
        heights_km = solution.heights_km[inside][order]

        from solareclipseworkbench.limb_correction import solar_limb_reach
        reach = solar_limb_reach(elements, angles)[inside][order]
        # Both curves as heights above k2, in km, then arcseconds.
        sun_km = (reach - K2) * EARTH_RADIUS_KM
        margin = sun_km - heights_km

        span_km = max(heights_km.max(), sun_km.max()) - min(heights_km.min(), sun_km.min())
        span_km = max(span_km, 1.0)
        low_km = min(heights_km.min(), sun_km.min()) - 0.1 * span_km
        high_km = max(heights_km.max(), sun_km.max()) + 0.1 * span_km

        left, right, top, bottom = 60, self.width() - 12, 12, self.height() - 34

        def to_x(angle):
            return left + (angle / (high - low)) * (right - left)

        def to_y(km):
            return bottom - (km - low_km) / (high_km - low_km) * (bottom - top)

        # Mean limb, k2.
        painter.setPen(QPen(MEAN_COLOUR, 1, Qt.PenStyle.DashLine))
        painter.drawLine(left, int(to_y(0.0)), right, int(to_y(0.0)))

        # Beads: where the Sun still shows past the true limb.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BEAD_COLOUR)
        lit = margin > 0.0
        if lit.any():
            run_start = None
            for index, is_lit in enumerate(lit):
                if is_lit and run_start is None:
                    run_start = index
                elif not is_lit and run_start is not None:
                    self._fill_bead(painter, window_angles, heights_km, sun_km,
                                    run_start, index, to_x, to_y)
                    run_start = None
            if run_start is not None:
                self._fill_bead(painter, window_angles, heights_km, sun_km,
                                run_start, len(lit), to_x, to_y)

        self._draw_curve(painter, window_angles, heights_km, to_x, to_y, PROFILE_COLOUR, 2)
        self._draw_curve(painter, window_angles, sun_km, to_x, to_y, SUN_COLOUR, 1)

        painter.setPen(QColor(200, 200, 210))
        painter.drawText(left, bottom + 22,
                         f"position angle {low % 360:.0f}° to {high % 360:.0f}°")
        painter.drawText(left, top + 14,
                         f"{self.contact} {self.offset_s:+.2f} s     "
                         f"{int(lit.sum() and self._bead_count(lit))} beads")
        painter.setPen(PROFILE_COLOUR)
        painter.drawText(8, int(to_y(high_km)) + 20, "km")

    @staticmethod
    def _bead_count(lit):
        return int((lit & ~np.roll(lit, 1)).sum())

    @staticmethod
    def _draw_curve(painter, angles, values, to_x, to_y, colour, width):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(colour, width))
        polygon = QPolygonF([QPointF(to_x(a), to_y(v)) for a, v in zip(angles, values)])
        painter.drawPolyline(polygon)

    @staticmethod
    def _fill_bead(painter, angles, limb, sun, start, end, to_x, to_y):
        piece = list(range(start, end))
        points = [QPointF(to_x(angles[i]), to_y(sun[i])) for i in piece]
        points += [QPointF(to_x(angles[i]), to_y(limb[i])) for i in reversed(piece)]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BEAD_COLOUR)
        painter.drawPolygon(QPolygonF(points))


class BeadsPanel(QWidget):
    """Baily's beads, sitting under the eclipse geometry.

    Idle until it is told where and when the eclipse is; solving the limb
    profile costs a couple of seconds, so it happens once per location or date
    rather than on every repaint.
    """

    def __init__(self):
        super().__init__()
        self.solution = None
        self._context = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        self.summary = QLabel("Limb correction \u2014 set a location and a date")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.view = BeadsView(None)
        self.view.setMinimumHeight(150)
        layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.contact_box = QComboBox()
        self.contact_box.addItems(["C2", "C3"])
        self.contact_box.currentTextChanged.connect(self._on_contact)
        controls.addWidget(self.contact_box)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-SLIDER_STEPS, SLIDER_STEPS)
        self.slider.valueChanged.connect(self._on_slider)
        controls.addWidget(self.slider, 1)

        self.time_label = QLabel("-")
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled):
        self.contact_box.setEnabled(enabled)
        self.slider.setEnabled(enabled)

    def set_context(self, eclipse_date, longitude, latitude, altitude):
        """Point the panel at a place and a date, and solve the limb there."""
        context = (eclipse_date, longitude, latitude, altitude)
        if context == self._context:
            return
        self._context = context

        try:
            self.solution = solve_limb(eclipse_date, latitude, longitude, altitude)
        except Exception as exc:
            logging.warning("Could not solve the lunar limb profile: %s", exc)
            self.solution = None

        self.view.solution = self.solution
        self._set_controls_enabled(self.solution is not None)
        self.slider.setValue(0)
        self.refresh()

    def refresh(self):
        """Redraw the summary, which depends on whether the correction is on."""
        mark = "\u2713" if is_enabled() else "\u2717"

        if self.solution is None:
            self.summary.setText(f"{mark} Limb correction \u2014 no profile here")
            self.view.update()
            return

        solution = self.solution
        change = ((solution.c3_limb - solution.c2_limb) - (solution.c3 - solution.c2)) * 3600.0
        text = (f"{mark} Limb correction{'' if is_enabled() else ' (off)'}   "
                f"C2 {solution.correction_seconds('C2'):+.1f}s   "
                f"C3 {solution.correction_seconds('C3'):+.1f}s   "
                f"beads {solution.window_seconds('C2'):.1f}/{solution.window_seconds('C3'):.1f}s   "
                f"totality {change:+.1f}s")

        biggest = max(abs(solution.correction_seconds(name)) for name in ("C2", "C3"))
        if biggest > 15.0:
            text += "   \u26a0 large, check the beads"

        self.summary.setText(text)
        self.summary.setStyleSheet("color: #e0a030;" if biggest > 15.0 else "")
        self._on_slider(self.slider.value())
        self.view.update()

    def _on_contact(self, contact):
        self.view.set_contact(contact)
        self.slider.setValue(0)
        self._on_slider(0)

    def _on_slider(self, value):
        if self.solution is None:
            return
        offset = value / SLIDER_STEPS * SLIDER_RANGE_S
        self.view.set_offset(offset)
        moment = self.view.moment_hours()
        self.time_label.setText(
            f"{self.solution.to_utc(moment).strftime('%H:%M:%S.%f')[:-4]}  ({offset:+.2f} s)")
