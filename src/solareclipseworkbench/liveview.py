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
from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, QThread, pyqtSignal, pyqtSlot, QTimer, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox,
    QStatusBar, QSizePolicy, QDockWidget,
)

from fujixsdk import LiveViewStream
from fujixsdk._constants import (
    FOCUS_MODE_NAMES,
    SHUTTER_SPEED_NAMES,
    LIVEVIEW_QUALITY_FINE,
    LIVEVIEW_QUALITY_NAMES,
    LIVEVIEW_SIZE_XGA,
    PRIORITY_CAMERA,
    PRIORITY_PC,
)
from fujixsdk._errors import BusyError, XSDKError
from fujixsdk.camera import Camera

log = logging.getLogger(__name__)

# What the X-T4 can actually be set to, out of the 89 values the SDK names -
# the table runs from 1/180000" to 60 minutes and neither end exists on this
# body.  Microseconds, as the SDK counts them.
_SHUTTER_MIN_US = 1_000_000 // 8000
_SHUTTER_MAX_US = 30 * 1_000_000

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

# Focus peaking: edge threshold and overlay colour
_PEAKING_THRESHOLD = 30
_PEAKING_COLOR = QColor(255, 0, 0, 180)  # semi-transparent red


# ------------------------------------------------------------------
# Laplacian helpers (shared by focus peaking overlay + focus score)
# ------------------------------------------------------------------

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


def _peaking_mask_from_lap(lap: np.ndarray, w: int, h: int) -> QImage:
    """Build an RGBA overlay from Laplacian edges above threshold."""
    edges = np.abs(lap) > _PEAKING_THRESHOLD
    overlay = np.zeros((h, w, 4), dtype=np.uint8)
    r, g, b, a = (_PEAKING_COLOR.red(), _PEAKING_COLOR.green(),
                   _PEAKING_COLOR.blue(), _PEAKING_COLOR.alpha())
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

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

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
            try:
                data = self._stream.read_frame()
            except Exception as e:
                self.error.emit(str(e))
                break
            finally:
                self._usb_lock.release()
            if data:
                self.frame_ready.emit(data)
                idle_count = 0
                QThread.msleep(5)
            else:
                idle_count += 1
                QThread.msleep(30 if idle_count > 5 else 10)

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
        self._camera = getattr(camera, "_sdk_cam", camera)
        self._stream: LiveViewStream | None = None
        self._worker: _FrameWorker | None = None
        self._thread: QThread | None = None

        # Software processing state
        self._zoom_factor = 1
        self._zoom_center = QPointF(0.5, 0.5)
        self._peaking_enabled = False
        self._histogram_enabled = False

        # FPS tracking
        self._frame_count = 0
        self._fps = 0.0
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps)

        self.setMinimumSize(680, 560)
        self._build_ui()

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
        layout.addWidget(self._focus_graph)

        self._histogram = _Histogram()
        self._histogram.setVisible(False)
        layout.addWidget(self._histogram)

        # Control bar
        controls = QHBoxLayout()

        self._peaking_btn = QPushButton("Focus Peaking: OFF")
        self._peaking_btn.setCheckable(True)
        self._peaking_btn.toggled.connect(self._on_peaking_toggled)
        controls.addWidget(self._peaking_btn)

        self._histogram_btn = QPushButton("Histogram: OFF")
        self._histogram_btn.setCheckable(True)
        self._histogram_btn.toggled.connect(self._on_histogram_toggled)
        controls.addWidget(self._histogram_btn)

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
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(self.start_stream)
        btn_bar.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.stop_stream)
        btn_bar.addWidget(self._stop_btn)

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
        """Query camera for focus mode indicator."""
        try:
            fm = self._camera.get_focus_mode()
            self._focus_label.setText(f"Focus: {FOCUS_MODE_NAMES.get(fm, f'0x{fm:04X}')}")
        except XSDKError:
            pass

    def _reconnect_camera(self) -> bool:
        """Reconnect the SDK session to clear a stuck busy state."""
        self._status_bar.showMessage("Camera busy -- reconnecting...")
        try:
            self._camera.reconnect()
        except Exception as e:
            log.error("Reconnect failed: %s", e)
            self._status_bar.showMessage(f"Error: reconnect failed -- {e}")
            return False
        try:
            if not self._camera.wait_ready(timeout_s=5.0):
                log.error("Camera still busy after reconnect")
                self._status_bar.showMessage("Error: camera busy -- power-cycle camera")
                return False
        except Exception as e:
            log.error("Camera communication lost after reconnect: %s", e)
            self._status_bar.showMessage(f"Error: camera disconnected -- {e}")
            return False
        try:
            self._camera.set_priority(PRIORITY_PC)
        except XSDKError as e:
            log.warning("Could not set PC priority after reconnect: %s", e)
        return True

    def _start_live_view_stream(self) -> bool:
        """Create and start a LiveViewStream. Returns True on success."""
        quality = self._quality_combo.currentData() or LIVEVIEW_QUALITY_FINE
        self._stream = LiveViewStream(self._camera, size=LIVEVIEW_SIZE_XGA, quality=quality)
        try:
            self._stream.start()
            return True
        except XSDKError as e:
            log.warning("Live view start failed: %s", e)
            self._stream = None
            return False

    def start_stream(self):
        """Start the live view stream and frame worker thread."""
        if self._thread is not None:
            return

        self._start_btn.setEnabled(False)
        self._status_bar.showMessage("Preparing camera...")

        # Everything below talks to the SDK, so it waits for the scheduler the
        # same way a scheduled command waits for it.
        if not self._usb_lock.acquire(timeout=_STREAM_SETUP_WAIT_S):
            self._status_bar.showMessage(
                "The camera is busy with the schedule - try again in a moment", 6000)
            self._start_btn.setEnabled(True)
            return
        try:
            self._start_stream_locked()
        finally:
            self._usb_lock.release()

    def _start_stream_locked(self):

        # Camera may be busy from session init -- drain and wait
        try:
            self._camera.drain_buffer()
        except Exception:
            pass

        try:
            ready = self._camera.wait_ready(timeout_s=5.0)
        except Exception as e:
            log.error("Camera communication error: %s", e)
            self._status_bar.showMessage(f"Error: camera disconnected -- {e}")
            self._start_btn.setEnabled(True)
            return

        if not ready:
            if not self._reconnect_camera():
                self._start_btn.setEnabled(True)
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
                self._start_btn.setEnabled(True)
                return
            if not self._start_live_view_stream():
                log.error("Failed to start live view after reconnect")
                self._status_bar.showMessage("Error: live view failed -- power-cycle camera")
                self._start_btn.setEnabled(True)
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
        self._fps_timer.start(1000)
        # The dials may have been turned by hand since the window was opened.
        self._refresh_exposure()

        self._stop_btn.setEnabled(True)
        self._status_bar.showMessage("Streaming...")

    def stop_stream(self):
        """Stop the live view stream and worker thread."""
        self._fps_timer.stop()

        if self._worker:
            self._worker.stop()
            # Disconnect signals to prevent callbacks during teardown
            try:
                self._worker.frame_ready.disconnect(self._on_frame)
                self._worker.error.disconnect(self._on_error)
            except (TypeError, RuntimeError):
                pass
        if self._thread:
            self._thread.quit()
            if not self._thread.wait(5000):
                log.warning("Live view thread did not stop in time, terminating")
                self._thread.terminate()
                self._thread.wait(2000)
            self._thread = None
        self._worker = None

        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                log.debug("Error stopping live view stream", exc_info=True)
            self._stream = None

        # Give the camera back.  Live view takes PRIORITY_PC to stream and this
        # never returned it, so the body stayed in PC priority for the rest of
        # the run - while the relay goes on firing the shutter physically and the
        # session's idea of who is in charge no longer matches the body's.  That
        # is the shape of the 0x2001 that took two rehearsals down, and it
        # outlived closing the window, which is why it looked unrelated.
        try:
            self._camera.set_priority(PRIORITY_CAMERA)
            log.info("Live view stopped; camera priority returned to the body")
        except Exception:
            log.warning("Could not return camera priority after live view; "
                        "the body may refuse the relay until it is reconnected",
                        exc_info=True)

        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._display.clear()
        self._display.setText("No Signal")
        self._fps_label.setText("FPS: --")
        self._status_bar.showMessage("Stopped")

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
                overlay = _peaking_mask_from_lap(lap, img.width(), img.height())
                painter = QPainter(img)
                painter.drawImage(0, 0, overlay)
                painter.end()

        pixmap = QPixmap.fromImage(img)
        scaled = pixmap.scaled(
            self._display.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._display.setPixmap(scaled)
        self._frame_count += 1

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
        self._histogram.setVisible(checked)
        if not checked:
            self._histogram.clear()

    def _on_zoom_changed(self, index: int):
        val = self._zoom_combo.currentData()
        if val is not None:
            self._zoom_factor = val
            self._display.set_zoom_factor(val)
            self._focus_graph.reset()

    def _on_zoom_center_changed(self, nx: float, ny: float):
        self._zoom_center = QPointF(nx, ny)
        self._focus_graph.reset()

    def _on_quality_changed(self, index: int):
        val = self._quality_combo.currentData()
        if val is not None and self._stream:
            try:
                self._camera.set_live_view_quality(val)
            except XSDKError as e:
                log.warning("Failed to set live view quality: %s", e)

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------

    def _populate_exposure(self):
        """Fill the shutter and ISO lists and show what the body is set to.

        The shutter values come from the SDK's own name table rather than from
        CapShutterSpeed, which this body does not implement - it answers with an
        empty list, and a dropdown built from that would be empty too.
        """
        speeds = sorted(k for k in SHUTTER_SPEED_NAMES
                        if isinstance(k, int) and _SHUTTER_MIN_US <= k <= _SHUTTER_MAX_US)
        self._shutter_combo.blockSignals(True)
        self._shutter_combo.clear()
        for value in speeds:
            self._shutter_combo.addItem(str(SHUTTER_SPEED_NAMES[value]), value)
        self._shutter_combo.blockSignals(False)

        try:
            isos = [i for i in self._camera.get_supported_iso() if i > 0]
        except Exception:
            isos = []
        if not isos:
            isos = _ISO_FALLBACK
        self._iso_combo.blockSignals(True)
        self._iso_combo.clear()
        for value in sorted(isos):
            self._iso_combo.addItem(str(value), value)
        self._iso_combo.blockSignals(False)

        self._refresh_exposure()

    def _refresh_exposure(self):
        """Read the body's exposure and show it, without firing the signals."""
        try:
            speed, _bulb = self._camera.get_shutter_speed()
            iso = self._camera.get_iso()
        except Exception as exc:
            log.debug("Could not read the exposure: %s", exc)
            self._exposure_label.setText("Exposure: unreadable")
            return

        name = SHUTTER_SPEED_NAMES.get(speed, f"{speed}us")
        self._exposure_label.setText(f"Exposure: {name}  ISO {iso}")
        for combo, value in ((self._shutter_combo, speed), (self._iso_combo, iso)):
            index = combo.findData(value)
            if index >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _write_exposure(self, action, what: str, dial_hint: str) -> bool:
        """Write one exposure setting, with live view actually stopped.

        Pausing the frame loop is not enough.  The body answers 0x1006 for as
        long as it is in live view at all, not merely while a frame is in
        flight - measured: with the worker paused and the lock held, every
        retry across a two second budget was refused.  It is a mode, not a
        transient, so waiting it out cannot work however long the budget.

        So the stream is stopped for the write and started again after.  The
        preview drops for about a second, which is the price of the setting
        landing at all.  Nothing else about the session changes: the handle,
        the PC priority and the worker thread all stay as they were.
        """
        worker = self._worker
        if worker is not None:
            worker.pause()
        if not self._usb_lock.acquire(timeout=_STREAM_SETUP_WAIT_S):
            # Logged, not only shown: this path was silent, so a refusal here
            # and a refusal from the body were indistinguishable afterwards.
            # And it does not blame a schedule - the lock is held by whatever is
            # using the camera, which with no script loaded is this window.
            log.warning("Could not set the %s: the camera was still in use after "
                        "%.0fs", what, _STREAM_SETUP_WAIT_S)
            self._status_bar.showMessage(
                f"Could not set the {what}: the camera did not come free", 6000)
            if worker is not None:
                worker.resume()
            return False

        was_streaming = self._stream is not None
        if was_streaming:
            self._status_bar.showMessage(f"Setting {what}...")
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
            self._status_bar.showMessage(
                f"{what.capitalize()} refused: the camera stayed busy", 6000)
            return False
        except XSDKError as exc:
            # The code matters: 0x1003 on a dead handle is not 0x1002 on a value
            # the body will not take, and both used to read as "check the dial".
            log.warning("Could not set the %s: %s", what, exc, exc_info=True)
            self._status_bar.showMessage(
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
            if worker is not None:
                worker.resume()
            if restart_failed:
                self.stop_stream()
                self._status_bar.showMessage(
                    "Live view stopped after the setting was written - "
                    "start it again", 8000)
            else:
                self._refresh_exposure()

    def _on_shutter_changed(self, index: int):
        value = self._shutter_combo.currentData()
        if value is None:
            return
        self._write_exposure(lambda: self._camera.set_shutter_speed(value),
                             "shutter speed", "is the shutter dial on T?")

    def _on_iso_changed(self, index: int):
        value = self._iso_combo.currentData()
        if value is None:
            return
        self._write_exposure(lambda: self._camera.set_iso(value),
                             "ISO", "is the ISO dial on C?")

    def is_streaming(self) -> bool:
        """True when frames are actually being fetched.

        Part of the contract both live view windows answer; see the note beside
        the gphoto2 one in gui.py.  It exists so callers stop reading `_thread`,
        which means different things in the two implementations - this one tears
        the thread down, the other keeps it and pauses it.
        """
        return self._thread is not None

    def set_totality_paused(self, paused: bool):
        """Stop streaming from just before C2 until just after C3, and resume.

        The controller calls this every clock tick.  Its gphoto2 counterpart has
        always had it; this window was written before that guard existed and
        stranded on a branch before it arrived, so restoring it crashed the clock
        with AttributeError the moment a live view was open.

        Totality is the one stretch where the camera cannot afford to share the
        connection: the frames are dense and every one of them is unrepeatable.
        A stream the user started themselves is resumed afterwards, so the pause
        does not quietly turn live view off for the rest of the eclipse.
        """
        if getattr(self, '_totality_paused', False) == paused:
            return
        self._totality_paused = paused
        if paused:
            self._resume_after_totality = self._thread is not None
            if self._thread is not None:
                log.info("Live view paused for totality")
                self.stop_stream()
        elif getattr(self, '_resume_after_totality', False):
            self._resume_after_totality = False
            log.info("Totality over, live view resuming")
            self.start_stream()

    def closeEvent(self, event):
        try:
            self.stop_stream()
        except Exception:
            log.exception("Error stopping stream during close")
        super().closeEvent(event)
