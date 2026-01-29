"""
Weather Overlay Widget for TravelViz Globe
Displays weather conditions at game venues in a collapsible panel
"""

import os
import json
import requests
from pathlib import Path

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QScrollArea, QPushButton, QFrame)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve, QThread
from PyQt6.QtGui import QFont, QColor
from typing import Dict, List, Optional


class WeatherService:
    """Self-contained weather service using OpenWeatherMap API"""

    def __init__(self, api_key: str = None):
        # Try to load API key from api_keys.json if not provided
        if not api_key:
            api_key = self._load_api_key()

        self.api_key = api_key
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"

        if not self.api_key:
            raise ValueError("OpenWeather API key not found in api_keys.json")

        print(f"✅ WeatherService initialized with API key: {self.api_key[:8]}...")

    def _load_api_key(self) -> Optional[str]:
        """Load API key from api_keys.json"""
        try:
            config_path = Path(__file__).parent / "api_keys.json"
            if config_path.exists():
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    return config.get("open_weather")
        except Exception as e:
            print(f"⚠️ Failed to load API key from config: {e}")
        return None

    def get_weather_by_location(self, lat: float, lon: float) -> dict:
        """Fetch weather data for a location"""
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self.api_key,
            "units": "imperial"  # Get data in imperial units (°F, mph)
        }

        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            return response.json()
        except requests.RequestException as e:
            return {"error": str(e)}

    def extract_weather_data(self, weather_json: dict) -> dict:
        """Extract relevant weather data from API response"""
        weather_data = {
            "wind_speed": weather_json.get("wind", {}).get("speed", 0),
            "wind_direction": weather_json.get("wind", {}).get("deg", 0),
            "temperature": weather_json.get("main", {}).get("temp", 0),
            "humidity": weather_json.get("main", {}).get("humidity", 0),
            "condition": weather_json.get("weather", [{}])[0].get("main", "Unknown"),
            "description": weather_json.get("weather", [{}])[0].get("description", ""),
            "precipitation": 0  # Default to 0
        }

        # Add precipitation data if available
        if "rain" in weather_json:
            weather_data["precipitation"] = weather_json["rain"].get("1h", weather_json["rain"].get("3h", 0) / 3)
        elif "snow" in weather_json:
            weather_data["precipitation"] = weather_json["snow"].get("1h", weather_json["snow"].get("3h", 0) / 3)

        return weather_data


class WeatherFetchWorker(QThread):
    """Background worker for fetching weather data"""
    weatherFetched = pyqtSignal(str, dict)  # venue_id, weather_data
    fetchError = pyqtSignal(str, str)  # venue_id, error_message

    def __init__(self, weather_service, venues: List[Dict]):
        super().__init__()
        self.weather_service = weather_service
        self.venues = venues
        self._running = True

    def run(self):
        for venue in self.venues:
            if not self._running:
                break

            venue_id = venue.get('id', venue.get('name', 'unknown'))
            lat = venue.get('lat')
            lon = venue.get('lon')

            if lat is None or lon is None:
                self.fetchError.emit(venue_id, "Missing coordinates")
                continue

            try:
                raw_data = self.weather_service.get_weather_by_location(lat, lon)
                if 'main' in raw_data:
                    weather_data = self.weather_service.extract_weather_data(raw_data)
                    weather_data['venue'] = venue
                    self.weatherFetched.emit(venue_id, weather_data)
                else:
                    self.fetchError.emit(venue_id, raw_data.get('message', 'API error'))
            except Exception as e:
                self.fetchError.emit(venue_id, str(e))

    def stop(self):
        self._running = False


class VenueWeatherCard(QFrame):
    """Individual weather card for a venue"""
    clicked = pyqtSignal(float, float, str)  # lat, lon, venue_name

    def __init__(self, venue_data: Dict, weather_data: Dict, parent=None):
        super().__init__(parent)
        self.venue_data = venue_data
        self.weather_data = weather_data
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(31, 41, 55, 220);
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 4px;
            }
            QFrame:hover {
                background-color: rgba(45, 55, 72, 240);
                border-color: #10B981;
            }
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        # Venue name
        venue_name = self.venue_data.get('name', 'Unknown Venue')
        name_label = QLabel(venue_name)
        name_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        name_label.setStyleSheet("color: #10B981; border: none; background: transparent;")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        # Weather info row
        info_layout = QHBoxLayout()
        info_layout.setSpacing(8)

        # Temperature with color coding
        temp = self.weather_data.get('temperature', 0)
        temp_color = self._get_temp_color(temp)
        temp_label = QLabel(f"{temp:.0f}°F")
        temp_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        temp_label.setStyleSheet(f"color: {temp_color}; border: none; background: transparent;")
        info_layout.addWidget(temp_label)

        # Condition icon and text
        condition = self.weather_data.get('condition', 'Clear')
        icon = self._get_weather_icon(condition)
        condition_label = QLabel(f"{icon} {condition}")
        condition_label.setFont(QFont("Segoe UI", 9))
        condition_label.setStyleSheet("color: #D1D5DB; border: none; background: transparent;")
        info_layout.addWidget(condition_label)

        info_layout.addStretch()
        layout.addLayout(info_layout)

        # Additional details row
        details_layout = QHBoxLayout()
        details_layout.setSpacing(12)

        # Wind
        wind_speed = self.weather_data.get('wind_speed', 0)
        wind_label = QLabel(f"Wind: {wind_speed:.0f} mph")
        wind_label.setFont(QFont("Segoe UI", 8))
        wind_label.setStyleSheet("color: #9CA3AF; border: none; background: transparent;")
        details_layout.addWidget(wind_label)

        # Humidity
        humidity = self.weather_data.get('humidity', 0)
        humidity_label = QLabel(f"Humidity: {humidity}%")
        humidity_label.setFont(QFont("Segoe UI", 8))
        humidity_label.setStyleSheet("color: #9CA3AF; border: none; background: transparent;")
        details_layout.addWidget(humidity_label)

        details_layout.addStretch()
        layout.addLayout(details_layout)

    def _get_temp_color(self, temp: float) -> str:
        """Get color based on temperature"""
        if temp <= 32:
            return "#60A5FA"  # Cold - blue
        elif temp <= 50:
            return "#34D399"  # Cool - teal
        elif temp <= 70:
            return "#FBBF24"  # Mild - yellow
        elif temp <= 85:
            return "#F97316"  # Warm - orange
        else:
            return "#EF4444"  # Hot - red

    def _get_weather_icon(self, condition: str) -> str:
        """Get emoji icon for weather condition"""
        condition_lower = condition.lower()
        if 'clear' in condition_lower or 'sun' in condition_lower:
            return "☀️"
        elif 'cloud' in condition_lower:
            if 'partly' in condition_lower or 'few' in condition_lower:
                return "⛅"
            return "☁️"
        elif 'rain' in condition_lower or 'drizzle' in condition_lower:
            return "🌧️"
        elif 'thunder' in condition_lower or 'storm' in condition_lower:
            return "⛈️"
        elif 'snow' in condition_lower:
            return "❄️"
        elif 'fog' in condition_lower or 'mist' in condition_lower:
            return "🌫️"
        elif 'wind' in condition_lower:
            return "💨"
        else:
            return "🌤️"

    def mousePressEvent(self, event):
        """Emit clicked signal with venue coordinates"""
        lat = self.venue_data.get('lat')
        lon = self.venue_data.get('lon')
        name = self.venue_data.get('name', 'Unknown')
        if lat is not None and lon is not None:
            self.clicked.emit(lat, lon, name)
        super().mousePressEvent(event)


class WeatherOverlay(QWidget):
    """Collapsible overlay showing weather at game venues"""

    venueSelected = pyqtSignal(float, float, str)  # lat, lon, name
    visibilityChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.is_expanded = False
        self.animation = None
        self.collapsed_width = 30
        self.expanded_width = 260

        self.weather_cards: Dict[str, VenueWeatherCard] = {}
        self.fetch_worker: Optional[WeatherFetchWorker] = None

        # Initialize weather service
        try:
            self.weather_service = WeatherService()
        except Exception as e:
            print(f"❌ WeatherOverlay: Failed to initialize WeatherService: {e}")
            self.weather_service = None

        self.setup_ui()
        self.setup_animations()

        # Start collapsed
        self.setMinimumWidth(self.collapsed_width)
        self.setMaximumWidth(self.collapsed_width)
        self.content_frame.setVisible(False)

    def setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toggle button on the left (arrow points toward content)
        self.toggle_btn = QPushButton("◀")  # Points left = "expand to reveal content"
        self.toggle_btn.setFixedSize(28, 80)
        self.toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(31, 41, 55, 240);
                border: 1px solid #374151;
                border-right: none;
                border-top-left-radius: 8px;
                border-bottom-left-radius: 8px;
                color: #F59E0B;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(41, 51, 65, 240);
                color: #FBBF24;
            }
            QPushButton:pressed {
                background-color: rgba(21, 31, 45, 240);
            }
        """)
        self.toggle_btn.clicked.connect(self.toggle_visibility)
        layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignRight)

        # Content frame
        self.content_frame = QFrame()
        self.content_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 20, 35, 240);
                border: 1px solid #374151;
                border-right: none;
                border-top-left-radius: 8px;
                border-bottom-left-radius: 8px;
            }
        """)

        content_layout = QVBoxLayout(self.content_frame)
        content_layout.setContentsMargins(8, 6, 8, 6)
        content_layout.setSpacing(5)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("☀️ VENUE WEATHER")
        title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #F59E0B; border: none;")
        header_layout.addWidget(title_label)

        # Refresh button
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedSize(20, 20)
        self.refresh_btn.setFont(QFont("Segoe UI", 10))
        self.refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(55, 65, 81, 180);
                border: 1px solid #4B5563;
                border-radius: 3px;
                color: #9CA3AF;
            }
            QPushButton:hover {
                background-color: rgba(75, 85, 99, 200);
                color: #D1D5DB;
            }
        """)
        self.refresh_btn.clicked.connect(self.refresh_weather)
        header_layout.addWidget(self.refresh_btn)

        content_layout.addLayout(header_layout)

        # Scrollable area for weather cards
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
            QScrollBar:vertical {
                background-color: rgba(31, 41, 55, 150);
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background-color: #4B5563;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(4)
        self.cards_layout.addStretch()

        scroll_area.setWidget(self.cards_container)
        content_layout.addWidget(scroll_area)

        # Status label
        self.status_label = QLabel("No venues loaded")
        self.status_label.setFont(QFont("Segoe UI", 7))
        self.status_label.setStyleSheet("color: #6B7280; border: none;")
        content_layout.addWidget(self.status_label)

        layout.addWidget(self.content_frame)

        self.setStyleSheet("QWidget { background-color: transparent; }")

    def setup_animations(self):
        self.animation = QPropertyAnimation(self, b"minimumWidth")
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def toggle_visibility(self):
        print(f"🌤️ WeatherOverlay toggle_visibility called, is_expanded={self.is_expanded}")
        if self.is_expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self):
        print(f"🌤️ WeatherOverlay.expand() called, already expanded={self.is_expanded}")
        if self.is_expanded:
            return

        self.is_expanded = True
        self.content_frame.setVisible(True)
        self.toggle_btn.setText("▶")  # Points right = "collapse to hide content"

        # Set both min and max width for immediate effect
        self.setMinimumWidth(self.expanded_width)
        self.setMaximumWidth(self.expanded_width)

        print(f"✅ WeatherOverlay expanded to width={self.expanded_width}, cards={len(self.weather_cards)}")
        self.visibilityChanged.emit(True)

    def collapse(self):
        print(f"🌤️ WeatherOverlay.collapse() called, is_expanded={self.is_expanded}")
        if not self.is_expanded:
            return

        self.is_expanded = False
        self.toggle_btn.setText("◀")  # Points left = "expand to reveal content"

        # Set both min and max width for immediate effect
        self.content_frame.setVisible(False)
        self.setMinimumWidth(self.collapsed_width)
        self.setMaximumWidth(self.collapsed_width)

        print(f"✅ WeatherOverlay collapsed to width={self.collapsed_width}")
        self.visibilityChanged.emit(False)

    def load_venues(self, venues: List[Dict]):
        """Load venues and fetch weather data for each"""
        print(f"🌤️ WeatherOverlay.load_venues called with {len(venues) if venues else 0} venues")

        if not self.weather_service:
            print("❌ WeatherOverlay: weather_service is None - cannot fetch weather")
            self.status_label.setText("Weather service unavailable")
            return

        # Clear existing cards
        self._clear_cards()

        if not venues:
            print("⚠️ WeatherOverlay: No venues provided")
            self.status_label.setText("No venues to display")
            return

        print(f"✅ WeatherOverlay: Starting weather fetch for {len(venues)} venues")
        self.status_label.setText(f"Loading weather for {len(venues)} venues...")

        # Stop any existing fetch
        if self.fetch_worker and self.fetch_worker.isRunning():
            self.fetch_worker.stop()
            self.fetch_worker.wait()

        # Start background fetch
        self.fetch_worker = WeatherFetchWorker(self.weather_service, venues)
        self.fetch_worker.weatherFetched.connect(self._on_weather_fetched)
        self.fetch_worker.fetchError.connect(self._on_fetch_error)
        self.fetch_worker.finished.connect(self._on_fetch_complete)
        self.fetch_worker.start()

    def _on_weather_fetched(self, venue_id: str, weather_data: Dict):
        """Handle successful weather fetch"""
        venue = weather_data.get('venue', {})
        card = VenueWeatherCard(venue, weather_data)
        card.clicked.connect(self._on_card_clicked)

        # Insert before the stretch
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
        self.weather_cards[venue_id] = card

    def _on_fetch_error(self, venue_id: str, error: str):
        """Handle weather fetch error"""
        print(f"Weather fetch error for {venue_id}: {error}")

    def _on_fetch_complete(self):
        """Handle fetch completion"""
        count = len(self.weather_cards)
        self.status_label.setText(f"{count} venue{'s' if count != 1 else ''} loaded")

    def _on_card_clicked(self, lat: float, lon: float, name: str):
        """Handle venue card click"""
        self.venueSelected.emit(lat, lon, name)

    def _clear_cards(self):
        """Clear all weather cards"""
        for card in self.weather_cards.values():
            card.deleteLater()
        self.weather_cards.clear()

    def refresh_weather(self):
        """Refresh weather data for all venues"""
        if self.weather_cards:
            venues = [card.venue_data for card in self.weather_cards.values()]
            self.load_venues(venues)

    def cleanup(self):
        """Clean up resources"""
        if self.fetch_worker and self.fetch_worker.isRunning():
            self.fetch_worker.stop()
            self.fetch_worker.wait()
