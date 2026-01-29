import math
import sys
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                             QWidget, QSplitter, QStatusBar, QMenuBar, QMenu,
                             QMessageBox, QProgressBar, QLabel, QFileDialog,
                             QComboBox, QSpinBox, QCheckBox, QGroupBox, QPushButton, QInputDialog, QSlider, QFrame)
from PyQt6.QtCore import (Qt, QSettings,pyqtSignal,QThread)
from PyQt6.QtGui import QAction, QIcon, QFont, QVector3D

# Import components
from data_client import (ESPNSportsDataAggregator, TeamTravelData, TeamInfo,
                        AmadeusWorker, TeamTravelIntelligence, AirportInfo,
                        HotelOption, RouteInsights)
from flight_tracker_panel import FlightControlPanel
from globe_widget import FlightGlobeWidget
from upcoming_games_overlay import UpcomingGamesOverlay
from weather_overlay import WeatherOverlay
from live_flight_tracker import RealTimeFlightTracker, DirectFlightTracker
from venue_tooltip import VenueTooltip

class ConfigLoader:
    """Load API configuration from files"""

    def __init__(self, config_file: str = "api_keys.json"):
        self.config_file = Path(config_file)
        self.config = {}
        self.load_config()

    def load_config(self) -> Dict[str, str]:
        """Load API keys from configuration file"""
        if not self.config_file.exists():
            self.create_default_config()
            return {}

        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
            return self.config
        except Exception as e:
            print(f"Error loading config: {e}")
            return {}

    def create_default_config(self):
        """Create default configuration file"""
        default_config = {
            "amadeus": "",
            "amadeus_secret": "",
            "backup_apis_note": "Additional APIs for future expansion"
        }

        try:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            print(f"Created default config file: {self.config_file}")
        except Exception as e:
            print(f"Error creating config file: {e}")

    def get_api_keys(self) -> Dict[str, str]:
        """Get API keys dictionary"""
        return self.config

    def is_configured(self) -> bool:
        """Check if APIs are configured - ESPN scraping is always available"""
        return True

#TODO: Implement logic to switch between in season sports for inital view
class SportsTrackerMainWindow(QMainWindow):
    """Main window with multi-league ESPN schedule scraping support"""

    def __init__(self):
        super().__init__()

        # Load configuration
        self.config_loader = ConfigLoader()

        # Initialize components
        self.sports_aggregator = None
        self.data_update_timer = None
        self.current_travel_data = []
        self.all_teams = []
        self.current_league = "NHL"  # If Default league is not in season, UI will hang
        self.current_season = str(datetime.now().year)

        # Live flight tracker
        self.flight_tracker = None
        self.direct_tracker = None
        self.live_tracking_active = False

        # Setup UI
        self.setup_ui()
        self.setup_menu()
        self.setup_status_bar()
        self.setup_sports_system()
        self.setup_live_flight_tracker()
        self.connect_signals()

        # Sync dropdown and Default league
        self.synchronize_league_state()

        # Load settings
        self.settings = QSettings("SportsTracker", "TeamTravel")

        self.start_sports_monitoring()

    def setup_ui(self):
        """Setup the main window UI"""
        self.setWindowTitle(f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")
        self.setGeometry(100, 100, 1900, 1100)

        # Central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # Create splitter for resizable panels
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Control panel (left side)
        control_widget = QWidget()
        control_layout = QVBoxLayout(control_widget)

        # Season controls
        season_group = self.create_season_controls()
        control_layout.addWidget(season_group)

        # Original control panel
        self.control_panel = FlightControlPanel()
        control_layout.addWidget(self.control_panel)

        control_widget.setMinimumWidth(350)
        control_widget.setMaximumWidth(500)
        splitter.addWidget(control_widget)

        # Globe widget (right side)
        self.globe_widget = FlightGlobeWidget()
        splitter.addWidget(self.globe_widget)

        passive_spin_button = QPushButton(f"passive spin: {('enabled' if self.globe_widget.passive_spin else 'disabled')}")
        def PassiveSpinToggleCallback(_):
            self.globe_widget.passive_spin = not self.globe_widget.passive_spin
            passive_spin_button.setText(f"passive spin: {('enabled' if self.globe_widget.passive_spin else 'disabled')}")
        passive_spin_button.clicked.connect(PassiveSpinToggleCallback)
        passive_spin_button.setFixedWidth(150)
        passive_spin_button.setStyleSheet(""" QPushButton {
            background-color: rgba(31, 41, 55, 10);
            color: white;
        }""")

        slider_label = QLabel("rotation speed: 0.25")
        slider_label.setFixedWidth(150)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-200, 200)
        slider.setValue(25)
        slider.setFixedWidth(150)
        slider.setStyleSheet(""" QSlider {
            background-color: rgba(31, 41, 55, 10);
            color: #10B98110;
        }""")

        def RotationSliderCallback(slider_val):
            slider_label.setText(f"rotation speed: {slider_val/100:.3f}")
            self.globe_widget.passive_rotation_speed = QVector3D(0.05, slider_val/100, 0)

        slider.valueChanged.connect(RotationSliderCallback)

        slider_layout = QVBoxLayout(self.globe_widget)
        slider_layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        slider_layout.setContentsMargins(10, 10, 0, 0)
        slider_layout.addWidget(passive_spin_button)
        slider_layout.addWidget(slider_label)
        slider_layout.addWidget(slider)


        # Upcoming games overlay (positioned as floating widget over globe)
        # Will be positioned absolutely in resizeEvent
        self.games_overlay = UpcomingGamesOverlay(self.globe_widget)
        self.games_overlay.setFixedHeight(300)
        self.games_overlay.move(0, 10)  # Initial position at top-left of globe
        self.games_overlay.raise_()  # Ensure it's on top

        # Weather overlay (positioned on right side of globe)
        self.weather_overlay = WeatherOverlay(self.globe_widget)
        self.weather_overlay.setFixedHeight(350)
        self.weather_overlay.move(self.globe_widget.width() - 30, 10)
        self.weather_overlay.raise_()
        self.weather_overlay.venueSelected.connect(self.on_weather_venue_selected)
        self.weather_overlay.visibilityChanged.connect(self.on_weather_overlay_toggled)

        # Venue tooltip for marker clicks
        self.venue_tooltip = VenueTooltip(self)

        # Set splitter proportions (30% control panel, 70% globe)
        splitter.setSizes([400, 1500])

        # Apply dark sports theme
        self.apply_sports_theme()

    def create_season_controls(self) -> QGroupBox:
        """Create season and data management controls"""
        group = QGroupBox("SEASON DATA")
        layout = QVBoxLayout(group)

        # League selection section
        league_layout = QHBoxLayout()
        league_layout.addWidget(QLabel("League:"))

        self.league_status_label = QLabel("MLB")
        self.league_status_label.setStyleSheet("font-weight: bold; color: #00AA00;")
        league_layout.addWidget(self.league_status_label)

        league_layout.addStretch()
        layout.addLayout(league_layout)

        # Season selection
        season_layout = QHBoxLayout()
        season_layout.addWidget(QLabel("Season:"))
        self.season_combo = QComboBox()

        # Add recent seasons (will be updated when league changes)
        self.update_season_combo_for_league("MLB")

        season_layout.addWidget(self.season_combo)
        layout.addLayout(season_layout)

        # Data loading controls
        load_layout = QHBoxLayout()

        self.load_full_season_btn = QPushButton("Load Full Season")
        self.load_full_season_btn.setToolTip("Load complete season schedule (recommended)")
        load_layout.addWidget(self.load_full_season_btn)

        self.load_current_week_btn = QPushButton("Current Week")
        self.load_current_week_btn.setToolTip("Load just current week for quick preview")
        load_layout.addWidget(self.load_current_week_btn)

        layout.addLayout(load_layout)

        # Team selection for focused view
        team_layout = QHBoxLayout()
        team_layout.addWidget(QLabel("Focus Team:"))
        self.focus_team_combo = QComboBox()
        self.focus_team_combo.addItem("All Teams", "")
        team_layout.addWidget(self.focus_team_combo)
        layout.addLayout(team_layout)

        # Data status and statistics
        self.season_status_label = QLabel("No season data loaded")
        self.season_status_label.setStyleSheet("font-style: italic; color: #888;")
        layout.addWidget(self.season_status_label)

        # Progress bar for season loading
        self.season_progress = QProgressBar()
        self.season_progress.setVisible(False)
        layout.addWidget(self.season_progress)

        return group

    def update_season_combo_for_league(self, league: str):
        """Update season combo box based on selected league"""
        self.season_combo.clear()

        current_year = datetime.now().year
        current_month = datetime.now().month

        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons span two years (e.g., 2024-25)
            # The season runs from October to June of the next year

            # Determine the current season's start year
            if current_month >= 10:  # October or later - new season has started
                current_season_start = current_year
            else:  # Before October - still in previous season
                current_season_start = current_year - 1

            # Generate seasons starting from current season, going back 5 years
            for i in range(6):  # Current + 5 previous seasons
                start_year = current_season_start - i
                season_str = f"{start_year}-{str(start_year + 1)[2:]}"
                self.season_combo.addItem(season_str, season_str)

        else:  # MLB
            # MLB seasons are single years and run calendar year
            for year in range(current_year, current_year - 5, -1):
                season_str = str(year)
                self.season_combo.addItem(season_str, season_str)

        # Set current season as default (this should now work correctly)
        if self.sports_aggregator:
            current_season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
            print(f"🐛 DEBUG - Setting default season for {league}: '{current_season}'")
            print(f"🐛 DEBUG - Available seasons in combo: {[self.season_combo.itemText(i) for i in range(self.season_combo.count())]}")

            index = self.season_combo.findText(current_season)
            if index >= 0:
                self.season_combo.setCurrentIndex(index)
                self.current_season = current_season
                print(f"🐛 DEBUG - Successfully set season to '{current_season}' at index {index}")
            else:
                print(f"🐛 DEBUG - WARNING: Could not find season '{current_season}' in combo box!")
                # Fallback to first item
                if self.season_combo.count() > 0:
                    fallback_season = self.season_combo.itemText(0)
                    self.season_combo.setCurrentIndex(0)
                    self.current_season = fallback_season
                    print(f"🐛 DEBUG - Fallback: Using first season '{fallback_season}'")




    def setup_sports_system(self):
        """Setup sports data system with ESPN scraping and Amadeus integration"""
        api_keys = self.config_loader.get_api_keys()

        # Initialize sports data aggregator with config dict
        self.sports_aggregator = ESPNSportsDataAggregator(api_keys)

        # Set up Amadeus integration if keys are available
        amadeus_key = api_keys.get("amadeus")
        amadeus_secret = api_keys.get("amadeus_secret")

        if amadeus_key and amadeus_secret:
            success = self.sports_aggregator.set_amadeus_credentials(amadeus_key, amadeus_secret)
            if success:
                print("✅ Amadeus integration configured")
            else:
                print("⚠️  Amadeus integration failed to initialize")
        else:
            print("ℹ️  Amadeus API keys not found - analysis features disabled")

        # Sync drop down to League button select
        self.sports_aggregator.set_league(self.current_league)

        # Connect aggregator signals
        self.sports_aggregator.dataUpdated.connect(self.on_travel_data_updated)
        self.sports_aggregator.progressUpdated.connect(self.on_progress_updated)
        self.sports_aggregator.errorOccurred.connect(self.on_data_error)
        self.sports_aggregator.seasonDataLoaded.connect(self.on_season_data_loaded)

        # Give control panel access to aggregator for distance calculations
        self.control_panel.aggregator = self.sports_aggregator

    def setup_live_flight_tracker(self):
        """Setup the real-time flight tracking system"""
        if not self.sports_aggregator:
            print("⚠️  Cannot setup flight tracker - sports aggregator not initialized")
            return

        api_keys = self.config_loader.get_api_keys()

        try:
            # Create the flight tracker with the database from sports_aggregator
            self.flight_tracker = RealTimeFlightTracker(
                db=self.sports_aggregator.db,
                api_keys=api_keys,
                league=self.current_league
            )

            # Connect tracker signals to handlers
            self.flight_tracker.flightDetected.connect(self.on_flight_detected)
            self.flight_tracker.flightUpdated.connect(self.on_flight_updated)
            self.flight_tracker.flightLanded.connect(self.on_flight_landed)
            self.flight_tracker.statusUpdate.connect(self.on_tracker_status)

            print("✅ Live flight tracker initialized")

        except Exception as e:
            print(f"⚠️  Failed to initialize flight tracker: {e}")
            self.flight_tracker = None

        # Setup direct flight tracker (for tail number/callsign tracking)
        try:
            from PyQt6.QtCore import Qt
            self.direct_tracker = DirectFlightTracker()
            # Use QueuedConnection to ensure signals from worker thread are processed in main thread
            self.direct_tracker.flightFound.connect(
                self.on_direct_flight_found, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.flightUpdated.connect(
                self.on_direct_flight_updated, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.flightLost.connect(
                self.on_direct_flight_lost, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.statusUpdate.connect(
                self.on_tracker_status, Qt.ConnectionType.QueuedConnection)
            print("✅ Direct flight tracker initialized")
        except Exception as e:
            print(f"⚠️  Failed to initialize direct flight tracker: {e}")
            import traceback
            traceback.print_exc()
            self.direct_tracker = None

    def start_live_tracking(self):
        """Start the live flight tracking"""
        if not self.flight_tracker:
            self.status_bar.showMessage("Flight tracker not available", 3000)
            return

        if self.live_tracking_active:
            self.status_bar.showMessage("Live tracking already active", 3000)
            return

        # Clear any previously displayed flights
        self.globe_widget.clear_live_flights()

        # Update tracker league if it changed
        if self.flight_tracker.league != self.current_league:
            self.flight_tracker.set_league(self.current_league)

        # Start the tracker immediately - API check happens in background thread
        self.flight_tracker.start()
        self.live_tracking_active = True
        self.status_bar.showMessage(f"🛫 Starting {self.current_league} flight tracking...", 3000)

        # Update menu action state
        if hasattr(self, 'start_tracking_action'):
            self.start_tracking_action.setEnabled(False)
        if hasattr(self, 'stop_tracking_action'):
            self.stop_tracking_action.setEnabled(True)

    def stop_live_tracking(self):
        """Stop the live flight tracking"""
        if not self.flight_tracker or not self.live_tracking_active:
            return

        self.flight_tracker.stop()
        self.flight_tracker.wait()  # Wait for thread to finish
        self.live_tracking_active = False

        # Clear live flights from globe
        self.globe_widget.clear_live_flights()

        self.status_bar.showMessage("🛑 Live flight tracking stopped", 3000)

        # Update menu action state
        if hasattr(self, 'start_tracking_action'):
            self.start_tracking_action.setEnabled(True)
        if hasattr(self, 'stop_tracking_action'):
            self.stop_tracking_action.setEnabled(False)

    def on_flight_detected(self, flight_data: dict):
        """Handle newly detected flight"""
        self.globe_widget.add_live_flight(flight_data)

        # Show notification in status bar
        team_id = flight_data.get('team_id', 'Unknown')
        callsign = flight_data.get('callsign', flight_data['icao24'])
        confidence = flight_data.get('confidence', 0)
        self.status_bar.showMessage(
            f"✈️ New flight: {team_id} - {callsign} ({confidence}% confidence)", 5000
        )

    def on_flight_updated(self, flight_data: dict):
        """Handle flight position update"""
        self.globe_widget.update_live_flight(flight_data)

    def on_flight_landed(self, icao24: str):
        """Handle flight landing/disappearing"""
        self.globe_widget.remove_live_flight(icao24)

    def on_tracker_status(self, message: str):
        """Handle status updates from tracker"""
        # Show in status bar (brief messages)
        self.status_bar.showMessage(message, 3000)

    def on_direct_flight_found(self, flight_data: dict):
        """Handle aircraft found by direct tracker"""
        self.globe_widget.add_live_flight(flight_data)
        label = flight_data.get('label', flight_data.get('registration', 'Unknown'))
        self.status_bar.showMessage(f"🎯 Found: {label}", 5000)

    def on_direct_flight_updated(self, flight_data: dict):
        """Handle position update from direct tracker"""
        self.globe_widget.update_live_flight(flight_data)

    def on_direct_flight_lost(self, identifier: str):
        """Handle aircraft lost by direct tracker"""
        self.globe_widget.remove_live_flight(identifier)

    def show_track_aircraft_dialog(self):
        """Show dialog to add aircraft to tracking watchlist"""
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox, QComboBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Track Aircraft")
        dialog.setMinimumWidth(350)

        layout = QFormLayout(dialog)

        # Identifier input
        identifier_input = QLineEdit()
        identifier_input.setPlaceholderText("e.g., N801DM, DAL123, a801dm")
        layout.addRow("Identifier:", identifier_input)

        # Type selector
        type_combo = QComboBox()
        type_combo.addItem("Callsign (e.g., SWA2294, DAL123)", "callsign")
        type_combo.addItem("Tail Number (e.g., N801DM, N8882Q)", "registration")
        type_combo.addItem("ICAO24 Hex (e.g., ac3e3a)", "hex")
        layout.addRow("Type:", type_combo)

        # Auto-detect type based on input
        def auto_detect_type():
            text = identifier_input.text().strip().upper()
            if not text:
                return
            # Tail numbers start with N (US) or C- (Canada) followed by numbers/letters
            if text.startswith('N') and len(text) >= 5 and text[1:2].isdigit():
                type_combo.setCurrentIndex(1)  # Registration
            # Hex codes are 6 chars, all hex digits
            elif len(text) == 6 and all(c in '0123456789ABCDEF' for c in text):
                type_combo.setCurrentIndex(2)  # Hex
            # Otherwise assume callsign (airline code + number)
            elif any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
                type_combo.setCurrentIndex(0)  # Callsign

        identifier_input.textChanged.connect(auto_detect_type)

        # Label input
        label_input = QLineEdit()
        label_input.setPlaceholderText("e.g., Mavericks Charter (optional)")
        layout.addRow("Label:", label_input)

        # Team association (optional)
        team_combo = QComboBox()
        team_combo.addItem("None", "")
        for team in sorted(self.all_teams, key=lambda t: t.display_name):
            team_combo.addItem(f"{team.display_name} ({team.abbreviation})", team.team_id)
        layout.addRow("Team:", team_combo)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            identifier = identifier_input.text().strip()
            if not identifier:
                QMessageBox.warning(self, "Error", "Please enter an identifier")
                return

            identifier_type = type_combo.currentData()
            label = label_input.text().strip() or identifier
            team_id = team_combo.currentData() or None

            if self.direct_tracker:
                self.direct_tracker.add_to_watchlist(
                    identifier=identifier,
                    identifier_type=identifier_type,
                    label=label,
                    team_id=team_id
                )

                # Start tracking if not already running
                if not self.direct_tracker.isRunning():
                    self.direct_tracker.start()
                    self.status_bar.showMessage(f"🎯 Started tracking: {label}", 3000)
                else:
                    self.status_bar.showMessage(f"🎯 Added to watchlist: {label}", 3000)
            else:
                QMessageBox.warning(self, "Error", "Direct flight tracker not available")

    def show_watchlist_dialog(self):
        """Show current watchlist and allow management"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QDialogButtonBox, QListWidgetItem

        if not self.direct_tracker:
            QMessageBox.warning(self, "Error", "Direct flight tracker not available")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Flight Watchlist")
        dialog.setMinimumWidth(400)
        dialog.setMinimumHeight(300)

        layout = QVBoxLayout(dialog)

        # List widget
        list_widget = QListWidget()

        watchlist = self.direct_tracker.get_watchlist()
        if not watchlist:
            item = QListWidgetItem("No aircraft in watchlist")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            list_widget.addItem(item)
        else:
            for entry in watchlist:
                status = "✈️ ACTIVE" if entry['is_active'] else "📡 Searching"
                team_str = f" [{entry['team_id']}]" if entry['team_id'] else ""
                item_text = f"{status} {entry['label']}{team_str}\n   {entry['type']}: {entry['identifier']}"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, entry['identifier'])
                list_widget.addItem(item)

        layout.addWidget(list_widget)

        # Buttons
        buttons = QDialogButtonBox()
        remove_btn = buttons.addButton("Remove Selected", QDialogButtonBox.ButtonRole.ActionRole)
        clear_btn = buttons.addButton("Clear All", QDialogButtonBox.ButtonRole.DestructiveRole)
        close_btn = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)

        def remove_selected():
            current = list_widget.currentItem()
            if current:
                identifier = current.data(Qt.ItemDataRole.UserRole)
                if identifier:
                    self.direct_tracker.remove_from_watchlist(identifier)
                    list_widget.takeItem(list_widget.row(current))

        def clear_all():
            self.direct_tracker.clear_watchlist()
            self.globe_widget.clear_live_flights()
            list_widget.clear()
            item = QListWidgetItem("No aircraft in watchlist")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            list_widget.addItem(item)

        remove_btn.clicked.connect(remove_selected)
        clear_btn.clicked.connect(clear_all)
        close_btn.clicked.connect(dialog.reject)

        layout.addWidget(buttons)
        dialog.exec()

    def connect_signals(self):
        """Connect UI signals"""
        # Season controls
        self.season_combo.currentTextChanged.connect(self.on_season_changed)
        self.load_full_season_btn.clicked.connect(self.load_full_season)
        self.load_current_week_btn.clicked.connect(self.load_current_week)
        self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

        # Control panel signals
        self.control_panel.refreshRequested.connect(self.force_refresh_season_data)
        self.control_panel.modeChanged.connect(self.on_league_changed)
        self.control_panel.teamChanged.connect(self.on_control_panel_team_changed)
        self.control_panel.amadeusAnalysisRequested.connect(self.start_amadeus_analysis)


        # Globe widget signals
        self.globe_widget.locationSelected.connect(self.on_location_selected)
        self.globe_widget.markerClicked.connect(self.on_marker_clicked)

        # Animation Signals
        self.globe_widget.animationStatusChanged.connect(self.on_animation_status_changed)
        self.globe_widget.animationProgressChanged.connect(self.on_animation_progress_changed)

        # Upcoming games overlay signals
        self.games_overlay.daysFilterChanged.connect(self.on_overlay_days_changed)

    def on_control_panel_team_changed(self, team_abbr: str):

        print(f"🔄 Control panel team changed to: {team_abbr}")

        # Update the main window's focus team combo to match (prevent signal loops)
        try:
            self.focus_team_combo.currentTextChanged.disconnect()
        except:
            pass  # Signal might not be connected

        # Find and set matching team in main window
        main_window_index = -1
        for i in range(self.focus_team_combo.count()):
            if self.focus_team_combo.itemData(i) == team_abbr:
                main_window_index = i
                break

        if main_window_index >= 0:
            self.focus_team_combo.setCurrentIndex(main_window_index)
            print(f"✅ Synced main window to team: {team_abbr}")
        else:
            print(f"⚠️ Could not find team {team_abbr} in main window combo")

        # Reconnect the signal
        self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

        # Load specific team data if needed
        if team_abbr:
            season = self.season_combo.currentText()
            if self.sports_aggregator:
                self.sports_aggregator.load_team_season_schedule(team_abbr, season, self.current_league)

        # Update overlay games
        if hasattr(self, 'games_overlay'):
            self.update_overlay_games()


    def start_amadeus_analysis(self, team_abbr: str, days_ahead: int):
        """Start Amadeus analysis for selected team - FIXED with debugging"""
        print(f"🚀 Main window received analysis request for {team_abbr}, {days_ahead} days")

        if not self.sports_aggregator:
            print("❌ No sports aggregator available")
            return

        config = self.config_loader.get_api_keys()
        amadeus_key = config.get("amadeus")
        amadeus_secret = config.get("amadeus_secret")

        if amadeus_key and amadeus_secret:
            print("✅ Amadeus credentials found, setting up analyzer")
            success = self.sports_aggregator.set_amadeus_credentials(amadeus_key, amadeus_secret)
            if success:
                print("✅ Amadeus credentials set successfully")

                # ESSENTIAL SIGNALS - Connect these first
                if not hasattr(self, '_amadeus_signals_connected'):
                    print("🔗 Connecting Amadeus signals...")

                    # When analysis completes successfully
                    self.sports_aggregator.amadeusIntelligenceReady.connect(
                        self.control_panel.on_analysis_complete  # ← NO PARENTHESES!
                    )

                    # When analysis fails
                    self.sports_aggregator.errorOccurred.connect(
                        self.control_panel.on_analysis_error  # ← NO PARENTHESES!
                    )

                    # When progress updates occur
                    self.sports_aggregator.amadeusProgressUpdated.connect(
                        self.control_panel.on_analysis_progress  # ← NO PARENTHESES!
                    )

                    self._amadeus_signals_connected = True
                    print("✅ Amadeus signals connected")

                # Show analysis starting in UI
                self.control_panel.analysis_progress.setVisible(True)
                self.control_panel.analysis_progress.setValue(0)
                self.control_panel.analyze_btn.setEnabled(False)

                print(f"🔄 Starting analysis for {team_abbr}...")

                # Start analysis
                self.sports_aggregator.get_team_travel_intelligence_async(team_abbr, days_ahead)
            else:
                print("❌ Failed to set Amadeus credentials")
        else:
            print("❌ Amadeus credentials not found in config")
            self.control_panel.status_message.setText("Amadeus API keys not configured")



    def on_league_changed(self, league: str):
        """Handle league change from control panel"""
        if league not in ["MLB", "NBA", "NHL"]:
            print(f"Unsupported league: {league}")
            return

        if league == self.current_league:
            return  # No change needed

        print(f"Switching from {self.current_league} to {league}")

        # Update current league
        self.current_league = league

        # Update sports aggregator
        if self.sports_aggregator:
            self.sports_aggregator.set_league(league)

        # Update flight tracker league
        if self.flight_tracker:
            self.flight_tracker.set_league(league)
            # Clear current flights when switching leagues
            self.globe_widget.clear_live_flights()

        # Update UI elements
        self.league_status_label.setText(league)
        self.update_season_combo_for_league(league)

        # Update window title
        current_season = self.season_combo.currentText() or self.current_season
        self.setWindowTitle(f"Sports Team Travel Tracker v4.0 - {league} {current_season} Season")

        # Clear current data
        self.current_travel_data = []
        self.globe_widget.load_flight_data([])

        # Update team combo
        self.populate_team_combo()

        # Update status
        self.season_status_label.setText(f"{league} - No data loaded")
        self.connection_status.setText(f"Connected to ESPN ({league})")

        # Auto-load current week for the new league
        self.load_current_week()

        print(f"Successfully switched to {league}")


    def start_sports_monitoring(self):
        """Modified startup - ensure clean state"""
        if not self.sports_aggregator:
            self.setup_sports_system()

        if self.sports_aggregator:
            # Set league first
            self.sports_aggregator.set_league(self.current_league)

            # Populate team dropdowns
            print("🔄 Populating team dropdowns on startup...")
            self.populate_team_combo()

        # Display today's games
        self.display_todays_games_startup()

    def get_todays_games(self) -> List[Dict]:
        """Get all games scheduled for today across all leagues"""
        if not self.sports_aggregator:
            return []

        today = datetime.now().date()
        todays_games = []
        seen_games = set()  # Track seen games to prevent duplicates
        seen_teams = set()  # ✅ Track teams to prevent multiple games per team

        # Check all leagues for today's games
        for league in ["MLB", "NBA", "NHL"]:
            try:
                season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
                games = self.sports_aggregator.db.load_games(season, league)

                # Filter for today's games
                for game in games:
                    game_date = game.date.date() if hasattr(game.date, 'date') else game.date
                    if game_date == today:
                        # Create unique key to prevent duplicates
                        game_key = f"{game.home_team.team_id}_{game.away_team.team_id}_{game.date.strftime('%Y%m%d_%H%M')}"

                        # ✅ Check if either team already has a game today
                        home_key = f"{league}_{game.home_team.team_id}"
                        away_key = f"{league}_{game.away_team.team_id}"

                        if game_key not in seen_games and home_key not in seen_teams and away_key not in seen_teams:
                            seen_games.add(game_key)
                            seen_teams.add(home_key)
                            seen_teams.add(away_key)

                            todays_games.append({
                                'game': game,
                                'league': league,
                                'home_team': game.home_team,
                                'away_team': game.away_team,
                                'venue_city': game.venue.city
                            })
                            print(f"✅ Added game: {game.away_team.abbreviation} @ {game.home_team.abbreviation}")
                        else:
                            print(f"🔄 Skipped: {game.away_team.abbreviation} @ {game.home_team.abbreviation} (team already playing)")
            except Exception as e:
                print(f"Error loading {league} games for today: {e}")
                continue

        print(f"📊 Total unique games today: {len(todays_games)}")
        return todays_games

    def display_todays_games_startup(self):
        """Display today's games as stacked spinning boxes - new startup view"""
        todays_games = self.get_todays_games()

        if not todays_games:
            print("No games today - displaying empty globe")
            self.globe_widget.team_city_markers = []
            return

        stacked_markers = []

        # Group games by venue city to handle multiple games in same city
        games_by_city = {}
        for game_info in todays_games:
            venue_city = game_info['venue_city']
            if venue_city not in games_by_city:
                games_by_city[venue_city] = []
            games_by_city[venue_city].append(game_info)

        for venue_city, city_games in games_by_city.items():
            # Get base coordinates for the city
            coords = self.globe_widget.get_city_coordinates(venue_city)
            if not coords:
                continue

            lat, lon = coords[0], coords[1]

            # For multiple games in same city, spread them out slightly
            for i, game_info in enumerate(city_games):
                home_team = game_info['home_team']
                away_team = game_info['away_team']
                league = game_info['league']  # ✅ Extract league from game_info

                if len(city_games) > 1:
                    angle = (2 * 3.14159 * i)/ len(city_games) # postion markers in circle
                    radius = 0.8 + (len(city_games)* 0.2)
                    lat_offset = radius * math.cos(angle)
                    lon_offset = radius * math.sin(angle)
                else:
                    lat_offset = 0
                    lon_offset = 0

                adjusted_lat = lat + lat_offset
                adjusted_lon = lon + lon_offset

                # Single cube position (no stacking needed)
                cube_3d = self.globe_widget.lat_lon_to_3d(adjusted_lat, adjusted_lon, 1.05)

                # ✅ FIXED: Single split cube marker for both teams
                split_cube_marker = {
                    'position': cube_3d,  # Use single cube position
                    'team_id': home_team.team_id,  # Primary team for fallback
                    'league': league,
                    'size': 1.5,  # Slightly larger for split cube
                    'type': 'split_cube_today',
                    'city_name': venue_city,
                    'game_info': game_info,
                    # Split cube specific data
                    'home_team_id': home_team.team_id,
                    'away_team_id': away_team.team_id,
                    'is_split_cube': True
                }

                stacked_markers.append(split_cube_marker)

        # Set the markers on the globe widget
        self.globe_widget.team_city_markers = stacked_markers

        # Update status
        game_count = len(todays_games)
        leagues_today = set(g['league'] for g in todays_games)
        leagues_str = ", ".join(sorted(leagues_today))

        self.season_status_label.setText(f"Today: {game_count} games ({leagues_str})")
        print(f"Displaying {game_count} games today across {leagues_str}")
        print(f"Created {len(stacked_markers)} markers with league context")

        # Populate weather overlay with today's game venues
        if hasattr(self, 'weather_overlay'):
            self.update_weather_venues()  # No season = use today's games


    def synchronize_league_state(self):
        """Ensure main window and control panel are using the same league"""
        # Set control panel to match main window's league

        # Update main window components for the current league
        self.league_status_label.setText(self.current_league)
        self.update_season_combo_for_league(self.current_league)

        # Update window title
        self.setWindowTitle(f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")

    def populate_team_combo(self):
        """Populate team selection combo with teams from current league - FIXED VERSION"""
        try:
            if self.sports_aggregator and self.sports_aggregator.current_league != self.current_league:
                print(f"🐛 DEBUG - League mismatch detected! Aggregator: {self.sports_aggregator.current_league}, Window: {self.current_league}")
                self.sports_aggregator.set_league(self.current_league)

            teams = self.sports_aggregator.get_all_teams(self.current_league)
            self.all_teams = teams

            print(f"🔄 Populating main window combo with {len(teams)} {self.current_league} teams")

            # Disconnect signal to prevent triggering during population
            try:
                self.focus_team_combo.currentTextChanged.disconnect()
            except:
                pass  # Signal might not be connected yet

            # Clear and repopulate main window combo
            current_selection = self.focus_team_combo.currentData()
            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")

            # Add teams sorted by name - USE SAME DATA FORMAT AS CONTROL PANEL
            for team in sorted(teams, key=lambda t: t.display_name):
                display_text = f"{team.display_name} ({team.abbreviation})"
                # CRITICAL: Use team.team_id to match control panel
                self.focus_team_combo.addItem(display_text, team.team_id)

            # Restore selection if possible
            if current_selection:
                index = self.focus_team_combo.findData(current_selection)
                if index >= 0:
                    self.focus_team_combo.setCurrentIndex(index)

            # Reconnect the signal
            self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

            # ALSO populate the control panel's team combo with consistent data
            self.control_panel.load_teams_for_league(teams)

            print(f"✅ Populated both combo boxes with {len(teams)} {self.current_league} teams")

        except Exception as e:
            print(f"❌ Error populating team combo: {e}")
            import traceback
            traceback.print_exc()

            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")

            # Also clear control panel combo on error
            if hasattr(self.control_panel, 'team_combo'):
                self.control_panel.team_combo.clear()
                self.control_panel.team_combo.addItem("Error loading teams", "")

    def on_season_changed(self, season_text: str):
        """Handle season change"""
        if season_text and season_text != self.current_season:
            self.current_season = season_text
            self.setWindowTitle(f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")
            self.season_status_label.setText(f"{self.current_league} {season_text} - No data loaded")
            # Clear current data
            self.current_travel_data = []
            self.globe_widget.load_flight_data([])

    def load_full_season(self):
        """Load complete season schedule"""
        if not self.sports_aggregator:
            return

        season = self.season_combo.currentText()
        if not season:
            return

        self.load_full_season_btn.setEnabled(False)
        self.load_full_season_btn.setText("Loading Season...")
        self.season_status_label.setText(f"Loading {self.current_league} {season} season schedule...")

        try:
            # Force refresh if it's the current season
            force_refresh = False

            self.sports_aggregator.load_full_season_schedule(season, self.current_league, force_refresh)

        except Exception as e:
            self.on_data_error(f"Failed to load {self.current_league} {season} season: {str(e)}")
            self.load_full_season_btn.setEnabled(True)
            self.load_full_season_btn.setText("Load Full Season")

    def load_current_week(self):
        """Load current week schedule for quick preview"""
        if not self.sports_aggregator:
            return

        self.load_current_week_btn.setEnabled(False)
        self.load_current_week_btn.setText("Loading...")

        try:
            self.sports_aggregator.get_current_week_schedule(self.current_league)
        except Exception as e:
            self.on_data_error(f"Failed to load current week: {str(e)}")
            self.load_current_week_btn.setEnabled(True)
            self.load_current_week_btn.setText("Current Week")

    def on_focus_team_changed(self):
        """Handle focus team selection change from main window - FIXED"""
        team_id = self.focus_team_combo.currentData()
        season = self.season_combo.currentText()

        print(f"🔄 Main window focus team changed to: {team_id}")

        # Sync the control panel's team combo using the new programmatic method
        if hasattr(self.control_panel, 'update_team_selection_programmatically'):
            self.control_panel.update_team_selection_programmatically(team_id or "")
        else:
            # Fallback to old method if new method not available
            if hasattr(self.control_panel, 'team_combo') and self.control_panel.team_combo:
                # Temporarily disconnect signal to prevent loops
                try:
                    self.control_panel.team_combo.currentTextChanged.disconnect()
                except:
                    pass

                # Find and set the matching team in control panel
                control_panel_index = -1
                for i in range(self.control_panel.team_combo.count()):
                    if self.control_panel.team_combo.itemData(i) == team_id:
                        control_panel_index = i
                        break

                if control_panel_index >= 0:
                    self.control_panel.team_combo.setCurrentIndex(control_panel_index)
                    print(f"✅ Synced control panel to team: {team_id}")

                    # FIXED: Manually enable analyze button since signal won't fire
                    if team_id and team_id != "":
                        self.control_panel.analyze_btn.setEnabled(True)
                        self.control_panel.analyze_btn.setToolTip(f"Analyze travel for {team_id}")
                        print(f"✅ Analyze button enabled for {team_id} (main window sync)")
                else:
                    print(f"⚠️ Could not find team {team_id} in control panel combo")

                # Reconnect the signal
                self.control_panel.team_combo.currentTextChanged.connect(
                    self.control_panel.on_team_selection_changed
                )

        # Load team data if needed
        if team_id and self.sports_aggregator:
            self.sports_aggregator.load_team_season_schedule(team_id, season, self.current_league)
        elif not team_id and self.current_travel_data:
            # Show all teams again
            self.globe_widget.load_flight_data(self.current_travel_data)

        # Update overlay games
        if hasattr(self, 'games_overlay'):
            self.update_overlay_games()

    def on_animation_status_changed(self, active: bool, team_id: str):
        """Handle animation status changes"""
        if active:
            self.status_bar.showMessage(f"Animating {team_id} travel sequence...")
        else:
            self.status_bar.showMessage("Animation stopped")

    def on_animation_progress_changed(self, progress: float, segment_info: dict):
        """Handle animation progress updates"""
        if segment_info:
            current_segment = segment_info.get('departure_city', '') + " → " + segment_info.get('arrival_city', '')
            self.status_bar.showMessage(f"Animation: {progress*100:.1f}% - {current_segment}")

    def on_overlay_days_changed(self, days: int):
        """Handle days ahead change from overlay"""
        # Update games list
        self.update_overlay_games()

    def update_overlay_games(self):
        """Update the overlay games list from control panel data"""
        print(f"\n🔍 update_overlay_games called")

        if not hasattr(self, 'games_overlay') or not self.sports_aggregator:
            print("   ❌ Missing games_overlay or sports_aggregator")
            return

        # Get current team selection
        team_id = self.focus_team_combo.currentData()
        season = self.season_combo.currentText()
        days_ahead = self.games_overlay.get_days_ahead()

        print(f"   Team: {team_id}, Season: {season}, Days: {days_ahead}")

        if not team_id or not season:
            print("   ⚠️ No team or season selected, clearing overlay")
            self.games_overlay.update_games([])
            return

        # Get games from database
        db = self.sports_aggregator.db
        games = db.load_games(season, self.current_league)
        print(f"   📊 Loaded {len(games)} total games from DB")

        cutoff_date = datetime.now() + timedelta(days=days_ahead)
        print(f"   📅 Date range: {datetime.now().strftime('%Y-%m-%d')} to {cutoff_date.strftime('%Y-%m-%d')}")

        # Filter for selected team (case-insensitive comparison)
        team_id_lower = team_id.lower() if team_id else ""
        upcoming = [
            g for g in games
            if datetime.now() <= g.date <= cutoff_date
               and (g.home_team.team_id.lower() == team_id_lower or
                    g.away_team.team_id.lower() == team_id_lower)
        ]

        print(f"   🎯 Filtered to {len(upcoming)} upcoming games for {team_id}")

        # Format games for display
        game_strings = []
        for g in upcoming:
            vs = f"{g.away_team.abbreviation} @ {g.home_team.abbreviation}"
            time_str = g.date.strftime("%b %d, %I:%M%p")
            game_strings.append(f"{time_str} - {vs}")

        print(f"   ✅ Sending {len(game_strings)} formatted games to overlay")
        self.games_overlay.update_games(game_strings)

    def on_season_data_loaded(self, season: str, league: str, game_count: int):
        """Handle season data loaded successfully"""
        if league == self.current_league:  # Only update if it's for current league
            self.season_status_label.setText(
                f"{league} {season}: {game_count:,} games, {len(self.current_travel_data):,} travel records"
            )

            # Update overlay games when season data loads
            if hasattr(self, 'games_overlay'):
                self.update_overlay_games()

            # Update weather overlay with venue data
            if hasattr(self, 'weather_overlay'):
                self.update_weather_venues(season)

        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")

    def update_weather_venues(self, season: str = None):
        """Update weather overlay with unique venue data from current games"""
        print(f"🌤️ update_weather_venues called with season={season}")

        if not hasattr(self, 'weather_overlay') or not self.sports_aggregator:
            print("❌ update_weather_venues: missing weather_overlay or sports_aggregator")
            return

        venues_seen = set()
        venues = []
        cutoff_date = datetime.now() + timedelta(days=7)

        if season:
            # Load from specific season
            db = self.sports_aggregator.db
            games = db.load_games(season, self.current_league)
            upcoming = [g for g in games if datetime.now() <= g.date <= cutoff_date]
            print(f"   Found {len(upcoming)} upcoming games in season {season}")
        else:
            # Use today's games from all leagues (startup mode)
            todays_games = self.get_todays_games()
            upcoming = [g['game'] for g in todays_games]
            print(f"   Using {len(upcoming)} today's games for weather venues")

        # Extract unique venues from games
        for game in upcoming:
            venue = game.venue
            venue_id = venue.venue_id

            if venue_id in venues_seen:
                continue
            venues_seen.add(venue_id)

            # Use venue's coordinates directly if available, otherwise try globe lookup
            if venue.latitude and venue.longitude:
                venues.append({
                    'id': venue_id,
                    'name': f"{venue.name} ({venue.city})",
                    'lat': venue.latitude,
                    'lon': venue.longitude
                })
            else:
                # Fallback to globe widget's city data
                coords = self.globe_widget.get_city_coordinates(venue.city)
                if coords:
                    venues.append({
                        'id': venue_id,
                        'name': f"{venue.name} ({venue.city})",
                        'lat': coords[0],
                        'lon': coords[1]
                    })

        print(f"   Passing {len(venues)} venues to weather overlay")
        self.weather_overlay.load_venues(venues[:10])  # Limit to 10 venues

    def force_refresh_season_data(self):
        """Force refresh current season data"""
        season = self.season_combo.currentText()
        if season and self.sports_aggregator:
            self.sports_aggregator.load_full_season_schedule(season, self.current_league, force_refresh=True)

    def apply_sports_theme(self):
        """Apply sports-themed dark styling"""
        style = """
        QMainWindow {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #0A1428, stop:0.5 #051225, stop:1 #020815);
            color: #E0E0E0;
        }

        QGroupBox {
            font-weight: bold;
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 8px;
            margin-top: 1ex;
            padding-top: 15px;
            background-color: rgba(10, 25, 50, 180);
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 15px;
            padding: 0 10px 0 10px;
            color: rgba(200, 220, 255, 255);
            font-size: 12px;
        }

        QPushButton {
            background-color: rgba(0, 100, 180, 160);
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 6px;
            padding: 10px 15px;
            font-weight: bold;
            font-size: 11px;
            min-height: 25px;
            color: white;
        }

        QPushButton:hover {
            background-color: rgba(0, 120, 200, 180);
            border: 1px solid rgba(120, 170, 220, 180);
        }

        QPushButton:pressed {
            background-color: rgba(0, 80, 160, 200);
        }

        QPushButton:disabled {
            background-color: rgba(60, 60, 60, 100);
            color: rgba(150, 150, 150, 150);
            border: 1px solid rgba(80, 80, 80, 100);
        }

        QComboBox, QSpinBox {
            background-color: rgba(20, 40, 70, 200);
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 4px;
            padding: 6px 10px;
            color: white;
            font-size: 11px;
            min-height: 20px;
        }

        QComboBox::drop-down {
            border: none;
            width: 25px;
        }

        QComboBox::down-arrow {
            color: white;
            width: 12px;
            height: 12px;
        }

        QComboBox QAbstractItemView {
            background-color: rgba(15, 30, 55, 250);
            color: white;
            border: 1px solid rgba(100, 150, 200, 150);
            selection-background-color: rgba(0, 120, 200, 180);
        }

        QLabel {
            color: rgba(220, 220, 220, 255);
            font-size: 11px;
        }

        QProgressBar {
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 4px;
            text-align: center;
            font-size: 10px;
            background-color: rgba(20, 40, 70, 150);
        }

        QProgressBar::chunk {
            background-color: rgba(0, 150, 250, 200);
            border-radius: 3px;
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

        QSplitter::handle {
            background-color: rgba(100, 150, 200, 120);
            width: 3px;
        }

        QSplitter::handle:hover {
            background-color: rgba(100, 150, 200, 200);
        }
        """
        self.setStyleSheet(style)

    def setup_menu(self):
        """Setup application menu"""
        menubar = self.menuBar()

        # Data menu
        data_menu = menubar.addMenu("Data")
        animation_menu = menubar.addMenu("Animation")
        tracking_menu = menubar.addMenu("Live Tracking")
        league_menu = menubar.addMenu("League")

        # League selection actions
        mlb_action = QAction("Switch to MLB", self)
        mlb_action.triggered.connect(lambda: self.on_league_changed("MLB"))
        league_menu.addAction(mlb_action)

        nba_action = QAction("Switch to NBA", self)
        nba_action.triggered.connect(lambda: self.on_league_changed("NBA"))
        league_menu.addAction(nba_action)

        nhl_action = QAction("Switch to NHL", self)
        nhl_action.triggered.connect(lambda: self.on_league_changed("NHL"))
        league_menu.addAction(nhl_action)

        league_menu.addSeparator()

        league_stats_action = QAction("League Statistics", self)
        league_stats_action.triggered.connect(self.show_league_statistics)
        league_menu.addAction(league_stats_action)

        start_animation_action = QAction("Start Team Animation", self)
        start_animation_action.setShortcut("Ctrl+A")
        start_animation_action.triggered.connect(self.start_team_animation_dialog)
        animation_menu.addAction(start_animation_action)

        stop_animation_action = QAction("Stop Animation", self)
        stop_animation_action.setShortcut("Esc")
        stop_animation_action.triggered.connect(self.globe_widget.stop_team_animation)
        animation_menu.addAction(stop_animation_action)

        # Live tracking actions
        self.start_tracking_action = QAction("Start Live Tracking", self)
        self.start_tracking_action.setShortcut("Ctrl+T")
        self.start_tracking_action.triggered.connect(self.start_live_tracking)
        tracking_menu.addAction(self.start_tracking_action)

        self.stop_tracking_action = QAction("Stop Live Tracking", self)
        self.stop_tracking_action.setShortcut("Ctrl+Shift+T")
        self.stop_tracking_action.triggered.connect(self.stop_live_tracking)
        self.stop_tracking_action.setEnabled(False)  # Disabled until tracking starts
        tracking_menu.addAction(self.stop_tracking_action)

        tracking_menu.addSeparator()

        # Direct flight tracking (by tail number, callsign, etc.)
        track_aircraft_action = QAction("Track Aircraft...", self)
        track_aircraft_action.setShortcut("Ctrl+F")
        track_aircraft_action.triggered.connect(self.show_track_aircraft_dialog)
        tracking_menu.addAction(track_aircraft_action)

        show_watchlist_action = QAction("Show Watchlist", self)
        show_watchlist_action.triggered.connect(self.show_watchlist_dialog)
        tracking_menu.addAction(show_watchlist_action)

        tracking_menu.addSeparator()

        clear_flights_action = QAction("Clear All Flights", self)
        clear_flights_action.triggered.connect(self.globe_widget.clear_live_flights)
        tracking_menu.addAction(clear_flights_action)

        # Season data actions
        load_season_action = QAction("Load Full Season", self)
        load_season_action.setShortcut("Ctrl+L")
        load_season_action.triggered.connect(self.load_full_season)
        data_menu.addAction(load_season_action)

        refresh_action = QAction("Refresh Season Data", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.force_refresh_season_data)
        data_menu.addAction(refresh_action)

        data_menu.addSeparator()

        export_action = QAction("Export Travel Data...", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_travel_data)
        data_menu.addAction(export_action)

        data_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        data_menu.addAction(exit_action)

        # View menu
        view_menu = menubar.addMenu("View")

        paths_action = QAction("Team Travel Paths", self)
        paths_action.setCheckable(True)
        paths_action.setChecked(True)
        paths_action.triggered.connect(self.toggle_travel_paths)
        view_menu.addAction(paths_action)

        cities_action = QAction("Team Cities", self)
        cities_action.setCheckable(True)
        cities_action.setChecked(True)
        cities_action.triggered.connect(self.toggle_team_cities)
        view_menu.addAction(cities_action)

        view_menu.addSeparator()

        day_night_action = QAction("Day/Night Cycle", self)
        day_night_action.setCheckable(True)
        day_night_action.setChecked(False)
        day_night_action.triggered.connect(self.toggle_day_night)
        view_menu.addAction(day_night_action)

        view_menu.addSeparator()

        reset_view_action = QAction("Reset View", self)
        reset_view_action.setShortcut("R")
        reset_view_action.triggered.connect(self.globe_widget.reset_view)
        view_menu.addAction(reset_view_action)

        # Help menu
        help_menu = menubar.addMenu("Help")

        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def show_league_statistics(self):
        """Show statistics for all leagues"""
        try:
            stats_text = "Multi-League Statistics:\n\n"

            for league in ["MLB", "NBA", "NHL"]:
                teams = self.sports_aggregator.get_all_teams(league)
                cached_seasons = self.sports_aggregator.get_cached_seasons(league)

                stats_text += f"{league}:\n"
                stats_text += f"  Teams: {len(teams)}\n"
                stats_text += f"  Cached Seasons: {len(cached_seasons)}\n"

                if cached_seasons:
                    latest_season = cached_seasons[0]
                    stats_text += f"  Latest Season: {latest_season['season']}\n"
                    stats_text += f"  Games: {latest_season['games_count']}\n"
                    stats_text += f"  Travel Records: {latest_season['travel_count']}\n"

                stats_text += "\n"

            db_stats = self.sports_aggregator.get_database_stats()
            stats_text += f"Database:\n"
            stats_text += f"  Total Teams: {db_stats.get('teams_count', 0)}\n"
            stats_text += f"  Total Games: {db_stats.get('games_count', 0)}\n"
            stats_text += f"  Total Travel Records: {db_stats.get('travel_count', 0)}\n"
            stats_text += f"  Database Size: {db_stats.get('db_size_mb', 0):.1f} MB\n"

            QMessageBox.information(self, "League Statistics", stats_text)

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to get league statistics: {str(e)}")

    def setup_status_bar(self):
        """Setup status bar"""
        self.status_bar = self.statusBar()

        # Status widgets
        self.connection_status = QLabel("Loading...")
        self.schedule_count_label = QLabel("Games: 0")
        self.data_age_label = QLabel("Data: Never")
        self.performance_label = QLabel("FPS: --")

        # Progress bar for data loading
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)

        # Add to status bar
        self.status_bar.addWidget(self.connection_status)
        self.status_bar.addWidget(QLabel(" | "))
        self.status_bar.addWidget(self.schedule_count_label)
        self.status_bar.addWidget(QLabel(" | "))
        self.status_bar.addWidget(self.data_age_label)
        self.status_bar.addPermanentWidget(self.performance_label)
        self.status_bar.addPermanentWidget(self.progress_bar)

        self.last_update_time = None



    def on_travel_data_updated(self, travel_data: List[TeamTravelData]):
        """Handle updated travel data"""
        self.current_travel_data = travel_data
        self.last_update_time = datetime.now()

        # Update globe widget
        self.globe_widget.load_flight_data(travel_data)

        # Update control panel with travel data
        self.control_panel.update_travel_data(travel_data)

        # Update status
        self.schedule_count_label.setText(f"Travel: {len(travel_data)}")
        self.connection_status.setText(f"Connected ({self.current_league})")
        self.connection_status.setStyleSheet("color: #00FF00;")

    def start_team_animation_dialog(self):
        """Show dialog to select team for animation"""
        available_teams = self.globe_widget.get_available_teams()
        if not available_teams:
            QMessageBox.information(self, "No Teams", f"No {self.current_league} teams available for animation. Load season data first.")
            return

        team_id, ok = QInputDialog.getItem(self, "Select Team", f"Choose {self.current_league} team to animate:",
                                           available_teams, 0, False)
        if ok and team_id:
            success = self.globe_widget.start_team_animation(team_id)
            if success:
                self.status_bar.showMessage(f"Started animation for {team_id}", 3000)
            else:
                QMessageBox.warning(self, "Animation Failed", f"Could not start animation for {team_id}")

    def on_progress_updated(self, progress: int):
        """Handle progress updates"""
        if progress > 0 and progress < 100:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(progress)
            self.season_progress.setVisible(True)
            self.season_progress.setValue(progress)
        else:
            self.progress_bar.setVisible(False)
            self.season_progress.setVisible(False)

    def on_data_error(self, error_message: str):
        """Handle data errors"""
        self.connection_status.setText("Error")
        self.connection_status.setStyleSheet("color: red;")
        self.status_bar.showMessage(f"Error: {error_message}", 10000)

        # Re-enable buttons
        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")



    def on_location_selected(self, lat: float, lon: float, location_name: str):
        """Handle location selection on globe"""
        self.status_bar.showMessage(f"Location: {location_name} ({lat:.2f}, {lon:.2f})", 5000)

    def on_marker_clicked(self, marker_id: str, marker_data: dict):
        """Handle marker click on globe - show venue tooltip"""
        # Get cursor position for tooltip placement
        cursor_pos = self.globe_widget.mapToGlobal(self.globe_widget.mapFromGlobal(
            self.globe_widget.cursor().pos()
        ))

        # Show tooltip with marker info
        self.venue_tooltip.show_marker(marker_data, cursor_pos)

        # Update status bar
        city = marker_data.get('city_name', marker_data.get('city', ''))
        self.status_bar.showMessage(f"Selected: {marker_id} - {city}", 3000)

    def toggle_travel_paths(self, checked: bool):
        """Toggle travel path display"""
        current_options = (checked, True, True, True)  # paths, cities, schedule, labels
        self.globe_widget.set_display_options(*current_options)

    def toggle_team_cities(self, checked: bool):
        """Toggle team cities display"""
        current_options = (True, checked, True, True)
        self.globe_widget.set_display_options(*current_options)

    def toggle_day_night(self, checked: bool):
        """Toggle day/night cycle visualization"""
        self.globe_widget.show_day_night = checked
        self.globe_widget.update()

    def on_weather_venue_selected(self, lat: float, lon: float, venue_name: str):
        """Handle venue selection from weather overlay - center globe on venue"""
        self.globe_widget.center_on_location(lat, lon)
        self.status_bar.showMessage(f"Centered on {venue_name}", 3000)

    def on_weather_overlay_toggled(self, is_expanded: bool):
        """Reposition weather overlay when it expands/collapses"""
        if not hasattr(self, 'weather_overlay') or not hasattr(self, 'globe_widget'):
            return

        globe_width = self.globe_widget.width()
        overlay_width = self.weather_overlay.width()

        # Position so right edge aligns with globe right edge
        new_x = globe_width - overlay_width
        self.weather_overlay.move(new_x, 10)
        print(f"🌤️ Weather overlay repositioned: expanded={is_expanded}, x={new_x}, width={overlay_width}")

    def export_travel_data(self):
        """Export current travel data"""
        if not self.current_travel_data:
            QMessageBox.information(self, "No Data", f"No {self.current_league} travel data to export.")
            return

        season = self.season_combo.currentText()
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Travel Data",
            f"{self.current_league.lower()}_travel_{season}_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            "JSON Files (*.json);;CSV Files (*.csv)"
        )

        if filename:
            try:
                export_data = []
                for travel in self.current_travel_data:
                    export_data.append({
                        'league': self.current_league,
                        'team_name': travel.team_name,
                        'team_id': travel.team_id,
                        'departure_city': travel.departure_city,
                        'arrival_city': travel.arrival_city,
                        'game_date': travel.game_date.isoformat() if travel.game_date else None,
                        'travel_date': travel.travel_date.isoformat() if travel.travel_date else None,
                        'confidence': travel.confidence,
                        'season': season
                    })

                with open(filename, 'w') as f:
                    json.dump(export_data, f, indent=2)

                self.status_bar.showMessage(f"Exported {len(export_data)} {self.current_league} travel records to {filename}", 5000)

            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export data: {str(e)}")

    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(self, "Giving it my all, maximum effort",
                         "travelViz, an Effort Odds widget"
                         )

    def resizeEvent(self, event):
        """Handle window resize - reposition overlays"""
        super().resizeEvent(event)

        # Reposition weather overlay to right side of globe
        if hasattr(self, 'weather_overlay') and hasattr(self, 'globe_widget'):
            globe_width = self.globe_widget.width()
            overlay_width = self.weather_overlay.width()
            self.weather_overlay.move(globe_width - overlay_width, 10)

    def closeEvent(self, event):
        """Handle window close - cleanup resources"""
        # Stop flight tracker if running
        if self.flight_tracker and self.live_tracking_active:
            print("🛑 Stopping flight tracker on exit...")
            self.flight_tracker.stop()
            self.flight_tracker.wait(5000)  # Wait up to 5 seconds

        # Stop direct flight tracker if running
        if hasattr(self, 'direct_tracker') and self.direct_tracker and self.direct_tracker.isRunning():
            print("🛑 Stopping direct flight tracker on exit...")
            self.direct_tracker.stop()
            self.direct_tracker.wait(5000)

        # Clean up weather overlay
        if hasattr(self, 'weather_overlay'):
            self.weather_overlay.cleanup()

        event.accept()


def main():
    """Main application entry point"""
    # Create QApplication
    app = QApplication(sys.argv)

    # Set application properties
    app.setApplicationName("Sports Team Travel Tracker")
    app.setApplicationVersion("4.0")
    app.setOrganizationName("SportsTracker")
    app.setOrganizationDomain("sportstracker.dev")

    # Set application font
    font = QFont("Segoe UI", 9)
    app.setFont(font)

    # Create and show main window
    window = SportsTrackerMainWindow()
    window.show()

    # Run application
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
