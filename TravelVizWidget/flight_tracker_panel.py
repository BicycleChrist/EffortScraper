from typing import List, Dict, Optional
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                            QWidget, QPushButton, QLabel, QFrame, QDateTimeEdit,
                            QCheckBox, QSlider, QGroupBox, QProgressBar, QComboBox,
                            QListWidget, QListWidgetItem, QSpinBox, QLineEdit,
                            QTextEdit, QScrollArea, QSplitter, QGridLayout)
from PyQt6.QtCore import (Qt, QTimer, QDateTime, pyqtSignal, QPropertyAnimation, QEasingCurve)
from PyQt6.QtGui import QFont, QPixmap, QIcon, QPalette, QColor
from datetime import datetime, timedelta

from data_client import TeamTravelIntelligence
from database_manager import TeamTravelData, DatabaseManager, TeamInfo


class FlightControlPanel(QWidget):

    # Signals
    modeChanged = pyqtSignal(str)  # League changed: "MLB", "NBA", "NHL"
    teamChanged = pyqtSignal(str)  # Team selection changed
    refreshRequested = pyqtSignal()
    amadeusAnalysisRequested = pyqtSignal(str, int)  # team_abbr, days_ahead

    def __init__(self, parent=None):
        super().__init__(parent)
        self.travel_data = []
        self.filtered_travel = []
        self.current_league = "MLB"
        self.current_intelligence = None
        self.aggregator = None
        
        # Initialize UI components first
        self.mlb_btn = None
        self.nba_btn = None
        self.nhl_btn = None
        self.team_combo = None
        self.analyze_btn = None
        self.refresh_btn = None
        self.days_spin = None
        self.analysis_progress = None
        self.status_message = None
        self.live_indicator = None
        self.total_miles_value = None
        self.risk_factor_value = None
        self.optimal_routes_value = None
        self.route_breakdown = None
        self.alerts_label = None
        self.season_analysis_btn = None
        self.compare_btn = None
        self.export_btn = None
        self.settings_btn = None
        
        
        # Upcoming Schedule display
        self.upcoming_games_list = QListWidget()
        self.upcoming_days_spin = QSpinBox()
        self.upcoming_days_spin.setRange(1, 30)
        self.upcoming_days_spin.setValue(14)
        self.upcoming_days_spin.setFixedWidth(50)
        
        
        # Build UI
        self.setup_ui()
        self.apply_styles()
        self.connect_signals()
        self.update_ui_for_league(self.current_league)
        
        
        

    def setup_ui(self):
        """Setup UI"""
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Header
        header_frame = self.create_header_section()
        layout.addWidget(header_frame)

        # League and Team Selection
        selection_frame = self.create_selection_section()
        layout.addWidget(selection_frame)

        # AMADEUS TRAVEL INTELLIGENCE (replaces basic travel stats)
        intelligence_frame = self.create_intelligence_section()
        layout.addWidget(intelligence_frame)

        # Live Analysis Status
        status_frame = self.create_analysis_status_section()
        layout.addWidget(status_frame)

        # Analysis Configuration
        config_frame = self.create_analysis_config_section()
        layout.addWidget(config_frame)

        
        layout.addStretch()
        self.setLayout(layout)
        
        layout.addWidget(self.create_upcoming_games_section())

    def create_header_section(self) -> QFrame:
        # Headers
        frame = QFrame()
        frame.setFixedHeight(45)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)

        # Terminal-style title
        title_label = QLabel("SPORTS TRAVEL INTELLIGENCE")
        title_label.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #00FF88; letter-spacing: 1px;")
        layout.addWidget(title_label)

        layout.addStretch()

        # Live indicator
        self.live_indicator = QLabel("● LIVE")
        self.live_indicator.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self.live_indicator.setStyleSheet("color: #FF6B00;")
        layout.addWidget(self.live_indicator)

        # Refresh button
        self.refresh_btn = QPushButton("REFRESH")
        self.refresh_btn.setFixedSize(80, 30)
        self.refresh_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout.addWidget(self.refresh_btn)

        return frame

    def create_selection_section(self) -> QFrame:
        """Create league and team selection"""
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setSpacing(8)

        # League Selection
        league_layout = QHBoxLayout()
        league_label = QLabel("LEAGUE:")
        league_label.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        league_label.setFixedWidth(60)
        league_layout.addWidget(league_label)

        self.mlb_btn = QPushButton("MLB")
        self.nba_btn = QPushButton("NBA") 
        self.nhl_btn = QPushButton("NHL")
        
        for btn in [self.mlb_btn, self.nba_btn, self.nhl_btn]:
            btn.setCheckable(True)
            btn.setFixedSize(50, 25)
            btn.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            league_layout.addWidget(btn)
        
        self.mlb_btn.setChecked(True)
        league_layout.addStretch()
        layout.addLayout(league_layout)

        # Team Selection
        team_layout = QHBoxLayout()
        team_label = QLabel("FOCUS:")
        team_label.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        team_label.setFixedWidth(60)
        team_layout.addWidget(team_label)

        self.team_combo = QComboBox()
        self.team_combo.setFixedHeight(25)
        self.team_combo.setFont(QFont("Consolas", 9))
        team_layout.addWidget(self.team_combo)

        # Analysis trigger
        self.analyze_btn = QPushButton("ANALYZE")
        self.analyze_btn.setFixedSize(80, 25)
        self.analyze_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        team_layout.addWidget(self.analyze_btn)

        layout.addLayout(team_layout)

        return frame

    def create_intelligence_section(self) -> QGroupBox:
        """Create Amadeus intelligence display"""
        group = QGroupBox("AMADEUS TRAVEL INTELLIGENCE")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)

        # Key metrics
        metrics_layout = QGridLayout()
        
        # Total Miles
        miles_label = QLabel("TOTAL MILES:")
        miles_label.setFont(QFont("Consolas", 8))
        self.total_miles_value = QLabel("--")
        self.total_miles_value.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.total_miles_value.setStyleSheet("color: #00FF88;")
        metrics_layout.addWidget(miles_label, 0, 0)
        metrics_layout.addWidget(self.total_miles_value, 0, 1)
        
        # Risk Factor
        risk_label = QLabel("RISK FACTOR:")
        risk_label.setFont(QFont("Consolas", 8))
        self.risk_factor_value = QLabel("--")
        self.risk_factor_value.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.risk_factor_value.setStyleSheet("color: #FFA500;")
        metrics_layout.addWidget(risk_label, 0, 2)
        metrics_layout.addWidget(self.risk_factor_value, 0, 3)
        
        # Optimal Routes
        optimal_label = QLabel("OPTIMAL ROUTES:")
        optimal_label.setFont(QFont("Consolas", 8))
        self.optimal_routes_value = QLabel("--")
        self.optimal_routes_value.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self.optimal_routes_value.setStyleSheet("color: #00BFFF;")
        metrics_layout.addWidget(optimal_label, 1, 0)
        metrics_layout.addWidget(self.optimal_routes_value, 1, 1)
        
        layout.addLayout(metrics_layout)
        
        # Route breakdown
        self.route_breakdown = QTextEdit()
        self.route_breakdown.setReadOnly(True)
        self.route_breakdown.setMaximumHeight(100)
        self.route_breakdown.setFont(QFont("Consolas", 8))
        self.route_breakdown.setPlainText("No intelligence data available")
        layout.addWidget(self.route_breakdown)
        
        # Real-time alerts
        self.alerts_label = QLabel("ALERTS: No active alerts")
        self.alerts_label.setFont(QFont("Consolas", 8))
        self.alerts_label.setStyleSheet("color: #00FF88; background: rgba(20, 40, 20, 100); padding: 4px; border-radius: 3px;")
        layout.addWidget(self.alerts_label)

        return group

    def create_analysis_status_section(self) -> QFrame:
        """Create real-time analysis status"""
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setSpacing(4)

        # Progress bar
        self.analysis_progress = QProgressBar()
        self.analysis_progress.setVisible(False)
        self.analysis_progress.setFixedHeight(6)
        layout.addWidget(self.analysis_progress)

        # Status message
        self.status_message = QLabel("Ready for analysis")
        self.status_message.setFont(QFont("Consolas", 8))
        self.status_message.setStyleSheet("color: #888;")
        layout.addWidget(self.status_message)

        return frame

    def create_analysis_config_section(self) -> QGroupBox:
        """Create analysis configuration section"""
        group = QGroupBox("ANALYSIS CONFIGURATION")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)

        # Days ahead configuration
        days_layout = QHBoxLayout()
        
        days_label = QLabel("DAYS AHEAD:")
        days_label.setFont(QFont("Consolas", 8))
        days_layout.addWidget(days_label)
        
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 30)
        self.days_spin.setValue(14)
        self.days_spin.setFixedSize(50, 20)
        self.days_spin.setFont(QFont("Consolas", 8))
        days_layout.addWidget(self.days_spin)
        
        days_layout.addStretch()
        
        layout.addLayout(days_layout)

        return group

    def create_upcoming_games_section(self) -> QGroupBox:
        group = QGroupBox("UPCOMING GAMES")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)
    
        # Controls
        control_layout = QHBoxLayout()
        label = QLabel("Days Ahead:")
        label.setFont(QFont("Consolas", 8))
        control_layout.addWidget(label)
        control_layout.addWidget(self.upcoming_days_spin)
        control_layout.addStretch()
        layout.addLayout(control_layout)
    
        # Game list
        self.upcoming_games_list.setFont(QFont("Consolas", 8))
        self.upcoming_games_list.setFixedHeight(150)
        layout.addWidget(self.upcoming_games_list)
    
        # Trigger refresh on spinbox change
        self.upcoming_days_spin.valueChanged.connect(self.update_upcoming_games)
    
        return group


    def connect_signals(self):
        """Connect all UI signals"""
        # League buttons - use lambda to avoid sender() issues
        self.mlb_btn.clicked.connect(lambda checked: self.on_league_changed("MLB", self.mlb_btn, checked))
        self.nba_btn.clicked.connect(lambda checked: self.on_league_changed("NBA", self.nba_btn, checked))
        self.nhl_btn.clicked.connect(lambda checked: self.on_league_changed("NHL", self.nhl_btn, checked))
    
        # Team selection
        self.team_combo.currentTextChanged.connect(self.on_team_selection_changed)
        
        # Analysis trigger
        self.analyze_btn.clicked.connect(self.trigger_amadeus_analysis)
        
        # Refresh
        self.refresh_btn.clicked.connect(lambda: self.refreshRequested.emit())

    def apply_styles(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #0A0E1A;
                color: #E0E6ED;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            
            QFrame {
                border: 1px solid #1E2A3A;
                border-radius: 4px;
                background-color: #0F1419;
            }
            
            QPushButton {
                background-color: #1A2332;
                border: 1px solid #2A3441;
                border-radius: 3px;
                padding: 4px 8px;
                color: #E0E6ED;
                font-weight: bold;
            }
            
            QPushButton:hover {
                background-color: #243040;
                border-color: #3A4651;
            }
            
            QPushButton:checked {
                background-color: #FF6B00;
                color: white;
                border-color: #FF8533;
            }
            
            QPushButton:pressed {
                background-color: #0F1824;
            }
            
            QGroupBox {
                font-weight: bold;
                border: 2px solid #2A3441;
                border-radius: 6px;
                margin-top: 1ex;
                padding-top: 12px;
                background-color: #111922;
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 8px 0 8px;
                color: #00FF88;
                font-size: 9px;
                letter-spacing: 1px;
            }
            
            QLabel {
                background: transparent;
                border: none;
            }
            
            QComboBox {
                background-color: #1A2332;
                border: 1px solid #2A3441;
                border-radius: 3px;
                padding: 2px 6px;
                color: #E0E6ED;
            }
            
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            
            QComboBox::down-arrow {
                image: none;
                border: none;
                color: #E0E6ED;
            }
            
            QComboBox QAbstractItemView {
                background-color: #1A2332;
                color: #E0E6ED;
                border: 1px solid #2A3441;
                selection-background-color: #FF6B00;
            }
            
            QSpinBox {
                background-color: #1A2332;
                border: 1px solid #2A3441;
                border-radius: 3px;
                padding: 2px;
                color: #E0E6ED;
            }
            
            QCheckBox {
                color: #E0E6ED;
                spacing: 5px;
            }
            
            QCheckBox::indicator {
                width: 12px;
                height: 12px;
                border: 1px solid #2A3441;
                border-radius: 2px;
                background-color: #1A2332;
            }
            
            QCheckBox::indicator:checked {
                background-color: #00FF88;
            }
            
            QTextEdit {
                background-color: #0F1419;
                border: 1px solid #2A3441;
                border-radius: 4px;
                font-family: 'Consolas', monospace;
            }
            
            QProgressBar {
                border: 1px solid #2A3441;
                border-radius: 3px;
                text-align: center;
                background-color: #1A2332;
            }
            
            QProgressBar::chunk {
                background-color: #00FF88;
                border-radius: 2px;
            }
        """)

    def on_league_changed(self, league: str, button: QPushButton, checked: bool):
        """Handle league change"""
        if not checked:
            # If unchecking, re-check it (one must always be selected)
            button.setChecked(True)
            return
        
        self.current_league = league
        
        # Update button states
        for btn in [self.mlb_btn, self.nba_btn, self.nhl_btn]:
            if btn != button:
                btn.setChecked(False)
        
        self.update_ui_for_league(league)
        self.modeChanged.emit(league)

    def update_upcoming_games(self):
        """Update the list of upcoming games for selected team"""
        if not hasattr(self, 'aggregator') or not self.aggregator:
            return
    
        db: DatabaseManager = self.aggregator.db
        team_id = self.team_combo.currentData()
        season = self.aggregator.current_season
        league = self.aggregator.current_league
    
        if not team_id or not season:
            self.upcoming_games_list.clear()
            return
    
        days_ahead = self.upcoming_days_spin.value()
        cutoff_date = datetime.now() + timedelta(days=days_ahead)
    
        games = db.load_games(season, league)
        upcoming = [
            g for g in games
            if datetime.now() <= g.date <= cutoff_date
               and (g.home_team.team_id == team_id or g.away_team.team_id == team_id)
        ]
    
        # Display in list
        self.upcoming_games_list.clear()
        for g in upcoming:
            vs = f"{g.away_team.abbreviation} @ {g.home_team.abbreviation}"
            time_str = g.date.strftime("%b %d, %I:%M%p")
            item = QListWidgetItem(f"{time_str} - {vs}")
            self.upcoming_games_list.addItem(item)
    
        if not upcoming:
            self.upcoming_games_list.addItem("No upcoming games found")


    def on_team_selection_changed(self, text: str):
        """Handle team selection change - FIXED version"""
        print(f"🎯 Team selection changed, text: '{text}'")
        
        if self.team_combo.count() > 0:
            team_abbr = self.team_combo.currentData()
            print(f"🎯 Current team data: '{team_abbr}'")
            
            if team_abbr and team_abbr != "":  # Check for valid team
                print(f"✅ Valid team selected: {team_abbr}")
                
                # Emit signal for main window
                self.teamChanged.emit(team_abbr)
                
                # Update games for upcoming schedule display
                self.update_upcoming_games()
                
                # FIXED: Ensure analyze button is enabled
                self.analyze_btn.setEnabled(True)
                self.analyze_btn.setToolTip(f"Analyze travel for {team_abbr}")
                print(f"✅ Analyze button enabled for {team_abbr}")
            else:
                print("🚫 No valid team selected")
                self.analyze_btn.setEnabled(False)
                self.analyze_btn.setToolTip("Select a team to analyze")
        else:
            print("🚫 No teams available in combo box")
            self.analyze_btn.setEnabled(False)
            self.analyze_btn.setToolTip("No teams available")

    def trigger_amadeus_analysis(self):
        """Trigger Amadeus analysis for selected team - FIXED with debugging"""
        print("🚀 Analyze button clicked!")
        
        if self.team_combo.count() > 0:
            team_abbr = self.team_combo.currentData()
            print(f"🎯 Selected team for analysis: '{team_abbr}'")
            
            if team_abbr:
                days_ahead = self.days_spin.value()
                print(f"🔄 Requesting analysis for {team_abbr}, {days_ahead} days ahead")
                
                # Emit the signal
                self.amadeusAnalysisRequested.emit(team_abbr, days_ahead)
                
                # Show that analysis is starting
                self.analysis_progress.setVisible(True)
                self.analysis_progress.setValue(0)
                self.analyze_btn.setEnabled(False)
                self.analyze_btn.setText("Analyzing...")
                self.status_message.setText("Starting Amadeus analysis...")
                
                print("✅ Analysis request emitted successfully")
            else:
                print("❌ No team selected for analysis")
                self.status_message.setText("Please select a team first")
        else:
            print("❌ No teams available for analysis")
            self.status_message.setText("No teams available")


    def on_analysis_progress(self, percentage: int, message: str):
        """Handle analysis progress updates"""
        self.analysis_progress.setValue(percentage)
        self.status_message.setText(message)

    def on_analysis_complete(self, intelligence: 'TeamTravelIntelligence'):
        """Handle completed analysis results"""
        self.current_intelligence = intelligence
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.status_message.setText("Analysis complete")
        
        # Update intelligence display
        if intelligence:
            self.update_intelligence_display(intelligence)

    def on_analysis_error(self, error_message: str):
        """Handle analysis errors"""
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.status_message.setText(f"Analysis failed: {error_message}")
        self.status_message.setStyleSheet("color: #FF6666;")

    def load_teams_for_league(self, teams: List['TeamInfo']):
        """Load teams into the combo box"""
        current_selection = self.team_combo.currentData() if self.team_combo.count() > 0 else None
        
        print(f"🔄 Loading {len(teams)} teams into control panel combo")
        
        self.team_combo.clear()
        self.team_combo.addItem("Select Team", "")
        
        for team in sorted(teams, key=lambda t: t.display_name):
            display_text = f"{team.display_name} ({team.abbreviation})"
            team_data_value = team.team_id
            
            self.team_combo.addItem(display_text, team_data_value)
            print(f"   Added: {display_text} -> {team_data_value}")
        
        # Restore selection if possible
        if current_selection:
            index = self.team_combo.findData(current_selection)
            if index >= 0:
                self.team_combo.setCurrentIndex(index)
                print(f"✅ Restored selection: {current_selection}")

    def update_intelligence_display(self, intelligence: 'TeamTravelIntelligence'):
        """Update the intelligence display with Amadeus data"""
        if not intelligence:
            return
        
        # Update key metrics using correct attribute names
        self.total_miles_value.setText(f"{intelligence.total_travel_distance:,.0f}")
        # Use travel_complexity_score as risk score (0-100 scale, convert to 0-10)
        self.risk_factor_value.setText(f"{intelligence.travel_complexity_score/10:.1f}/10")
        # Calculate optimization score (inverse of complexity for simplicity)
        optimization_score = max(0.0, 100.0 - float(intelligence.travel_complexity_score)) / 100.0
        self.optimal_routes_value.setText(f"{optimization_score:.0%}")
        
        # Update route breakdown
        route_breakdown_text = ""
        for i, route in enumerate(intelligence.upcoming_routes[:5], 1):  # Show top 5 routes
            try:
                # Extract data from the correct attributes
                # Get airport codes from linked travel data if available
                if hasattr(route, 'travel_data') and route.travel_data:
                    departure_airport = route.travel_data.departure_airport or "UNK"
                    arrival_airport = route.travel_data.arrival_airport or "UNK"
                    opponent = route.travel_data.opponent or "UNK"
                    game_date = route.travel_data.game_date.strftime('%m/%d') if route.travel_data.game_date else "TBD"
                else:
                    # Fallback to route data
                    departure_airport = "UNK"  # Not available in RouteInsights
                    arrival_airport = route.primary_airport.iata_code if route.primary_airport else "UNK"
                    game_date = route.game_data.date.strftime('%m/%d') if route.game_data else "TBD"
                    # Determine opponent from game data
                    if hasattr(intelligence, 'team_info') and route.game_data:
                        if route.game_data.home_team.team_id == intelligence.team_info.team_id.lower():
                            opponent = route.game_data.away_team.abbreviation
                        else:
                            opponent = route.game_data.home_team.abbreviation
                    else:
                        opponent = "UNK"
                
                confidence = route.travel_confidence  # Correct attribute name
                distance = route.travel_distance  # Correct attribute name
                
                route_breakdown_text += f"{i}. {departure_airport} → {arrival_airport} vs {opponent} [{confidence}] {game_date} ({distance:.0f}mi)\n"
                
            except Exception as e:
                print(f"Error formatting route {i}: {e}")
                route_breakdown_text += f"{i}. Route formatting error\n"
        
        self.route_breakdown.setPlainText(route_breakdown_text.strip())
    
        # Update alerts based on risk factors
        all_risk_factors = []
        for route in intelligence.upcoming_routes:
            all_risk_factors.extend(route.risk_factors)
        
        if all_risk_factors:
            alerts_text = "ALERTS: " + "; ".join(all_risk_factors[:3])  # Show top 3 alerts
            self.alerts_label.setStyleSheet("color: #FF8800; background: rgba(40, 20, 20, 100); padding: 4px; border-radius: 3px;")
        else:
            alerts_text = "ALERTS: No active alerts"
            self.alerts_label.setStyleSheet("color: #00FF88; background: rgba(20, 40, 20, 100); padding: 4px; border-radius: 3px;")
        
        self.alerts_label.setText(alerts_text)

    def update_ui_for_league(self, league: str):
        """Update UI elements for specific league"""
        self.current_league = league
        
        # Clear team selection when league changes
        self.team_combo.clear()
        self.team_combo.addItem("Loading teams...", "")
        
        # Update the analyze button state
        self.analyze_btn.setEnabled(False)
        
        print(f"🔄 Updated UI for {league} league")

    def update_travel_data(self, travel_data: List['TeamTravelData']):
        """Update the control panel with new travel data"""
        self.travel_data = travel_data
        print(f"📊 Control panel received {len(travel_data)} travel records")
