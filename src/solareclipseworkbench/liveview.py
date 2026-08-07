"""Live View dockable window with software focus peaking and digital zoom.

Written on the `fujisdk` branch in February and stranded there when the Fuji
work was re-applied onto the flat layout: it lived under ``ui/`` and nothing
carried it across.  Restored unchanged apart from this note - every SDK call it
makes (reconnect, wait_ready, set_priority, the live view properties) is still
in ``fujixsdk.camera``.

It drives the Fuji SDK directly rather than gphoto2, so it is the only preview
path the X-T4 has.  ``LiveViewThread`` in ``camera.py`` is the gphoto2 one and
does not apply here.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
from PyQt6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF, QThread,
                          pyqtSignal, pyqtSlot, QTimer, Qt)
from PyQt6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox,
    QStatusBar, QSizePolicy, QDockWidget,
)

from fujixsdk import LiveViewStream
from fujixsdk._constants import (
    ERRCODE_COMBINATION,
    FOCUS_MODE_NAMES,
    SHUTTER_SPEED_NAMES,
    LIVEVIEW_QUALITY_FINE,
    LIVEVIEW_QUALITY_NAMES,
    LIVEVIEW_SIZE_XGA,
    PRIORITY_CAMERA,
    PRIORITY_PC,
)
from solareclipseworkbench.hardware_registry import HARDWARE, seconds_to_next_camera_job
from fujixsdk import recovery as sdk_recovery
from fujixsdk._errors import BusyError, XSDKError
from fujixsdk.camera import Camera

log = logging.getLogger(__name__)

# What the X-T4 can actually be set to, out of the 89 values the SDK names -
# the table runs from 1/180000" to 60 minutes and neither end exists on this
# body.  Microseconds, as the SDK counts them.
def _shutter_table_key(name: str) -> int:
    """The table key for a named speed, because the arithmetic lies.

    Fuji's third-stop values are powers of two underneath the labels: the
    speed sold as 1/8000 is keyed 122 us (a true 1/8192), and 30 seconds is
    keyed 32_000_000 us.  Bounds computed as 1_000_000/8000 and 30*1_000_000
    missed both endpoints, so the dropdown ran 1/6400 to 25" - with 1/8000,
    the speed the whole eclipse is shot at, not on the list.
    """
    return next(k for k, v in SHUTTER_SPEED_NAMES.items() if v == name)


#: The X-T4 stills range on the mechanical shutter.  The electronic-only
#: speeds above 1/8000 stay out: the eclipse body runs MS, which refuses them.
_SHUTTER_FASTEST_US = _shutter_table_key('1/8000"')
_SHUTTER_SLOWEST_US = _shutter_table_key('30"')


#: Speeds in the SDK's table that this body refuses outright.  Measured
#: 7 August, all 64 dropdown values set and read back with retries: these nine
#: come back 0x2003 every time.  They are the half-stop-only values other
#: bodies in the range use - the table is one grid for every model, and the
#: X-T4 steps in thirds.  This is what the 5 August "invalid parameter
#: combination" was: a value on the list that the body never had.
_REFUSED_NAMES = ('1/6000"', '1/3000"', '1/1500"', '1/750"', '1/350"',
                  '1/180"', '1/90"', '1/45"', '1/1.5"')


def dropdown_shutter_speeds() -> list[int]:
    """Every speed the shutter dropdown offers, fastest first.

    Cut from the SDK's name table because the X-T4 answers CapShutterSpeed
    with an empty list; module-level so a test can hold the endpoints still.
    """
    refused = {k for k, v in SHUTTER_SPEED_NAMES.items() if v in _REFUSED_NAMES}
    return sorted(k for k in SHUTTER_SPEED_NAMES
                  if isinstance(k, int) and k not in refused
                  and _SHUTTER_FASTEST_US <= k <= _SHUTTER_SLOWEST_US)

# The body reports no ISO list on some firmware, so this is the fallback.  These
# are the native values; the extended ones below 160 and above 12800 are pulled
# from a different sensor gain and are not what a corona wants.
_ISO_FALLBACK = [160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600,
                 2000, 2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800]

# How long to keep trying an exposure write while the body says it is busy.  The
# frame that was already in flight when the stream paused has to land first.
_EXPOSURE_WRITE_BUDGET_S = 2.0

# How long to wait for the scheduler to finish with the camera before giving up
# on starting a stream or writing a setting.  Longer than a single frame, short
# enough that the window answers rather than hanging.
_STREAM_SETUP_WAIT_S = 3.0

#: How long to wait for a frame read already in flight before calling the link
#: stalled.  Measured on the X-T4, 80 reads: the frame wait is 13.6 ms median,
#: 63 ms at the 90th percentile and 95 ms at worst, and fetching the JPEG is
#: another 24 ms.  Nothing came near a second.  Six seconds is therefore not a
#: budget for a slow read - it is far beyond any healthy one, so reaching it
#: means the read is not coming back at all.
_FRAME_READ_WAIT_S = 6.0

#: A camera job this close leaves no room for the exposure catch-up as the
#: stream tears down: the catch-up takes the camera lock, and this teardown
#: usually runs *because* that job wants the camera.  Wide enough to cover the
#: drop guard's own longest wait, so the frame never loses the race.
_CATCHUP_MIN_GAP_S = 6.0


def _seconds_to_next_camera_job():
    """Seconds to the next scheduled camera job, or None if nothing is due."""
    scheduler = HARDWARE.get('scheduler')
    if scheduler is None:
        return None
    try:
        return seconds_to_next_camera_job(scheduler)
    except Exception:
        log.debug("Could not read the gap to the next camera job", exc_info=True)
        return None

#: How long a person's click waits for the camera lock.  Longer than the stream
#: setup wait on purpose: somebody has just asked for something and is watching
#: for it to happen, while the camera panel's background polling of the body can
#: hold the lock for seconds at a time.  Failing at three seconds made a
#: deliberate change look broken when it only needed to queue.
_EXPOSURE_LOCK_WAIT_S = 12.0

#: The teardown of the last stream, still running on its finisher thread.  A
#: new stream must not start under it: the finisher ends with StopLiveView and
#: a priority handover, which fired into a freshly started stream would stop
#: it dead.  Module-level because every open builds a fresh window.
_pending_stop: threading.Thread | None = None

# How long to wait before asking the body for another frame.  Measured on the
# X-T4: it emits a frame every ~200 ms and will not be hurried - polling with no
# gap at all, or at 5, 20, 40 or 80 ms, returned the same 40 frames in eight
# seconds, and the frame size (L, M or S) made no difference either.  Five
# frames a second is simply what the body gives.
#
# So the old 5/10 ms loop asked five times per frame and threw four answers
# away, each one a USB round trip taken with the camera lock held.  That is the
# traffic the shooting path had to compete with, and the pressure that let a
# waiting thread starve.  Measured side by side over twelve seconds:
#
#     5 / 10 ms     299 polls    61 frames    4.9 polls per frame
#     120 / 40 ms    60 polls    60 frames    1.0 polls per frame
#
# The cost is latency: a frame can now sit up to ~40 ms longer before being
# collected.  On a preview watched by eye that is invisible, and it buys back
# four fifths of the USB traffic during a run.
_POLL_AFTER_FRAME_MS = 120
_POLL_WHEN_EMPTY_MS = 40

#: Once several polls running have come back empty the stream is not producing,
#: so there is nothing to be gained by asking briskly.
_POLL_WHEN_QUIET_MS = 200
_QUIET_AFTER_EMPTY_POLLS = 5

# Focus peaking: edge threshold and overlay colour
_PEAKING_THRESHOLD = 30
_PEAKING_COLOR = QColor(255, 0, 0, 180)  # semi-transparent red


# ------------------------------------------------------------------
# Laplacian helpers (shared by focus peaking overlay + focus score)
# ------------------------------------------------------------------

#: Half-width of the gap left open at the centre of the crosshair, in pixels.
#: The sun is centred by the gap, so it must not be covered by the very lines
#: that mark it.
_CROSSHAIR_GAP_PX = 14

#: Radius of the ring drawn around the gap - a circle is easier to centre a
#: disc in than crossing lines alone.
_CROSSHAIR_RING_PX = 34


def _draw_crosshair(pixmap: QPixmap) -> None:
    """Mark the centre of the frame, in place.

    Drawn twice: a wide dark line first, then a thin bright one on top, so the
    mark reads against both the black sky around totality and the white disc
    of the partial phases.  Neither colour alone survives both.
    """
    width, height = pixmap.width(), pixmap.height()
    if width <= 0 or height <= 0:
        return
    cx, cy = width // 2, height // 2

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for colour, thickness in ((QColor(0, 0, 0, 160), 3), (QColor(255, 255, 255, 220), 1)):
        painter.setPen(QPen(colour, thickness))
        painter.drawLine(0, cy, cx - _CROSSHAIR_GAP_PX, cy)
        painter.drawLine(cx + _CROSSHAIR_GAP_PX, cy, width, cy)
        painter.drawLine(cx, 0, cx, cy - _CROSSHAIR_GAP_PX)
        painter.drawLine(cx, cy + _CROSSHAIR_GAP_PX, cx, height)
        painter.drawEllipse(QPoint(cx, cy), _CROSSHAIR_RING_PX, _CROSSHAIR_RING_PX)
    painter.end()


def _compute_laplacian(img: QImage) -> np.ndarray | None:
    """Compute Laplacian of a QImage. Returns the raw Laplacian array or None."""
    w, h = img.width(), img.height()
    if w < 8 or h < 8:
        return None
    img_gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img_gray.bits()
    ptr.setsize(img_gray.bytesPerLine() * h)
    gray = np.frombuffer(ptr, dtype=np.uint8).reshape(h, img_gray.bytesPerLine())
    gray = gray[:, :w].astype(np.int16)
    lap = (
        gray[:-2, 1:-1] + gray[2:, 1:-1] +
        gray[1:-1, :-2] + gray[1:-1, 2:] -
        4 * gray[1:-1, 1:-1]
    )
    return lap


def _focus_score(lap: np.ndarray) -> float:
    """Focus sharpness metric: mean |Laplacian|. Higher = sharper."""
    return float(np.mean(np.abs(lap)))


#: Peaking colours worth having on a sun.  Red is the camera convention and
#: the default, but it is the one colour a red-filtered or low-hanging sun
#: already is - hence the alternatives, which no amount of threshold fixes.
PEAKING_COLOURS = {
    "red": QColor(255, 0, 0, 180),
    "cyan": QColor(0, 255, 255, 200),
    "yellow": QColor(255, 255, 0, 200),
    "white": QColor(255, 255, 255, 220),
}

#: How much edge a pixel needs before it is painted.  Lower shows more, which
#: on a filtered disc with little contrast is what finds the limb at all;
#: higher keeps only the strongest edges, for when everything lights up.
PEAKING_SENSITIVITY = {"high": 12, "normal": 30, "low": 60}


def _peaking_mask_from_lap(lap: np.ndarray, w: int, h: int,
                           colour: QColor | None = None,
                           threshold: int | None = None) -> QImage:
    """Build an RGBA overlay from Laplacian edges above threshold."""
    colour = colour or _PEAKING_COLOR
    edges = np.abs(lap) > (_PEAKING_THRESHOLD if threshold is None else threshold)
    overlay = np.zeros((h, w, 4), dtype=np.uint8)
    r, g, b, a = (colour.red(), colour.green(), colour.blue(), colour.alpha())
    overlay[1:h - 1, 1:w - 1, 0][edges] = r
    overlay[1:h - 1, 1:w - 1, 1][edges] = g
    overlay[1:h - 1, 1:w - 1, 2][edges] = b
    overlay[1:h - 1, 1:w - 1, 3][edges] = a
    return QImage(overlay.data, w, h, w * 4,
                  QImage.Format.Format_RGBA8888).copy()


# ------------------------------------------------------------------
# Focus score sparkline graph
# ------------------------------------------------------------------

class _Histogram(QWidget):
    """Luminance histogram of the preview, with a clipped-pixel readout.

    Read the caveat before trusting it for exposure: this is the 8-bit JPEG the
    body sends for live view, already through a film simulation and a tone
    curve, not the RAW.  Highlights that read as clipped here often still have a
    stop or more of headroom in the file, and the preview only tracks the
    exposure at all when the body has "preview exposure in manual mode" on.

    What it is good for is the partial phases: through the solar filter the disc
    is the only bright thing in the frame, so the top end of the histogram is
    the disc and nothing else, and watching it not touch the wall is a real
    check that the filtered exposure is in range.
    """

    _HEIGHT = 74

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(self._HEIGHT)
        self._bins: np.ndarray | None = None
        self._clipped = 0.0
        self._black = 0.0

    def set_image(self, img: QImage):
        w, h = img.width(), img.height()
        if w < 8 or h < 8:
            return
        gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
        ptr = gray.bits()
        ptr.setsize(gray.bytesPerLine() * h)
        data = np.frombuffer(ptr, dtype=np.uint8).reshape(h, gray.bytesPerLine())[:, :w]
        self._bins = np.bincount(data.reshape(-1), minlength=256).astype(float)
        total = float(data.size)
        # 250 rather than 255: the JPEG's own tone curve rolls the top off, so
        # waiting for a true 255 understates how close the disc is to the wall.
        self._clipped = 100.0 * self._bins[250:].sum() / total
        self._black = 100.0 * self._bins[:6].sum() / total
        self.update()

    def clear(self):
        self._bins = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 24))
        w, h = self.width(), self.height()
        if self._bins is None or w < 16:
            painter.setPen(QColor(120, 120, 130))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Histogram - no frame")
            painter.end()
            return

        # Log scale: a solar disc is a few percent of the frame against black, so
        # on a linear scale the disc is invisible next to the background spike.
        bins = np.log1p(self._bins)
        peak = bins.max() or 1.0
        painter.setPen(QColor(210, 210, 220))
        for x in range(w):
            lo = x * 256 // w
            hi = max(lo + 1, (x + 1) * 256 // w)
            value = bins[lo:hi].max() / peak
            bar = int(value * (h - 18))
            if bar > 0:
                painter.drawLine(x, h - 18, x, h - 18 - bar)

        painter.setPen(QColor(90, 90, 100))
        painter.drawLine(0, h - 18, w, h - 18)
        painter.setPen(QColor(200, 80, 90) if self._clipped > 0.05 else QColor(150, 150, 160))
        painter.drawText(6, h - 4, f"clipped {self._clipped:.2f}%")
        painter.setPen(QColor(150, 150, 160))
        painter.drawText(w - 96, h - 4, f"black {self._black:.1f}%")
        painter.end()


class _FocusScoreGraph(QWidget):
    """Sparkline graph showing recent focus score history with peak marker."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scores: list[float] = []
        self._peak = 0.0
        self.setFixedHeight(36)
        self.setMinimumWidth(200)

    def set_score(self, score: float):
        self._scores.append(score)
        self._peak = max(self._peak, score)
        if len(self._scores) > 200:
            self._scores.pop(0)
        self.update()

    def reset(self):
        self._scores.clear()
        self._peak = 0.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(20, 20, 20))

        if len(self._scores) < 2 or self._peak <= 0:
            painter.setPen(QColor(100, 100, 100))
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                             "Focus Score: --")
            painter.end()
            return

        text_w = 70
        graph_w = w - text_w - 4
        n = len(self._scores)
        margin = 3

        # Peak reference line (top of graph)
        peak_y = margin
        painter.setPen(QPen(QColor(255, 60, 60, 100), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, peak_y, graph_w, peak_y)

        # Sparkline
        painter.setPen(QPen(QColor(0, 200, 0), 1))
        step = graph_w / max(n - 1, 1)
        graph_h = h - 2 * margin
        for i in range(1, n):
            x0 = int((i - 1) * step)
            x1 = int(i * step)
            y0 = int(h - margin - (self._scores[i - 1] / self._peak) * graph_h)
            y1 = int(h - margin - (self._scores[i] / self._peak) * graph_h)
            painter.drawLine(x0, y0, x1, y1)

        # Current score text — green if near peak, yellow otherwise
        score = self._scores[-1]
        near_peak = score > self._peak * 0.95
        color = QColor(0, 255, 0) if near_peak else QColor(255, 200, 0)
        painter.setPen(color)
        painter.drawText(graph_w + 4, 0, text_w, h // 2,
                         Qt.AlignmentFlag.AlignVCenter, f"{score:.1f}")
        # Peak value (smaller, red)
        painter.setPen(QColor(255, 80, 80))
        painter.drawText(graph_w + 4, h // 2, text_w, h // 2,
                         Qt.AlignmentFlag.AlignVCenter, f"pk {self._peak:.1f}")

        painter.end()


# ------------------------------------------------------------------
# Display label with click-to-zoom and zoom box overlay
# ------------------------------------------------------------------

class _Loupe(QWidget):
    """A patch of the frame at native pixels or better, beside the preview.

    The preview is scaled to fit a dock, so the picture on screen is a
    downsample of what the sensor read - and a downsample of a soft edge and
    a downsample of a sharp one look far more alike than the originals do.
    This shows a small region without that scaling, magnified with nearest
    neighbour so no interpolation smooths the very edge being judged.  It is
    the loupe convention: focus on the patch, frame on the picture.

    The patch follows the zoom centre, so clicking the limb in the preview
    points the loupe at it.
    """

    #: Side of the patch taken from the source frame, in source pixels.
    PATCH_PX = 96

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._score: float | None = None
        self._peak = 0.0
        self.setFixedSize(180, 180)
        self.setToolTip(
            "The centre of the zoom box at native pixels, magnified without "
            "smoothing.  Focus on this, frame on the picture.")

    def reset(self):
        self._pixmap = None
        self._score = None
        self._peak = 0.0
        self.update()

    def set_patch(self, patch: QImage, score: float | None) -> None:
        side = min(self.width(), self.height()) - 2
        self._pixmap = QPixmap.fromImage(patch).scaled(
            side, side, Qt.AspectRatioMode.KeepAspectRatio,
            # Nearest neighbour on purpose: smoothing here would invent the
            # sharpness the person is trying to judge.
            Qt.TransformationMode.FastTransformation)
        self._score = score
        if score is not None:
            self._peak = max(self._peak, score)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(0, 0, self.width(), self.height(), QColor(20, 20, 20))
        if self._pixmap is None:
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(0, 0, self.width(), self.height(),
                             Qt.AlignmentFlag.AlignCenter, "Loupe")
            painter.end()
            return

        x = (self.width() - self._pixmap.width()) // 2
        y = (self.height() - self._pixmap.height()) // 2
        painter.drawPixmap(x, y, self._pixmap)

        if self._score is not None:
            # Against the peak this patch has reached, not the whole frame's:
            # turning the focuser is a search for this number's maximum, and
            # a percentage says which way it is going at a glance.
            share = (self._score / self._peak) if self._peak > 0 else 0.0
            colour = QColor(0, 255, 0) if share > 0.95 else QColor(255, 200, 0)
            painter.fillRect(0, self.height() - 16, self.width(), 16,
                             QColor(0, 0, 0, 150))
            painter.setPen(colour)
            painter.drawText(0, self.height() - 16, self.width(), 16,
                             Qt.AlignmentFlag.AlignCenter,
                             f"{self._score:.1f}   {share * 100:.0f}% of best")
        painter.end()


class _DisplayLabel(QLabel):
    """QLabel that handles clicks to reposition the zoom center and draws
    a green zoom-region box overlay on the unzoomed (1x) view."""

    zoom_center_changed = pyqtSignal(float, float)  # source-image normalized

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._zoom_center = QPointF(0.5, 0.5)
        self._zoom_factor = 1
        self._show_box = False
        # Crop info set each frame by LiveViewWindow
        self._source_size: tuple[int, int] | None = None
        self._crop_rect: tuple[int, int, int, int] | None = None  # (x, y, w, h)

    @property
    def zoom_center(self) -> QPointF:
        return self._zoom_center

    def set_zoom_factor(self, factor: int):
        self._zoom_factor = factor
        self.update()

    def set_frame_info(self, source_w: int, source_h: int,
                       crop_rect: tuple[int, int, int, int] | None):
        """Update source image dimensions and current crop region."""
        self._source_size = (source_w, source_h)
        self._crop_rect = crop_rect

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.pixmap() and not self.pixmap().isNull():
            pm = self.pixmap()
            pm_w, pm_h = pm.width(), pm.height()
            x_off = (self.width() - pm_w) / 2
            y_off = (self.height() - pm_h) / 2

            # Click normalized within the displayed pixmap (0-1)
            nx = (event.position().x() - x_off) / pm_w
            ny = (event.position().y() - y_off) / pm_h
            nx = max(0.0, min(1.0, nx))
            ny = max(0.0, min(1.0, ny))

            # Map back to source image coordinates if we're showing a crop
            if self._crop_rect and self._source_size:
                cx, cy, cw, ch = self._crop_rect
                sw, sh = self._source_size
                src_nx = (cx + nx * cw) / sw
                src_ny = (cy + ny * ch) / sh
            else:
                src_nx, src_ny = nx, ny

            src_nx = max(0.0, min(1.0, src_nx))
            src_ny = max(0.0, min(1.0, src_ny))
            self._zoom_center = QPointF(src_nx, src_ny)
            self._show_box = True
            self.zoom_center_changed.emit(src_nx, src_ny)
            self.update()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        # Draw box only on the full (uncropped) view to preview the zoom region
        if not self._show_box or self._crop_rect is not None:
            return
        pm = self.pixmap()
        if pm is None or pm.isNull():
            return

        pm_w, pm_h = pm.width(), pm.height()
        x_off = (self.width() - pm_w) / 2
        y_off = (self.height() - pm_h) / 2

        # At 1x combo, preview the 2x crop; otherwise use the combo value
        factor = max(self._zoom_factor, 2)
        box_w = pm_w / factor
        box_h = pm_h / factor
        bcx = x_off + self._zoom_center.x() * pm_w
        bcy = y_off + self._zoom_center.y() * pm_h

        bx = max(x_off, min(x_off + pm_w - box_w, bcx - box_w / 2))
        by = max(y_off, min(y_off + pm_h - box_h, bcy - box_h / 2))

        painter = QPainter(self)
        pen = QPen(QColor(0, 255, 0, 200))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(bx, by, box_w, box_h))
        painter.end()


# ------------------------------------------------------------------
# Background frame reader
# ------------------------------------------------------------------

class _FrameWorker(QObject):
    """Polls LiveViewStream in a background thread and emits JPEG frames."""

    frame_ready = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, stream: LiveViewStream, usb_lock):
        super().__init__()
        self._stream = stream
        self._usb_lock = usb_lock
        self._running = False
        # Set while something else needs the camera.  Reading frames leaves a
        # 5 ms gap, so a setting written while this loop runs is answered 0x1006
        # almost every time - the body really is busy, with us.
        self._paused = threading.Event()
        # Clear while a frame read is in flight.  read_frame holds the camera
        # lock for its whole duration, so anything else that needs the camera
        # waits on this rather than racing the lock - and racing is what failed:
        # a read is 14 ms and never over 95 ms (measured, 80 reads), yet an ISO
        # change still gave up after three seconds.  The loop releases the lock
        # and takes it again immediately, Python locks are not fair, and the
        # waiting thread simply kept losing.  Pausing the loop is what makes the
        # lock reachable; waiting for idle is what makes it safe.
        self._idle = threading.Event()
        self._idle.set()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no frame read is in flight.  False if one still is.

        False means the USB link is stalled inside the SDK, not that the loop
        is busy: a frame read that has not returned will not return because of
        anything done here.
        """
        return self._idle.wait(timeout)

    def set_stream(self, stream: LiveViewStream):
        """Point the loop at a new stream, for when one is stopped and remade.

        Writing an exposure stops live view outright, so the stream this worker
        was built with is gone by the time it resumes.  Without this the loop
        would read from the dead one.
        """
        self._stream = stream

    @pyqtSlot()
    def run(self):
        self._running = True
        idle_count = 0
        while self._running:
            if self._paused.is_set():
                QThread.msleep(20)
                continue

            # The SDK is not thread-safe and a scheduled frame runs in its own
            # thread.  Reading a preview while one is shooting killed the session
            # outright - 0x2001, repeating - so the frame is skipped rather than
            # taken whenever the camera is spoken for.  The gphoto2 live view has
            # always done this; this one was written without it.
            if not self._usb_lock.acquire(timeout=0.05):
                QThread.msleep(20)
                continue
            self._idle.clear()
            try:
                data = self._stream.read_frame()
            except Exception as e:
                self.error.emit(str(e))
                break
            finally:
                self._idle.set()
                self._usb_lock.release()
            if data:
                self.frame_ready.emit(data)
                idle_count = 0
                # A frame just arrived, so the next is ~200 ms away.  Asking
                # before then only produces empty answers.
                QThread.msleep(_POLL_AFTER_FRAME_MS)
            else:
                idle_count += 1
                QThread.msleep(_POLL_WHEN_QUIET_MS
                               if idle_count > _QUIET_AFTER_EMPTY_POLLS
                               else _POLL_WHEN_EMPTY_MS)

    def stop(self):
        self._running = False


# ------------------------------------------------------------------
# Main live view dock widget
# ------------------------------------------------------------------

class LiveViewWindow(QDockWidget):
    """Dockable live view window with software focus peaking and digital zoom.

    Can be docked to left/right/bottom of the main window, floated as a
    standalone window, or closed via the title-bar button.
    """

    #: A background exposure write has finished, however it went.  The write
    #: cannot touch a widget itself: it runs off the GUI thread precisely so
    #: the window keeps painting while it waits.
    write_finished = pyqtSignal()

    #: The write thread's other three wishes, delivered onto the GUI thread
    #: the same way.  It used to call the widgets directly - a status bar
    #: message, a combo resync, even a full stop_stream, all from a plain
    #: Python thread, which Qt does not survive reliably.  And the old
    #: QTimer.singleShot(0, stop_stream) escape hatch never fired at all: a
    #: plain thread has no Qt event loop to run a timer on.
    status_message = pyqtSignal(str, int)
    stop_requested = pyqtSignal()
    exposure_resync = pyqtSignal()
    #: An exposure read coming home from its worker: (speed, iso), or None
    #: when the body would not answer.
    exposure_read = pyqtSignal(object)
    #: Stream negotiation coming home from its worker: ready carries the
    #: body's settable shutter speeds, failed carries the message to show.
    stream_ready = pyqtSignal(object)
    stream_failed = pyqtSignal(str)
    #: The window-build probe coming home: focus mode and the body's ISO list.
    camera_probed = pyqtSignal(object)

    _ZOOM_LEVELS = [1, 2, 4, 8]

    def __init__(self, camera, parent: QWidget | None = None):
        """`camera` is the workbench adapter, not the bare SDK handle.

        It is taken whole so the preview can serialise on the very lock the
        scheduler holds while it shoots.  Anything less and two threads end up
        in the SDK together, which drops the USB session for good.
        """
        super().__init__("Live View", parent)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )

        # Both from the adapter: the handle to talk to, and the lock that says
        # when it is safe to.
        self._usb_lock = getattr(camera, "_usb_lock", None) or threading.RLock()
        # Two objects, deliberately: the SDK camera to stream from, and the
        # adapter it came wrapped in, which is what carries ensure_ready and the
        # rest of the workbench's camera contract.  Calling an adapter method on
        # the unwrapped handle is what took the GUI down on 4 August.
        self.write_finished.connect(self._on_write_finished)
        #: The last exposure read from the body, for when it is too busy to ask.
        self._last_exposure = None
        self._write_thread = None
        self._adapter = camera
        self._camera = getattr(camera, "_sdk_cam", camera)
        self._stream: LiveViewStream | None = None
        self._worker: _FrameWorker | None = None
        self._thread: QThread | None = None
        # Threads left running because a frame read never came back.  Held only
        # so Qt does not delete a running QThread; nothing reads this back.
        self._stalled_threads: list = []

        # Software processing state
        self._zoom_factor = 1
        self._zoom_center = QPointF(0.5, 0.5)
        self._peaking_enabled = False
        self._histogram_enabled = False
        # On by default: centring the sun is what the preview is opened for
        # more often than focusing, and a mark that has to be switched on is
        # one more thing to remember at C2 minus two minutes.
        self._crosshair_enabled = True
        self._loupe_enabled = True
        self._peaking_colour = PEAKING_COLOURS["red"]
        self._peaking_threshold = PEAKING_SENSITIVITY["normal"]

        # FPS tracking
        self._frame_count = 0
        self._fps = 0.0
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps)

        # Before _build_ui: building the widgets kicks off the first exposure
        # read and the camera probe, and their workers check these flags and
        # emit these signals.  Initialised after, the window died with an
        # AttributeError before it ever appeared (6 August).
        self._exposure_read_busy = False
        self._starting = False
        self._wanted_quality = None
        self.exposure_resync.connect(self._refresh_exposure)
        self.exposure_read.connect(self._show_exposure)
        self.stream_ready.connect(self._on_stream_ready)
        self.stream_failed.connect(self._on_stream_failed)
        self.camera_probed.connect(self._apply_probe)
        self.stop_requested.connect(self.stop_stream)

        self.setMinimumSize(680, 560)
        self._build_ui()

        # After the widgets exist: the status bar the messengers write to.
        self.status_message.connect(self._status_bar.showMessage)

    def _build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)

        # Display area
        self._display = _DisplayLabel()
        self._display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._display.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._display.setMinimumSize(640, 480)
        self._display.setStyleSheet("background-color: black; color: #888; font-size: 24px;")
        self._display.setText("No Signal")
        self._display.zoom_center_changed.connect(self._on_zoom_center_changed)
        layout.addWidget(self._display)

        # Focus score graph
        self._focus_graph = _FocusScoreGraph()
        # The loupe sits beside the focus trace, so the patch, its score and
        # the score's history are read in one glance while a hand is on the
        # focuser.
        focus_row = QHBoxLayout()
        focus_row.addWidget(self._focus_graph, 1)
        self._loupe = _Loupe()
        focus_row.addWidget(self._loupe)
        layout.addLayout(focus_row)

        self._histogram = _Histogram()
        self._histogram.setVisible(False)
        layout.addWidget(self._histogram)

        # Control bar
        controls = QHBoxLayout()

        self._peaking_btn = QPushButton("Focus Peaking: OFF")
        self._peaking_btn.setCheckable(True)
        self._peaking_btn.toggled.connect(self._on_peaking_toggled)
        controls.addWidget(self._peaking_btn)

        # Red is the camera convention, and the one colour a filtered sun
        # already is; the alternatives are what make peaking readable on a
        # disc no threshold can fix.
        self._peaking_colour_combo = QComboBox()
        for name in PEAKING_COLOURS:
            self._peaking_colour_combo.addItem(name, name)
        self._peaking_colour_combo.setToolTip("What colour to paint the edges")
        self._peaking_colour_combo.currentIndexChanged.connect(
            self._on_peaking_colour_changed)
        controls.addWidget(self._peaking_colour_combo)

        self._peaking_sensitivity_combo = QComboBox()
        for name in PEAKING_SENSITIVITY:
            self._peaking_sensitivity_combo.addItem(name, name)
        self._peaking_sensitivity_combo.setCurrentText("normal")
        self._peaking_sensitivity_combo.setToolTip(
            "How much edge counts: high shows more, for a low-contrast disc")
        self._peaking_sensitivity_combo.currentIndexChanged.connect(
            self._on_peaking_sensitivity_changed)
        controls.addWidget(self._peaking_sensitivity_combo)

        self._loupe_btn = QPushButton("Loupe: ON")
        self._loupe_btn.setCheckable(True)
        self._loupe_btn.setChecked(True)
        self._loupe_btn.setToolTip(
            "A patch at native pixels, magnified without smoothing (L)")
        self._loupe_btn.toggled.connect(self._on_loupe_toggled)
        controls.addWidget(self._loupe_btn)

        self._histogram_btn = QPushButton("Histogram: OFF")
        self._histogram_btn.setCheckable(True)
        self._histogram_btn.toggled.connect(self._on_histogram_toggled)
        controls.addWidget(self._histogram_btn)

        self._crosshair_btn = QPushButton("Crosshair: ON")
        self._crosshair_btn.setCheckable(True)
        self._crosshair_btn.setChecked(True)
        self._crosshair_btn.setToolTip(
            "Mark the centre of the frame, to centre the sun by (C)")
        self._crosshair_btn.toggled.connect(self._on_crosshair_toggled)
        controls.addWidget(self._crosshair_btn)

        controls.addWidget(QLabel("Zoom:"))
        self._zoom_combo = QComboBox()
        for level in self._ZOOM_LEVELS:
            self._zoom_combo.addItem(f"{level}x", level)
        self._zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)
        controls.addWidget(self._zoom_combo)

        controls.addWidget(QLabel("Quality:"))
        self._quality_combo = QComboBox()
        for val, name in LIVEVIEW_QUALITY_NAMES.items():
            self._quality_combo.addItem(name, val)
        self._quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        controls.addWidget(self._quality_combo)

        # Exposure.  Live view shows what the body is set to, so a preview too
        # dark to focus by can only be fixed here - and until now the window
        # neither showed the exposure nor let it be changed.
        controls.addWidget(QLabel("Shutter:"))
        self._shutter_combo = QComboBox()
        self._shutter_combo.currentIndexChanged.connect(self._on_shutter_changed)
        controls.addWidget(self._shutter_combo)

        controls.addWidget(QLabel("ISO:"))
        self._iso_combo = QComboBox()
        self._iso_combo.currentIndexChanged.connect(self._on_iso_changed)
        controls.addWidget(self._iso_combo)

        controls.addStretch(1)
        layout.addLayout(controls)

        # Buttons
        btn_bar = QHBoxLayout()
        # No Start button: opening the window IS starting - nobody opens a
        # preview in order to not look through it.  The stream begins as soon
        # as the window is up (deferred one tick so it paints first), and Stop
        # closes the whole window, so one button does the one thing left.
        # _start_btn survives as a hidden widget because the stream machinery
        # reports into it on failure paths; it is never shown.
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self.start_stream)
        self._start_btn.hide()

        self._stop_btn = QPushButton("Stop && Close")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setMinimumHeight(34)
        self._stop_btn.clicked.connect(self.close)
        btn_bar.addWidget(self._stop_btn, 1)

        # Focusing wants every pixel the screen has.  F (or double-clicking
        # the picture) fills the screen; F or Esc brings the window back.
        self._fullscreen_btn = QPushButton("⛶")
        self._fullscreen_btn.setToolTip("Fullscreen (F, or double-click the "
                                        "picture; Esc to come back)")
        self._fullscreen_btn.setFixedWidth(36)
        self._fullscreen_btn.setMinimumHeight(34)
        self._fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        btn_bar.addWidget(self._fullscreen_btn)
        for keys, action in (("F", self.toggle_fullscreen),
                             ("Escape", self._leave_fullscreen),
                             ("C", self._crosshair_btn.toggle),
                             ("L", self._loupe_btn.toggle)):
            shortcut = QShortcut(QKeySequence(keys), self)
            # Reaches the toggle from whichever child holds the focus.
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
        self._display.installEventFilter(self)
        self._was_floating = True

        QTimer.singleShot(200, self._auto_start)

        layout.addLayout(btn_bar)

        # Status bar
        self._status_bar = QStatusBar()
        self._focus_label = QLabel("Focus: --")
        self._exposure_label = QLabel("Exposure: --")
        self._fps_label = QLabel("FPS: --")
        self._status_bar.addWidget(self._focus_label)
        self._status_bar.addWidget(self._exposure_label)
        self._status_bar.addPermanentWidget(self._fps_label)
        layout.addWidget(self._status_bar)

        self.setWidget(container)
        self._populate_controls()
        self._populate_exposure()

    def _populate_controls(self):
        """Ask the body its focus mode and true ISO list, off the GUI thread.

        On a worker: the same SDK-on-the-GUI-thread hazard as everywhere
        else, and a wedged body here would hang the window as it opens.
        """
        def worker():
            probe: dict = {}
            try:
                probe["focus"] = self._camera.get_focus_mode()
            except Exception:
                probe["focus"] = None
            try:
                probe["isos"] = [i for i in self._camera.get_supported_iso() if i > 0]
            except Exception:
                probe["isos"] = []
            try:
                self.camera_probed.emit(probe)
            except RuntimeError:
                log.debug("No window left for the camera probe")

        threading.Thread(target=worker, daemon=True, name="liveview-probe").start()

    def _apply_probe(self, probe: dict):
        """Back on the GUI thread: show the focus mode, upgrade the ISO list."""
        fm = probe.get("focus")
        if fm is not None:
            self._focus_label.setText(f"Focus: {FOCUS_MODE_NAMES.get(fm, f'0x{fm:04X}')}")
        isos = probe.get("isos") or []
        if isos:
            self._fill_iso_combo(isos)

    def _reconnect_camera(self) -> bool:
        """Reconnect the SDK session to clear a stuck busy state.

        Runs on the negotiation worker, so every message goes through the
        guarded queued emit rather than straight into the status bar.
        """
        self._say("Camera busy -- reconnecting...")
        try:
            self._camera.reconnect()
        except Exception as e:
            log.error("Reconnect failed: %s", e)
            self._say(f"Error: reconnect failed -- {e}")
            return False
        try:
            if not self._camera.wait_ready(timeout_s=5.0):
                log.error("Camera still busy after reconnect")
                self._say("Error: camera busy -- power-cycle camera")
                return False
        except Exception as e:
            log.error("Camera communication lost after reconnect: %s", e)
            self._say(f"Error: camera disconnected -- {e}")
            return False
        try:
            self._camera.set_priority(PRIORITY_PC)
        except XSDKError as e:
            log.warning("Could not set PC priority after reconnect: %s", e)
        return True

    def _ensure_ready(self, priority: int) -> bool:
        """Clear whatever is stopping the body, and take the given priority.

        Prefers the adapter, which is where the workbench's camera contract
        lives; falls back to the SDK recovery directly for a window handed a
        bare SDK camera, which the constructor still allows.

        Never lets the shutter fire: a preview being opened is not a reason for
        a frame to go off.
        """
        ready = getattr(self._adapter, 'ensure_ready', None)
        if ready is not None:
            return ready(priority, allow_shot=False, why="live view")
        return sdk_recovery.unblock(self._camera, priority, allow_shot=False,
                                    why="live view")

    def _start_live_view_stream(self) -> bool:
        """Create and start a LiveViewStream. Returns True on success.

        Runs on the negotiation worker, so the quality combo is not read here
        - the GUI half of start_stream stashed its value before the worker
        was spawned.
        """
        quality = self._wanted_quality or LIVEVIEW_QUALITY_FINE
        self._stream = LiveViewStream(self._camera, size=LIVEVIEW_SIZE_XGA, quality=quality)
        try:
            self._stream.start()
            return True
        except XSDKError as e:
            log.warning("Live view start failed: %s", e)
            self._stream = None
            return False

    def _auto_start(self):
        """Start streaming the moment the window exists.

        Deferred a tick so the dock is painted before the seconds of camera
        negotiation begin, and guarded so a window closed in that tick does
        not start a stream into nowhere.
        """
        try:
            if self.isVisible() and self._thread is None:
                self.start_stream()
        except Exception:
            log.warning("Live view auto-start failed", exc_info=True)

    def start_stream(self):
        """Start the live view stream and frame worker thread.

        Only the widgets are handled here; every SDK step runs on a
        negotiation worker and reports back through stream_ready or
        stream_failed.  Never on the GUI thread: on macOS the SDK waits by
        running the Cocoa run loop, which dispatches Qt events, and any slot
        that then touches the SDK deadlocks inside the SDK's critical
        section.
        """
        if self._thread is not None or self._starting:
            return

        # A previous stream may still be shutting down on its finisher thread.
        # Normally that is over in well under a second; when it is not, the
        # link is stalled and starting into it would only wedge this stream
        # too, so wait briefly and come back rather than freeze the window.
        pending = _pending_stop
        if pending is not None and pending.is_alive():
            pending.join(timeout=2.0)
            if pending.is_alive():
                self._status_bar.showMessage(
                    "The last live view is still shutting down - retrying "
                    "shortly; reconnect the camera if this goes on", 8000)
                QTimer.singleShot(3000, self._auto_start)
                return

        self._starting = True
        self._start_btn.setEnabled(False)
        self._status_bar.showMessage("Preparing camera...")
        # Read on the GUI thread, used on the worker.
        self._wanted_quality = self._quality_combo.currentData()

        threading.Thread(target=self._negotiate_stream, daemon=True,
                         name="liveview-start").start()

    def _fail_start(self, message: str) -> None:
        """stream_failed.emit that survives the window being destroyed."""
        try:
            self.stream_failed.emit(message)
        except RuntimeError:
            log.debug("No window left to tell: %s", message)

    def _negotiate_stream(self):
        """Every SDK step of starting the stream, on the negotiation worker."""
        # Talking to the SDK, so it waits for the scheduler the same way a
        # scheduled command waits for it.
        if not self._usb_lock.acquire(timeout=_STREAM_SETUP_WAIT_S):
            self._fail_start(
                "The camera is busy with the schedule - try again in a moment")
            return
        try:
            # Whatever the body is holding on to, clear it and take PC priority
            # in one step.  This used to drain, wait five seconds, then set the
            # priority and hope - which failed with "camera is busy" whenever
            # the blocker was something a drain does not fix, such as a live
            # view left running by a crashed run.  No flush shot: a shutter
            # firing because somebody opened a preview is never what was
            # wanted.
            self._ensure_ready(PRIORITY_PC)

            try:
                ready = self._camera.wait_ready(timeout_s=5.0)
            except Exception as e:
                log.error("Camera communication error: %s", e)
                self._fail_start(f"Error: camera disconnected -- {e}")
                return

            if not ready:
                if not self._reconnect_camera():
                    self._fail_start("Error: the camera stayed busy")
                    return
            else:
                try:
                    self._camera.set_priority(PRIORITY_PC)
                except XSDKError as e:
                    log.warning("Could not set PC priority: %s", e)

            # Try to start live view; if camera is busy, reconnect and retry once
            if not self._start_live_view_stream():
                log.info("Live view failed, attempting reconnect...")
                if not self._reconnect_camera():
                    self._fail_start("Error: the camera stayed busy")
                    return
                if not self._start_live_view_stream():
                    log.error("Failed to start live view after reconnect")
                    self._fail_start("Error: live view failed -- power-cycle camera")
                    return

            # Now, not at window build, is when the body can be asked which
            # speeds it takes: CapShutterSpeed answers for the current exposure
            # mode and shutter type, so with PC priority held and the session in
            # its real state its answer replaces the static table - and the mixed
            # third-stop/half-stop grid that produced 0x2003 refusals goes away.
            try:
                supported = sorted(v for v in
                                   self._camera.get_supported_shutter_speeds()
                                   if v > 0)
            except XSDKError as exc:
                log.debug("CapShutterSpeed not answering: %s", exc)
                supported = []
        finally:
            self._usb_lock.release()

        try:
            self.stream_ready.emit(supported)
        except RuntimeError:
            # The window died while the camera was being negotiated; the
            # stream must not keep running into nowhere.
            log.debug("No window left for the stream; stopping it")
            stream, self._stream = self._stream, None
            try:
                if stream is not None:
                    stream.stop()
            except Exception:
                log.debug("Error stopping an orphaned stream", exc_info=True)

    def _on_stream_failed(self, message: str):
        self._starting = False
        self._status_bar.showMessage(message, 6000)
        self._start_btn.setEnabled(True)

    def _on_stream_ready(self, supported):
        """Back on the GUI thread with the camera streaming: wire the widgets."""
        self._starting = False
        if self._thread is not None:
            return
        if not self.isVisible():
            # Closed while the camera was being negotiated.  stop_stream on
            # the way out found nothing to stop - the stream did not exist
            # yet - so it is stopped here instead of running into nowhere.
            self.stop_stream()
            return

        self._worker = _FrameWorker(self._stream, self._usb_lock)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.error.connect(self._on_error)
        self._thread.start()

        self._frame_count = 0
        self._focus_graph.reset()
        self._loupe.reset()
        self._fps_timer.start(1000)

        if supported:
            log.info("The body lists %d settable shutter speeds in this mode",
                     len(supported))
            self._fill_shutter_combo(supported)

        # The dials may have been turned by hand since the window was opened.
        self._refresh_exposure()

        self._stop_btn.setEnabled(True)
        self._status_bar.showMessage("Streaming...")

    def stop_stream(self):
        """Stop the live view stream and worker thread.

        Only the flag-setting and the widgets are handled here.  The waiting -
        for the frame read in flight, for the worker thread, for the body to
        take its priority back - moves to a finisher thread: on 5 August a
        stalled frame read kept this method on the GUI thread for over half a
        minute, and a Stop button that freezes the whole app reads as a hang,
        not a stop.
        """
        self._fps_timer.stop()

        worker, thread, stream = self._worker, self._thread, self._stream
        self._worker = self._thread = self._stream = None

        if worker:
            worker.stop()
            # Disconnect signals to prevent callbacks during teardown
            try:
                worker.frame_ready.disconnect(self._on_frame)
                worker.error.disconnect(self._on_error)
            except (TypeError, RuntimeError):
                pass

        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._display.clear()
        self._display.setText("No Signal")
        self._fps_label.setText("FPS: --")
        self._status_bar.showMessage("Stopped")

        if worker is None and thread is None and stream is None:
            # Nothing ever started, so there is nothing to wait out and no
            # priority to hand back.
            return

        # Module-level, not an attribute: every open makes a fresh window, so
        # the window starting the next stream is never the one whose teardown
        # is still in flight.
        global _pending_stop
        _pending_stop = threading.Thread(
            target=self._finish_stop, args=(worker, thread, stream),
            name="liveview-stop", daemon=True)
        _pending_stop.start()

    def _finish_stop(self, worker, thread, stream):
        """The slow half of stop_stream, off the GUI thread.

        Everything here talks to the SDK through the adapter, which serialises
        on the usb lock, so a stream restarted while this still runs cannot
        end up inside the SDK alongside it.
        """
        if worker is not None:
            # The loop exits between frames, so what decides how long this takes
            # is the frame read in flight, not the flag just set.
            worker.wait_idle(_FRAME_READ_WAIT_S)
        if thread is not None:
            thread.quit()
            if not thread.wait(5000):
                # Not terminated.  Killing a thread parked inside an SDK call
                # leaves the SDK holding a half-finished USB transfer: on
                # 4 August that turned a slow stop into a window that would not
                # close at all, and it is the likeliest source of the segfault
                # the night before.  The thread is kept referenced instead -
                # deleting a running QThread crashes Qt outright - and it ends
                # when its read finally returns.
                log.error("A live view frame read has not returned; the USB link "
                          "is stalled.  Leaving the thread to finish rather than "
                          "killing it mid-transfer - reconnect the camera if the "
                          "preview does not come back")
                self._stalled_threads.append((thread, worker))

        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.debug("Error stopping live view stream", exc_info=True)

        # Give the camera back.  Live view takes PRIORITY_PC to stream and this
        # never returned it, so the body stayed in PC priority for the rest of
        # the run - while the relay goes on firing the shutter physically and the
        # session's idea of who is in charge no longer matches the body's.  That
        # is the shape of the 0x2001 that took two rehearsals down, and it
        # outlived closing the window, which is why it looked unrelated.
        # Ask the bus before dialling the body: `vanished` is only set by
        # whoever meets the first CommunicationError, which after a hub drop
        # may be nobody.  Detect never touches the handle, so it is the one
        # question that is safe to ask here.
        if not getattr(self._camera, 'vanished', False):
            still_here = getattr(self._camera, 'still_on_bus', None)
            if still_here is not None and not still_here():
                log.warning("The camera is no longer on the USB bus; abandoning "
                            "the live view handle rather than handing priority "
                            "back to a body that is not there")
                try:
                    self._camera.vanished = True
                except Exception:
                    log.debug("Could not mark the camera vanished", exc_info=True)

        if getattr(self._camera, 'vanished', False):
            # No handshake with a body that is off the bus: on 5 August the
            # calls made here after a power-off ended in a SIGSEGV from the
            # SDK's device-removal path, with no Python frame on the stack.
            log.warning("Live view stopped, but the camera is off the bus; "
                        "power it on and detect it again")
        elif self._thread is not None:
            # A new stream took the body over while this one was still being
            # waited out; the priority is its to keep now.
            log.info("Live view stopped; a new stream already owns the camera")
        else:
            try:
                self._camera.set_priority(PRIORITY_CAMERA)
                log.info("Live view stopped; camera priority returned to the body")
            except Exception:
                log.warning("Could not return camera priority after live view; "
                            "the body may refuse the relay until it is reconnected",
                            exc_info=True)
            # The settings the stream blocked go back on the body: any write
            # that fell inside its 0x1006 window - a beads exposure load, a
            # bracket rung - would otherwise stay stale into whatever fires
            # next, including a relay burst that cannot set exposure at all.
            # Losses while the window was open are the accepted price; losses
            # after it closes are not.
            #
            # Not when a frame is already waiting, though.  The catch-up takes
            # the camera lock, and this teardown often runs *because* a frame
            # wants the camera - doing it here would make the stream's exit
            # the reason that frame was dropped.  Nothing is lost by waiting:
            # the cache rides along with the next configure, which is that
            # very frame's.
            gap = _seconds_to_next_camera_job()
            if gap is not None and gap < _CATCHUP_MIN_GAP_S:
                log.info("Live view stopped; leaving the blocked settings to "
                         "the frame due in %.1fs, which will carry them", gap)
            else:
                apply_pending = getattr(self._adapter, "apply_pending", None)
                if apply_pending is not None:
                    try:
                        apply_pending()
                    except Exception:
                        log.warning("Exposure catch-up after live view failed",
                                    exc_info=True)

    @pyqtSlot(bytes)
    def _on_frame(self, data: bytes):
        img = QImage()
        if not img.loadFromData(data):
            return

        if self._histogram_enabled:
            # The whole frame, before the zoom crop: the exposure is a property
            # of the scene, not of whichever corner is being magnified to focus.
            self._histogram.set_image(img)

        source_w, source_h = img.width(), img.height()
        crop_rect = None

        # Taken from the frame as it arrived, before the zoom crop and before
        # peaking paints over it: the loupe is there to judge the pixels the
        # body actually sent.
        if self._loupe_enabled:
            self._update_loupe(img)

        # Software digital zoom -- crop around zoom center
        if self._zoom_factor > 1:
            crop_w = source_w // self._zoom_factor
            crop_h = source_h // self._zoom_factor
            cx = int(self._zoom_center.x() * source_w)
            cy = int(self._zoom_center.y() * source_h)
            x = max(0, min(source_w - crop_w, cx - crop_w // 2))
            y = max(0, min(source_h - crop_h, cy - crop_h // 2))
            crop_rect = (x, y, crop_w, crop_h)
            img = img.copy(QRect(x, y, crop_w, crop_h))

        # Tell the display label what region of the source image is shown
        self._display.set_frame_info(source_w, source_h, crop_rect)

        # Compute Laplacian once — used for both focus score and peaking overlay
        lap = _compute_laplacian(img)
        if lap is not None:
            self._focus_graph.set_score(_focus_score(lap))
            if self._peaking_enabled:
                overlay = _peaking_mask_from_lap(lap, img.width(), img.height(),
                                                 self._peaking_colour,
                                                 self._peaking_threshold)
                painter = QPainter(img)
                painter.drawImage(0, 0, overlay)
                painter.end()

        pixmap = QPixmap.fromImage(img)
        scaled = pixmap.scaled(
            self._display.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if self._crosshair_enabled:
            _draw_crosshair(scaled)
        self._display.setPixmap(scaled)
        self._frame_count += 1

    def _update_loupe(self, img: QImage) -> None:
        """Send the patch around the zoom centre to the loupe, with its score.

        The score is the patch's own, not the frame's: focusing on the limb
        while the rest of the frame is empty sky moves the whole-frame number
        barely at all, which is what made it hard to read.
        """
        side = min(_Loupe.PATCH_PX, img.width(), img.height())
        if side < 8:
            return
        cx = int(self._zoom_center.x() * img.width())
        cy = int(self._zoom_center.y() * img.height())
        x = max(0, min(img.width() - side, cx - side // 2))
        y = max(0, min(img.height() - side, cy - side // 2))
        patch = img.copy(QRect(x, y, side, side))

        lap = _compute_laplacian(patch)
        self._loupe.set_patch(patch, None if lap is None else _focus_score(lap))

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        log.error("Live view error: %s", msg)
        self._status_bar.showMessage(f"Error: {msg}")
        self.stop_stream()

    def _update_fps(self):
        self._fps = self._frame_count
        self._frame_count = 0
        self._fps_label.setText(f"FPS: {self._fps:.0f}")

    def _on_peaking_toggled(self, checked: bool):
        self._peaking_enabled = checked
        self._peaking_btn.setText(f"Focus Peaking: {'ON' if checked else 'OFF'}")

    def _on_histogram_toggled(self, checked: bool):
        self._histogram_enabled = checked
        self._histogram_btn.setText(f"Histogram: {'ON' if checked else 'OFF'}")
        # These two lines had been pasted into the peaking-sensitivity
        # handler, where `checked` does not exist: changing the sensitivity
        # raised NameError, and the histogram button set a flag without ever
        # showing or hiding the widget.
        self._histogram.setVisible(checked)
        if not checked:
            self._histogram.clear()

    def _on_crosshair_toggled(self, checked: bool):
        self._crosshair_enabled = checked
        self._crosshair_btn.setText(f"Crosshair: {'ON' if checked else 'OFF'}")

    def _on_loupe_toggled(self, checked: bool):
        self._loupe_enabled = checked
        self._loupe_btn.setText(f"Loupe: {'ON' if checked else 'OFF'}")
        self._loupe.setVisible(checked)

    def _on_peaking_colour_changed(self):
        name = self._peaking_colour_combo.currentData()
        self._peaking_colour = PEAKING_COLOURS.get(name, PEAKING_COLOURS["red"])

    def _on_peaking_sensitivity_changed(self):
        name = self._peaking_sensitivity_combo.currentData()
        self._peaking_threshold = PEAKING_SENSITIVITY.get(name,
                                                          PEAKING_SENSITIVITY["normal"])

    def _on_zoom_changed(self, index: int):
        val = self._zoom_combo.currentData()
        if val is not None:
            self._zoom_factor = val
            self._display.set_zoom_factor(val)
            self._focus_graph.reset()
            self._loupe.reset()

    def _on_zoom_center_changed(self, nx: float, ny: float):
        self._zoom_center = QPointF(nx, ny)
        self._focus_graph.reset()
        self._loupe.reset()

    def _on_quality_changed(self, index: int):
        """Change the preview quality, through the same guarded write path.

        Never straight from here: the SDK is not thread-safe, and the frame
        worker is inside read_image.
        """
        val = self._quality_combo.currentData()
        if val is not None and self._stream:
            self._write_in_background(
                lambda: self._camera.set_live_view_quality(val),
                "preview quality", "")

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------

    def _fill_shutter_combo(self, speeds):
        self._shutter_combo.blockSignals(True)
        self._shutter_combo.clear()
        for value in speeds:
            self._shutter_combo.addItem(
                str(SHUTTER_SPEED_NAMES.get(value, f"{value}us")), value)
        self._shutter_combo.blockSignals(False)

    def _populate_exposure(self):
        """Fill the shutter and ISO lists and show what the body is set to.

        From the static tables only, so window build never waits on the SDK.
        The shutter values start from the SDK's name table: CapShutterSpeed
        answers for the current exposure mode and shutter type, so asked here -
        at window build, before PC priority and the mode are established - it
        answers empty (the manual: "set the exposure mode and shutter type
        before calling this function").  Once the stream is up and the session
        is in its real state, the list is rebuilt from the body's own answer;
        the ISO list is likewise upgraded when the build probe comes home.
        """
        self._fill_shutter_combo(dropdown_shutter_speeds())
        self._fill_iso_combo(_ISO_FALLBACK)
        self._refresh_exposure()

    def _fill_iso_combo(self, isos):
        self._iso_combo.blockSignals(True)
        self._iso_combo.clear()
        for value in sorted(isos):
            self._iso_combo.addItem(str(value), value)
        self._iso_combo.blockSignals(False)

    def _refresh_exposure(self):
        """Read the body's exposure on a worker and show it when it lands.

        Under the camera lock, like everything else that talks to the SDK.  It
        ran without it - two reads from the GUI thread while the frame worker
        was inside read_image - which is a second thread in a library that
        cannot take one.  Skipped rather than waited for: this only updates a
        label, and the next call is a second away.

        On a worker thread: the lock timeout guards the lock, never the call
        itself, and a body that stops answering mid-read hangs the reader.  A
        worker can hang there and cost a label; the GUI thread cannot.
        """
        if self._exposure_read_busy:
            return
        self._exposure_read_busy = True

        def worker():
            skip, result = True, None
            try:
                skip, result = self._read_exposure()
            finally:
                self._exposure_read_busy = False
                if not skip:
                    try:
                        self.exposure_read.emit(result)
                    except RuntimeError:
                        log.debug("No window left to show the exposure on")

        threading.Thread(target=worker, daemon=True, name="exposure-read").start()

    def _read_exposure(self):
        """The SDK half of a refresh: ``(skip, result)``, safe on any thread.

        ``result`` is (speed, iso), or None when the body would not answer.
        ``skip`` means there is nothing worth showing - the camera was busy
        and nothing has ever been read.
        """
        if self._usb_lock.acquire(timeout=0.2):
            try:
                speed, _bulb = self._camera.get_shutter_speed()
                iso = self._camera.get_iso()
                self._last_exposure = (speed, iso)
                return False, self._last_exposure
            except Exception as exc:
                log.debug("Could not read the exposure: %s", exc)
                return False, None
            finally:
                self._usb_lock.release()
        # Busy: show the last exposure actually read rather than reading
        # anyway.  The controls still have to be put back - a refused write
        # leaves the dropdown showing what was clicked, which is the lie this
        # method exists to correct - and the last known value is a better
        # answer to "what is the camera on" than the one it refused.
        result = getattr(self, '_last_exposure', None)
        if result is None:
            log.debug("Skipped an exposure read: the camera was busy")
            return True, None
        return False, result

    def _show_exposure(self, result):
        """Back on the GUI thread: put the read (or its failure) on screen."""
        if result is None:
            self._exposure_label.setText("Exposure: unreadable")
            return
        speed, iso = result

        name = SHUTTER_SPEED_NAMES.get(speed, f"{speed}us")
        self._exposure_label.setText(f"Exposure: {name}  ISO {iso}")
        for combo, value, label in ((self._shutter_combo, speed, name),
                                    (self._iso_combo, iso, str(iso))):
            index = combo.findData(value)
            combo.blockSignals(True)
            if index < 0:
                # The body is on something the list does not offer.  Adding it
                # is the only way the control can tell the truth: without this
                # the resync quietly does nothing and the dropdown keeps
                # whatever was last clicked, so it reads 1/2 s while the camera
                # is somewhere else entirely.
                combo.addItem(label, value)
                index = combo.findData(value)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _say(self, text: str, ms: int = 0) -> None:
        """status_message.emit that survives the window being destroyed.

        The write thread outlives a redetect or a STOP; an emit on a dead
        window raises RuntimeError, and one of these sits between the lock
        acquire and the finally that releases it - unguarded, that error
        leaves the camera lock held forever.
        """
        try:
            self.status_message.emit(text, ms)
        except RuntimeError:
            log.debug("No window left to say: %s", text)

    def _ask_stop(self) -> None:
        try:
            self.stop_requested.emit()
        except RuntimeError:
            pass

    def _ask_resync(self) -> None:
        try:
            self.exposure_resync.emit()
        except RuntimeError:
            pass

    def _write_exposure(self, action, what: str, dial_hint: str) -> bool:
        """Write one exposure setting, with live view actually stopped.

        Two separate things had to be dealt with here.

        The body answers 0x1006 for as long as it is in live view at all, not
        merely while a frame is in flight - measured: with the worker paused and
        the lock held, every retry across a two second budget was refused.  It
        is a mode, not a transient, so no budget waits it out.  Hence stopping
        the stream rather than pausing the loop.

        And the loop must be paused before the lock is asked for.  Reads are
        14 ms and never over 95 ms, so the three second lock timeouts in the log
        were not one slow read - they were the loop taking the lock again the
        moment it dropped it, with the waiting thread losing every race.

        So the stream is stopped for the write and started again after.  The
        preview drops for about a second, which is the price of the setting
        landing at all.  Nothing else about the session changes: the handle,
        the PC priority and the worker thread all stay as they were.
        """
        worker = self._worker
        if worker is not None:
            worker.pause()
            # Paused above, so the loop will not take the lock again; this
            # waits out the one read still in flight.  Racing it instead is what
            # burned the whole three second timeout - not because a read is
            # slow, but because the loop reacquires immediately and the waiter
            # never wins.
            if not worker.wait_idle(_FRAME_READ_WAIT_S):
                # Do not write into a stalled link.  On 4 August the write went
                # ahead anyway and got 0x2001 - the session was already gone,
                # and everything after it, including handing priority back,
                # failed the same way.  Stopping is the honest outcome: the
                # preview is not coming back on this session.
                log.warning("A frame read has not returned after %.0fs; the USB "
                            "link is stalled, so the %s was not written",
                            _FRAME_READ_WAIT_S, what)
                self._say(
                    "The camera stopped responding - live view stopped", 8000)
                worker.stop()
                self._ask_stop()
                return False
        if not self._usb_lock.acquire(timeout=_EXPOSURE_LOCK_WAIT_S):
            # Logged, not only shown: this path was silent, so a refusal here
            # and a refusal from the body were indistinguishable afterwards.
            # And it does not blame a schedule - the lock is held by whatever is
            # using the camera, which with no script loaded is this window's own
            # polling of the body for the camera panel.
            log.warning("Could not set the %s: the camera was still in use after "
                        "%.0fs", what, _EXPOSURE_LOCK_WAIT_S)
            self._say(
                f"Could not set the {what}: the camera did not come free", 6000)
            if worker is not None:
                worker.resume()
            # Put the dropdown back to what the body is actually set to.  It
            # keeps the value that was clicked otherwise, so the control says
            # 1/500 while the camera is on 1/4000 and the picture does not
            # change - which is exactly how this was reported.
            self._ask_resync()
            return False

        was_streaming = self._stream is not None
        if was_streaming:
            self._say(f"Setting {what}...", 0)
            try:
                self._stream.stop()
            except Exception:
                log.debug("Error stopping live view for an exposure write",
                          exc_info=True)
            self._stream = None

        restart_failed = False
        try:
            deadline = time.monotonic() + _EXPOSURE_WRITE_BUDGET_S
            while True:
                try:
                    action()
                    return True
                except BusyError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        except BusyError as exc:
            # Busy with live view stopped is a different animal from busy while
            # it runs, and it is not a dial problem - so it does not get the
            # dial hint, which sent the last diagnosis chasing the wrong thing.
            log.warning("Could not set the %s: %s (live view was stopped for the "
                        "write, so the body is busy with something else)",
                        what, exc)
            self._say(
                f"{what.capitalize()} refused: the camera stayed busy", 6000)
            return False
        except XSDKError as exc:
            if getattr(exc, 'code', None) == ERRCODE_COMBINATION:
                # 0x2003 is not "bad value" - it is "this value conflicts with
                # the mode the body is in".  The T-dial hint sent the 5 August
                # diagnosis to the wrong dial: the usual culprits are the drive
                # dial (CL/CH restrict the range) and the MS/ES shutter type.
                log.warning("Could not set the %s: the body refuses it in its "
                            "current mode (%s) - check the drive dial and the "
                            "mechanical/electronic shutter setting", what, exc)
                self._say(
                    f"{what.capitalize()} refused: the body will not take it "
                    "in this mode - check drive dial and MS/ES setting", 8000)
                return False
            # The code matters: 0x1003 on a dead handle is not 0x1002 on a value
            # the body will not take, and both used to read as "check the dial".
            log.warning("Could not set the %s: %s", what, exc, exc_info=True)
            self._say(
                f"{what.capitalize()} refused ({exc}) - {dial_hint}", 6000)
            return False
        finally:
            if was_streaming:
                if self._start_live_view_stream():
                    if worker is not None:
                        worker.set_stream(self._stream)
                else:
                    # The preview cannot come back on its own from here; better
                    # to land in the stopped state the buttons describe than to
                    # leave a worker polling nothing.
                    log.error("Live view did not restart after setting the %s",
                              what)
                    restart_failed = True
            self._usb_lock.release()
            if restart_failed:
                # Never resumed: the worker still points at the stream that was
                # stopped for the write, and reading from that is what parks a
                # thread inside the SDK with the camera lock held.
                if worker is not None:
                    worker.stop()
                self._ask_stop()
                self._say(
                    "Live view stopped after the setting was written - "
                    "start it again", 8000)
            else:
                if worker is not None:
                    worker.resume()
                self._ask_resync()

    def _on_shutter_changed(self, index: int):
        value = self._shutter_combo.currentData()
        if value is None:
            return
        # The value goes into the name: a refusal logged as "the shutter
        # speed" left 5 August's 0x2003 undiagnosable - refused at what?
        name = SHUTTER_SPEED_NAMES.get(value, f"{value}us")
        self._write_in_background(
            lambda: self._camera.set_shutter_speed(value),
            f"shutter speed to {name}", "is the shutter dial on T?")

    def _on_iso_changed(self, index: int):
        value = self._iso_combo.currentData()
        if value is None:
            return
        self._write_in_background(lambda: self._camera.set_iso(value),
                                  f"ISO to {value}", "is the ISO dial on C?")

    def _write_in_background(self, action, what: str, dial_hint: str) -> None:
        """Write the setting off the GUI thread.

        The write waits for the frame in flight and then for the camera lock -
        up to eighteen seconds between them.  Done here, that is eighteen
        seconds of frozen window, which is what an ISO change looked like on
        4 August: the application hung.  Waiting is correct; waiting in the
        thread that paints is not.

        Only one write runs at a time.  A second click while one is in flight
        would have two threads writing exposures to a camera whose SDK is not
        thread-safe.
        """
        if getattr(self, '_write_thread', None) is not None \
                and self._write_thread.is_alive():
            self._status_bar.showMessage("Still setting the last one...", 3000)
            return

        self._shutter_combo.setEnabled(False)
        self._iso_combo.setEnabled(False)
        self._status_bar.showMessage("Setting the %s..." % what)

        def run():
            try:
                self._write_exposure(action, what, dial_hint)
            finally:
                try:
                    self.write_finished.emit()
                except RuntimeError:
                    # The window was destroyed while the write was in flight -
                    # a redetect or a STOP tore it down.  The setting landed
                    # or it did not; there is no widget left to tell.
                    log.debug("The live view closed before its write finished")

        self._write_thread = threading.Thread(
            target=run, name="exposure-write", daemon=True)
        self._write_thread.start()

    def _on_write_finished(self):
        """Back on the GUI thread once a write has finished, however it went."""
        self._shutter_combo.setEnabled(True)
        self._iso_combo.setEnabled(True)
        self._refresh_exposure()

    def is_streaming(self) -> bool:
        """True when frames are actually being fetched.

        Part of the contract both live view windows answer; see the note beside
        the gphoto2 one in gui.py.  It exists so callers stop reading `_thread`,
        which means different things in the two implementations - this one tears
        the thread down, the other keeps it and pauses it.
        """
        return self._thread is not None

    def set_totality_paused(self, paused: bool):
        """Stop streaming when the camera is needed, and stay stopped.

        The controller calls this every clock tick: for totality, and before
        any scheduled frame.

        It does not restart the stream.  Restarting used to look considerate -
        the preview came back by itself - but a stream start is a session
        operation, and this SDK does not survive many of them.  Measured on
        4 August, live view open during a run: an exposure write stopped and
        restarted the stream, the restart was refused 0x1006 because a frame
        had the camera, and within twenty seconds every call was answering
        0x2001 with the session dead.  The same afternoon, detection churning
        sessions corrupted the heap outright.

        So a preview during a run is a thing you open, look at, and lose to the
        next frame.  That is enough for a focus check before second contact,
        which is what it is for, and it costs the schedule one stream stop
        rather than one per frame for the rest of the eclipse.
        """
        if getattr(self, '_totality_paused', False) == paused:
            return
        self._totality_paused = paused
        if paused and self._thread is not None:
            log.info("Live view stopped: the camera is needed for a frame. "
                     "Open it again when there is a gap")
            self.stop_stream()
            self._status_bar.showMessage(
                "Stopped - the camera is needed for a frame", 8000)

    def toggle_fullscreen(self):
        """Fill the screen with the preview, or come back from it.

        A docked window is floated first - fullscreen is a top-level state -
        and where it was is put back on the way out.
        """
        if self.isFullScreen():
            self.showNormal()
            if not self._was_floating:
                self.setFloating(False)
        else:
            self._was_floating = self.isFloating()
            if not self.isFloating():
                self.setFloating(True)
            self.showFullScreen()

    def _leave_fullscreen(self):
        if self.isFullScreen():
            self.toggle_fullscreen()

    def eventFilter(self, obj, event):
        if obj is getattr(self, '_display', None) \
                and event.type() == QEvent.Type.MouseButtonDblClick:
            self.toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        try:
            self.stop_stream()
        except Exception:
            log.exception("Error stopping stream during close")
        super().closeEvent(event)
