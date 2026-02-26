"""Shared PyQt6 utilities for Solar Eclipse Workbench.

Provides helpers for detecting and applying the host system's colour scheme
(light / dark) in a cross-platform way.

Detection order
---------------
1. ``QGuiApplication.styleHints().colorScheme()`` – available since Qt 6.5 /
   PyQt6 6.5.  Works natively on macOS (Cocoa), Windows 10/11, and any Linux
   desktop that exposes its preference via *xdg-desktop-portal* (GNOME 43+,
   KDE Plasma 5.27+).
2. On Linux only: ``gsettings get org.gnome.desktop.interface color-scheme``
   – covers GNOME 42 and distros where xdg-desktop-portal is not integrated
   with Qt yet.
"""

import subprocess
import sys
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QLineEdit


def _is_dark_mode_preferred() -> bool:
    """Return True when the system prefers a dark colour scheme.

    Never raises; returns False on any error so that the application falls
    back to the default light theme gracefully.

    Detection order
    ---------------
    Linux:  gsettings (most reliable on GNOME), then Qt colorScheme().
            Qt's xcb/Wayland plugins often report Light even on dark desktops
            because xdg-desktop-portal integration varies by distro/Qt build,
            so gsettings is checked first.
    Other:  Qt colorScheme() (works natively on macOS Cocoa and Windows).
    """
    if sys.platform.startswith("linux"):
        # Primary: GNOME/GTK gsettings
        try:
            result = subprocess.run(
                ["gsettings", "get",
                 "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return "dark" in result.stdout.lower()
        except Exception:
            pass

        # Secondary: KDE / Qt portal integration
        try:
            from PyQt6.QtCore import Qt
            scheme = QApplication.styleHints().colorScheme()
            if scheme != Qt.ColorScheme.Unknown:
                return scheme == Qt.ColorScheme.Dark
        except AttributeError:
            pass

        return False

    # --- macOS / Windows: trust Qt natively ----------------------------
    try:
        from PyQt6.QtCore import Qt
        scheme = QApplication.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Dark
    except AttributeError:
        pass

    return False


def _build_dark_palette() -> QPalette:
    """Return a QPalette that approximates GNOME Adwaita-dark / system dark."""
    palette = QPalette()

    # --- Window / base surfaces ----------------------------------------
    palette.setColor(QPalette.ColorRole.Window,          QColor(32,  32,  32))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(238, 238, 238))
    palette.setColor(QPalette.ColorRole.Base,            QColor(24,  24,  24))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(40,  40,  40))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(50,  50,  50))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(238, 238, 238))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(130, 130, 130))

    # --- Text / buttons ------------------------------------------------
    palette.setColor(QPalette.ColorRole.Text,       QColor(238, 238, 238))
    palette.setColor(QPalette.ColorRole.Button,     QColor(50,  50,  50))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(238, 238, 238))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255,  60,  60))

    # --- Highlight / accent --------------------------------------------
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(52,  152, 219))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))

    # --- Links ---------------------------------------------------------
    palette.setColor(QPalette.ColorRole.Link,        QColor(86,  180, 233))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(180, 100, 220))

    # --- Disabled state ------------------------------------------------
    for role in (QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(100, 100, 100))

    return palette


def dark_lineedit_style() -> str:
    """Return a stylesheet string to set directly on a QLineEdit in dark mode.

    A per-widget ``setStyleSheet`` bypasses ``autoFillBackground`` and palette
    inheritance completely, making it the only reliable way to force a dark
    background on ``QLineEdit`` widgets inside a ``QWizard`` with Fusion style.
    Returns an empty string when dark mode is not active.
    """
    if not _is_dark_mode_preferred():
        return ""
    return (
        "QLineEdit {"
        "  background-color: #3c3f41;"
        "  color: #eeeeee;"
        "  border: 1px solid #555555;"
        "  border-radius: 3px;"
        "  padding: 4px;"
        "}"
        "QLineEdit:focus {"
        "  border: 2px solid #3498db;"
        "}"
        "QLineEdit:disabled {"
        "  color: #888888;"
        "}"
    )


class _DarkLineEditFilter(QObject):
    """Event filter that re-applies a dark stylesheet whenever QWizard
    (or any other Qt-internal code) overwrites it via setStyleSheet.

    ``QWizard`` calls ``QLineEdit.setStyleSheet`` on registered mandatory-field
    widgets after ``initializePage`` returns, to mark them as incomplete.  This
    filter intercepts the resulting ``QEvent.Type.StyleChange`` event and
    immediately restores the dark stylesheet, preventing an infinite loop by
    guarding with ``_applying``.
    """

    def __init__(self, style: str, parent: QLineEdit):
        super().__init__(parent)
        self._style = style
        self._applying = False

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.StyleChange and not self._applying:
            self._applying = True
            obj.setStyleSheet(self._style)
            self._applying = False
        return False  # never consume the event


def apply_dark_to_lineedit(edit: QLineEdit) -> None:
    """Apply a persistent dark stylesheet to *edit* that survives QWizard resets.

    Installs a :class:`_DarkLineEditFilter` event filter on *edit* so that any
    subsequent call to ``setStyleSheet`` by Qt-internal code (e.g.
    ``QWizard``\'s mandatory-field marker) is immediately overridden.  Does
    nothing when dark mode is not active.
    """
    style = dark_lineedit_style()
    if not style:
        return
    edit.setStyleSheet(style)
    f = _DarkLineEditFilter(style, edit)
    edit.installEventFilter(f)


def apply_system_color_scheme(app: QApplication) -> None:
    """Apply the host system's colour scheme to *app*.

    On macOS and Windows Qt already handles this natively (the function is a
    no-op there unless the Qt 6.5 API is unavailable).  On Linux/GNOME the
    function reads the system preference and applies a matching QPalette and
    Fusion style when dark mode is active, so that all Qt widgets respect the
    dark theme.
    """
    if _is_dark_mode_preferred():
        # Fusion honours QPalette colours fully; the platform plugin alone
        # may not propagate palette changes to all widget types on Linux.
        app.setStyle("Fusion")
        app.setPalette(_build_dark_palette())
