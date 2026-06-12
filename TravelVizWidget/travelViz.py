"""Standalone window wrapper for TravelVizPanel.

All application logic lives in travel_viz_panel.TravelVizPanel (an embeddable
QWidget). This module only adds window chrome: menubar, status bar, and a
QMainWindow shell, and remains the standalone entry point.
"""

import sys
import logging
from datetime import datetime

from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QProgressBar, QLabel
from PyQt6.QtGui import QAction, QFont

from travel_viz_panel import TravelVizPanel

logger = logging.getLogger(__name__)


class SportsTrackerMainWindow(QMainWindow):
    """Thin QMainWindow shell around TravelVizPanel for standalone use."""

    def __init__(self):
        super().__init__()

        self.panel = TravelVizPanel(self)
        self.setCentralWidget(self.panel)

        self.setWindowTitle(
            f"travelViz: An EffortOdds widget - {self.panel.current_league} "
            f"{self.panel.current_season} Season")
        self.setGeometry(100, 100, 1900, 1100)
        self.setStyleSheet("""
        QMainWindow {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #0A1428, stop:0.5 #051225, stop:1 #020815);
            color: #E0E0E0;
        }
        QMenuBar {
            background-color: rgba(10, 25, 50, 220);
            color: #E0E0E0;
            border-bottom: 1px solid rgba(100, 150, 200, 100);
            padding: 4px;
        }
        QMenuBar::item {
            background: transparent;
            padding: 6px 12px;
            border-radius: 4px;
        }
        QMenuBar::item:selected {
            background-color: rgba(0, 100, 180, 150);
        }
        QStatusBar {
            background-color: rgba(10, 25, 50, 200);
            color: #B0B0B0;
            border-top: 1px solid rgba(100, 150, 200, 100);
            font-size: 11px;
        }
        """)

        self.setup_menu()
        self.setup_status_bar()
        self.connect_panel_signals()

    def setup_menu(self):
        """Build the menubar from the panel's shared QActions."""
        menubar = self.menuBar()
        acts = self.panel.actions

        data_menu = menubar.addMenu("Data")
        data_menu.addAction(acts["load_season"])
        data_menu.addAction(acts["refresh"])
        data_menu.addSeparator()
        data_menu.addAction(acts["export"])
        data_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        data_menu.addAction(exit_action)

        animation_menu = menubar.addMenu("Animation")
        animation_menu.addAction(acts["start_animation"])
        animation_menu.addAction(acts["stop_animation"])

        tracking_menu = menubar.addMenu("Live Tracking")
        tracking_menu.addAction(acts["start_tracking"])
        tracking_menu.addAction(acts["stop_tracking"])
        tracking_menu.addSeparator()
        tracking_menu.addAction(acts["track_aircraft"])
        tracking_menu.addAction(acts["watchlist"])
        tracking_menu.addSeparator()
        tracking_menu.addAction(acts["clear_flights"])

        league_menu = menubar.addMenu("League")
        league_menu.addAction(acts["league_mlb"])
        league_menu.addAction(acts["league_nba"])
        league_menu.addAction(acts["league_nhl"])
        league_menu.addSeparator()
        league_menu.addAction(acts["league_stats"])

        view_menu = menubar.addMenu("View")
        view_menu.addAction(acts["toggle_paths"])
        view_menu.addAction(acts["toggle_cities"])
        view_menu.addSeparator()
        view_menu.addAction(acts["toggle_day_night"])
        view_menu.addSeparator()
        view_menu.addAction(acts["reset_view"])

        help_menu = menubar.addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def setup_status_bar(self):
        self.status_bar = self.statusBar()

        self.connection_status = QLabel("Loading...")
        self.schedule_count_label = QLabel("Games: 0")
        self.data_age_label = QLabel("Data: Never")
        self.performance_label = QLabel("FPS: --")

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)

        self.status_bar.addWidget(self.connection_status)
        self.status_bar.addWidget(QLabel(" | "))
        self.status_bar.addWidget(self.schedule_count_label)
        self.status_bar.addWidget(QLabel(" | "))
        self.status_bar.addWidget(self.data_age_label)
        self.status_bar.addPermanentWidget(self.performance_label)
        self.status_bar.addPermanentWidget(self.progress_bar)

    def connect_panel_signals(self):
        self.panel.statusMessage.connect(self.on_status_message)
        self.panel.titleChanged.connect(self.setWindowTitle)
        self.panel.connectionChanged.connect(self.on_connection_changed)
        self.panel.progressChanged.connect(self.on_progress_changed)
        self.panel.travelCountChanged.connect(
            lambda n: self.schedule_count_label.setText(f"Travel: {n}"))
        self.panel.travelCountChanged.connect(
            lambda n: self.data_age_label.setText(
                "Data: " + datetime.now().strftime('%H:%M:%S')))
        self.panel.globe_widget.fpsUpdated.connect(
            lambda fps: self.performance_label.setText(f"FPS: {fps:.0f}"))

    def on_status_message(self, message: str, timeout: int):
        self.status_bar.showMessage(message, timeout)

    def on_connection_changed(self, text: str, healthy: bool):
        self.connection_status.setText(text)
        self.connection_status.setStyleSheet("color: #00FF00;" if healthy else "color: red;")

    def on_progress_changed(self, progress: int):
        if 0 < progress < 100:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(progress)
        else:
            self.progress_bar.setVisible(False)

    def show_about(self):
        QMessageBox.about(self, "Giving it my all, maximum effort",
                          "travelViz, an Effort Odds widget")

    def closeEvent(self, event):
        self.panel.shutdown()
        event.accept()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    app = QApplication(sys.argv)

    app.setApplicationName("Sports Team Travel Tracker")
    app.setApplicationVersion("4.0")
    app.setOrganizationName("SportsTracker")
    app.setOrganizationDomain("sportstracker.dev")

    font = QFont("Segoe UI", 9)
    app.setFont(font)

    window = SportsTrackerMainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
