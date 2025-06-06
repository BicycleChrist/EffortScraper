import sys
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                             QWidget, QSplitter, QStatusBar, QMenuBar, QMenu,
                             QMessageBox, QProgressBar, QLabel, QFileDialog,
                             QComboBox, QSpinBox, QCheckBox, QGroupBox, QPushButton, QInputDialog)
from PyQt6.QtCore import (Qt, QTimer, QThread, QObject, pyqtSignal, QSettings)
from PyQt6.QtGui import QAction, QIcon, QFont

# Import components
from data_client import ESPNSportsDataAggregator, TeamTravelData, TeamInfo
from flight_tracker_panel import FlightControlPanel
from globe_widget import FlightGlobeWidget


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
            "espn_scraping_note": "ESPN scraping requires no API key",
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
        self.current_league = "MLB"  # Default league
        self.current_season = str(datetime.now().year)
        
        # Setup UI
        self.setup_ui()
        self.setup_menu()
        self.setup_status_bar()
        self.setup_sports_system()
        self.connect_signals()
        
        # Sync dropdown and Default league FUCKING H
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
        """Setup sports data system with ESPN scraping"""
        api_keys = self.config_loader.get_api_keys()
        
        # Initialize sports data aggregator with config dict
        self.sports_aggregator = ESPNSportsDataAggregator(api_keys)
        
        # Sync drop down to League button select
        self.sports_aggregator.set_league(self.current_league)
        
        # Connect aggregator signals
        self.sports_aggregator.dataUpdated.connect(self.on_travel_data_updated)
        self.sports_aggregator.progressUpdated.connect(self.on_progress_updated)
        self.sports_aggregator.errorOccurred.connect(self.on_data_error)
        self.sports_aggregator.seasonDataLoaded.connect(self.on_season_data_loaded)
    
    def connect_signals(self):
        """Connect UI signals"""
        # Season controls
        self.season_combo.currentTextChanged.connect(self.on_season_changed)
        self.load_full_season_btn.clicked.connect(self.load_full_season)
        self.load_current_week_btn.clicked.connect(self.load_current_week)
        self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)
        
        # Control panel signals
        self.control_panel.refreshRequested.connect(self.force_refresh_season_data)
        self.control_panel.modeChanged.connect(self.on_league_changed)  # Updated connection
        self.control_panel.airlineFilterChanged.connect(self.on_team_filter_changed)
        self.control_panel.statusFilterChanged.connect(self.on_schedule_filter_changed)
        self.control_panel.routeFilterChanged.connect(self.on_route_filter_changed)
        self.control_panel.flightSelected.connect(self.on_game_selected)
        
        # Globe widget signals
        self.globe_widget.performanceUpdate.connect(self.on_performance_update)
        self.globe_widget.locationSelected.connect(self.on_location_selected)
        
        # Animation Signals
        self.globe_widget.animationStatusChanged.connect(self.on_animation_status_changed)
        self.globe_widget.animationProgressChanged.connect(self.on_animation_progress_changed)
    
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
        """Modified startup - display today's games instead of demo mode"""
        if not self.sports_aggregator:
            self.setup_sports_system()
        
        if self.sports_aggregator:
            self.sports_aggregator.set_league(self.current_league)
        
        # Populate dropdowns first
        self.populate_team_combo()
        
        # Load today's games as the new default startup view
        self.display_todays_games_startup()
        
        # Logic for refresh if needed
        #if not self.data_update_timer:
        #    self.data_update_timer = QTimer()
        #    self.data_update_timer.timeout.connect(self.display_todays_games_startup)
        #    self.data_update_timer.start(300000)  # Refresh every 5 minutes
    
    
    
    
    def get_todays_games(self) -> List[Dict]:
        """Get all games scheduled for today across all leagues"""
        if not self.sports_aggregator:
            return []
        
        today = datetime.now().date()
        todays_games = []
        
        # Check all leagues for today's games
        for league in ["MLB", "NBA", "NHL"]:
            try:
                season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
                games = self.sports_aggregator.db.load_games(season, league)
                
                # Filter for today's games
                for game in games:
                    game_date = game.date.date() if hasattr(game.date, 'date') else game.date
                    if game_date == today:
                        todays_games.append({
                            'game': game,
                            'league': league,
                            'home_team': game.home_team,
                            'away_team': game.away_team,
                            'venue_city': game.venue.city
                        })
            except Exception as e:
                print(f"Error loading {league} games for today: {e}")
                continue
        
        return todays_games
    
    
    # Properly stacked the two team cubes one of top of the other
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
                
                # Add small angular offset for multiple games in same city
                lat_offset = (i * 0.2)  # 0.2 degrees per game
                lon_offset = (i * 0.2)
                
                adjusted_lat = lat + lat_offset
                adjusted_lon = lon + lon_offset
                
                # Home team marker (on surface) - smaller size
                home_3d = self.globe_widget.lat_lon_to_3d(adjusted_lat, adjusted_lon, 1.01)
                
                # Calculate surface normal for proper vertical stacking
                surface_normal = self.calculate_surface_normal(adjusted_lat, adjusted_lon)
                stack_offset = 0.04  # Reduced stacking distance
                
                # Away team marker (stacked above home team along surface normal)
                away_3d = (
                    home_3d[0] + surface_normal[0] * stack_offset,
                    home_3d[1] + surface_normal[1] * stack_offset,
                    home_3d[2] + surface_normal[2] * stack_offset
                )
                
                # Home team marker (bottom) - much smaller cubes
                home_marker = {
                    'position': home_3d,
                    'team_id': home_team.team_id,
                    'size': 3.0,  # Reduced from 4.5
                    'type': 'home_today',
                    'city_name': venue_city,
                    'game_info': game_info
                }
                
                # Away team marker (stacked on top)
                away_marker = {
                    'position': away_3d,
                    'team_id': away_team.team_id,
                    'size': 3.0,  # Reduced from 4.5
                    'type': 'away_today',
                    'city_name': venue_city,
                    'game_info': game_info
                }
                
                stacked_markers.extend([home_marker, away_marker])
        
        # Set the markers on the globe widget
        self.globe_widget.team_city_markers = stacked_markers
        
        # Update status
        game_count = len(todays_games)
        leagues_today = set(g['league'] for g in todays_games)
        leagues_str = ", ".join(sorted(leagues_today))
        
        self.season_status_label.setText(f"Today: {game_count} games ({leagues_str})")
        print(f"Displaying {game_count} games today across {leagues_str}")
    
    def calculate_surface_normal(self, lat: float, lon: float):
        """Calculate the surface normal vector at a given lat/lon for proper stacking"""
        import math
        
        # Convert to radians
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        
        # Surface normal on a sphere points radially outward
        x = math.cos(lat_rad) * math.cos(lon_rad)
        y = math.sin(lat_rad)
        z = -math.cos(lat_rad) * math.sin(lon_rad)
        
        # Normalize the vector (should already be unit length, but ensure it)
        length = math.sqrt(x*x + y*y + z*z)
        if length > 0:
            x /= length
            y /= length
            z /= length
        
        return (x, y, z)

    
    def synchronize_league_state(self):
        """Ensure main window and control panel are using the same league"""
        # Set control panel to match main window's league
        self.control_panel.set_current_league(self.current_league)
        
        # Update main window components for the current league
        self.league_status_label.setText(self.current_league)
        self.update_season_combo_for_league(self.current_league)
        
        # Update window title
        self.setWindowTitle(f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")
    
    
    def populate_team_combo(self):
        """Populate team selection combo with teams from current league"""
        try:
            
            if self.sports_aggregator and self.sports_aggregator.current_league != self.current_league:
                print(f"🐛 DEBUG - League mismatch detected! Aggregator: {self.sports_aggregator.current_league}, Window: {self.current_league}")
                self.sports_aggregator.set_league(self.current_league)
        
            teams = self.sports_aggregator.get_all_teams(self.current_league)
            self.all_teams = teams
            
            # Clear and repopulate
            current_selection = self.focus_team_combo.currentData()
            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")
            
            # Add teams sorted by name
            for team in sorted(teams, key=lambda t: t.display_name):
                display_text = f"{team.display_name} ({team.abbreviation})"
                self.focus_team_combo.addItem(display_text, team.team_id)
            
            # Restore selection if possible
            if current_selection:
                index = self.focus_team_combo.findData(current_selection)
                if index >= 0:
                    self.focus_team_combo.setCurrentIndex(index)
            
            print(f"Populated team combo with {len(teams)} {self.current_league} teams")
            
        except Exception as e:
            print(f"Error populating team combo: {e}")
            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")
    

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
            current_season = self.sports_aggregator.espn_scraper.get_current_season_for_league(self.current_league)
            force_refresh = (season == current_season)
            
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
        """Handle focus team selection change"""
        team_id = self.focus_team_combo.currentData()
        season = self.season_combo.currentText()
        
        if team_id and self.sports_aggregator:
            # Load specific team schedule
            self.sports_aggregator.load_team_season_schedule(team_id, season, self.current_league)
        elif not team_id and self.current_travel_data:
            # Show all teams again
            self.globe_widget.load_flight_data(self.current_travel_data)
    
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
    
    def on_season_data_loaded(self, season: str, league: str, game_count: int):
        """Handle season data loaded successfully"""
        if league == self.current_league:  # Only update if it's for current league
            self.season_status_label.setText(
                f"{league} {season}: {game_count:,} games, {len(self.current_travel_data):,} travel records"
            )
        
        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")
    
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
        league_menu = menubar.addMenu("League")  # New league menu
    
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
        
        # Update control panel
        self.control_panel.update_flight_data(travel_data)
        
        # Update globe widget
        self.globe_widget.load_flight_data(travel_data)
        
        # Update status
        self.schedule_count_label.setText(f"Travel: {len(travel_data)}")
        self.connection_status.setText(f"Connected ({self.current_league})")
        self.connection_status.setStyleSheet("color: #00FF00;")
        self.control_panel.set_connection_status(True)
    
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
            self.control_panel.set_loading_progress(progress)
        else:
            self.progress_bar.setVisible(False)
            self.season_progress.setVisible(False)
            self.control_panel.set_loading_progress(0)
    
    def on_data_error(self, error_message: str):
        """Handle data errors"""
        self.connection_status.setText("Error")
        self.connection_status.setStyleSheet("color: red;")
        self.control_panel.set_connection_status(False)
        self.status_bar.showMessage(f"Error: {error_message}", 10000)
        
        # Re-enable buttons
        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")
    
    
    
    def on_performance_update(self, fps: float):
        """Handle performance updates"""
        self.performance_label.setText(f"FPS: {fps:.1f}")
        self.control_panel.update_fps(fps)
    
    def on_team_filter_changed(self, teams: List[str]):
        """Handle team filter changes"""
        if teams and self.current_travel_data:
            filtered_travel = [t for t in self.current_travel_data if t.team_id in teams]
            self.globe_widget.filter_flights(filtered_travel)
        elif not teams:
            self.globe_widget.filter_flights(self.current_travel_data)
    
    def on_schedule_filter_changed(self, statuses: List[str]):
        """Handle schedule filter changes"""
        if self.current_travel_data:
            filtered_travel = [t for t in self.current_travel_data 
                              if t.confidence in statuses]
            self.globe_widget.filter_flights(filtered_travel)
    
    def on_route_filter_changed(self, departure: str, arrival: str):
        """Handle route filter changes"""
        if self.current_travel_data:
            route_travel = [t for t in self.current_travel_data
                           if t.departure_city == departure and t.arrival_city == arrival]
            self.globe_widget.filter_flights(route_travel)
    
    def on_game_selected(self, game_id: str):
        """Handle game selection"""
        self.status_bar.showMessage(f"Selected game: {game_id}", 5000)
    
    def on_location_selected(self, lat: float, lon: float, location_name: str):
        """Handle location selection on globe"""
        self.status_bar.showMessage(f"Location: {location_name} ({lat:.2f}, {lon:.2f})", 5000)
    
    def toggle_travel_paths(self, checked: bool):
        """Toggle travel path display"""
        current_options = (checked, True, True, True)  # paths, cities, schedule, labels
        self.globe_widget.set_display_options(*current_options)
    
    def toggle_team_cities(self, checked: bool):
        """Toggle team cities display"""
        current_options = (True, checked, True, True)
        self.globe_widget.set_display_options(*current_options)
    
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
    
    def save_window_settings(self):
        """Save window settings"""
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
    
    
    
    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(self, "About Sports Team Travel Tracker", 
                         "Sports Team Travel Tracker v4.0\n\n"
                         "Multi-league sports team movement visualization\n\n"
                         "Features:\n"
                         "• Full season schedule loading via ESPN scraping\n"
                         "• Multi-league support (MLB, NBA, NHL)\n"
                         "• Team management and filtering\n"
                         "• Multi-season support\n"
                         "• Team-focused schedule views\n\n"
                         "Data Sources:\n"
                         "• ESPN Schedule Pages (primary)\n"
                         "• Interactive 3D globe with travel paths\n"
                         "• Team city highlighting and route visualization\n\n"
                         "Built with PyQt6, OpenGL, and ESPN Schedule Data")


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
