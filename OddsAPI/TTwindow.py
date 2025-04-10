import sys
import json
import os
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QComboBox, QTabWidget, QTableWidget, QTableWidgetItem, 
    QPushButton, QLineEdit, QSplitter, QGroupBox, QScrollArea, 
    QGridLayout, QHeaderView, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QFont, QColor

# Import the client for data fetching
import asyncio
from TableTennisClient import main as fetch_data

class TableTennisGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Table Tennis Tracker")
        self.setMinimumSize(1200, 800)
        
        # Store data
        self.data = {}
        self.leagues = {
            "22307": "Setka Cup",
            "22742": "Czech Republic Liga Pro",
            "22534": "TT CUP",
            "24536": "Poland TT Elite Series"
        }
        
        # Setup UI
        self.setup_ui()
        
        # Load data from JSON files
        self.load_data()
        
        # Set up auto-refresh timer (every 5 minutes)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.refresh_timer.start(300000)  # 5 minutes

    def setup_ui(self):
        # Create central widget and main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        
        # Create main layout with splitter
        self.main_layout = QHBoxLayout(self.central_widget)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_layout.addWidget(self.splitter)
        
        # Set up the three panels
        self.setup_control_panel()
        self.setup_matches_panel()
        self.setup_details_panel()
        
        # Set splitter sizes
        self.splitter.setSizes([250, 500, 450])
        
        # Apply dark theme
        self.apply_dark_theme()

    def setup_control_panel(self):
        # Left control panel
        self.control_panel = QWidget()
        self.control_layout = QVBoxLayout(self.control_panel)
        
        # Logo or title
        title_label = QLabel("TABLE TENNIS TRACKER")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #3498db; margin: 10px;")
        self.control_layout.addWidget(title_label)
        
        # League selection
        league_group = QGroupBox("Leagues")
        league_layout = QVBoxLayout(league_group)
        
        self.league_checkboxes = {}
        for league_id, league_name in self.leagues.items():
            checkbox = QPushButton(league_name)
            checkbox.setCheckable(True)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.filter_matches)
            checkbox.setStyleSheet("""
                QPushButton {
                    text-align: left;
                    padding: 5px;
                    border: none;
                    border-radius: 3px;
                    background-color: #2c3e50;
                }
                QPushButton:checked {
                    background-color: #3498db;
                }
            """)
            self.league_checkboxes[league_id] = checkbox
            league_layout.addWidget(checkbox)
        
        self.control_layout.addWidget(league_group)
        
        # Search box
        search_group = QGroupBox("Search")
        search_layout = QVBoxLayout(search_group)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by player name...")
        self.search_input.textChanged.connect(self.filter_matches)
        search_layout.addWidget(self.search_input)
        
        self.control_layout.addWidget(search_group)
        
        # Time filter
        time_group = QGroupBox("Time Window")
        time_layout = QVBoxLayout(time_group)
        
        self.time_combo = QComboBox()
        self.time_combo.addItems(["Next 1 hour", "Next 3 hours", "Next 6 hours", "All matches"])
        self.time_combo.setCurrentIndex(2)  # Default to 6 hours
        self.time_combo.currentIndexChanged.connect(self.filter_matches)
        time_layout.addWidget(self.time_combo)
        
        self.control_layout.addWidget(time_group)
        
        # Refresh button
        self.refresh_btn = QPushButton("Refresh Data")
        self.refresh_btn.clicked.connect(self.refresh_data)
        self.refresh_btn.setStyleSheet("background-color: #27ae60; padding: 8px;")
        self.control_layout.addWidget(self.refresh_btn)
        
        # Status label
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.control_layout.addWidget(self.status_label)
        
        # Add stretcher to push everything up
        self.control_layout.addStretch()
        
        # Add to splitter
        self.splitter.addWidget(self.control_panel)

    def setup_matches_panel(self):
        # Center matches panel
        self.matches_panel = QWidget()
        self.matches_layout = QVBoxLayout(self.matches_panel)
        
        # Matches count and header
        self.matches_header = QLabel("Upcoming Matches (0)")
        self.matches_header.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.matches_layout.addWidget(self.matches_header)
        
        # Matches table
        self.matches_table = QTableWidget(0, 5)
        self.matches_table.setHorizontalHeaderLabels(["Time", "League", "Home", "Away", "Odds"])
        self.matches_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.matches_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.matches_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.matches_table.verticalHeader().setVisible(False)
        self.matches_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.matches_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.matches_table.itemSelectionChanged.connect(self.show_match_details)
        
        self.matches_layout.addWidget(self.matches_table)
        
        # Add to splitter
        self.splitter.addWidget(self.matches_panel)

    def setup_details_panel(self):
        # Right details panel
        self.details_panel = QScrollArea()
        self.details_panel.setWidgetResizable(True)
        
        # Container widget for scroll area
        self.details_widget = QWidget()
        self.details_layout = QVBoxLayout(self.details_widget)
        
        # Match details header
        self.match_title = QLabel("Select a match to view details")
        self.match_title.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.match_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(self.match_title)
        
        # Match summary
        self.match_summary = QGroupBox("Match Info")
        summary_layout = QGridLayout(self.match_summary)
        
        self.summary_labels = {
            "league": QLabel("League: "),
            "time": QLabel("Time: "),
            "home": QLabel("Home: "),
            "away": QLabel("Away: ")
        }
        
        row = 0
        label_keys = list(self.summary_labels.keys())  # Create a static list to iterate over
        for key in label_keys:
            label = self.summary_labels[key]
            value_label = QLabel("-")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.summary_labels[key + "_val"] = value_label
            summary_layout.addWidget(label, row, 0)
            summary_layout.addWidget(value_label, row, 1)
            row += 1
            
        self.details_layout.addWidget(self.match_summary)
        
        # Odds details
        self.odds_group = QGroupBox("Betting Odds")
        odds_layout = QGridLayout(self.odds_group)
        
        odds_layout.addWidget(QLabel("Market"), 0, 0)
        odds_layout.addWidget(QLabel("Home"), 0, 1)
        odds_layout.addWidget(QLabel("Away/Under"), 0, 2)
        odds_layout.addWidget(QLabel("Handicap/Total"), 0, 3)
        
        self.odds_labels = {
            "moneyline_home": QLabel("-"),
            "moneyline_away": QLabel("-"),
            "spread_home": QLabel("-"),
            "spread_away": QLabel("-"),
            "spread_handicap": QLabel("-"),
            "total_over": QLabel("-"),
            "total_under": QLabel("-"),
            "total_points": QLabel("-")
        }
        
        odds_layout.addWidget(QLabel("Moneyline"), 1, 0)
        odds_layout.addWidget(self.odds_labels["moneyline_home"], 1, 1)
        odds_layout.addWidget(self.odds_labels["moneyline_away"], 1, 2)
        odds_layout.addWidget(QLabel("-"), 1, 3)
        
        odds_layout.addWidget(QLabel("Spread"), 2, 0)
        odds_layout.addWidget(self.odds_labels["spread_home"], 2, 1)
        odds_layout.addWidget(self.odds_labels["spread_away"], 2, 2)
        odds_layout.addWidget(self.odds_labels["spread_handicap"], 2, 3)
        
        odds_layout.addWidget(QLabel("Total"), 3, 0)
        odds_layout.addWidget(self.odds_labels["total_over"], 3, 1)
        odds_layout.addWidget(self.odds_labels["total_under"], 3, 2)
        odds_layout.addWidget(self.odds_labels["total_points"], 3, 3)
        
        self.details_layout.addWidget(self.odds_group)
        
        # Head-to-head
        self.h2h_group = QGroupBox("Head-to-Head History")
        h2h_layout = QVBoxLayout(self.h2h_group)
        
        self.h2h_summary = QLabel("No head-to-head data available")
        self.h2h_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h2h_layout.addWidget(self.h2h_summary)
        
        self.h2h_table = QTableWidget(0, 4)
        self.h2h_table.setHorizontalHeaderLabels(["Date", "Home", "Away", "Score"])
        self.h2h_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.h2h_table.verticalHeader().setVisible(False)
        self.h2h_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h2h_layout.addWidget(self.h2h_table)
        
        self.details_layout.addWidget(self.h2h_group)
        
        # Win probability chart
        self.prob_group = QGroupBox("Win Probability")
        prob_layout = QHBoxLayout(self.prob_group)
        
        self.home_prob = QLabel("Home: 50%")
        self.away_prob = QLabel("Away: 50%")
        prob_layout.addWidget(self.home_prob)
        prob_layout.addWidget(self.away_prob)
        
        self.details_layout.addWidget(self.prob_group)
        
        # Set the widget to the scroll area
        self.details_panel.setWidget(self.details_widget)
        
        # Add to splitter
        self.splitter.addWidget(self.details_panel)

    def apply_dark_theme(self):
        # Set dark theme colors
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #f0f0f0;
            }
            QTableWidget {
                background-color: #2d2d2d;
                alternate-background-color: #353535;
                gridline-color: #3a3a3a;
                border: 1px solid #3a3a3a;
            }
            QTableWidget::item:selected {
                background-color: #2980b9;
            }
            QHeaderView::section {
                background-color: #2c3e50;
                padding: 4px;
                border: 1px solid #2c3e50;
                color: white;
            }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
                color: #3498db;
            }
            QLineEdit, QComboBox {
                background-color: #2d2d2d;
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                padding: 5px;
                color: white;
            }
            QPushButton {
                background-color: #2980b9;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #3498db;
            }
            QPushButton:pressed {
                background-color: #1c6ea4;
            }
            QLabel {
                color: #f0f0f0;
            }
        """)

    def refresh_data(self):
        """Refresh data by running the client script"""
        self.status_label.setText("Refreshing data...")
        QApplication.processEvents()
        
        try:
            # Run client asynchronously
            asyncio.run(fetch_data())
            self.status_label.setText(f"Data refreshed: {datetime.now().strftime('%H:%M:%S')}")
            
            # Load the updated data
            self.load_data()
        except Exception as e:
            self.status_label.setText(f"Error refreshing data: {str(e)}")
            print(f"Error: {str(e)}")

    def load_data(self):
        """Load data from JSON files"""
        try:
            # Path to save directory
            save_dir = Path.cwd() / "TTT_savedata"
            
            # Check if directory exists
            if not save_dir.exists():
                self.status_label.setText("No data found. Click 'Refresh Data' to fetch.")
                return
                
            # Load all-upcoming-events.json
            all_events_path = save_dir / "all-upcoming-events.json"
            if all_events_path.exists():
                with open(all_events_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                    
                # Update status and UI
                self.status_label.setText(f"Data loaded: {datetime.now().strftime('%H:%M:%S')}")
                self.populate_matches_table()
            else:
                # Try to load individual league files
                self.data = {}
                for league_name in self.leagues.values():
                    league_file = save_dir / f"{league_name.replace(' ', '-')}.json"
                    if league_file.exists():
                        with open(league_file, 'r', encoding='utf-8') as f:
                            league_data = json.load(f)
                            self.data[league_name] = league_data
                
                if self.data:
                    self.status_label.setText(f"Data loaded: {datetime.now().strftime('%H:%M:%S')}")
                    self.populate_matches_table()
                else:
                    self.status_label.setText("No data found. Click 'Refresh Data' to fetch.")
        except Exception as e:
            self.status_label.setText(f"Error loading data: {str(e)}")
            print(f"Error: {str(e)}")

    def populate_matches_table(self):
        """Populate matches table with filtered data"""
        # Clear table
        self.matches_table.setRowCount(0)
        
        total_matches = 0
        row_index = 0
        
        # Determine time filter
        time_filter_hours = 6  # Default
        time_filter_text = self.time_combo.currentText()
        if "1 hour" in time_filter_text:
            time_filter_hours = 1
        elif "3 hours" in time_filter_text:
            time_filter_hours = 3
        
        current_time = int(datetime.now().timestamp())
        max_time = current_time + (time_filter_hours * 3600)
        
        # Get active leagues
        active_leagues = [league_id for league_id, checkbox in self.league_checkboxes.items() 
                         if checkbox.isChecked()]
        
        # Get search text
        search_text = self.search_input.text().lower()
        
        # Process each league
        for league_name, league_data in self.data.items():
            if not isinstance(league_data, dict) or 'results' not in league_data:
                continue
                
            # Filter by league
            league_id = None
            for lid, lname in self.leagues.items():
                if lname == league_name:
                    league_id = lid
                    break
                    
            if league_id not in active_leagues:
                continue
                
            # Process each match
            for match in league_data.get('results', []):
                # Get match time
                match_time = int(match.get('time', 0))
                
                # Apply time filter
                if not (current_time <= match_time <= max_time) and "All matches" not in time_filter_text:
                    continue
                    
                # Get player names
                home_name = match.get('home', {}).get('name', '')
                away_name = match.get('away', {}).get('name', '')
                
                # Apply search filter
                if search_text and search_text not in home_name.lower() and search_text not in away_name.lower():
                    continue
                
                # Add to table
                self.matches_table.insertRow(row_index)
                
                # Format time
                match_datetime = datetime.fromtimestamp(match_time)
                time_str = match_datetime.strftime("%H:%M")
                
                # Add basic info
                time_item = QTableWidgetItem(time_str)
                league_item = QTableWidgetItem(league_name)
                home_item = QTableWidgetItem(home_name)
                away_item = QTableWidgetItem(away_name)
                
                # Add odds if available
                odds_str = "-"
                for market_data in league_data.get('markets', []):
                    if market_data.get('event_id') == match.get('id'):
                        market_odds = market_data.get('markets', {}).get('odds', {}).get('92_1', [])
                        if market_odds and len(market_odds) > 0:
                            home_odd = market_odds[0].get('home_od', '-')
                            away_odd = market_odds[0].get('away_od', '-')
                            odds_str = f"{home_odd} / {away_odd}"
                            break
                
                odds_item = QTableWidgetItem(odds_str)
                
                # Set items
                self.matches_table.setItem(row_index, 0, time_item)
                self.matches_table.setItem(row_index, 1, league_item)
                self.matches_table.setItem(row_index, 2, home_item)
                self.matches_table.setItem(row_index, 3, away_item)
                self.matches_table.setItem(row_index, 4, odds_item)
                
                # Store event ID and league in hidden data
                time_item.setData(Qt.ItemDataRole.UserRole, match.get('id'))
                time_item.setData(Qt.ItemDataRole.UserRole + 1, league_name)
                
                # Color code based on odds
                try:
                    home_odd_val = float(home_odd) if 'home_odd' in locals() else 0
                    if home_odd_val < 1.5:
                        home_item.setBackground(QColor(100, 200, 100, 80))  # Green for heavy favorite
                    elif home_odd_val < 2.0:
                        home_item.setBackground(QColor(180, 180, 100, 80))  # Yellow for moderate favorite
                        
                    away_odd_val = float(away_odd) if 'away_odd' in locals() else 0
                    if away_odd_val < 1.5:
                        away_item.setBackground(QColor(100, 200, 100, 80))
                    elif away_odd_val < 2.0:
                        away_item.setBackground(QColor(180, 180, 100, 80))
                except:
                    pass
                
                row_index += 1
                total_matches += 1
        
        # Update header with match count
        self.matches_header.setText(f"Upcoming Matches ({total_matches})")
        
        # Auto-resize rows for better readability
        self.matches_table.resizeRowsToContents()
    
    def filter_matches(self):
        """Apply filters and repopulate the table"""
        self.populate_matches_table()

    def show_match_details(self):
        """Display details for the selected match"""
        selected_items = self.matches_table.selectedItems()
        if not selected_items:
            return
            
        # Get event ID and league from the first cell
        row = selected_items[0].row()
        time_item = self.matches_table.item(row, 0)
        
        event_id = time_item.data(Qt.ItemDataRole.UserRole)
        league_name = time_item.data(Qt.ItemDataRole.UserRole + 1)
        
        if not event_id or not league_name:
            return
            
        # Get match data
        league_data = self.data.get(league_name, {})
        
        # Find match in results
        match_data = None
        for match in league_data.get('results', []):
            if match.get('id') == event_id:
                match_data = match
                break
                
        if not match_data:
            return
            
        # Update match title
        home_name = match_data.get('home', {}).get('name', '')
        away_name = match_data.get('away', {}).get('name', '')
        self.match_title.setText(f"{home_name} vs {away_name}")
        
        # Update summary info
        self.summary_labels["league_val"].setText(league_name)
        self.summary_labels["time_val"].setText(match_data.get('formatted_time', '-'))
        self.summary_labels["home_val"].setText(home_name)
        self.summary_labels["away_val"].setText(away_name)
        
        # Find market data
        market_data = None
        for market in league_data.get('markets', []):
            if market.get('event_id') == event_id:
                market_data = market.get('markets', {})
                break
                
        # Update odds if available
        if market_data:
            # Moneyline
            moneyline_odds = market_data.get('odds', {}).get('92_1', [])
            if moneyline_odds and len(moneyline_odds) > 0:
                self.odds_labels["moneyline_home"].setText(moneyline_odds[0].get('home_od', '-'))
                self.odds_labels["moneyline_away"].setText(moneyline_odds[0].get('away_od', '-'))
                
                # Color code based on value
                try:
                    home_odd = float(moneyline_odds[0].get('home_od', 0))
                    away_odd = float(moneyline_odds[0].get('away_od', 0))
                    
                    # Set color based on odds value
                    home_color = self.get_odds_color(home_odd)
                    away_color = self.get_odds_color(away_odd)
                    
                    self.odds_labels["moneyline_home"].setStyleSheet(f"color: {home_color};")
                    self.odds_labels["moneyline_away"].setStyleSheet(f"color: {away_color};")
                except:
                    pass
            
            # Spread
            spread_odds = market_data.get('odds', {}).get('92_2', [])
            if spread_odds and len(spread_odds) > 0:
                self.odds_labels["spread_home"].setText(spread_odds[0].get('home_od', '-'))
                self.odds_labels["spread_away"].setText(spread_odds[0].get('away_od', '-'))
                self.odds_labels["spread_handicap"].setText(spread_odds[0].get('handicap', '-'))
            
            # Totals
            total_odds = market_data.get('odds', {}).get('92_3', [])
            if total_odds and len(total_odds) > 0:
                self.odds_labels["total_over"].setText(total_odds[0].get('over_od', '-'))
                self.odds_labels["total_under"].setText(total_odds[0].get('under_od', '-'))
                self.odds_labels["total_points"].setText(total_odds[0].get('handicap', '-'))
        else:
            # Clear odds
            for label in self.odds_labels.values():
                label.setText("-")
                label.setStyleSheet("")
                
        # Find H2H data
        h2h_data = None
        for history in league_data.get('history', []):
            if history.get('event_id') == event_id:
                h2h_data = history.get('history', {})
                break
                
        # Update H2H table
        self.update_h2h_data(h2h_data, home_name, away_name)

    def update_h2h_data(self, h2h_data, home_name, away_name):
        """Update head-to-head data display"""
        # Clear table
        self.h2h_table.setRowCount(0)
        
        if not h2h_data or not h2h_data.get('h2h'):
            self.h2h_summary.setText("No head-to-head data available")
            
            # Reset win probability
            self.home_prob.setText("Home: 50%")
            self.away_prob.setText("Away: 50%")
            
            return
            
        # Count wins
        home_wins = 0
        away_wins = 0
        total_matches = 0
        
        # Populate H2H table
        h2h_matches = h2h_data.get('h2h', [])
        
        for i, match in enumerate(h2h_matches):
            self.h2h_table.insertRow(i)
            
            # Get match details
            match_time = match.get('time', 0)
            match_date = datetime.fromtimestamp(int(match_time)).strftime("%Y-%m-%d")
            
            h2h_home = match.get('home', {}).get('name', '')
            h2h_away = match.get('away', {}).get('name', '')
            score = match.get('ss', '-')
            
            # Add to table
            date_item = QTableWidgetItem(match_date)
            home_item = QTableWidgetItem(h2h_home)
            away_item = QTableWidgetItem(h2h_away)
            score_item = QTableWidgetItem(score)
            
            self.h2h_table.setItem(i, 0, date_item)
            self.h2h_table.setItem(i, 1, home_item)
            self.h2h_table.setItem(i, 2, away_item)
            self.h2h_table.setItem(i, 3, score_item)
            
            # Count wins for probability
            total_matches += 1
            
            if score and '-' in score:
                home_score, away_score = score.split('-')
                try:
                    # Check who won
                    if int(home_score) > int(away_score):
                        # Home team won
                        if h2h_home == home_name:
                            home_wins += 1
                        else:
                            away_wins += 1
                    else:
                        # Away team won
                        if h2h_away == home_name:
                            home_wins += 1
                        else:
                            away_wins += 1
                except:
                    pass
        
        # Update H2H summary
        self.h2h_summary.setText(f"Head-to-Head: {len(h2h_matches)} previous matches")
        
        # Update win probability
        if total_matches > 0:
            home_pct = int((home_wins / total_matches) * 100)
            away_pct = int((away_wins / total_matches) * 100)
            
            # Account for no wins case
            if home_pct == 0 and away_pct == 0:
                home_pct = 50
                away_pct = 50
            
            self.home_prob.setText(f"Home: {home_pct}%")
            self.away_prob.setText(f"Away: {away_pct}%")
            
            # Set color based on probability
            if home_pct > 60:
                self.home_prob.setStyleSheet("color: #2ecc71; font-weight: bold;")
            elif home_pct < 40:
                self.home_prob.setStyleSheet("color: #e74c3c; font-weight: bold;")
            else:
                self.home_prob.setStyleSheet("color: #f39c12; font-weight: bold;")
                
            if away_pct > 60:
                self.away_prob.setStyleSheet("color: #2ecc71; font-weight: bold;")
            elif away_pct < 40:
                self.away_prob.setStyleSheet("color: #e74c3c; font-weight: bold;")
            else:
                self.away_prob.setStyleSheet("color: #f39c12; font-weight: bold;")
                
    def get_odds_color(self, odds_value):
        """Return a color based on odds value"""
        if odds_value <= 1.5:
            return "#2ecc71"  # Green - strong favorite
        elif odds_value <= 2.0:
            return "#f39c12"  # Orange - slight favorite
        elif odds_value <= 3.0:
            return "#e67e22"  # Dark orange - slight underdog
        else:
            return "#e74c3c"  # Red - strong underdog
            
# Main application entry point
def main():
    app = QApplication(sys.argv)
    window = TableTennisGUI()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
