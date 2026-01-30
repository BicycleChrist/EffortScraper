#!/usr/bin/env python3
"""
Compact Shortwave Radio Widget for PyQt6
Searches and plays sports talk radio stations with audio visualizer
Uses RadioBrowser API (same backend as Shortwave app)
"""

import sys
import json
import urllib.request
import urllib.parse
import threading
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QListWidget, QLabel, QApplication, QFrame,
    QGraphicsDropShadowEffect
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QUrl, QRect, QPoint
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QLinearGradient, QBrush,
    QPainterPath, QPolygon, QFont
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
import numpy as np


class AudioVisualizer(QWidget):
    """Cyberpunk audio visualizer widget with neon glow effects"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self.setMaximumHeight(60)
        self.bars = [0] * 32  # More bars for detailed visualization
        self.active = False
        self.scanline_offset = 0

    def update_levels(self, levels):
        """Update bar levels for visualization"""
        # Interpolate to 32 bars if needed
        if len(levels) != 32:
            new_levels = []
            ratio = len(levels) / 32
            for i in range(32):
                idx = int(i * ratio)
                new_levels.append(levels[min(idx, len(levels)-1)])
            self.bars = new_levels
        else:
            self.bars = levels
        self.scanline_offset = (self.scanline_offset + 1) % 4
        self.update()

    def set_active(self, active):
        """Set whether audio is playing"""
        self.active = active
        if not active:
            self.bars = [0] * 32
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        width = self.width()
        height = self.height()
        bar_width = width / len(self.bars)

        # Dark background with subtle grid
        painter.fillRect(0, 0, width, height, QColor(8, 12, 18))

        # Draw cyberpunk grid lines (horizontal)
        painter.setPen(QPen(QColor(0, 255, 255, 25), 1))
        for i in range(6):
            y = height * i / 5
            painter.drawLine(0, int(y), width, int(y))

        # Draw vertical grid lines
        for i in range(0, width, 20):
            painter.drawLine(i, 0, i, height)

        # Draw bars with neon glow effect
        for i, level in enumerate(self.bars):
            x = i * bar_width
            bar_height = level * height * 0.92

            if bar_height < 2:
                continue

            # Cyberpunk color scheme: cyan -> magenta -> yellow based on level
            if level > 0.8:
                color = QColor(255, 255, 0)  # Neon yellow (peak)
                glow_color = QColor(255, 255, 0, 80)
            elif level > 0.6:
                color = QColor(255, 0, 128)  # Hot magenta
                glow_color = QColor(255, 0, 128, 60)
            elif level > 0.4:
                color = QColor(255, 0, 255)  # Magenta
                glow_color = QColor(255, 0, 255, 50)
            else:
                color = QColor(0, 255, 255)  # Cyan
                glow_color = QColor(0, 255, 255, 40)

            if not self.active:
                color.setAlpha(30)
                glow_color.setAlpha(10)

            # Draw glow (slightly larger, behind)
            painter.fillRect(
                int(x),
                int(height - bar_height - 2),
                int(bar_width),
                int(bar_height + 2),
                glow_color
            )

            # Draw main bar
            painter.fillRect(
                int(x + 1),
                int(height - bar_height),
                int(bar_width - 2),
                int(bar_height),
                color
            )

            # Draw bright cap on top of bar
            if bar_height > 4:
                cap_color = QColor(255, 255, 255, 200 if self.active else 40)
                painter.fillRect(
                    int(x + 1),
                    int(height - bar_height),
                    int(bar_width - 2),
                    2,
                    cap_color
                )

        # Draw scanlines for CRT effect
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1))
        for y in range(self.scanline_offset, height, 3):
            painter.drawLine(0, y, width, y)

        # Draw border glow
        border_pen = QPen(QColor(0, 255, 255, 100), 1)
        painter.setPen(border_pen)
        painter.drawRect(0, 0, width - 1, height - 1)


class ShortWaveRadioWidget(QWidget):
    """Compact radio widget using RadioBrowser API"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stations = []
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.7)

        self.init_ui()
        self.setup_visualizer_timer()

    def init_ui(self):
        """Initialize the compact UI with cyberpunk styling"""
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Cyberpunk dark theme with neon accents
        self.setStyleSheet("""
            QWidget {
                background-color: #0a0e14;
                color: #00ffff;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            QLineEdit {
                background-color: #0d1117;
                border: 1px solid #1a3a45;
                border-left: 3px solid #00d4d4;
                border-radius: 0px;
                padding: 8px 12px;
                color: #a0c8d0;
                font-size: 12px;
                font-family: 'Consolas', 'Courier New', monospace;
                selection-background-color: #ff0080;
            }
            QLineEdit:focus {
                border: 1px solid #00d4d4;
                border-left: 3px solid #ff0080;
                background-color: #101820;
                color: #00ffff;
            }
            QPushButton {
                background-color: rgba(0, 40, 50, 0.5);
                border: 1px solid #00d4d4;
                border-radius: 0px;
                padding: 8px 16px;
                color: #00d4d4;
                font-size: 11px;
                font-weight: bold;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            QPushButton:hover {
                background-color: rgba(0, 212, 212, 0.2);
                border: 1px solid #00ffff;
                color: #ffffff;
            }
            QPushButton:pressed {
                background-color: rgba(0, 212, 212, 0.35);
            }
            QPushButton:disabled {
                border: 1px solid #1a2a35;
                color: #2a3a45;
                background-color: rgba(10, 20, 25, 0.5);
            }
            QPushButton#playBtn {
                background-color: rgba(0, 50, 35, 0.6);
                border: 1px solid #00cc70;
                border-left: 3px solid #00ff88;
                color: #00ff88;
            }
            QPushButton#playBtn:hover {
                background-color: rgba(0, 80, 50, 0.7);
                border: 1px solid #00ff88;
                border-left: 3px solid #ffffff;
                color: #ffffff;
            }
            QPushButton#playBtn:disabled {
                border: 1px solid #0a2a1a;
                border-left: 3px solid #0a2a1a;
                color: #1a3a2a;
                background-color: rgba(5, 15, 10, 0.5);
            }
            QPushButton#stopBtn {
                background-color: rgba(50, 0, 35, 0.6);
                border: 1px solid #cc0066;
                border-right: 3px solid #ff0080;
                color: #ff0080;
            }
            QPushButton#stopBtn:hover {
                background-color: rgba(80, 0, 50, 0.7);
                border: 1px solid #ff0080;
                border-right: 3px solid #ffffff;
                color: #ffffff;
            }
            QPushButton#stopBtn:disabled {
                border: 1px solid #2a0a1a;
                border-right: 3px solid #2a0a1a;
                color: #3a1a2a;
                background-color: rgba(15, 5, 10, 0.5);
            }
            QPushButton#searchBtn {
                background-color: rgba(50, 45, 0, 0.5);
                border: 1px solid #cccc00;
                border-left: 3px solid #ffff00;
                color: #ffff00;
            }
            QPushButton#searchBtn:hover {
                background-color: rgba(70, 65, 0, 0.6);
                border: 1px solid #ffff00;
                color: #ffffff;
            }
            QListWidget {
                background-color: #0a0f14;
                border: 1px solid #1a2530;
                border-left: 2px solid #00d4d4;
                border-radius: 0px;
                padding: 2px;
                color: #7a8590;
                font-size: 11px;
                font-family: 'Consolas', 'Courier New', monospace;
                outline: none;
            }
            QListWidget::item {
                padding: 5px 8px;
                border: none;
                border-bottom: 1px solid #0d1520;
                margin: 0px;
            }
            QListWidget::item:selected {
                background-color: rgba(0, 212, 212, 0.15);
                color: #00ffff;
                border-left: 2px solid #00ffff;
            }
            QListWidget::item:hover {
                background-color: rgba(0, 212, 212, 0.08);
                color: #a0d0d8;
            }
            QScrollBar:vertical {
                background-color: #080c10;
                width: 6px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background-color: #2a4050;
                min-height: 20px;
                border-radius: 0px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #00d4d4;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background-color: #080c10;
                height: 6px;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background-color: #2a4050;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background-color: #00d4d4;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QLabel {
                color: #506070;
                font-size: 10px;
                font-family: 'Consolas', 'Courier New', monospace;
            }
        """)

        # Header with cyberpunk title
        header_layout = QHBoxLayout()
        header_layout.setSpacing(0)

        # Corner accent
        corner_label = QLabel("//")
        corner_label.setStyleSheet("""
            QLabel {
                color: #ff0080;
                font-size: 12px;
                font-weight: bold;
                padding: 0px 4px 0px 0px;
            }
        """)

        title_label = QLabel("RADIO SCANNER")
        title_label.setStyleSheet("""
            QLabel {
                color: #00d4d4;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
                padding: 2px 0px;
            }
        """)

        freq_label = QLabel("FM/AM")
        freq_label.setStyleSheet("""
            QLabel {
                color: #606a75;
                font-size: 9px;
                padding: 0px 0px 0px 6px;
            }
        """)

        header_layout.addWidget(corner_label)
        header_layout.addWidget(title_label)
        header_layout.addWidget(freq_label)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # Separator line
        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.Shape.HLine)
        sep_line.setStyleSheet("""
            QFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d4d4, stop:0.5 #ff0080, stop:1 #00d4d4);
                max-height: 1px;
                border: none;
            }
        """)
        layout.addWidget(sep_line)

        # Search bar
        search_layout = QHBoxLayout()
        search_layout.setSpacing(4)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("> QUERY STATIONS...")
        self.search_input.returnPressed.connect(self.search_stations)

        search_btn = QPushButton("SCAN")
        search_btn.setObjectName("searchBtn")
        search_btn.setFixedWidth(70)
        search_btn.clicked.connect(self.search_stations)

        search_layout.addWidget(self.search_input)
        search_layout.addWidget(search_btn)
        layout.addLayout(search_layout)

        # Station list
        self.station_list = QListWidget()
        self.station_list.setMaximumHeight(180)
        self.station_list.setMinimumHeight(120)
        self.station_list.itemDoubleClicked.connect(self.play_station)
        layout.addWidget(self.station_list)

        # Controls
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(4)

        self.play_btn = QPushButton("PLAY")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.clicked.connect(self.play_station)

        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self.stop_playback)
        self.stop_btn.setEnabled(False)

        controls_layout.addWidget(self.play_btn)
        controls_layout.addWidget(self.stop_btn)
        layout.addLayout(controls_layout)

        # Status label with cyberpunk terminal style
        self.status_label = QLabel("> SYSTEM READY_")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #00cc70;
                font-size: 10px;
                padding: 6px 8px;
                background-color: #060a10;
                border: 1px solid #0a1a18;
                border-left: 2px solid #00cc70;
                font-family: 'Consolas', 'Courier New', monospace;
            }
        """)
        layout.addWidget(self.status_label)

        # Audio visualizer with cyberpunk container
        visualizer_container = QWidget()
        visualizer_container.setStyleSheet("""
            QWidget {
                background-color: #060a10;
                border: 1px solid #1a2530;
                padding: 0px;
            }
        """)
        visualizer_layout = QVBoxLayout(visualizer_container)
        visualizer_layout.setContentsMargins(2, 2, 2, 2)

        self.visualizer = AudioVisualizer()
        visualizer_layout.addWidget(self.visualizer)

        layout.addWidget(visualizer_container)

        # Bottom accent line
        bottom_line = QFrame()
        bottom_line.setFrameShape(QFrame.Shape.HLine)
        bottom_line.setStyleSheet("""
            QFrame {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1a3040, stop:0.4 #00d4d4, stop:0.6 #ff0080, stop:1 #1a3040);
                max-height: 2px;
                border: none;
            }
        """)
        layout.addWidget(bottom_line)

        self.setLayout(layout)
        self.setMaximumWidth(380)
        self.setMinimumWidth(320)

        # Auto-search on startup
        QTimer.singleShot(100, self.auto_search)

    def setup_visualizer_timer(self):
        """Setup timer for visualizer animation"""
        self.viz_timer = QTimer()
        self.viz_timer.timeout.connect(self.update_visualizer)
        self.viz_phase = 0

    def update_visualizer(self):
        """Animate visualizer with simulated audio levels"""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            # Simulate audio levels with varying patterns - more dynamic for cyberpunk feel
            self.viz_phase += 0.15
            levels = []
            for i in range(32):
                # Multi-wave pattern for more interesting visualization
                wave1 = np.sin(self.viz_phase + i * 0.25) * 0.25
                wave2 = np.sin(self.viz_phase * 1.5 + i * 0.15) * 0.2
                wave3 = np.sin(self.viz_phase * 0.7 + i * 0.4) * 0.15
                base = wave1 + wave2 + wave3 + 0.4
                variation = np.random.random() * 0.25
                levels.append(max(0.05, min(1, base + variation)))
            self.visualizer.update_levels(levels)

    def auto_search(self):
        """Auto-search for sports talk stations on startup"""
        self.search_input.setText("sports")
        self.search_stations()

    def _format_status(self, text):
        # Format status text
        return f"> {text.upper()}_"

    def search_stations(self):
        """Search for stations using RadioBrowser API"""
        query = self.search_input.text().strip()
        if not query:
            query = "sports talk"

        self.status_label.setText(self._format_status(f"SCANNING: {query}"))
        self.station_list.clear()

        # Run search in thread to avoid blocking UI
        thread = threading.Thread(target=self._search_thread, args=(query,))
        thread.daemon = True
        thread.start()

    def _search_thread(self, query):
        """Background thread for station search using RadioBrowser API"""
        try:
            all_stations = {}  # Use dict to deduplicate by stationuuid

            # Search methods to try (in order)
            search_methods = [
                ('bytag', f"https://de1.api.radio-browser.info/json/stations/bytag/{urllib.parse.quote(query)}"),
                ('byname', f"https://de1.api.radio-browser.info/json/stations/byname/{urllib.parse.quote(query)}"),
                ('search', f"https://de1.api.radio-browser.info/json/stations/search?name={urllib.parse.quote(query)}&tag={urllib.parse.quote(query)}&order=votes&reverse=true&limit=200")
            ]

            # Try each search method and combine results
            for method_name, api_url in search_methods:
                try:
                    req = urllib.request.Request(
                        api_url,
                        headers={'User-Agent': 'PyQt6RadioWidget/1.0'}
                    )

                    with urllib.request.urlopen(req, timeout=10) as response:
                        data = json.loads(response.read().decode())

                    # Add stations to our collection (deduplicate by UUID)
                    for station in data:
                        station_uuid = station.get('stationuuid')
                        if station_uuid and (station.get('url_resolved') or station.get('url')):
                            # Only add if not already in collection
                            if station_uuid not in all_stations:
                                all_stations[station_uuid] = {
                                    'name': station.get('name', 'Unknown'),
                                    'url': station.get('url_resolved') or station.get('url'),
                                    'country': station.get('country', ''),
                                    'tags': station.get('tags', ''),
                                    'bitrate': station.get('bitrate', 0),
                                    'votes': station.get('votes', 0),
                                    'codec': station.get('codec', '')
                                }
                except Exception as e:
                    # Continue with other search methods if one fails
                    continue

            # Convert dict to list and sort by votes (popularity)
            self.stations = sorted(
                all_stations.values(),
                key=lambda x: x['votes'],
                reverse=True
            )

            # Update UI in main thread
            QTimer.singleShot(0, self._update_station_list)

        except urllib.error.URLError as e:
            QTimer.singleShot(0, lambda: self.status_label.setText(self._format_status(f"NET_ERR: {str(e)[:30]}")))
        except Exception as e:
            QTimer.singleShot(0, lambda: self.status_label.setText(self._format_status(f"SYS_ERR: {str(e)[:30]}")))

    def _update_station_list(self):
        """Update station list in UI"""
        self.station_list.clear()
        if self.stations:
            for station in self.stations:
                # Format: Name - Country [codec] (bitrate)
                display = f"{station['name']}"

                details = []
                if station['country']:
                    details.append(station['country'])
                if station['codec']:
                    details.append(f"[{station['codec'].upper()}]")
                if station['bitrate'] > 0:
                    details.append(f"{station['bitrate']}k")

                if details:
                    display += f" - {' '.join(details)}"

                self.station_list.addItem(display)
            self.status_label.setText(self._format_status(f"LOCKED: {len(self.stations)} SIGNALS"))
        else:
            self.status_label.setText(self._format_status("NO SIGNAL DETECTED"))

    def play_station(self):
        """Play selected station"""
        current_item = self.station_list.currentItem()
        if not current_item:
            return

        index = self.station_list.currentRow()
        if index < 0 or index >= len(self.stations):
            return

        station = self.stations[index]
        self.stop_playback()

        try:
            # Start playback using QMediaPlayer
            self.player.setSource(QUrl(station['url']))
            self.player.play()

            # Truncate station name if too long
            name = station['name'][:35] + "..." if len(station['name']) > 35 else station['name']
            self.status_label.setText(self._format_status(f"STREAM: {name}"))
            self.play_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.visualizer.set_active(True)
            self.viz_timer.start(50)  # Update visualizer at 20 FPS

        except Exception as e:
            self.status_label.setText(self._format_status(f"STREAM_ERR: {str(e)[:25]}"))

    def stop_playback(self):
        """Stop current playback"""
        self.player.stop()
        self.status_label.setText(self._format_status("STREAM TERMINATED"))
        self.play_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.visualizer.set_active(False)
        self.viz_timer.stop()

    def closeEvent(self, event):
        """Cleanup on close"""
        self.stop_playback()
        event.accept()


def main():
    """Standalone demo"""
    app = QApplication(sys.argv)

    # Create main window for demo
    widget = ShortWaveRadioWidget()
    widget.setWindowTitle("// RADIO SCANNER")
    widget.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
