from typing import List, Dict, Optional
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                            QWidget, QPushButton, QLabel, QFrame, QDateTimeEdit,
                            QCheckBox, QSlider, QGroupBox, QProgressBar, QComboBox,
                            QListWidget, QListWidgetItem, QSpinBox, QLineEdit,
                            QTextEdit, QScrollArea, QSplitter)
from PyQt6.QtCore import (Qt, QTimer, QDateTime, pyqtSignal, QThread)
from PyQt6.QtGui import QFont, QPixmap, QIcon


class FlightControlPanel(QWidget):
    """Enhanced control panel for sports team travel tracking"""

    # Signals
    modeChanged = pyqtSignal(str)  # "live" or "historical"
    airlineFilterChanged = pyqtSignal(list)  # List of team IDs
    statusFilterChanged = pyqtSignal(list)  # List of confidence levels
    routeFilterChanged = pyqtSignal(str, str)  # departure, arrival
    refreshRequested = pyqtSignal()
    flightSelected = pyqtSignal(str)  # team or game ID

    def __init__(self, parent=None):
        super().__init__(parent)
        self.travel_data = []
        self.filtered_travel = []
        self.setup_ui()
        self.apply_styles()
        self.connect_signals()

    def setup_ui(self):
        """Setup the enhanced sports control panel UI"""
        layout = QVBoxLayout()
        layout.setSpacing(10)

        # Header with refresh button
        header_frame = self.create_header_section()
        layout.addWidget(header_frame)

        # League selection
        league_frame = self.create_league_section()
        layout.addWidget(league_frame)

        # Travel filters
        filter_frame = self.create_filter_section()
        layout.addWidget(filter_frame)

        # Live travel statistics
        stats_frame = self.create_statistics_section()
        layout.addWidget(stats_frame)

        # Active travel list
        travel_frame = self.create_travel_section()
        layout.addWidget(travel_frame)

        # Route builder
        route_frame = self.create_route_section()
        layout.addWidget(route_frame)

        layout.addStretch()
        self.setLayout(layout)

    def create_header_section(self) -> QFrame:
        """Create header with title and refresh button"""
        frame = QFrame()
        layout = QHBoxLayout(frame)

        title_label = QLabel("SPORTS TRACKER")
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        layout.addWidget(title_label)

        layout.addStretch()

        self.refresh_btn = QPushButton("⟲ REFRESH")
        self.refresh_btn.setMinimumHeight(30)
        layout.addWidget(self.refresh_btn)

        # Status indicator
        self.status_label = QLabel("● LOADING")
        self.status_label.setStyleSheet("color: #FFA500;")
        layout.addWidget(self.status_label)

        return frame

    def create_league_section(self) -> QFrame:
        """Create league selection section"""
        frame = QFrame()
        layout = QHBoxLayout(frame)

        self.mlb_btn = QPushButton("MLB")
        self.nfl_btn = QPushButton("NFL")
        self.nba_btn = QPushButton("NBA")
        self.nhl_btn = QPushButton("NHL")

        self.mlb_btn.setCheckable(True)
        self.nfl_btn.setCheckable(True)
        self.nba_btn.setCheckable(True)
        self.nhl_btn.setCheckable(True)
        self.mlb_btn.setChecked(True)  # Default to MLB

        layout.addWidget(self.mlb_btn)
        layout.addWidget(self.nfl_btn)
        layout.addWidget(self.nba_btn)
        layout.addWidget(self.nhl_btn)

        return frame

    def create_filter_section(self) -> QGroupBox:
        """Create travel filter controls"""
        group = QGroupBox("FILTERS")
        layout = QVBoxLayout(group)

        # Team filter
        team_layout = QHBoxLayout()
        team_layout.addWidget(QLabel("Teams:"))
        self.team_combo = QComboBox()
        self.team_combo.addItem("All Teams", "")
        self.team_combo.setEditable(False)
        team_layout.addWidget(self.team_combo)
        layout.addLayout(team_layout)

        # Confidence filter
        confidence_layout = QVBoxLayout()
        confidence_layout.addWidget(QLabel("Travel Confidence:"))

        self.confidence_checkboxes = {}
        confidence_levels = [
            ("Schedule Inferred", "schedule_inferred", True),
            ("Confirmed", "confirmed", True),
            ("Demo Data", "demo", False),
            ("Historical", "historical", False)
        ]

        for display_name, confidence_code, default_checked in confidence_levels:
            checkbox = QCheckBox(display_name)
            checkbox.setChecked(default_checked)
            checkbox.setProperty("confidence_code", confidence_code)
            self.confidence_checkboxes[confidence_code] = checkbox
            confidence_layout.addWidget(checkbox)

        layout.addLayout(confidence_layout)

        # Date range filter
        date_layout = QHBoxLayout()
        date_layout.addWidget(QLabel("Days ahead:"))
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 30)
        self.days_spin.setValue(7)
        self.days_spin.setSuffix(" days")
        date_layout.addWidget(self.days_spin)
        layout.addLayout(date_layout)

        return group

    def create_statistics_section(self) -> QGroupBox:
        """Create live travel statistics"""
        group = QGroupBox("TRAVEL STATISTICS")
        layout = QVBoxLayout(group)

        # Statistics labels
        stats_layout = QHBoxLayout()

        left_stats = QVBoxLayout()
        self.total_travel_label = QLabel("Total Travel: 0")
        self.active_travel_label = QLabel("This Week: 0")
        self.teams_traveling_label = QLabel("Teams: 0")
        left_stats.addWidget(self.total_travel_label)
        left_stats.addWidget(self.active_travel_label)
        left_stats.addWidget(self.teams_traveling_label)

        right_stats = QVBoxLayout()
        self.avg_distance_label = QLabel("Avg Distance: 0 mi")
        self.longest_trip_label = QLabel("Longest: N/A")
        self.busiest_route_label = QLabel("Busiest: N/A")
        right_stats.addWidget(self.avg_distance_label)
        right_stats.addWidget(self.longest_trip_label)
        right_stats.addWidget(self.busiest_route_label)

        stats_layout.addLayout(left_stats)
        stats_layout.addLayout(right_stats)
        layout.addLayout(stats_layout)

        # Progress bar for data updates
        self.data_progress = QProgressBar()
        self.data_progress.setVisible(False)
        layout.addWidget(self.data_progress)

        return group

    def create_travel_section(self) -> QGroupBox:
        """Create active travel list"""
        group = QGroupBox("TEAM TRAVEL")
        layout = QVBoxLayout(group)

        # Search box
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.travel_search = QLineEdit()
        self.travel_search.setPlaceholderText("Team, city, or route...")
        search_layout.addWidget(self.travel_search)
        layout.addLayout(search_layout)

        # Travel list
        self.travel_list = QListWidget()
        self.travel_list.setMaximumHeight(200)
        layout.addWidget(self.travel_list)

        # Travel details
        self.travel_details = QTextEdit()
        self.travel_details.setMaximumHeight(100)
        self.travel_details.setReadOnly(True)
        self.travel_details.setPlaceholderText("Select travel to see details...")
        layout.addWidget(self.travel_details)

        return group

    def create_route_section(self) -> QGroupBox:
        """Create route explorer section"""
        group = QGroupBox("ROUTE EXPLORER")
        layout = QVBoxLayout(group)

        # Route inputs
        route_layout = QHBoxLayout()

        # Departure
        dep_layout = QVBoxLayout()
        dep_layout.addWidget(QLabel("From:"))
        self.departure_input = QComboBox()
        self.departure_input.setEditable(True)
        self.departure_input.setPlaceholderText("City name")
        dep_layout.addWidget(self.departure_input)

        # Arrival
        arr_layout = QVBoxLayout()
        arr_layout.addWidget(QLabel("To:"))
        self.arrival_input = QComboBox()
        self.arrival_input.setEditable(True)
        self.arrival_input.setPlaceholderText("City name")
        arr_layout.addWidget(self.arrival_input)

        route_layout.addLayout(dep_layout)
        route_layout.addLayout(arr_layout)

        # Find route button
        self.find_route_btn = QPushButton("FIND TRAVEL")
        route_layout.addWidget(self.find_route_btn)

        layout.addLayout(route_layout)

        # Route results
        self.route_results = QLabel("Enter departure and arrival cities")
        self.route_results.setWordWrap(True)
        self.route_results.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self.route_results)

        return group

    def connect_signals(self):
        """Connect UI signals"""
        # League buttons
        self.mlb_btn.clicked.connect(lambda: self.on_league_changed("MLB"))
        self.nfl_btn.clicked.connect(lambda: self.on_league_changed("NFL"))
        self.nba_btn.clicked.connect(lambda: self.on_league_changed("NBA"))
        self.nhl_btn.clicked.connect(lambda: self.on_league_changed("NHL"))

        # Refresh button
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)

        # Filter controls
        self.team_combo.currentTextChanged.connect(self.update_filters)
        self.travel_search.textChanged.connect(self.filter_travel)

        # Confidence checkboxes
        for checkbox in self.confidence_checkboxes.values():
            checkbox.stateChanged.connect(self.update_filters)

        # Travel selection
        self.travel_list.currentItemChanged.connect(self.on_travel_selected)

        # Route explorer
        self.find_route_btn.clicked.connect(self.find_route)

    def apply_styles(self):
        """Apply enhanced styles for sports theme"""
        style = """
        QWidget {
            background-color: rgba(5, 25, 45, 220);
            color: white;
            font-family: 'Consolas', 'Monaco', monospace;
            border-radius: 6px;
        }

        QPushButton {
            background-color: rgba(0, 100, 180, 140);
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 4px;
            padding: 8px 16px;
            font-weight: bold;
            font-size: 11px;
            min-height: 20px;
        }

        QPushButton:checked {
            background-color: rgba(0, 150, 250, 200);
            border: 2px solid rgba(100, 200, 255, 220);
        }

        QPushButton:hover {
            background-color: rgba(0, 120, 200, 160);
        }

        QGroupBox {
            font-weight: bold;
            border: 1px solid rgba(100, 150, 200, 120);
            border-radius: 6px;
            margin-top: 1ex;
            padding-top: 12px;
            font-size: 11px;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 8px 0 8px;
            color: rgba(200, 220, 255, 255);
        }

        QLabel {
            color: rgba(220, 220, 220, 255);
            font-size: 11px;
        }

        QComboBox, QLineEdit, QSpinBox {
            background-color: rgba(20, 40, 70, 180);
            border: 1px solid rgba(100, 150, 200, 120);
            border-radius: 3px;
            padding: 4px 8px;
            color: white;
            font-size: 10px;
        }

        QComboBox::drop-down {
            border: none;
            width: 20px;
        }

        QComboBox::down-arrow {
            border: none;
            color: white;
        }

        QCheckBox {
            color: rgba(200, 200, 200, 255);
            spacing: 5px;
            font-size: 10px;
        }

        QCheckBox::indicator {
            width: 14px;
            height: 14px;
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 2px;
            background-color: rgba(20, 40, 70, 150);
        }

        QCheckBox::indicator:checked {
            background-color: rgba(0, 150, 250, 200);
        }

        QListWidget {
            background-color: rgba(10, 25, 45, 200);
            border: 1px solid rgba(100, 150, 200, 100);
            border-radius: 4px;
            font-size: 10px;
        }

        QListWidget::item {
            padding: 4px;
            border-bottom: 1px solid rgba(100, 150, 200, 50);
        }

        QListWidget::item:selected {
            background-color: rgba(0, 120, 200, 150);
        }

        QTextEdit {
            background-color: rgba(10, 25, 45, 200);
            border: 1px solid rgba(100, 150, 200, 100);
            border-radius: 4px;
            font-size: 10px;
            font-family: 'Consolas', monospace;
        }

        QProgressBar {
            border: 1px solid rgba(100, 150, 200, 100);
            border-radius: 3px;
            text-align: center;
            font-size: 10px;
        }

        QProgressBar::chunk {
            background-color: rgba(0, 150, 250, 180);
            border-radius: 2px;
        }
        """
        self.setStyleSheet(style)

    def on_league_changed(self, league: str):
        """Handle league change"""
        # Uncheck other league buttons
        if league == "MLB":
            self.nfl_btn.setChecked(False)
            self.nba_btn.setChecked(False)
            self.nhl_btn.setChecked(False)
        elif league == "NFL":
            self.mlb_btn.setChecked(False)
            self.nba_btn.setChecked(False)
            self.nhl_btn.setChecked(False)
        elif league == "NBA":
            self.mlb_btn.setChecked(False)
            self.nfl_btn.setChecked(False)
            self.nhl_btn.setChecked(False)
        elif league == "NHL":
            self.mlb_btn.setChecked(False)
            self.nfl_btn.setChecked(False)
            self.nba_btn.setChecked(False)

        self.modeChanged.emit(league)

    def update_flight_data(self, travel_data: List):
        """Update the panel with new travel data"""
        self.travel_data = travel_data
        self.update_statistics()
        self.update_team_filter()
        self.update_city_filters()
        self.filter_travel()

    def update_statistics(self):
        """Update travel statistics"""
        if not self.travel_data:
            self.total_travel_label.setText("Total Travel: 0")
            self.active_travel_label.setText("This Week: 0")
            self.teams_traveling_label.setText("Teams: 0")
            self.avg_distance_label.setText("Avg Distance: 0 mi")
            self.longest_trip_label.setText("Longest: N/A")
            self.busiest_route_label.setText("Busiest: N/A")
            return

        from datetime import datetime, timedelta

        total = len(self.travel_data)

        # Count travel this week - handle timezone aware/naive datetime comparison
        week_start = datetime.now()
        week_end = week_start + timedelta(days=7)

        this_week = 0
        for t in self.travel_data:
            if hasattr(t, 'travel_date') and t.travel_date:
                # Convert to naive datetime if timezone-aware
                travel_date = t.travel_date
                if hasattr(travel_date, 'tzinfo') and travel_date.tzinfo is not None:
                    travel_date = travel_date.replace(tzinfo=None)

        # Count travel this week - handle timezone aware/naive datetime comparison
        week_start = datetime.now()
        week_end = week_start + timedelta(days=7)

        this_week = 0
        for t in self.travel_data:
            if hasattr(t, 'travel_date') and t.travel_date:
                # Convert to naive datetime if timezone-aware
                travel_date = t.travel_date
                if hasattr(travel_date, 'tzinfo') and travel_date.tzinfo is not None:
                    travel_date = travel_date.replace(tzinfo=None)

                if week_start <= travel_date <= week_end:
                    this_week += 1

        # Count unique teams
        teams = set()
        for travel in self.travel_data:
            if hasattr(travel, 'team_id') and travel.team_id:
                teams.add(travel.team_id)

        # Calculate distance statistics (simplified)
        distances = []
        routes = {}
        longest_distance = 0
        longest_route = "N/A"

        for travel in self.travel_data:
            if hasattr(travel, 'departure_city') and hasattr(travel, 'arrival_city'):
                route = f"{travel.departure_city}-{travel.arrival_city}"
                routes[route] = routes.get(route, 0) + 1

                # Estimate distance (very simplified)
                dist = self.estimate_distance(travel.departure_city, travel.arrival_city)
                if dist > 0:
                    distances.append(dist)
                    if dist > longest_distance:
                        longest_distance = dist
                        longest_route = route

        avg_distance = int(sum(distances) / len(distances)) if distances else 0

        # Find busiest route
        busiest_route = "N/A"
        if routes:
            busiest_route = max(routes.items(), key=lambda x: x[1])[0]

        # Update labels
        self.total_travel_label.setText(f"Total Travel: {total}")
        self.active_travel_label.setText(f"This Week: {this_week}")
        self.teams_traveling_label.setText(f"Teams: {len(teams)}")
        self.avg_distance_label.setText(f"Avg Distance: {avg_distance:,} mi")
        self.longest_trip_label.setText(f"Longest: {longest_route}")
        self.busiest_route_label.setText(f"Busiest: {busiest_route}")

    def estimate_distance(self, city1: str, city2: str) -> int:
        """Estimate distance between cities (very simplified)"""
        # Simple distance estimates for major city pairs
        distance_map = {
            ("Los Angeles", "New York"): 2445,
            ("New York", "Los Angeles"): 2445,
            ("Chicago", "Los Angeles"): 1745,
            ("Los Angeles", "Chicago"): 1745,
            ("Boston", "Los Angeles"): 2596,
            ("Los Angeles", "Boston"): 2596,
            ("New York", "Chicago"): 790,
            ("Chicago", "New York"): 790,
            ("Miami", "Seattle"): 2724,
            ("Seattle", "Miami"): 2724,
            ("Dallas", "Boston"): 1551,
            ("Boston", "Dallas"): 1551,
        }

        key1 = (city1, city2)
        key2 = (city2, city1)

        if key1 in distance_map:
            return distance_map[key1]
        elif key2 in distance_map:
            return distance_map[key2]
        else:
            # Default estimate based on city names
            return 1000  # Default 1000 miles

    def update_team_filter(self):
        """Update team filter dropdown"""
        current_text = self.team_combo.currentText()
        self.team_combo.clear()
        self.team_combo.addItem("All Teams", "")

        teams = set()
        for travel in self.travel_data:
            if hasattr(travel, 'team_name') and hasattr(travel, 'team_id'):
                team_name = travel.team_name
                team_id = travel.team_id
                if team_name and team_id:
                    teams.add((team_name, team_id))

        for team_name, team_id in sorted(teams):
            self.team_combo.addItem(f"{team_name} ({team_id})", team_id)

        # Restore selection if possible
        index = self.team_combo.findText(current_text)
        if index >= 0:
            self.team_combo.setCurrentIndex(index)

    def update_city_filters(self):
        """Update city filter dropdowns"""
        cities = set()
        for travel in self.travel_data:
            if hasattr(travel, 'departure_city') and hasattr(travel, 'arrival_city'):
                dep_city = travel.departure_city
                arr_city = travel.arrival_city
                if dep_city:
                    cities.add(dep_city)
                if arr_city:
                    cities.add(arr_city)

        # Update departure combo
        current_dep = self.departure_input.currentText()
        self.departure_input.clear()
        for city in sorted(cities):
            self.departure_input.addItem(city)

        # Update arrival combo
        current_arr = self.arrival_input.currentText()
        self.arrival_input.clear()
        for city in sorted(cities):
            self.arrival_input.addItem(city)

        # Restore selections
        dep_index = self.departure_input.findText(current_dep)
        if dep_index >= 0:
            self.departure_input.setCurrentIndex(dep_index)

        arr_index = self.arrival_input.findText(current_arr)
        if arr_index >= 0:
            self.arrival_input.setCurrentIndex(arr_index)

    def update_filters(self):
        """Update filters and emit signals"""
        # Get selected team
        selected_team = self.team_combo.currentData()
        if selected_team:
            self.airlineFilterChanged.emit([selected_team])
        else:
            self.airlineFilterChanged.emit([])

        # Get selected confidence levels
        selected_confidence = []
        for confidence_code, checkbox in self.confidence_checkboxes.items():
            if checkbox.isChecked():
                selected_confidence.append(confidence_code)

        self.statusFilterChanged.emit(selected_confidence)
        self.filter_travel()

    def filter_travel(self):
        """Filter travel based on current criteria"""
        search_text = self.travel_search.text().lower()
        selected_team = self.team_combo.currentData()
        selected_confidence = [code for code, cb in self.confidence_checkboxes.items() if cb.isChecked()]
        days_ahead = self.days_spin.value()

        from datetime import datetime, timedelta
        cutoff_date = datetime.now() + timedelta(days=days_ahead)

        filtered = []
        for travel in self.travel_data:
            if not hasattr(travel, 'team_name'):
                continue

            # Text search
            if search_text:
                searchable_text = (
                    f"{getattr(travel, 'team_name', '')} "
                    f"{getattr(travel, 'departure_city', '')} "
                    f"{getattr(travel, 'arrival_city', '')} "
                    f"{getattr(travel, 'team_id', '')}"
                ).lower()

                if search_text not in searchable_text:
                    continue

            # Team filter
            if selected_team and getattr(travel, 'team_id', '') != selected_team:
                continue

            # Confidence filter
            if selected_confidence and getattr(travel, 'confidence', '') not in selected_confidence:
                continue

        # Date filter - handle timezone aware/naive datetime comparison
        if hasattr(travel, 'travel_date') and travel.travel_date:
            # Convert to naive datetime if timezone-aware for comparison
            travel_date = travel.travel_date
            if hasattr(travel_date, 'tzinfo') and travel_date.tzinfo is not None:
                travel_date = travel_date.replace(tzinfo=None)

            if travel_date > cutoff_date:
                pass

            filtered.append(travel)

        self.filtered_travel = filtered
        self.update_travel_list()

    def update_travel_list(self):
        """Update the travel list widget"""
        self.travel_list.clear()

        for travel in self.filtered_travel[:50]:  # Limit to 50 for performance
            if not hasattr(travel, 'team_name'):
                continue

            # Create list item text
            confidence_icon = {
                'schedule_inferred': '📅',
                'confirmed': '✅',
                'demo': '🎭',
                'historical': '📚'
            }.get(getattr(travel, 'confidence', ''), '❓')

            travel_date = getattr(travel, 'travel_date', None)
            date_text = ""
            if travel_date:
                date_text = f" ({travel_date.strftime('%m/%d')})"

            opponent_text = ""
            if hasattr(travel, 'opponent') and travel.opponent:
                opponent_text = f" vs {travel.opponent}"

            item_text = (f"{confidence_icon} {travel.team_name} "
                        f"{travel.departure_city}→{travel.arrival_city}"
                        f"{date_text}{opponent_text}")

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, travel)
            self.travel_list.addItem(item)

    def on_travel_selected(self, current_item, previous_item):
        """Handle travel selection"""
        if not current_item:
            self.travel_details.clear()
            return

        travel = current_item.data(Qt.ItemDataRole.UserRole)
        if not travel or not hasattr(travel, 'team_name'):
            return

        # Format travel details
        details = []
        details.append(f"Team: {travel.team_name}")
        details.append(f"Team ID: {getattr(travel, 'team_id', 'Unknown')}")
        details.append(f"Route: {travel.departure_city} → {travel.arrival_city}")
        details.append(f"Confidence: {getattr(travel, 'confidence', 'Unknown')}")

        if hasattr(travel, 'travel_date') and travel.travel_date:
            details.append(f"Travel Date: {travel.travel_date.strftime('%Y-%m-%d %H:%M')}")

        if hasattr(travel, 'game_date') and travel.game_date:
            details.append(f"Game Date: {travel.game_date.strftime('%Y-%m-%d %H:%M')}")

        if hasattr(travel, 'opponent') and travel.opponent:
            details.append(f"Opponent: {travel.opponent}")

        if hasattr(travel, 'departure_airport') and travel.departure_airport:
            details.append(f"Departure Airport: {travel.departure_airport}")

        if hasattr(travel, 'arrival_airport') and travel.arrival_airport:
            details.append(f"Arrival Airport: {travel.arrival_airport}")

        self.travel_details.setText('\n'.join(details))

        # Emit signal with team ID or game ID
        travel_id = getattr(travel, 'team_id', '') or getattr(travel, 'game_id', '')
        if travel_id:
            self.flightSelected.emit(travel_id)

    def find_route(self):
        """Find travel for specified route"""
        departure = self.departure_input.currentText()
        arrival = self.arrival_input.currentText()

        if not departure or not arrival:
            self.route_results.setText("Please enter both departure and arrival cities")
            return

        # Find matching travel
        route_travel = [t for t in self.travel_data
                       if (hasattr(t, 'departure_city') and hasattr(t, 'arrival_city') and
                           t.departure_city == departure and t.arrival_city == arrival)]

        if route_travel:
            count = len(route_travel)
            teams = set(getattr(t, 'team_name', 'Unknown') for t in route_travel)
            self.route_results.setText(
                f"Found {count} travel record(s) for {departure}→{arrival}\n"
                f"Teams: {', '.join(sorted(teams))}"
            )
        else:
            self.route_results.setText(f"No travel found for {departure}→{arrival}")

        self.routeFilterChanged.emit(departure, arrival)

    def update_fps(self, fps: float):
        """Update FPS display"""
        # This method exists for compatibility but FPS display is in main window
        pass

    def set_loading_progress(self, progress: int):
        """Set loading progress"""
        if progress > 0 and progress < 100:
            self.data_progress.setVisible(True)
            self.data_progress.setValue(progress)
        else:
            self.data_progress.setVisible(False)

    def set_connection_status(self, connected: bool):
        """Update connection status"""
        if connected:
            self.status_label.setText("● CONNECTED")
            self.status_label.setStyleSheet("color: #00FF00;")
        else:
            self.status_label.setText("● DISCONNECTED")
            self.status_label.setStyleSheet("color: #FF0000;")
