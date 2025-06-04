import sys
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                            QWidget, QSplitter, QStatusBar, QMenuBar, QMenu, 
                            QMessageBox, QProgressBar, QLabel, QFileDialog)
from PyQt6.QtCore import (Qt, QTimer, QThread, QObject, pyqtSignal, QSettings)
from PyQt6.QtGui import QAction, QIcon, QFont

# Import our enhanced components
from data_client import SportsDataAggregator, TeamTravelData
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
            "amadeus": "",
            "amadeus_secret": ""
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
        """Check if APIs are configured - ESPN is always available"""
        return True  # ESPN API is free and requires no authentication


class SportsTrackerMainWindow(QMainWindow):
    """Main window for the sports team travel tracker application"""
    
    def __init__(self):
        super().__init__()
        
        # Load configuration
        self.config_loader = ConfigLoader()
        
        # Initialize components
        self.sports_aggregator = None
        self.data_update_timer = None
        self.current_travel_data = []
        
        # Setup UI
        self.setup_ui()
        self.setup_menu()
        self.setup_status_bar()
        self.setup_sports_system()
        self.connect_signals()
        
        # Load settings
        self.settings = QSettings("SportsTracker", "TeamTravel")
        self.load_window_settings()
        
        # Start with demo data, then load real data
        self.load_demo_data()
        self.start_sports_monitoring()
    
    def setup_ui(self):
        """Setup the main window UI"""
        self.setWindowTitle("Sports Team Travel Tracker - Real-time Team Movement Analysis")
        self.setGeometry(100, 100, 1800, 1000)
        
        # Central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # Create splitter for resizable panels
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)
        
        # Control panel (left side)
        self.control_panel = FlightControlPanel()
        self.control_panel.setMinimumWidth(350)
        self.control_panel.setMaximumWidth(500)
        splitter.addWidget(self.control_panel)
        
        # Globe widget (right side)
        self.globe_widget = FlightGlobeWidget()
        splitter.addWidget(self.globe_widget)
        
        # Set splitter proportions (30% control panel, 70% globe)
        splitter.setSizes([400, 1400])
        
        # Apply dark sports theme
        self.apply_sports_theme()
    
    def apply_sports_theme(self):
        """Apply sports-themed dark styling"""
        style = """
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
        
        QMenu {
            background-color: rgba(15, 30, 55, 240);
            color: #E0E0E0;
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 6px;
        }
        
        QMenu::item {
            padding: 8px 20px;
            border-radius: 4px;
        }
        
        QMenu::item:selected {
            background-color: rgba(0, 120, 200, 150);
        }
        
        QStatusBar {
            background-color: rgba(10, 25, 50, 200);
            color: #B0B0B0;
            border-top: 1px solid rgba(100, 150, 200, 100);
            font-size: 11px;
        }
        
        QSplitter::handle {
            background-color: rgba(100, 150, 200, 100);
            width: 2px;
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
        
        # Refresh actions
        refresh_action = QAction("Refresh Schedule Data", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.force_refresh_schedule_data)
        data_menu.addAction(refresh_action)
        
        export_action = QAction("Export Travel Data...", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_travel_data)
        data_menu.addAction(export_action)
        
        data_menu.addSeparator()
        
        # League selection
        mlb_action = QAction("Load MLB Schedule", self)
        mlb_action.triggered.connect(lambda: self.load_league_schedule("MLB"))
        data_menu.addAction(mlb_action)
        
        nfl_action = QAction("Load NFL Schedule", self)
        nfl_action.triggered.connect(lambda: self.load_league_schedule("NFL"))
        data_menu.addAction(nfl_action)
        
        nba_action = QAction("Load NBA Schedule", self)
        nba_action.triggered.connect(lambda: self.load_league_schedule("NBA"))
        data_menu.addAction(nba_action)
        
        data_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        data_menu.addAction(exit_action)
        
        # View menu
        view_menu = menubar.addMenu("View")
        
        # Display options
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
        
        schedule_action = QAction("Current Schedule", self)
        schedule_action.setCheckable(True)
        schedule_action.setChecked(True)
        schedule_action.triggered.connect(self.toggle_schedule_display)
        view_menu.addAction(schedule_action)
        
        view_menu.addSeparator()
        
        debug_action = QAction("Debug Markers", self)
        view_menu.addAction(debug_action)
        
        reset_view_action = QAction("Reset View", self)
        reset_view_action.setShortcut("R")
        reset_view_action.triggered.connect(self.globe_widget.reset_view)
        view_menu.addAction(reset_view_action)
        
        # Help menu
        help_menu = menubar.addMenu("Help")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
    
    def setup_status_bar(self):
        """Setup status bar"""
        self.status_bar = self.statusBar()
        
        # Status widgets
        self.connection_status = QLabel("Loading...")
        self.schedule_count_label = QLabel("Games: 0")
        self.data_age_label = QLabel("Last Update: Never")
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
        
        # Update timer for data age
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(30000)  # Update every 30 seconds
        
        self.last_update_time = None
    
    def setup_sports_system(self):
        """Setup sports data system"""
        api_keys = self.config_loader.get_api_keys()
        
        # Initialize sports data aggregator
        self.sports_aggregator = SportsDataAggregator(api_keys)
        
        # Use QTimer for periodic updates
        self.data_update_timer = QTimer()
        self.data_update_timer.timeout.connect(self.update_schedule_data_periodic)
        
        # Connect aggregator signals
        self.sports_aggregator.dataUpdated.connect(self.on_travel_data_updated)
        self.sports_aggregator.progressUpdated.connect(self.on_progress_updated)
        self.sports_aggregator.errorOccurred.connect(self.on_data_error)
    
    def connect_signals(self):
        """Connect UI signals"""
        # Control panel signals
        self.control_panel.refreshRequested.connect(self.force_refresh_schedule_data)
        self.control_panel.modeChanged.connect(self.on_mode_changed)
        self.control_panel.airlineFilterChanged.connect(self.on_team_filter_changed)
        self.control_panel.statusFilterChanged.connect(self.on_schedule_filter_changed)
        self.control_panel.routeFilterChanged.connect(self.on_route_filter_changed)
        self.control_panel.flightSelected.connect(self.on_game_selected)
        
        # Globe widget signals
        self.globe_widget.performanceUpdate.connect(self.on_performance_update)
        self.globe_widget.locationSelected.connect(self.on_location_selected)
    
    def start_sports_monitoring(self):
        """Start sports data monitoring"""
        if self.sports_aggregator:
            # Start with MLB by default
            self.load_league_schedule("MLB")
            # Update every hour
            self.data_update_timer.start(3600000)
            self.connection_status.setText("Connecting to ESPN...")
            self.connection_status.setStyleSheet("color: orange;")
    
    def load_league_schedule(self, league: str):
        """Load schedule for specified league"""
        if not self.sports_aggregator:
            return
            
        self.on_progress_updated(10)
        self.connection_status.setText(f"Loading {league} schedule...")
        
        try:
            # Load current week's schedule
            self.sports_aggregator.load_league_schedule(league)
        except Exception as e:
            self.on_data_error(f"Failed to load {league} schedule: {str(e)}")
    
    
    def force_refresh_schedule_data(self):
        """Force refresh schedule data from ESPN API"""
        if not self.sports_aggregator:
            return
            
        self.on_progress_updated(10)
        print("Force refreshing schedule data from ESPN...")
        
        try:
            # Refresh current league data
            self.sports_aggregator.refresh_current_data()
            self.on_progress_updated(100)
            
        except Exception as e:
            self.on_data_error(f"Refresh failed: {str(e)}")
            self.on_progress_updated(0)
    
    def update_schedule_data_periodic(self):
        """Periodic update of schedule data"""
        if self.sports_aggregator:
            self.sports_aggregator.update_data()
    
    def load_demo_data(self):
        """Load demo travel data for demonstration"""
        demo_travel_data = [
            TeamTravelData(
                team_name="Los Angeles Dodgers",
                team_id="LAD",
                departure_city="Los Angeles",
                arrival_city="New York", 
                game_date=datetime.now() + timedelta(days=2),
                travel_date=datetime.now() + timedelta(days=1),
                departure_airport="LAX",
                arrival_airport="JFK",
                confidence="demo"
            ),
            TeamTravelData(
                team_name="New York Yankees",
                team_id="NYY", 
                departure_city="New York",
                arrival_city="Boston",
                game_date=datetime.now() + timedelta(days=3),
                travel_date=datetime.now() + timedelta(days=2),
                departure_airport="JFK",
                arrival_airport="BOS",
                confidence="demo"
            ),
            TeamTravelData(
                team_name="Boston Red Sox",
                team_id="BOS",
                departure_city="Boston", 
                arrival_city="Chicago",
                game_date=datetime.now() + timedelta(days=5),
                travel_date=datetime.now() + timedelta(days=4),
                departure_airport="BOS",
                arrival_airport="ORD",
                confidence="demo"
            )
        ]
        
        # Load demo data into the interface
        self.on_travel_data_updated(demo_travel_data)
        self.connection_status.setText("Demo Mode")
        self.connection_status.setStyleSheet("color: orange;")
        
        
    
    def on_travel_data_updated(self, travel_data: List[TeamTravelData]):
        """Handle updated travel data"""
        self.current_travel_data = travel_data
        self.last_update_time = datetime.now()
        
        # Update control panel
        self.control_panel.update_flight_data(travel_data)
        
        # Update globe widget
        self.globe_widget.load_flight_data(travel_data)
        
        # Update status
        self.schedule_count_label.setText(f"Games: {len(travel_data)}")
        self.connection_status.setText("Connected to ESPN")
        self.connection_status.setStyleSheet("color: #00FF00;")
        self.control_panel.set_connection_status(True)
    
    def on_progress_updated(self, progress: int):
        """Handle progress updates"""
        if progress > 0 and progress < 100:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(progress)
            self.control_panel.set_loading_progress(progress)
        else:
            self.progress_bar.setVisible(False)
            self.control_panel.set_loading_progress(0)
    
    def on_data_error(self, error_message: str):
        """Handle data errors"""
        self.connection_status.setText("Error")
        self.connection_status.setStyleSheet("color: red;")
        self.control_panel.set_connection_status(False)
        
        # Show error message in status bar
        self.status_bar.showMessage(f"Error: {error_message}", 10000)
    
    def on_performance_update(self, fps: float):
        """Handle performance updates"""
        self.performance_label.setText(f"FPS: {fps:.1f}")
        self.control_panel.update_fps(fps)
    
    def on_mode_changed(self, mode: str):
        """Handle mode changes"""
        if mode == "historical":
            # Could implement historical schedule loading here
            pass
    
    def on_team_filter_changed(self, teams: List[str]):
        """Handle team filter changes"""
        if teams and self.sports_aggregator:
            filtered_travel = []
            for team in teams:
                filtered_travel.extend(self.sports_aggregator.get_travel_by_team(team))
            self.globe_widget.filter_flights(filtered_travel)
    
    def on_schedule_filter_changed(self, statuses: List[str]):
        """Handle schedule filter changes"""
        if self.current_travel_data:
            filtered_travel = [t for t in self.current_travel_data 
                              if t.confidence in statuses]
            self.globe_widget.filter_flights(filtered_travel)
    
    def on_route_filter_changed(self, departure: str, arrival: str):
        """Handle route filter changes"""
        if self.sports_aggregator:
            route_travel = self.sports_aggregator.get_travel_by_route(departure, arrival)
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
    
    def toggle_schedule_display(self, checked: bool):
        """Toggle schedule display"""
        current_options = (True, True, checked, True)
        self.globe_widget.set_display_options(*current_options)
    
    def export_travel_data(self):
        """Export current travel data"""
        if not self.current_travel_data:
            QMessageBox.information(self, "No Data", "No travel data to export.")
            return
        
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Travel Data", 
            f"team_travel_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            "JSON Files (*.json);;CSV Files (*.csv)"
        )
        
        if filename:
            try:
                export_data = []
                for travel in self.current_travel_data:
                    export_data.append({
                        'team_name': travel.team_name,
                        'team_id': travel.team_id,
                        'departure_city': travel.departure_city,
                        'arrival_city': travel.arrival_city,
                        'game_date': travel.game_date.isoformat() if travel.game_date else None,
                        'travel_date': travel.travel_date.isoformat() if travel.travel_date else None,
                        'confidence': travel.confidence
                    })
                
                with open(filename, 'w') as f:
                    json.dump(export_data, f, indent=2)
                
                self.status_bar.showMessage(f"Exported {len(export_data)} travel records to {filename}", 5000)
                
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export data: {str(e)}")
    
    def update_status(self):
        """Update status bar information"""
        if self.last_update_time:
            age = datetime.now() - self.last_update_time
            minutes = int(age.total_seconds() / 60)
            if minutes < 1:
                age_text = "Just now"
            elif minutes < 60:
                age_text = f"{minutes}m ago"
            else:
                hours = minutes // 60
                age_text = f"{hours}h ago"
            
            self.data_age_label.setText(f"Last Update: {age_text}")
    
    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(self, "About Sports Team Travel Tracker", 
                         "Sports Team Travel Tracker v3.0\n\n"
                         "Real-time sports team movement visualization\n\n"
                         "Features:\n"
                         "• Live team schedules from ESPN API\n"
                         "• Interactive 3D globe with travel paths\n"
                         "• Team movement inference from game schedules\n"
                         "• Multiple league support (MLB, NFL, NBA, NHL)\n"
                         "• Team city highlighting and route visualization\n\n"
                         "Built with PyQt6, OpenGL, and ESPN API")
    
    def load_window_settings(self):
        """Load window settings"""
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)
    
    def save_window_settings(self):
        """Save window settings"""
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
    
    def closeEvent(self, event):
        """Handle application close"""
        # Stop data monitoring
        if self.data_update_timer:
            self.data_update_timer.stop()
        
        # Save settings
        self.save_window_settings()
        
        # Accept close event
        event.accept()
    
    
    def setup_debug_menu(self):
        """Add debug menu for marker testing"""
        # Add to your existing menu setup
        debug_menu = self.menuBar().addMenu("Debug")
        
        # Marker debug actions
        debug_markers_action = QAction("Debug Marker Pipeline", self)
        debug_markers_action.triggered.connect(self.debug_marker_visibility)
        debug_menu.addAction(debug_markers_action)
        
        create_test_marker_action = QAction("Create Test Marker", self)
        create_test_marker_action.triggered.connect(self.create_test_marker)
        debug_menu.addAction(create_test_marker_action)
        
        massive_marker_action = QAction("Create MASSIVE Marker", self)
        massive_marker_action.triggered.connect(self.create_massive_marker)
        debug_menu.addAction(massive_marker_action)


    




def main():
    """Main application entry point"""
    # Create QApplication
    app = QApplication(sys.argv)
    
    # Set application properties
    app.setApplicationName("Sports Team Travel Tracker")
    app.setApplicationVersion("3.0")
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
