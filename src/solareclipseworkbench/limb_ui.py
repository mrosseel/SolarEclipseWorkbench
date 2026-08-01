"""Baily's beads window, and the switch that governs the limb correction.

Two jobs in one dialog.  It draws the true lunar limb against the Sun's limb so
the beads can be seen forming and going out, and it carries the switch that
decides whether the corrected contacts reach the scheduler at all -- with the
size of the correction spelled out next to it, so an implausible one at an
untested location is obvious before it is trusted.
"""

import logging

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                             QSlider, QVBoxLayout, QWidget)

from solareclipseworkbench.limb_correction import (K2, EARTH_RADIUS_KM, is_enabled,
                                                   set_enabled, solve_limb)

# How far either side of a contact the slider reaches.
SLIDER_RANGE_S = 8.0
SLIDER_STEPS = 320

PROFILE_COLOUR = QColor(150, 120, 200)
SUN_COLOUR = QColor(240, 170, 60)
BEAD_COLOUR = QColor(255, 220, 90)
MEAN_COLOUR = QColor(130, 130, 130)


def _to_arcsec(value_km, distance_km):
    """Convert a radial distance in km at the Moon into arcseconds."""
    return np.degrees(value_km / distance_km) * 3600.0


class BeadsView(QWidget):
    """The profile itself: position angle across, limb height up."""

    def __init__(self, solution):
        super().__init__()
        self.solution = solution
        self.contact = "C2"
        self.offset_s = 0.0
        self.setMinimumSize(720, 320)

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


class BeadsWindow(QDialog):
    """Baily's beads, and the limb-correction switch."""

    def __init__(self, eclipse_date, longitude, latitude, altitude, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Baily's beads")

        try:
            self.solution = solve_limb(eclipse_date, latitude, longitude, altitude)
        except Exception as exc:
            logging.warning("Could not solve the lunar limb profile: %s", exc)
            self.solution = None

        layout = QVBoxLayout(self)

        if self.solution is None:
            layout.addWidget(QLabel(
                "No limb profile available here.\n\n"
                "Either this location sees no totality, or the lunar limb blob is not\n"
                "installed.  Contact times will use the mean lunar limb."))
            return

        layout.addWidget(QLabel(self._summary()))

        self.enabled_box = QCheckBox("Apply the limb correction to scheduled moments")
        self.enabled_box.setChecked(is_enabled())
        self.enabled_box.toggled.connect(self._on_toggled)
        layout.addWidget(self.enabled_box)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        self.view = BeadsView(self.solution)
        layout.addWidget(self.view)

        controls = QHBoxLayout()
        self.contact_box = QComboBox()
        self.contact_box.addItems(["C2", "C3"])
        self.contact_box.currentTextChanged.connect(self.view.set_contact)
        self.contact_box.currentTextChanged.connect(lambda _: self.slider.setValue(0))
        controls.addWidget(self.contact_box)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-SLIDER_STEPS, SLIDER_STEPS)
        self.slider.setValue(0)
        self.slider.valueChanged.connect(self._on_slider)
        controls.addWidget(self.slider, 1)

        self.time_label = QLabel()
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        self._on_slider(0)
        self._check_plausibility()

    def _summary(self):
        solution = self.solution
        lines = []
        for name in ("C2", "C3"):
            contact = solution.c2 if name == "C2" else solution.c3
            corrected = solution.c2_limb if name == "C2" else solution.c3_limb
            lines.append(
                f"{name}  mean limb {solution.to_utc(contact).strftime('%H:%M:%S.%f')[:-4]}"
                f"   corrected {solution.to_utc(corrected).strftime('%H:%M:%S.%f')[:-4]}"
                f"   {solution.correction_seconds(name):+.2f} s"
                f"   bead window {solution.window_seconds(name):.1f} s")
        mean_duration = (solution.c3 - solution.c2) * 3600.0
        corrected_duration = (solution.c3_limb - solution.c2_limb) * 3600.0
        lines.append(f"totality  {mean_duration:.1f} s  ->  {corrected_duration:.1f} s "
                     f"({corrected_duration - mean_duration:+.1f} s)")
        return "\n".join(lines)

    def _check_plausibility(self):
        """Say so when a correction is large enough to deserve a second look."""
        biggest = max(abs(self.solution.correction_seconds(name)) for name in ("C2", "C3"))
        if biggest > 15.0:
            self.warning.setText(
                f"This correction is {biggest:.0f} s, which is large.  That is normal "
                "close to the edge of the path, where a single valley governs the "
                "contact, but it is also what a bad profile looks like.  Check the "
                "beads below against the shape you expect before trusting it.")
            self.warning.setStyleSheet("color: #e0a030;")
        else:
            self.warning.setText("")

    def _on_toggled(self, checked):
        set_enabled(checked)
        logging.info("Lunar limb correction %s", "enabled" if checked else "disabled")

    def _on_slider(self, value):
        offset = value / SLIDER_STEPS * SLIDER_RANGE_S
        self.view.set_offset(offset)
        moment = self.view.moment_hours()
        self.time_label.setText(
            f"{self.solution.to_utc(moment).strftime('%H:%M:%S.%f')[:-4]}  ({offset:+.2f} s)")
