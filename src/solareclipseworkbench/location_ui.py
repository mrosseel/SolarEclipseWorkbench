"""
Shared location UI components for Solar Eclipse Workbench.

Provides:
    - ConfigManager: saves/loads camera and location configurations (JSON).
    - GeocodingWorker: background thread that geocodes an address via Nominatim
      and fetches elevation from Open-Elevation.
    - LocationWidget: a self-contained QWidget with a saved-locations drop-down,
      an optional address-search bar and coordinate fields.  Used by both
      gui.py (LocationPopup) and wizard.py (EclipseConfigPage).
"""
import json
import time
import requests
from pathlib import Path
from typing import Optional, Dict, List

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QMessageBox,
)
from solareclipseworkbench.qt_utils import dark_lineedit_style, apply_dark_to_lineedit

try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    GEOPY_AVAILABLE = True
except ImportError:
    GEOPY_AVAILABLE = False


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """Manages saving and loading of camera and location configurations."""

    def __init__(self):
        self.config_file = Path.home() / ".sew_wizard_config.json"
        self.config = self._load_config()

    def _load_config(self) -> Dict:
        """Load configuration from file, or create a default one."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return self._default_config()
        return self._default_config()

    def _default_config(self) -> Dict:
        """Return the default (empty) configuration structure."""
        return {
            "cameras": [],
            "locations": [],
            "last_used": {
                "camera": None,
                "location": None,
            },
            "fuji_sdk_path": None,
        }

    def save_config(self):
        """Persist the in-memory configuration to disk."""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except IOError as e:
            print(f"Warning: Could not save configuration: {e}")

    # ------------------------------------------------------------------
    # Camera helpers
    # ------------------------------------------------------------------

    def add_camera(self, name: str, focal_length: int, aperture_min: float,
                   aperture_max: float, filter_nd: str, preferred_iso: int = 400,
                   iso_min: int = 100, iso_max: int = 1600) -> None:
        """Add or update a camera configuration."""
        for camera in self.config["cameras"]:
            if camera["name"] == name:
                camera.update({
                    "focal_length": focal_length,
                    "aperture_min": aperture_min,
                    "aperture_max": aperture_max,
                    "filter_nd": filter_nd,
                    "preferred_iso": preferred_iso,
                    "iso_min": iso_min,
                    "iso_max": iso_max,
                })
                self.save_config()
                return
        self.config["cameras"].append({
            "name": name,
            "focal_length": focal_length,
            "aperture_min": aperture_min,
            "aperture_max": aperture_max,
            "filter_nd": filter_nd,
            "preferred_iso": preferred_iso,
            "iso_min": iso_min,
            "iso_max": iso_max,
        })
        self.save_config()

    def get_cameras(self) -> List[Dict]:
        """Return all saved camera configurations."""
        return self.config["cameras"]

    def get_camera(self, name: str) -> Optional[Dict]:
        """Return a camera configuration by name, or None."""
        for camera in self.config["cameras"]:
            if camera["name"] == name:
                return camera
        return None

    def set_last_used_camera(self, name: str) -> None:
        """Record the most-recently used camera name."""
        self.config["last_used"]["camera"] = name
        self.save_config()

    def get_last_used_camera(self) -> Optional[str]:
        """Return the most-recently used camera name, or None."""
        return self.config["last_used"].get("camera")

    # ------------------------------------------------------------------
    # Location helpers
    # ------------------------------------------------------------------

    def add_location(self, name: str, latitude: float, longitude: float,
                     altitude: float) -> None:
        """Add or update a named location."""
        for location in self.config["locations"]:
            if location["name"] == name:
                location.update({
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude": altitude,
                })
                self.save_config()
                return
        self.config["locations"].append({
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude,
        })
        self.save_config()

    def get_locations(self) -> List[Dict]:
        """Return all saved locations."""
        return self.config["locations"]

    def get_location(self, name: str) -> Optional[Dict]:
        """Return a saved location by name, or None."""
        for location in self.config["locations"]:
            if location["name"] == name:
                return location
        return None

    def set_last_used_location(self, name: str) -> None:
        """Record the most-recently used location name."""
        self.config["last_used"]["location"] = name
        self.save_config()

    def get_last_used_location(self) -> Optional[str]:
        """Return the most-recently used location name, or None."""
        return self.config["last_used"].get("location")

    # ------------------------------------------------------------------
    # Fuji SDK path
    # ------------------------------------------------------------------

    def get_fuji_sdk_path(self) -> Optional[str]:
        """Return the configured Fuji SDK path, or None."""
        return self.config.get("fuji_sdk_path")

    def set_fuji_sdk_path(self, path: str) -> None:
        """Store the Fuji SDK library path."""
        self.config["fuji_sdk_path"] = path
        self.save_config()


# ---------------------------------------------------------------------------
# GeocodingWorker
# ---------------------------------------------------------------------------

class GeocodingWorker(QThread):
    """Background thread that geocodes an address and fetches its elevation.

    Signals:
        finished(dict): emitted on success with keys latitude, longitude,
            altitude, display_name.
        error(str): emitted on failure with a human-readable message.
    """

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, address: str):
        super().__init__()
        self.address = address

    def run(self):
        """Perform geocoding in a background thread."""
        try:
            if not GEOPY_AVAILABLE:
                self.error.emit(
                    "Geopy library not installed. Install with: pip install geopy"
                )
                return

            geolocator = Nominatim(user_agent="SolarEclipseWorkbench/1.3.0")

            # Nominatim usage policy: max 1 request per second.
            time.sleep(1)

            location = geolocator.geocode(self.address, timeout=10)
            if not location:
                self.error.emit(f"Address not found: {self.address}")
                return

            latitude = location.latitude
            longitude = location.longitude
            display_name = location.address

            # Try to fetch elevation from Open-Elevation.
            altitude = 0.0
            try:
                response = requests.get(
                    f"https://api.open-elevation.com/api/v1/lookup"
                    f"?locations={latitude},{longitude}",
                    timeout=10,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("results"):
                        altitude = float(data["results"][0]["elevation"])
            except Exception as exc:
                print(f"Elevation lookup failed: {exc}")

            self.finished.emit({
                "latitude": latitude,
                "longitude": longitude,
                "altitude": altitude,
                "display_name": display_name,
            })

        except GeocoderTimedOut:
            self.error.emit("Geocoding service timed out. Please try again.")
        except GeocoderServiceError as exc:
            self.error.emit(f"Geocoding service error: {exc}")
        except Exception as exc:
            self.error.emit(f"Error: {exc}")


# ---------------------------------------------------------------------------
# LocationWidget
# ---------------------------------------------------------------------------

class LocationWidget(QWidget):
    """Self-contained widget for choosing an observation location.

    Features
    --------
    * Drop-down of saved locations (populated from *config_manager*).
    * Optional address-search bar (requires the *geopy* package).
    * Editable coordinate fields (longitude, latitude, altitude).
    * "Save Location" button that persists the entered coordinates.

    The widget exposes the individual ``QLineEdit`` widgets as public
    attributes so that callers (wizard field-registration, plot callbacks,
    etc.) can access their text directly:

        ``longitude_edit``, ``latitude_edit``, ``altitude_edit``,
        ``location_name_edit``, ``location_combo``

    Usage
    -----
    ::

        mgr = ConfigManager()
        w = LocationWidget(mgr)
        # pre-fill from an existing model:
        w.set_coordinates(lon, lat, alt)
        # retrieve validated coordinates:
        lon, lat, alt = w.get_coordinates()   # raises ValueError if invalid
    """

    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self._config_manager = config_manager
        self._geocoding_worker: Optional[GeocodingWorker] = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_coordinates(self, longitude: Optional[float],
                        latitude: Optional[float],
                        altitude: Optional[float]) -> None:
        """Pre-fill coordinate fields with existing values (may be None)."""
        if longitude is not None:
            self.longitude_edit.setText(str(longitude))
        if latitude is not None:
            self.latitude_edit.setText(str(latitude))
        if altitude is not None:
            self.altitude_edit.setText(str(altitude))

    def get_coordinates(self):
        """Return ``(longitude, latitude, altitude)`` as floats.

        Raises ``ValueError`` if any field contains a non-numeric value or
        is empty.
        """
        longitude = float(self.longitude_edit.text())
        latitude = float(self.latitude_edit.text())
        altitude = float(self.altitude_edit.text())
        return longitude, latitude, altitude

    def reload_saved_locations(self) -> None:
        """Repopulate the drop-down with the current saved locations."""
        # Remove all "(Saved)" items, then re-add from config.
        for i in range(self.location_combo.count() - 1, -1, -1):
            if self.location_combo.itemText(i).endswith(" (Saved)"):
                self.location_combo.removeItem(i)
        for saved in self._config_manager.get_locations():
            self.location_combo.addItem(f"{saved['name']} (Saved)")

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Saved-locations drop-down ---
        saved_row = QHBoxLayout()
        saved_row.addWidget(QLabel("Saved Location:"))
        self.location_combo = QComboBox()
        self.location_combo.addItem("Custom")
        for saved in self._config_manager.get_locations():
            self.location_combo.addItem(f"{saved['name']} (Saved)")
        saved_row.addWidget(self.location_combo, 1)
        layout.addLayout(saved_row)

        # --- Address search (only when geopy is available) ---
        if GEOPY_AVAILABLE:
            search_row = QHBoxLayout()
            search_row.addWidget(QLabel("Search Address:"))
            self.address_search_edit = QLineEdit()
            self.address_search_edit.setPlaceholderText(
                "Enter city, street, or landmark"
            )
            apply_dark_to_lineedit(self.address_search_edit)
            self.address_search_edit.returnPressed.connect(self._search_address)
            search_row.addWidget(self.address_search_edit, 1)

            self.search_btn = QPushButton("Search")
            self.search_btn.clicked.connect(self._search_address)
            self.search_btn.setToolTip(
                "Search for the address and auto-fill coordinates & elevation"
            )
            search_row.addWidget(self.search_btn)
            layout.addLayout(search_row)

            self.search_status_label = QLabel("")
            self.search_status_label.setStyleSheet(
                "QLabel { color: #555; font-style: italic; }"
            )
            self.search_status_label.setWordWrap(True)
            layout.addWidget(self.search_status_label)
        else:
            self.address_search_edit = None
            self.search_btn = None
            self.search_status_label = None

        # --- Coordinate fields ---
        coord_grid = QGridLayout()

        coord_grid.addWidget(QLabel("Location Name:"), 0, 0)
        self.location_name_edit = QLineEdit()
        self.location_name_edit.setPlaceholderText("Name this location to save it")
        apply_dark_to_lineedit(self.location_name_edit)
        coord_grid.addWidget(self.location_name_edit, 0, 1)

        coord_grid.addWidget(QLabel("Longitude [°]:"), 1, 0)
        self.longitude_edit = QLineEdit()
        self.longitude_edit.setPlaceholderText("-180 to 180 (E: +, W: −)")
        self.longitude_edit.setValidator(QDoubleValidator(-180.0, 180.0, 5))
        apply_dark_to_lineedit(self.longitude_edit)
        self.longitude_edit.setToolTip(
            "Positive values: East of Greenwich meridian; "
            "Negative values: West of Greenwich meridian"
        )
        coord_grid.addWidget(self.longitude_edit, 1, 1)

        coord_grid.addWidget(QLabel("Latitude [°]:"), 2, 0)
        self.latitude_edit = QLineEdit()
        self.latitude_edit.setPlaceholderText("-90 to 90 (N: +, S: −)")
        self.latitude_edit.setValidator(QDoubleValidator(-90.0, 90.0, 5))
        apply_dark_to_lineedit(self.latitude_edit)
        self.latitude_edit.setToolTip(
            "Positive values: Northern hemisphere; Negative values: Southern hemisphere"
        )
        coord_grid.addWidget(self.latitude_edit, 2, 1)

        coord_grid.addWidget(QLabel("Altitude [m]:"), 3, 0)
        self.altitude_edit = QLineEdit()
        self.altitude_edit.setPlaceholderText("Altitude above sea level")
        self.altitude_edit.setValidator(QDoubleValidator(-500.0, 9000.0, 1))
        apply_dark_to_lineedit(self.altitude_edit)
        coord_grid.addWidget(self.altitude_edit, 3, 1)

        self.save_location_btn = QPushButton("Save Location")
        self.save_location_btn.clicked.connect(self._save_location)
        self.save_location_btn.setToolTip("Save this location for future use")
        coord_grid.addWidget(self.save_location_btn, 4, 1)

        layout.addLayout(coord_grid)

        # Connect combo *after* creating all sub-widgets.
        self.location_combo.currentTextChanged.connect(self._on_location_changed)

        # Restore last-used location (fires _on_location_changed if found).
        last = self._config_manager.get_last_used_location()
        if last:
            idx = self.location_combo.findText(f"{last} (Saved)")
            if idx >= 0:
                self.location_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_fields_editable(self, editable: bool) -> None:
        """Enable or disable the manual-entry coordinate fields."""
        self.location_name_edit.setEnabled(editable)
        self.longitude_edit.setEnabled(editable)
        self.latitude_edit.setEnabled(editable)
        self.altitude_edit.setEnabled(editable)
        self.save_location_btn.setEnabled(editable)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_location_changed(self, location_name: str) -> None:
        """Populate or clear coordinate fields when the combo changes."""
        if location_name.endswith(" (Saved)"):
            actual_name = location_name[: -len(" (Saved)")]
            saved = self._config_manager.get_location(actual_name)
            if saved:
                self._set_fields_editable(False)
                self.location_name_edit.setText(saved["name"])
                self.longitude_edit.setText(str(saved["longitude"]))
                self.latitude_edit.setText(str(saved["latitude"]))
                self.altitude_edit.setText(str(saved["altitude"]))
        else:
            # "Custom" – let the user type freely.
            self._set_fields_editable(True)
            self.location_name_edit.clear()
            self.longitude_edit.clear()
            self.latitude_edit.clear()
            self.altitude_edit.clear()

    def _save_location(self) -> None:
        """Validate fields and persist the current location."""
        location_name = self.location_name_edit.text().strip()
        if not location_name:
            QMessageBox.warning(
                self, "Invalid Location Name",
                "Please enter a name for this location."
            )
            return

        try:
            longitude = float(self.longitude_edit.text())
            latitude = float(self.latitude_edit.text())
            altitude = float(self.altitude_edit.text())
        except ValueError:
            QMessageBox.warning(
                self, "Invalid Coordinates",
                "Please enter valid numeric coordinates."
            )
            return

        self._config_manager.add_location(
            name=location_name,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
        )
        self._config_manager.set_last_used_location(location_name)

        combo_text = f"{location_name} (Saved)"
        if self.location_combo.findText(combo_text) < 0:
            self.location_combo.addItem(combo_text)

        # Switch the combo to the new entry without re-triggering the
        # handler (which would clear the fields).
        self.location_combo.blockSignals(True)
        self.location_combo.setCurrentText(combo_text)
        self.location_combo.blockSignals(False)

        self._set_fields_editable(False)
        QMessageBox.information(
            self, "Location Saved",
            f"Location '{location_name}' has been saved."
        )

    def _search_address(self) -> None:
        """Kick off a background geocoding request."""
        if not GEOPY_AVAILABLE:
            QMessageBox.warning(
                self, "Geocoding Not Available",
                "Geocoding requires the 'geopy' library.\n"
                "Install it with:  pip install geopy",
            )
            return

        address = self.address_search_edit.text().strip()
        if not address:
            QMessageBox.warning(
                self, "Empty Address",
                "Please enter an address to search."
            )
            return

        self.search_btn.setEnabled(False)
        self.search_status_label.setText(
            "Searching… (this may take a few seconds)"
        )
        self.search_status_label.setStyleSheet(
            "QLabel { color: #555; font-style: italic; }"
        )

        self._geocoding_worker = GeocodingWorker(address)
        self._geocoding_worker.finished.connect(self._on_geocoding_finished)
        self._geocoding_worker.error.connect(self._on_geocoding_error)
        self._geocoding_worker.start()

    def _on_geocoding_finished(self, result: dict) -> None:
        """Apply a successful geocoding result to the coordinate fields."""
        self.search_btn.setEnabled(True)

        # Switch the combo to "Custom" – this enables the fields via
        # _on_location_changed.
        self.location_combo.setCurrentText("Custom")
        self._set_fields_editable(True)

        self.longitude_edit.setText(f"{result['longitude']:.5f}")
        self.latitude_edit.setText(f"{result['latitude']:.5f}")
        self.altitude_edit.setText(f"{result['altitude']:.1f}")

        # Suggest a name derived from the first part of the full address.
        parts = result["display_name"].split(",")
        if len(parts) >= 2:
            suggested = (
                parts[0].strip()
                if len(parts[0]) < 50
                else parts[1].strip()
            )
        else:
            suggested = parts[0].strip()

        if not self.location_name_edit.text():
            self.location_name_edit.setText(suggested)

        self.search_status_label.setText(
            f"✓ Found: {result['display_name']}\n"
            f"Coordinates: {result['latitude']:.5f}°, "
            f"{result['longitude']:.5f}° | "
            f"Elevation: {result['altitude']:.0f} m"
        )
        self.search_status_label.setStyleSheet(
            "QLabel { color: #2a7d2a; font-style: italic; }"
        )

    def _on_geocoding_error(self, error_msg: str) -> None:
        """Show an error after a failed geocoding request."""
        self.search_btn.setEnabled(True)
        self.search_status_label.setText(f"✗ Error: {error_msg}")
        self.search_status_label.setStyleSheet(
            "QLabel { color: #c40000; font-style: italic; }"
        )
        QMessageBox.warning(self, "Geocoding Error", error_msg)
