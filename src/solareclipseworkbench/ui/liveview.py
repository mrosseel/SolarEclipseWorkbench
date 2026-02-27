"""Live View dockable window with software focus peaking and digital zoom."""

from __future__ import annotations

import logging

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
    LIVEVIEW_QUALITY_FINE,
    LIVEVIEW_QUALITY_NAMES,
    LIVEVIEW_SIZE_XGA,
    PRIORITY_PC,
)
from fujixsdk._errors import XSDKError
from fujixsdk.camera import Camera

log = logging.getLogger(__name__)

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

    def __init__(self, stream: LiveViewStream):
        super().__init__()
        self._stream = stream
        self._running = False

    @pyqtSlot()
    def run(self):
        self._running = True
        idle_count = 0
        while self._running:
            try:
                data = self._stream.read_frame()
            except Exception as e:
                self.error.emit(str(e))
                break
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

    def __init__(self, camera: Camera, parent: QWidget | None = None):
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

        self._camera = camera
        self._stream: LiveViewStream | None = None
        self._worker: _FrameWorker | None = None
        self._thread: QThread | None = None

        # Software processing state
        self._zoom_factor = 1
        self._zoom_center = QPointF(0.5, 0.5)
        self._peaking_enabled = False

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

        # Control bar
        controls = QHBoxLayout()

        self._peaking_btn = QPushButton("Focus Peaking: OFF")
        self._peaking_btn.setCheckable(True)
        self._peaking_btn.toggled.connect(self._on_peaking_toggled)
        controls.addWidget(self._peaking_btn)

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
        self._fps_label = QLabel("FPS: --")
        self._status_bar.addWidget(self._focus_label)
        self._status_bar.addPermanentWidget(self._fps_label)
        layout.addWidget(self._status_bar)

        self.setWidget(container)
        self._populate_controls()

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

        self._worker = _FrameWorker(self._stream)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.error.connect(self._on_error)
        self._thread.start()

        self._frame_count = 0
        self._focus_graph.reset()
        self._fps_timer.start(1000)

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

    def closeEvent(self, event):
        try:
            self.stop_stream()
        except Exception:
            log.exception("Error stopping stream during close")
        super().closeEvent(event)
