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
    # Style constants - Improved readability
    HEADER_FONT = QFont("Segoe UI", 7, QFont.Weight.Bold)
    CONTENT_FONT = QFont("Segoe UI", 8)
    HEADER_STYLE = "color: #9CA3AF; border: none; background: transparent;"
    CONTENT_STYLE = "color: #E5E7EB; border: none; background: transparent;"
    MUTED_STYLE = "color: #6B7280; border: none; background: transparent;"
    
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
        self.days_spin = None
        self.analysis_progress = None
        self.status_message = None
        self.live_indicator = None
        self.total_miles_value = None
        self.route_breakdown = None
        self.season_analysis_btn = None
        self.compare_btn = None
        self.export_btn = None
        self.settings_btn = None
        
        
        
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

        # AMADEUS TRAVEL INTELLIGENCE (includes analysis controls and progress)
        # Gets ALL remaining space now that Upcoming Games moved to overlay
        intelligence_frame = self.create_intelligence_section()
        layout.addWidget(intelligence_frame, 1)  # Takes all available space with stretch

        self.setLayout(layout)

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
        self.live_indicator.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.live_indicator.setStyleSheet("color: #FF6B00;")
        layout.addWidget(self.live_indicator)

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

        # Analysis trigger - wider to prevent clipping
        self.analyze_btn = QPushButton("ANALYZE")
        self.analyze_btn.setFixedSize(80, 25)
        self.analyze_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        team_layout.addWidget(self.analyze_btn)

        layout.addLayout(team_layout)

        return frame

    def create_intelligence_section(self) -> QGroupBox:
        """Create Amadeus intelligence display"""
        group = QGroupBox("AMADEUS TRAVEL INTELLIGENCE")
        group.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        layout = QVBoxLayout(group)
        layout.setSpacing(2)  # Very tight spacing
        layout.setContentsMargins(6, 1, 6, 6)  # Minimal top margin to move metrics up

        # Top metrics row - moved to very top of section
        top_metrics_layout = QHBoxLayout()
        
        # Total Miles
        miles_label = QLabel("TOTAL MILES:")
        miles_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.total_miles_value = QLabel("--")
        self.total_miles_value.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self.total_miles_value.setStyleSheet("color: #10B981;")
        top_metrics_layout.addWidget(miles_label)
        top_metrics_layout.addWidget(self.total_miles_value)
        
        top_metrics_layout.addStretch()
        
        # Days config on same line
        days_label = QLabel("DAYS AHEAD:")
        days_label.setFont(QFont("Segoe UI", 8))
        top_metrics_layout.addWidget(days_label)
        
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 30)
        self.days_spin.setValue(14)
        self.days_spin.setFixedSize(45, 20)  # Wider to show both digits
        self.days_spin.setFont(QFont("Segoe UI", 8))
        top_metrics_layout.addWidget(self.days_spin)
        
        layout.addLayout(top_metrics_layout)
        
        # Progress bar - moved right after top metrics
        self.analysis_progress = QProgressBar()
        self.analysis_progress.setVisible(False)
        self.analysis_progress.setFixedHeight(4)
        self.analysis_progress.setTextVisible(False)  # Hide percentage text
        layout.addWidget(self.analysis_progress)
        
        # Route breakdown - expanded for more visibility
        self.route_breakdown = QTextEdit()
        self.route_breakdown.setReadOnly(True)
        self.route_breakdown.setMaximumHeight(100)  # Larger for better readability
        self.route_breakdown.setFont(QFont("Segoe UI", 8))  # Slightly larger font
        self.route_breakdown.setPlainText("No intelligence data available")
        layout.addWidget(self.route_breakdown)
        
        # Hotel and Flight sections - no spacing between them
        self.current_hotels_widget = self.create_hotel_section("CURRENT STAY", "")
        self.next_hotels_widget = self.create_hotel_section("NEXT DESTINATION", "")
        self.flight_info_widget = self.create_flight_info_section()
        
        # Add to layout with minimal spacing for maximum space efficiency
        layout.addWidget(self.current_hotels_widget)
        layout.addWidget(self.next_hotels_widget)
        layout.addWidget(self.flight_info_widget)

        return group


    def create_hotel_section(self, title: str, icon: str) -> QFrame:
        """Create a compact hotel display section with clean professional styling"""
        frame = QFrame()
        frame.setVisible(True)
        frame.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border: none;
                margin: 0px;
                padding: 0px;
            }
        """)
        frame.setMinimumHeight(90)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        # Simple text header - no colored bar
        header_label = QLabel(title)
        header_label.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        header_label.setStyleSheet("color: #9CA3AF; border: none; background: transparent;")
        layout.addWidget(header_label)
        
        # Container for hotel items - custom layout, not list widget
        hotels_container = QWidget()
        self.hotels_layout = QVBoxLayout(hotels_container)
        self.hotels_layout.setContentsMargins(0, 0, 0, 0)
        self.hotels_layout.setSpacing(2)  # Better spacing so items aren't jammed
        
        # Add placeholder
        placeholder = QLabel("No data available")
        placeholder.setFont(self.CONTENT_FONT)
        placeholder.setStyleSheet(self.MUTED_STYLE)
        self.hotels_layout.addWidget(placeholder)
        
        layout.addWidget(hotels_container)
        
        # Store references for updates
        frame.hotels_layout = self.hotels_layout
        frame.placeholder = placeholder
        
        return frame
    
    def create_flight_info_section(self) -> QFrame:
        """Create compact flight intelligence section with clean professional styling"""
        frame = QFrame()
        frame.setVisible(True)
        frame.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border: none;
                margin: 0px;
                padding: 0px;
            }
        """)
        frame.setMinimumHeight(90)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(3)

        # Simple text header - no colored bar
        header_label = QLabel("FLIGHT INTELLIGENCE")
        header_label.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        header_label.setStyleSheet("color: #9CA3AF; border: none; background: transparent;")
        layout.addWidget(header_label)
        
        # Container for flight items - custom layout, not list widget
        flight_container = QWidget()
        self.flight_layout = QVBoxLayout(flight_container)
        self.flight_layout.setContentsMargins(0, 0, 0, 0)
        self.flight_layout.setSpacing(5)  # More spacing to prevent text jamming
        
        # Add placeholder
        placeholder = QLabel("No flight data available")
        placeholder.setFont(self.CONTENT_FONT)
        placeholder.setStyleSheet(self.MUTED_STYLE)
        self.flight_layout.addWidget(placeholder)
        
        layout.addWidget(flight_container)
        
        # Store references
        frame.flight_layout = self.flight_layout
        frame.placeholder = placeholder
        
        return frame
    

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

    def apply_styles(self):
        self.setStyleSheet("""
            /* Clean Professional Theme */
            QWidget {
                background-color: #1E2A3A;
                color: #E0E6ED;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            
            QFrame {
                border: 1px solid #2A3441;
                border-radius: 4px;
                background-color: #0F1419;
                margin: 2px;
            }
            
            /* Hotel and Flight sections */
            QFrame[objectName="hotel_section"], QFrame[objectName="flight_section"] {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1A2634, stop: 1 #152028);
                border: 1px solid #2563EB;
                border-radius: 10px;
                padding: 8px;
            }
            
            QPushButton {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #2563EB, stop: 1 #1D4ED8);
                border: 1px solid #3B82F6;
                border-radius: 6px;
                padding: 6px 12px;
                color: #FFFFFF;
                font-weight: 600;
                font-size: 11px;
            }
            
            QPushButton:hover {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #3B82F6, stop: 1 #2563EB);
                border-color: #60A5FA;
            }
            
            QPushButton:checked {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #F59E0B, stop: 1 #D97706);
                border-color: #FBBF24;
                color: #1F2937;
            }
            
            QPushButton:pressed {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1D4ED8, stop: 1 #1E40AF);
            }
            
            QGroupBox {
                font-weight: 600;
                border: 2px solid #374151;
                border-radius: 10px;
                margin-top: 12px;
                padding-top: 16px;
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1F2937, stop: 1 #111827);
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 12px 0 12px;
                color: #10B981;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 1px;
            }
            
            QLabel {
                background: transparent;
                border: none;
                color: #E8F4FD;
            }
            
            QComboBox {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #374151, stop: 1 #1F2937);
                border: 1px solid #4B5563;
                border-radius: 6px;
                padding: 4px 8px;
                color: #E8F4FD;
                font-size: 11px;
            }
            
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            
            QComboBox::down-arrow {
                image: none;
                border: none;
                color: #9CA3AF;
            }
            
            QComboBox QAbstractItemView {
                background-color: #374151;
                color: #E8F4FD;
                border: 1px solid #4B5563;
                border-radius: 6px;
                selection-background-color: #2563EB;
            }
            
            QSpinBox {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #374151, stop: 1 #1F2937);
                border: 1px solid #4B5563;
                border-radius: 6px;
                padding: 4px;
                color: #E8F4FD;
                font-size: 11px;
            }
            
            QSpinBox::up-button, QSpinBox::down-button {
                background: transparent;
                border: none;
                width: 16px;
            }
            
            QCheckBox {
                color: #E8F4FD;
                spacing: 6px;
                font-size: 11px;
            }
            
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 2px solid #4B5563;
                border-radius: 3px;
                background-color: #1F2937;
            }
            
            QCheckBox::indicator:checked {
                background-color: #10B981;
                border-color: #059669;
            }
            
            QTextEdit {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1F2937, stop: 1 #111827);
                border: 1px solid #374151;
                border-radius: 8px;
                font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
                font-size: 10px;
                color: #D1D5DB;
                padding: 8px;
            }
            
            QProgressBar {
                border: 1px solid #374151;
                border-radius: 4px;
                text-align: center;
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1F2937, stop: 1 #111827);
                color: #E8F4FD;
                font-size: 10px;
                font-weight: 600;
            }
            
            QProgressBar::chunk {
                background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #10B981, stop: 1 #059669);
                border-radius: 3px;
            }
            
            QListWidget {
                background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #1F2937, stop: 1 #111827);
                border: 1px solid #374151;
                border-radius: 8px;
                color: #D1D5DB;
                font-size: 10px;
                padding: 4px;
            }
            
            QListWidget::item {
                padding: 4px;
                border-radius: 4px;
                margin: 1px;
            }
            
            QListWidget::item:selected {
                background-color: #2563EB;
                color: #FFFFFF;
            }
        """)
        
        # Set object names for specific styling
        if hasattr(self, 'current_hotels_widget'):
            self.current_hotels_widget.setObjectName("hotel_section")
        if hasattr(self, 'next_hotels_widget'):
            self.next_hotels_widget.setObjectName("hotel_section")
        if hasattr(self, 'flight_info_widget'):
            self.flight_info_widget.setObjectName("flight_section")

    def set_league(self, league: str):
        """Set league programmatically and update UI"""
        if league not in ["MLB", "NBA", "NHL"]:
            return

        self.current_league = league
        
        # Update button states
        self.mlb_btn.setChecked(league == "MLB")
        self.nba_btn.setChecked(league == "NBA")
        self.nhl_btn.setChecked(league == "NHL")
        
        self.update_ui_for_league(league)

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


    def on_team_selection_changed(self, text: str):
        """Handle team selection change - FIXED version"""

        if self.team_combo.count() > 0:
            team_abbr = self.team_combo.currentData()

            if team_abbr and team_abbr != "":  # Check for valid team

                # Emit signal for main window
                self.teamChanged.emit(team_abbr)

                # FIXED: Ensure analyze button is enabled
                self.analyze_btn.setEnabled(True)
                self.analyze_btn.setToolTip(f"Analyze travel for {team_abbr}")
            else:
                self.analyze_btn.setEnabled(False)
                self.analyze_btn.setToolTip("Select a team to analyze")
        else:
            self.analyze_btn.setEnabled(False)
            self.analyze_btn.setToolTip("No teams available")

    def trigger_amadeus_analysis(self):
        """Trigger Amadeus analysis for selected team - FIXED with debugging"""
        
        if self.team_combo.count() > 0:
            team_abbr = self.team_combo.currentData()
            
            if team_abbr:
                days_ahead = self.days_spin.value()
                
                # Emit the signal
                self.amadeusAnalysisRequested.emit(team_abbr, days_ahead)
                
                # Show that analysis is starting
                self.analysis_progress.setVisible(True)
                self.analysis_progress.setValue(0)
                self.analyze_btn.setEnabled(False)
                self.analyze_btn.setText("Analyzing...")
                
            else:
                self.status_message.setText("Please select a team first")
        else:
            self.status_message.setText("No teams available")


    def on_analysis_progress(self, percentage: int, message: str):
        """Handle analysis progress updates"""
        self.analysis_progress.setValue(percentage)

    def on_analysis_complete(self, intelligence: 'TeamTravelIntelligence'):
        """Handle completed analysis results"""
        self.current_intelligence = intelligence
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("ANALYZE")
        
        # Hide status message after analysis is complete
        
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

        # Disconnect signal to prevent triggering during population
        try:
            self.team_combo.currentTextChanged.disconnect()
        except:
            pass  # Signal might not be connected yet

        self.team_combo.clear()
        self.team_combo.addItem("Select Team", "")

        for team in sorted(teams, key=lambda t: t.display_name):
            display_text = f"{team.display_name} ({team.abbreviation})"
            team_data_value = team.team_id

            self.team_combo.addItem(display_text, team_data_value)

        # Restore selection if possible
        if current_selection:
            index = self.team_combo.findData(current_selection)
            if index >= 0:
                self.team_combo.setCurrentIndex(index)

        # Reconnect the signal
        self.team_combo.currentTextChanged.connect(self.on_team_selection_changed)

    def update_team_selection_programmatically(self, team_id: str):
        """Update team selection without triggering signals - used for external synchronization"""
        try:
            self.team_combo.currentTextChanged.disconnect()
        except:
            pass

        # Find and set the matching team
        index = -1
        for i in range(self.team_combo.count()):
            if self.team_combo.itemData(i) == team_id:
                index = i
                break

        if index >= 0:
            self.team_combo.setCurrentIndex(index)
            print(f"✅ Control panel team set to: {team_id}")

            # Update analyze button state
            if team_id and team_id != "":
                self.analyze_btn.setEnabled(True)
                self.analyze_btn.setToolTip(f"Analyze travel for {team_id}")
            else:
                self.analyze_btn.setEnabled(False)
                self.analyze_btn.setToolTip("Select a team to analyze")
        else:
            print(f"⚠️ Team {team_id} not found in control panel combo")

        # Reconnect signal
        self.team_combo.currentTextChanged.connect(self.on_team_selection_changed)

    def update_intelligence_display(self, intelligence: 'TeamTravelIntelligence'):
        """Update the intelligence display with Amadeus data"""
        if not intelligence:
            return
        
        # Update key metrics using correct attribute names
        self.total_miles_value.setText(f"{intelligence.total_travel_distance:,.0f}")
        
        # Update route breakdown
        route_breakdown_text = ""
        for i, route in enumerate(intelligence.upcoming_routes[:5], 1):  # Show top 5 routes
            route_breakdown_text += self._format_route_text(i, route, intelligence)
        
        self.route_breakdown.setPlainText(route_breakdown_text.strip())
        
        # Update hotel displays
        self.update_hotel_displays(intelligence)
        
        # Update flight intelligence
        self.update_flight_intelligence(intelligence)
    
    def _format_route_text(self, index: int, route, intelligence) -> str:
        """Helper method to format a single route for display"""
        try:
            # Extract data from the correct attributes
            if hasattr(route, 'travel_data') and route.travel_data:
                departure_airport = route.travel_data.departure_airport or "UNK"
                arrival_airport = route.travel_data.arrival_airport or "UNK"
                opponent = route.travel_data.opponent or "UNK"
                game_date = route.travel_data.game_date.strftime('%m/%d') if route.travel_data.game_date else "TBD"
            else:
                # Fallback to route data
                departure_airport = "UNK"
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
            
            confidence = route.travel_confidence
            distance = route.travel_distance
            
            return f"{index}. {departure_airport} → {arrival_airport} vs {opponent} [{confidence}] {game_date} ({distance:.0f}mi)\n"
            
        except Exception:
            return f"{index}. Route formatting error\n"

    def update_ui_for_league(self, league: str):
        """Update UI elements for specific league"""
        self.current_league = league
        
        # Clear team selection when league changes
        self.team_combo.clear()
        self.team_combo.addItem("Loading teams...", "")
        
        # Update the analyze button state
        self.analyze_btn.setEnabled(False)
        

    def update_travel_data(self, travel_data: List['TeamTravelData']):
        """Update the control panel with new travel data"""
        self.travel_data = travel_data
    
    def update_hotel_displays(self, intelligence: 'TeamTravelIntelligence'):
        """Update hotel display sections"""
        
        if not intelligence or not intelligence.team_info:
            return
        
        team_id = intelligence.team_info.team_id
        
        # Simple fix: use different routes for each section
        if intelligence.upcoming_routes:
            # Current stay: use first route if available
            if len(intelligence.upcoming_routes) >= 1:
                route = intelligence.upcoming_routes[0]
                if route.destination_hotels:
                    self.populate_hotel_section(self.current_hotels_widget, route.destination_hotels[:3])
                    self.current_hotels_widget.setVisible(True)
                else:
                    self.populate_hotel_section(self.current_hotels_widget, [])
                    self.current_hotels_widget.setVisible(True)
            
            # Next destination: use second route if available, otherwise show "no data"
            if len(intelligence.upcoming_routes) >= 2:
                route = intelligence.upcoming_routes[1]
                if route.destination_hotels:
                    self.populate_hotel_section(self.next_hotels_widget, route.destination_hotels[:3])
                    self.next_hotels_widget.setVisible(True)
                else:
                    self.populate_hotel_section(self.next_hotels_widget, [])
                    self.next_hotels_widget.setVisible(True)
            else:
                self.populate_hotel_section(self.next_hotels_widget, [])
                self.next_hotels_widget.setVisible(True)
        else:
            self.populate_hotel_section(self.current_hotels_widget, [])
            self.populate_hotel_section(self.next_hotels_widget, [])
            self.current_hotels_widget.setVisible(True)
            self.next_hotels_widget.setVisible(True)
    
    
    def get_hotel_star_rating(self, hotel) -> str:
        """Convert hotel rating to star display"""
        if hotel.forbes_rating:
            rating = hotel.forbes_rating.get_numeric_rating()
            return "★" * max(2, min(5, rating))  # Ensure 2-5 stars
        else:
            # Use overall rating
            if hotel.overall_rating >= 90:
                return "★★★★★"
            elif hotel.overall_rating >= 80:
                return "★★★★"
            elif hotel.overall_rating >= 70:
                return "★★★"
            else:
                return "★★"
    
    def update_flight_intelligence(self, intelligence: 'TeamTravelIntelligence'):
        """Update flight intelligence section with custom layout"""
        if not intelligence.upcoming_routes:
            self.flight_info_widget.setVisible(False)
            return
        
        if not hasattr(self.flight_info_widget, 'flight_layout'):
            return
            
        # Clear existing items
        self.clear_layout(self.flight_info_widget.flight_layout)
        
        # Add flight info - improved formatting and readability
        for i, route in enumerate(intelligence.upcoming_routes[:5]):  # Show top 5 routes
            if route.primary_airport:
                airport = route.primary_airport
                venue_name = route.game_data.venue.name if route.game_data else "venue"

                # Truncate venue name if too long
                if len(venue_name) > 25:
                    venue_name = venue_name[:22] + "..."

                # Create flight item label with improved formatting (no emoji)
                flight_text = f"• {airport.iata_code} ({airport.on_time_probability:.0%}) - {airport.distance_from_venue:.0f}km from {venue_name}"
                flight_label = QLabel(flight_text)
                flight_label.setFont(self.CONTENT_FONT)
                flight_label.setStyleSheet(self.CONTENT_STYLE)
                flight_label.setWordWrap(True)  # Allow text to wrap if needed
                self.flight_info_widget.flight_layout.addWidget(flight_label)

        # Add "more" indicator if there are additional routes
        if len(intelligence.upcoming_routes) > 5:
            more_label = QLabel(f"+ {len(intelligence.upcoming_routes)-5} more routes")
            more_label.setFont(QFont("Segoe UI", 7))
            more_label.setStyleSheet(self.MUTED_STYLE)
            self.flight_info_widget.flight_layout.addWidget(more_label)
        
        self.flight_info_widget.setVisible(True)
    
    def populate_hotel_section(self, widget: QFrame, hotels: list):
        """Populate hotel section with hotel data using custom compact layout"""
        
        if not hasattr(widget, 'hotels_layout'):
            return
            
        # Clear existing items
        self.clear_layout(widget.hotels_layout)
        
        # Check if we have hotels to display
        if not hotels:
            # Show "no data" message
            no_data_label = QLabel("No data available")
            no_data_label.setFont(self.CONTENT_FONT)
            no_data_label.setStyleSheet(self.MUTED_STYLE)
            widget.hotels_layout.addWidget(no_data_label)
            return
        
        # Add hotel items in clean format with better spacing
        for hotel in hotels[:4]:  # Show top 4 now that we have more space
            # Truncate long hotel names for space efficiency
            name = hotel.name[:25] + "..." if len(hotel.name) > 25 else hotel.name
            stars = self.get_hotel_star_rating(hotel)

            # Create hotel item label with improved formatting
            hotel_label = QLabel(f"• {name} ({hotel.distance_from_venue:.1f}km) {stars}")
            hotel_label.setFont(self.CONTENT_FONT)
            hotel_label.setStyleSheet(self.CONTENT_STYLE)
            hotel_label.setWordWrap(False)
            widget.hotels_layout.addWidget(hotel_label)

        # Add "more" indicator if there are additional hotels
        if len(hotels) > 4:
            more_label = QLabel(f"+ {len(hotels)-4} more hotels")
            more_label.setFont(QFont("Segoe UI", 7))
            more_label.setStyleSheet(self.MUTED_STYLE)
            widget.hotels_layout.addWidget(more_label)
    
    
    def clear_layout(self, layout):
        """Helper to clear all widgets from a layout"""
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
