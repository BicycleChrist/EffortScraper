import qasync
import asyncio
import json
from datetime import datetime
import aiohttp
from math import pi # No clue wtf is going on here but im going with it
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QIcon,QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QHBoxLayout, QFrame, QSizePolicy, 
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query
from marketKeys import *


#TODO: MMA (Mixed Marital Arts) Markets ouput is nuked, gotta investigate that one
#TODO: Auto update cuts off last line and errors-out due to progress-bar apparently no longer existing.
# League market configurations
MAJOR_PROP_LEAGUES = {
    "basketball_nba": NBA_MARKETS,
    "baseball_mlb": MLB_MARKETS,
    "icehockey_nhl": NHL_MARKETS,
    "football_nfl": NFL_MARKETS,
    "aussierules_afl": AFL_MARKETS,
    "soccer_usa_mls": SOCCER_MARKETS
}

REGULAR_MARKETS = {"spreads"} # "h2h", "spreads", "totals"

class ColoredTableItem(QTableWidgetItem):
    """Custom table item that stores game ID for color coordination"""
    def __init__(self, text, game_id):
        super().__init__(text)
        self.game_id = game_id




class LeagueTabData:
    """Manages data and display state for each league tab"""
    def __init__(self, league_name, sport_key):
        self.league_name = league_name
        self.sport_key = sport_key
        self.num_rows = 0
        self.num_cols = 0
        self.table_rows = []
        self.table_data = {}
        self.game_colors = {}
        self.current_color_index = 0
        self.bookmakers = []
        self.previous_data = {}
        self.color_palette = [
            QColor(232, 240, 254),  # Sky Blue
            QColor(240, 247, 255),  # Ice Blue
            QColor(230, 255, 230),  # Mint
            QColor(240, 255, 240),  # Honeydew
            QColor(255, 240, 240),  # Misty Rose
            QColor(255, 245, 245),  # Lavender Blush
            QColor(245, 240, 255),  # Lavender
            QColor(240, 230, 255),  # Periwinkle
            QColor(255, 255, 240),  # Ivory
            QColor(255, 250, 240),  # Floral White
            QColor(240, 255, 255),  # Azure
            QColor(245, 255, 250),  # Mint Cream
            QColor(255, 245, 230),  # Peach
            QColor(245, 245, 245),  # White Smoke
            QColor(240, 248, 255),  # Alice Blue
            QColor(248, 248, 255),  # Ghost White
            QColor(255, 248, 220),  # Cornsilk
            QColor(240, 255, 244),  # Soft Mint
            QColor(255, 240, 245),  # Pink Snow
            QColor(245, 240, 240),  # Soft Pink
        ]
        self.table_widget = None
        self.last_update_time = None
        
    def create_table_widget(self):
        """Create and configure a new table widget for this league"""
        self.table_widget = QTableWidget()
        self.table_widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_widget.setMinimumSize(1000, 800)
        self.table_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table_widget.updateGeometry()
        # Style the header
        header = self.table_widget.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #2C3E50;
                color: white;
                padding: 4px;
                border: 1px solid #34495E;
            }
        """)
        return self.table_widget
    
    def get_game_color(self, game_id):
        """Get or assign a color for a specific game"""
        if game_id not in self.game_colors:
            self.game_colors[game_id] = self.color_palette[self.current_color_index]
            self.current_color_index = (self.current_color_index + 1) % len(self.color_palette)
        return self.game_colors[game_id]
        



#TODO: Make this work, spinbox not allowing for interval selection for update interval 
class UpdateProgressIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.progress = 0
        
    def setProgress(self, value):
        self.progress = value
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw background circle
        painter.setPen(QPen(QColor("#e9ecef"), 2))
        painter.drawEllipse(2, 2, 16, 16)
        
        # Draw progress arc
        painter.setPen(QPen(QColor("#007bff"), 2))
        angle = int(-self.progress * 360)
        painter.drawArc(2, 2, 16, 16, 90 * 16, angle * 16)




class DataManager(QObject):
    """Manages data fetching and processing for odds data"""
    games_updated = pyqtSignal(list)
    odds_updated = pyqtSignal(dict, str)  # Added league_name parameter

    def __init__(self):
        super().__init__()
        self.sport_key = None
        self.prop_client = None
        self.league_map = {}

    async def fetch_leagues(self):
        """Fetch available leagues from the API"""
        return await asyncio.to_thread(league_query)

    async def fetch_odds(self, sport, region, markets, odds_format, date_format):
        """Fetch odds for a specific sport, region, and markets"""
        return await asyncio.to_thread(odds_query, sport, region, markets, odds_format, date_format)




class ModernOddsWindow(QMainWindow):
    """Main window for displaying and managing odds data"""
    BUTTON_STYLE = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 1px solid #0056b3;
            padding: 5px 10px;
            margin-right: 5px;
            border-radius: 4px;
        }
        QPushButton:checked {
            background-color: #28a745;  /* Green */
            color: white;
            border: 1px solid #218838;
        }
        QPushButton:disabled {
            background-color: #e9ecef;
            color: #6c757d;
            border-color: #dee2e6;
        }
    """

    def __init__(self):
        super().__init__()
        self.timer = QTimer()
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.selected_markets = {"spreads"}  # Initialize with default market
        self.init_ui()
        self.connect_signals()

    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)
        self.setWindowIcon(QIcon("/home/retupmoc/Desktop/EffortScraper/OddsAPI/AppIcon.png")) 
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # League selection dropdown
        self.league_selector = QComboBox()
        layout.addWidget(QLabel("Select League:"))
        layout.addWidget(self.league_selector)
        
        # Market selection and refresh controls
        controls_container = QWidget()
        controls_layout = QHBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        # Left side container for regular markets
        left_container = QWidget()
        left_layout = QHBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Create regular market buttons
        self.market_buttons = {}
        for market in ["h2h", "spreads", "totals"]:
            btn = QPushButton(market.capitalize())
            btn.setCheckable(True)
            btn.setChecked(market in self.selected_markets)
            btn.setObjectName(f"market_{market}")
            self.market_buttons[market] = btn
            btn.setStyleSheet(self.BUTTON_STYLE)  # Apply style
            left_layout.addWidget(btn)

        # Add fetch button
        self.refresh_btn = QPushButton("Fetch Odds")
        self.refresh_btn.setFixedWidth(120)
        self.refresh_btn.setStyleSheet(self.BUTTON_STYLE)  # Apply style
        left_layout.addWidget(self.refresh_btn)

        # Right side container for props
        right_container = QWidget()
        right_layout = QHBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Add props button
        self.props_button = QPushButton("Props")
        self.props_button.setCheckable(True)
        self.props_button.setObjectName("market_props")
        self.props_button.setEnabled(False)
        self.props_button.setStyleSheet(self.BUTTON_STYLE)  # Apply style
        self.market_buttons["props"] = self.props_button
        right_layout.addWidget(self.props_button)

        # Add props availability label
        self.props_availability_label = QLabel("No Props available for this league")
        self.props_availability_label.setStyleSheet("color: #6c757d; font-style: italic;")
        self.props_availability_label.setVisible(False)
        right_layout.addWidget(self.props_availability_label)
        right_layout.addStretch()

        # Add both containers to main controls layout
        controls_layout.addWidget(left_container)
        controls_layout.addWidget(right_container)

        # Add controls container to main layout
        layout.addWidget(controls_container)
        
        # Progress bar
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        
        # Tab widget for different leagues
        self.tab_widget = QTabWidget()
        layout.addWidget(QLabel("Odds:"))
        layout.addWidget(self.tab_widget)
        
        # Auto-update controls
        update_controls_layout = QHBoxLayout()
        
        self.auto_update_check = QCheckBox("Auto-Update Odds")
        update_controls_layout.addWidget(self.auto_update_check)
        
        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(5)
        self.update_interval.setEnabled(True)
        update_controls_layout.addWidget(QLabel("Update Interval:"))
        update_controls_layout.addWidget(self.update_interval)
        
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.status_frame_layout = QHBoxLayout(self.status_frame)
        
        self.last_update_label = QLabel("Last Update: Never")
        self.last_update_label.setStyleSheet("color: #6c757d;")
        
        self.update_status = QLabel("")
        self.update_status.setStyleSheet("""
            QLabel {
                padding: 5px;
                border-radius: 3px;
                background-color: #f8f9fa;
            }
        """)
        
        # Progress indicator
        self.progress_indicator = UpdateProgressIndicator()
        self.status_frame_layout.addWidget(self.progress_indicator)
        self.status_frame_layout.addWidget(self.last_update_label)
        self.status_frame_layout.addWidget(self.update_status)
        update_controls_layout.addWidget(self.status_frame)
        update_controls_layout.addStretch()
        
        layout.addLayout(update_controls_layout)

    def update_market_selection(self):
        """Update selected markets based on button states"""
        self.selected_markets = {market for market, btn in self.market_buttons.items() 
                               if btn.isChecked() and btn.isEnabled()}
        print("Selected markets:", self.selected_markets)

    def handle_league_change(self):
        """Handle league selection changes"""
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)
        
        # Enable/disable props button based on league
        has_props = sport_key in MAJOR_PROP_LEAGUES
        self.props_button.setEnabled(has_props)
        self.props_availability_label.setVisible(not has_props)
        
        # Uncheck props button if league doesn't support it
        if not has_props and self.props_button.isChecked():
            self.props_button.setChecked(False)
            self.update_market_selection()

    def get_valid_markets(self, sport_key):
        """Get valid markets for the selected sport"""
        valid_markets = REGULAR_MARKETS.copy()
        if sport_key in MAJOR_PROP_LEAGUES:
            valid_markets.update(MAJOR_PROP_LEAGUES[sport_key].keys())
        return valid_markets

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.league_selector.currentTextChanged.connect(self.handle_league_change)
        for btn in self.market_buttons.values():
            btn.toggled.connect(self.update_market_selection)
        self.refresh_btn.setStyleSheet(self.BUTTON_STYLE)
        self.refresh_btn.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.update_interval.valueChanged.connect(self.update_timer_interval)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)
        self.timer.timeout.connect(self.refresh_data)

            
        

    # Rest of the class methods remain unchanged...
    # (RestartTimer, update_status_text, initialize, populate_leagues, 
    # handle_tab_change, create_league_tab, format_market_label, 
    # format_price, display_odds, update_table_display, 
    # toggle_auto_update, update_timer_interval, refresh_data)

    def RestartTimer(self):
        interval_ms = self.update_interval.value() * 60 * 1000
        self.timer.start(interval_ms)
        return interval_ms

    
    def update_status_text(self):
        """Update the status text showing time until next update"""
        if not self.auto_update_check.isChecked():
            self.update_status.setText("")
            self.progress_indicator.setProgress(0)
            return
    
        remaining_time = self.timer.remainingTime() // 1000  # Convert to milliseconds to seconds
        total_time = self.update_interval.value() * 60  # Total time in seconds
        progress = 1 - (remaining_time / total_time)  # Calculate progress (0 to 1)
        
        minutes = remaining_time // 60
        seconds = remaining_time % 60
        
        # Color coding based on remaining time
        if remaining_time < 60:  # Less than 1 minute
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
        elif remaining_time < 180:  # Less than 3 minutes
            self.update_status.setStyleSheet("background-color: #ffc107; color: #000;")
        else:
            self.update_status.setStyleSheet("background-color: #28a745; color: white;")
        
        self.update_status.setText(f"Next update in: {minutes}m {seconds}s")
        self.progress_indicator.setProgress(progress)
    
    
    
    
    async def initialize(self):
        """Initialize the window with league data"""
        await self.populate_leagues()
        self.leagues_loaded = True

    async def populate_leagues(self):
        """Fetch and populate leagues in the dropdown"""
        leagues = await self.data_manager.fetch_leagues()
        print("Fetched leagues:", leagues)
        self.league_selector.clear()
        self.data_manager.league_map.clear()
    
        for sport_category, league_list in leagues.items():
            for league in league_list:
                self.league_selector.addItem(league['title'])
                self.data_manager.league_map[league['title']] = league['key']
        
        # Call handle_league_change after populating
        if self.league_selector.count() > 0:
            self.handle_league_change()

    def handle_tab_change(self, index):
        """Handle tab switching events"""
        if index >= 0:
            self.current_league = self.tab_widget.tabText(index)

    def create_league_tab(self, league_name, sport_key):
        """Create a new tab for a league"""
        if league_name not in self.league_tabs:
            tab_data = LeagueTabData(league_name, sport_key)
            table_widget = tab_data.create_table_widget()
            self.tab_widget.addTab(table_widget, league_name)
            self.league_tabs[league_name] = tab_data
            self.current_league = league_name
            self.tab_widget.setCurrentIndex(self.tab_widget.count() - 1)
        return self.league_tabs[league_name]

    # Proper formatting for 3-way markets (likely redundant, but im tilt)
    def format_market_label(self, market_key, outcome):
        """Format the market label based on market type and outcome"""
        if market_key == 'h2h':
            return f"Moneyline: {outcome['name']}"
        elif market_key == 'h2h_3way':
            return f"3-Way Moneyline: {outcome['name']}"
        elif market_key == 'spreads':
            return f"Spread: {outcome['name']} {outcome.get('point', '')}"
        elif market_key == 'totals':
            return f"Total {outcome['name']} {outcome.get('point', '')}"
        return f"{market_key}: {outcome['name']}"



    def format_price(self, outcome):
        """Format the price display including point if available"""
        price = str(outcome.get('price', ''))
        if 'point' in outcome:
            price += f" ({outcome['point']})"
        return price

    def display_odds(self, odds: dict, league_name: str):
        """Update odds display efficiently by only processing changed data"""
        if not odds or 'bookmakers' not in odds:
            return
        
        tab_data = self.league_tabs.get(league_name)
        if not tab_data:
            return
        
        game_id = odds.get('id', 'unknown_game')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')
        
        # Collect bookmaker names
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in tab_data.bookmakers:
                tab_data.bookmakers.append(bm_title)
        
        # Add a header row for the game if it doesn't exist
        game_header = f"Game: {home_team} vs {away_team}"
        if game_header not in tab_data.table_rows:
            tab_data.table_rows.append(game_header)
            tab_data.table_data[game_header] = {'is_header': True, 'game_id': game_id}
            tab_data.num_rows += 1
        
        # Process markets and track changes
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                    
                    # Add new row if needed
                    if unique_label not in tab_data.table_rows:
                        tab_data.table_rows.append(unique_label)
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                        tab_data.num_rows += 1
                    
                    # Update price only if changed
                    price = self.format_price(outcome)
                    if unique_label not in tab_data.table_data:
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                    tab_data.table_data[unique_label][bm_title] = price
        
        self.update_table_display(tab_data)




    def update_table_display(self, tab_data: LeagueTabData):
        """Update table display with improved price change highlighting"""
        table = tab_data.table_widget
        current_rows = table.rowCount()
        current_cols = table.columnCount()
        
        # Update table structure if needed
        expected_cols = len(tab_data.bookmakers) + 1
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
        
        expected_rows = len(tab_data.table_rows)
        if current_rows != expected_rows:
            table.setRowCount(expected_rows)
        
        needs_resize = False
        
        for row_idx, row_label in enumerate(tab_data.table_rows):
            row_data = tab_data.table_data[row_label]
            game_id = row_data['game_id']
            color = tab_data.get_game_color(game_id)
            
            # Create or update row header if needed
            header_item = table.item(row_idx, 0)
            if not header_item:
                header_item = ColoredTableItem(row_label, game_id)
                table.setItem(row_idx, 0, header_item)
                needs_resize = True
            
            # Apply header styling with black text
            if row_data.get('is_header'):
                font = QFont()
                font.setBold(True)
                header_item.setFont(font)
                header_item.setBackground(color)
                header_item.setForeground(QColor('black'))
            else:
                market_color = QColor(color)
                market_color.setAlpha(230)
                header_item.setBackground(market_color)
                header_item.setForeground(QColor('black'))
            
            # Update bookmaker columns
            for col_idx, bm in enumerate(tab_data.bookmakers, 1):
                current_value = row_data.get(bm, "")
                previous_value = tab_data.previous_data.get((row_label, bm))
                
                item = table.item(row_idx, col_idx)
                if not item:
                    item = ColoredTableItem(current_value, game_id)
                    table.setItem(row_idx, col_idx, item)
                    needs_resize = True
                
                # Only update if value has changed
                if current_value != previous_value and previous_value is not None:
                    item.setText(current_value)
                    
                    # Parse the odds values for comparison
                    try:
                        current_odds = float(current_value.split()[0])
                        previous_odds = float(previous_value.split()[0])
                        
                        # Better odds (higher value) = green, worse odds = red
                        if current_odds > previous_odds:
                            highlight_color = QColor(0, 200, 0, 180)  # Semi-transparent green
                        else:
                            highlight_color = QColor(200, 0, 0, 180)  # Semi-transparent red
                        
                        item.setBackground(highlight_color)
                        item.setForeground(QColor('black'))  # Keep text black for readability
                        
                        # Reset background after 5 seconds
                        market_color = QColor(color)
                        market_color.setAlpha(230)
                        QTimer.singleShot(5000, lambda i=item, c=market_color: (
                            i.setBackground(c),
                            i.setForeground(QColor('black'))
                        ))
                    except (ValueError, IndexError):
                        # If we can't parse the odds, just update without highlighting
                        item.setText(current_value)
                    
                    tab_data.previous_data[(row_label, bm)] = current_value
                    needs_resize = True
                elif current_value != previous_value:
                    # First time seeing this value
                    item.setText(current_value)
                    tab_data.previous_data[(row_label, bm)] = current_value
                
                # Maintain consistent background and text color when not highlighted
                if not row_data.get('is_header') and item.background().color().alpha() != 180:  # Don't override highlight
                    market_color = QColor(color)
                    market_color.setAlpha(230)
                    item.setBackground(market_color)
                    item.setForeground(QColor('black'))
        
        # Only resize if needed
        if needs_resize:
            table.resizeColumnsToContents()
            table.resizeRowsToContents()
    
    
    def toggle_auto_update(self):
        """Enable or disable auto-update functionality"""
        if self.auto_update_check.isChecked():
           self.update_interval.setEnabled(True)
           interval_ms = self.RestartTimer()
           print(f"next update in: {int(interval_ms/1000)} seconds")
        else:
            self.timer.stop()
            print("timer stopped")
        # self.update_status_text() # crashes
        
    
    def update_timer_interval(self):
        """Update the timer interval when spinbox value changes"""
        if self.auto_update_check.isChecked():
            interval_ms = self.update_interval.value() * 60 * 1000
            self.timer.start(interval_ms)
            print(f"timer interval updated: {interval_ms}")
        # self.update_status_text() # crashes

    @qasync.asyncSlot() 
    # This function might just be too fucking much
    async def refresh_data(self):
        """Fetch and update odds data asynchronously"""
        if not self.leagues_loaded:
            print("Leagues not loaded yet. Please wait.")
            return
    
        self.progress.setValue(0)
        self.refresh_btn.setEnabled(False)
        
        # Update last update time
        current_time = datetime.now().strftime("%H:%M:%S")
        self.last_update_label.setText(f"Last Update: {current_time}")
        
        # Update status during refresh
        self.update_status.setStyleSheet("background-color: #007bff; color: black;")
        self.update_status.setText("Updating odds...")
        
        try:
            selected_league = self.league_selector.currentText()
            sport_key = self.data_manager.league_map.get(selected_league)
            if not sport_key:
                print(f"No valid sport key found for the selected league: {selected_league}")
                self.update_status.setStyleSheet("background-color: #dc3545; color: black;")
                self.update_status.setText("Error: Invalid league selection")
                return
    
            # Create or get existing tab
            tab_data = self.create_league_tab(selected_league, sport_key)
            
            self.data_manager.sport_key = sport_key
            self.data_manager.prop_client = PropClient(sport_key)
    
            # Reset tab data for fresh query
            tab_data.num_rows = 0
            tab_data.table_rows.clear()
            tab_data.table_data.clear()
            tab_data.game_colors.clear()
            tab_data.current_color_index = 0
    
            async with aiohttp.ClientSession() as session:
                games = await self.data_manager.prop_client.get_games(session)
            print(f"Fetched games response: {games}")
    
            if not isinstance(games, list):
                print(f"Unexpected response from get_games(): {games}")
                self.update_status.setStyleSheet("background-color: #dc3545; color: black;")
                self.update_status.setText("Error: Invalid response format")
                return
    
            total_games = len(games)
            for index, game in enumerate(games):
                game_id = game.get('id', '')
                if not game_id:
                    print("No game ID found in the response.")
                    continue
    
                # Update progress based on game processing
                progress_value = int((index + 1) / total_games * 100)
                self.progress.setValue(progress_value)
    
                async with aiohttp.ClientSession() as session:
                    available_markets = self.selected_markets.copy()
                    odds = await self.data_manager.prop_client.get_event_odds(
                        session, game_id, available_markets, region="us" # us,us2,eu,au,uk
                    )
                
                print(selected_league)
                print(game_id)
                print(odds)
                self.data_manager.odds_updated.emit(odds, selected_league)
    
            self.progress.setValue(100)
            
            # Reset status text if auto-update is enabled
            if self.auto_update_check.isChecked():
                self.update_status_text()
                self.RestartTimer()
            else:
                self.update_status.setStyleSheet("background-color: #28a745; color: white;")
                self.update_status.setText("Update complete")
                
        except aiohttp.ClientError as e:
            print(f"Network error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("Network error occurred")
        except Exception as e:
            print(f"Error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("An error occurred")
        finally:
            self.refresh_btn.setEnabled(True)
            # Reset progress bar
            QTimer.singleShot(2000, lambda: self.progress.setValue(0))
            # Reset error messages after a delay
            if not self.auto_update_check.isChecked():
                QTimer.singleShot(5000, lambda: self.update_status.setText(""))


async def main():
    """Main function to start the application"""
    app = QApplication([])
    
    # Create main window before setting up event loop
    window = ModernOddsWindow()
    window.show()
    
    # Set up event loop after creating window but before initialization
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    # Initialize window
    await window.initialize()
    await loop.run_forever() # Run event loop
    return 


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
