from typing import List, Dict, Optional
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                            QWidget, QPushButton, QLabel, QFrame, QDateTimeEdit,
                            QCheckBox, QSlider, QGroupBox, QProgressBar, QComboBox,
                            QListWidget, QListWidgetItem, QSpinBox, QLineEdit,
                            QTextEdit, QScrollArea, QSplitter, QGridLayout)
from PyQt6.QtCore import (Qt, QTimer, QDateTime, pyqtSignal, QPropertyAnimation, QEasingCurve)
from PyQt6.QtGui import QFont, QPixmap, QIcon, QPalette, QColor


class FlightControlPanel(QWidget):
    """Bloomberg Terminal-style control panel for sports team travel intelligence"""

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
        
        self.setup_ui()
        self.apply_bloomberg_styles()
        self.connect_signals()
        self.update_ui_for_league(self.current_league)

    def setup_ui(self):
        """Setup Bloomberg Terminal-style UI"""
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

        # Travel Schedule List
        schedule_frame = self.create_schedule_section()
        layout.addWidget(schedule_frame)

        # Route Analysis Tools
        tools_frame = self.create_tools_section()
        layout.addWidget(tools_frame)

        layout.addStretch()
        self.setLayout(layout)

    def create_header_section(self) -> QFrame:
        """Create Bloomberg-style header"""
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
        """Create Amadeus travel intelligence display (REPLACES basic travel stats)"""
        group = QGroupBox("AMADEUS TRAVEL INTELLIGENCE")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)

        # Key Metrics Grid
        metrics_grid = QGridLayout()
        
        # Row 1: Complexity and Risk
        self.complexity_label = QLabel("COMPLEXITY: --")
        self.complexity_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        metrics_grid.addWidget(self.complexity_label, 0, 0)
        
        self.risk_label = QLabel("RISK LEVEL: --")
        self.risk_label.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        metrics_grid.addWidget(self.risk_label, 0, 1)

        # Row 2: Distance and Routes  
        self.distance_label = QLabel("TOTAL DISTANCE: --")
        self.distance_label.setFont(QFont("Consolas", 9))
        metrics_grid.addWidget(self.distance_label, 1, 0)
        
        self.routes_label = QLabel("ROUTES: --")
        self.routes_label.setFont(QFont("Consolas", 9))
        metrics_grid.addWidget(self.routes_label, 1, 1)

        # Row 3: Airport Performance
        self.airport_perf_label = QLabel("AIRPORT PERF: --")
        self.airport_perf_label.setFont(QFont("Consolas", 9))
        metrics_grid.addWidget(self.airport_perf_label, 2, 0)
        
        self.hotel_quality_label = QLabel("HOTEL QUALITY: --")
        self.hotel_quality_label.setFont(QFont("Consolas", 9))
        metrics_grid.addWidget(self.hotel_quality_label, 2, 1)

        layout.addLayout(metrics_grid)

        # Risk Alerts
        self.alerts_label = QLabel("ALERTS: No active alerts")
        self.alerts_label.setFont(QFont("Consolas", 8))
        self.alerts_label.setWordWrap(True)
        self.alerts_label.setStyleSheet("color: #888; background: rgba(20, 20, 20, 50); padding: 4px; border-radius: 3px;")
        layout.addWidget(self.alerts_label)

        # Route Breakdown
        self.route_breakdown = QTextEdit()
        self.route_breakdown.setFont(QFont("Consolas", 8))
        self.route_breakdown.setMaximumHeight(80)
        self.route_breakdown.setPlaceholderText("Route analysis will appear here...")
        layout.addWidget(self.route_breakdown)

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

    def create_schedule_section(self) -> QGroupBox:
        """Create upcoming schedule display"""
        group = QGroupBox("UPCOMING SCHEDULE")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)

        # Filters
        filter_layout = QHBoxLayout()
        
        days_label = QLabel("DAYS:")
        days_label.setFont(QFont("Consolas", 8))
        filter_layout.addWidget(days_label)
        
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 30)
        self.days_spin.setValue(14)
        self.days_spin.setFixedSize(50, 20)
        self.days_spin.setFont(QFont("Consolas", 8))
        filter_layout.addWidget(self.days_spin)
        
        filter_layout.addStretch()
        
        # Away games only checkbox
        self.away_only_check = QCheckBox("Away Games Only")
        self.away_only_check.setChecked(True)
        self.away_only_check.setFont(QFont("Consolas", 8))
        filter_layout.addWidget(self.away_only_check)
        
        layout.addLayout(filter_layout)

        # Schedule list
        self.schedule_list = QListWidget()
        self.schedule_list.setFont(QFont("Consolas", 8))
        self.schedule_list.setMaximumHeight(120)
        layout.addWidget(self.schedule_list)

        return group

    def create_tools_section(self) -> QGroupBox:
        """Create analysis tools"""
        group = QGroupBox("ANALYSIS TOOLS")
        group.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        layout = QVBoxLayout(group)

        # Quick analysis buttons
        tools_layout = QHBoxLayout()
        
        self.season_analysis_btn = QPushButton("SEASON ANALYSIS")
        self.season_analysis_btn.setFixedHeight(25)
        self.season_analysis_btn.setFont(QFont("Consolas", 8))
        tools_layout.addWidget(self.season_analysis_btn)
        
        self.compare_btn = QPushButton("COMPARE TEAMS")
        self.compare_btn.setFixedHeight(25)
        self.compare_btn.setFont(QFont("Consolas", 8))
        tools_layout.addWidget(self.compare_btn)
        
        layout.addLayout(tools_layout)

        # Export/Settings
        export_layout = QHBoxLayout()
        
        self.export_btn = QPushButton("EXPORT DATA")
        self.export_btn.setFixedHeight(20)
        self.export_btn.setFont(QFont("Consolas", 7))
        export_layout.addWidget(self.export_btn)
        
        self.settings_btn = QPushButton("⚙ SETTINGS")
        self.settings_btn.setFixedHeight(20)
        self.settings_btn.setFont(QFont("Consolas", 7))
        export_layout.addWidget(self.settings_btn)
        
        layout.addLayout(export_layout)

        return group

    def connect_signals(self):
        """Connect all UI signals"""
        # League buttons
        self.mlb_btn.clicked.connect(lambda: self.on_league_changed("MLB"))
        self.nba_btn.clicked.connect(lambda: self.on_league_changed("NBA"))
        self.nhl_btn.clicked.connect(lambda: self.on_league_changed("NHL"))

        # Team selection
        self.team_combo.currentTextChanged.connect(self.on_team_changed)
        
        # Analysis trigger
        self.analyze_btn.clicked.connect(self.trigger_amadeus_analysis)
        
        # Refresh
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        
        # Schedule filters
        self.days_spin.valueChanged.connect(self.update_schedule_display)
        self.away_only_check.stateChanged.connect(self.update_schedule_display)

    def apply_bloomberg_styles(self):
        """Apply Bloomberg Terminal-inspired styling"""
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
            
            QListWidget {
                background-color: #0F1419;
                border: 1px solid #2A3441;
                border-radius: 4px;
                alternate-background-color: #111922;
            }
            
            QListWidget::item {
                padding: 3px;
                border-bottom: 1px solid #1E2A3A;
            }
            
            QListWidget::item:selected {
                background-color: #FF6B00;
                color: white;
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

    def on_league_changed(self, league: str):
        """Handle league change"""
        if not self.sender().isChecked():
            self.sender().setChecked(True)
            return
        
        self.current_league = league
        
        # Update button states
        for btn in [self.mlb_btn, self.nba_btn, self.nhl_btn]:
            if btn != self.sender():
                btn.setChecked(False)
        
        self.update_ui_for_league(league)
        self.modeChanged.emit(league)

    def on_team_changed(self, team_abbr: str):
        """Handle team selection change"""
        if team_abbr:
            self.teamChanged.emit(team_abbr)

    def trigger_amadeus_analysis(self):
        """Trigger Amadeus analysis for selected team"""
        team_abbr = self.team_combo.currentData()
        if team_abbr:
            days_ahead = self.days_spin.value()
            self.amadeusAnalysisRequested.emit(team_abbr, days_ahead)
            
            # Show that analysis is starting
            self.analysis_progress.setVisible(True)
            self.analysis_progress.setValue(0)
            self.analyze_btn.setEnabled(False)
            self.status_message.setText("Starting analysis...")

    def on_analysis_progress(self, percentage: int, message: str):
        """Handle analysis progress updates"""
        self.analysis_progress.setValue(percentage)
        self.status_message.setText(message)

    def on_amadeus_complete(self, intelligence):
        """Handle completed Amadeus analysis"""
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.status_message.setText("Analysis complete")
        
        self.current_intelligence = intelligence
        self.update_intelligence_display(intelligence)

    def on_analysis_error(self, error_message: str):
        """Handle analysis errors"""
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.status_message.setText(f"Error: {error_message}")

    def update_intelligence_display(self, intelligence):
        """Update the intelligence display with Amadeus data (FIXED ROUTE FORMAT)"""
        if not intelligence:
            return
    
        # Update key metrics
        complexity = intelligence.travel_complexity_score
        self.complexity_label.setText(f"COMPLEXITY: {complexity:.1f}/100")
        
        if complexity > 75:
            self.complexity_label.setStyleSheet("color: #FF4444; font-weight: bold;")
        elif complexity > 50:
            self.complexity_label.setStyleSheet("color: #FF8800; font-weight: bold;")
        else:
            self.complexity_label.setStyleSheet("color: #00FF88; font-weight: bold;")
    
        # Risk level
        if intelligence.highest_risk_route:
            risk_confidence = intelligence.highest_risk_route.travel_confidence
            self.risk_label.setText(f"RISK LEVEL: {risk_confidence}")
            
            if risk_confidence == "LOW":
                self.risk_label.setStyleSheet("color: #FF4444; font-weight: bold;")
            elif risk_confidence == "MEDIUM":
                self.risk_label.setStyleSheet("color: #FF8800; font-weight: bold;")
            else:
                self.risk_label.setStyleSheet("color: #00FF88; font-weight: bold;")
        else:
            self.risk_label.setText("RISK LEVEL: MINIMAL")
            self.risk_label.setStyleSheet("color: #00FF88; font-weight: bold;")
    
        # Distance and routes
        total_distance = intelligence.total_travel_distance
        self.distance_label.setText(f"TOTAL DISTANCE: {total_distance:.0f} MI")
        
        route_count = len(intelligence.upcoming_routes)
        self.routes_label.setText(f"ROUTES: {route_count}")
    
        # Airport Performance (average of all routes)
        if intelligence.upcoming_routes:
            avg_airport_perf = sum(
                r.primary_airport.on_time_probability for r in intelligence.upcoming_routes 
                if r.primary_airport
            ) / len(intelligence.upcoming_routes)
            self.airport_perf_label.setText(f"AIRPORT PERF: {avg_airport_perf*100:.0f}%")
            
            # Hotel Quality (average of best hotels per route)
            avg_hotel_quality = sum(
                max(h.overall_rating for h in r.destination_hotels) if r.destination_hotels else 75
                for r in intelligence.upcoming_routes
            ) / len(intelligence.upcoming_routes)
            self.hotel_quality_label.setText(f"HOTEL QUALITY: {avg_hotel_quality:.0f}%")
        else:
            self.airport_perf_label.setText("AIRPORT PERF: --")
            self.hotel_quality_label.setText("HOTEL QUALITY: --")
    
        # *** FIXED: Route Breakdown with proper departure → arrival airport codes ***
        route_breakdown_text = ""
        for i, route in enumerate(intelligence.upcoming_routes, 1):
            try:
                # Get airport codes from linked travel data
                if route.travel_data:
                    departure_airport = route.travel_data.departure_airport or "UNK"
                    arrival_airport = route.travel_data.arrival_airport or "UNK"
                    game_date = route.travel_data.game_date.strftime("%m/%d") if route.travel_data.game_date else "TBD"
                    opponent = route.travel_data.opponent or "UNK"
                else:
                    # Fallback to RouteInsights data
                    departure_airport = "UNK"
                    arrival_airport = route.primary_airport.iata_code if route.primary_airport else "UNK"
                    game_date = route.game_data.date.strftime("%m/%d") if route.game_data.date else "TBD"
                    
                    # Determine opponent from game data
                    if route.game_data.home_team.team_id == intelligence.team_info.team_id:
                        opponent = route.game_data.away_team.abbreviation
                    else:
                        opponent = route.game_data.home_team.abbreviation
                
                confidence = route.travel_confidence
                distance = route.travel_distance
                
                # *** PERFECT FORMAT: departure_airport → arrival_airport vs opponent [CONFIDENCE] DATE (distance) ***
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
        # This would be connected to your data aggregator to populate teams
        pass

    def update_schedule_display(self):
        """Update the schedule display based on current filters"""
        # This would show upcoming games for the selected team
        pass

    def load_teams_for_league(self, teams: List):
        """Load teams into the combo box"""
        self.team_combo.clear()
        for team in teams:
            self.team_combo.addItem(f"{team.abbreviation} - {team.display_name}", team.team_id)

    def set_status(self, status: str, color: str = "#888"):
        """Set status message with color"""
        self.status_message.setText(status)
        self.status_message.setStyleSheet(f"color: {color};")

    def set_live_indicator(self, is_live: bool):
        """Update live indicator"""
        if is_live:
            self.live_indicator.setText("● LIVE")
            self.live_indicator.setStyleSheet("color: #00FF88; font-weight: bold;")
        else:
            self.live_indicator.setText("● OFFLINE")
            self.live_indicator.setStyleSheet("color: #FF4444; font-weight: bold;")
