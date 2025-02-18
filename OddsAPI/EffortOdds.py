import qasync
import asyncio
import json
from datetime import datetime
import aiohttp
from math import pi # No clue wtf is going on here but im going with it
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QHBoxLayout, QFrame, QSizePolicy
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query
from marketKeys import *


#TODO: MMA (Mixed Marital Arts) Markets ouput is nuked, gotta investigate that one
#TODO: Auto update cuts off last line and errors-out due to progress-bar apparently no longer existing.
#TODO: 
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
    def __init__(self):
        super().__init__()
        self.timer = QTimer()
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.init_ui()
        self.connect_signals()

    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)
        #TODO: Change this path for the icon
        self.setWindowIcon(QIcon("/home/retupmoc/Desktop/EffortScraper/OddsAPI/AppIcon.png")) 
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # League selection dropdown
        self.league_selector = QComboBox()
        layout.addWidget(QLabel("Select League:"))
        layout.addWidget(self.league_selector)
        
        # Refresh button
        self.refresh_btn = QPushButton("Fetch Odds")
        layout.addWidget(self.refresh_btn)
        
        # Progress bar
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        
        # Tab widget for different leagues
        self.tab_widget = QTabWidget()
        layout.addWidget(QLabel("Odds:"))
        layout.addWidget(self.tab_widget)
        
        # Auto-update controls
        update_controls_layout = QHBoxLayout()  # Create horizontal layout for controls
        
        self.auto_update_check = QCheckBox("Auto-Update Odds")
        update_controls_layout.addWidget(self.auto_update_check)
        
        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(5)
        self.update_interval.setEnabled(True)
        update_controls_layout.addWidget(QLabel("Update Interval:"))
        update_controls_layout.addWidget(self.update_interval)
        
        # Create a frame for the status indicators
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.status_frame_layout = QHBoxLayout(self.status_frame)
        
        # Last update time
        self.last_update_label = QLabel("Last Update: Never")
        self.last_update_label.setStyleSheet("color: #6c757d;")
        
        # Status label
        self.update_status = QLabel("")
        self.update_status.setStyleSheet("""
            QLabel {
                padding: 5px;
                border-radius: 3px;
                background-color: #f8f9fa;
            }
        """)
        
        # Add widgets to the status frame
        self.status_frame_layout.addWidget(self.last_update_label)
        self.status_frame_layout.addWidget(self.update_status)
        update_controls_layout.addWidget(self.status_frame)
        
        update_controls_layout.addStretch()  # Add stretch to keep controls left-aligned
        
        layout.addLayout(update_controls_layout)

    def RestartTimer(self):
        interval_ms = self.update_interval.value() * 60 * 1000
        self.timer.start(interval_ms)
        return interval_ms

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.refresh_btn.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.update_interval.valueChanged.connect(self.update_timer_interval)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)
        self.timer.timeout.connect(self.refresh_data)
    
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
        if not odds or 'bookmakers' not in odds:
            return
        
        tab_data = self.league_tabs.get(league_name)
        if not tab_data:
            return
        
        game_id = odds.get('id', 'unknown_game')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')
        
        # Collect bookmaker names
        bookmakers = []
        for bm in odds['bookmakers']:
            if bm['title'] not in bookmakers:
                bookmakers.append(bm['title'])
        tab_data.num_cols = len(bookmakers)
        
        # Add a header row for the game
        game_header = f"Game: {home_team} vs {away_team}"
        if game_header not in tab_data.table_rows:
            tab_data.table_rows.append(game_header)
            tab_data.table_data[game_header] = {'is_header': True, 'game_id': game_id}
            tab_data.num_rows += 1
        
        # Process markets
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    # Make the row label unique by including game_id or teams
                    unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                    
                    if unique_label not in tab_data.table_rows:
                        tab_data.table_rows.append(unique_label)
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                        tab_data.num_rows += 1
                    
                    price = self.format_price(outcome)
                    tab_data.table_data[unique_label][bm_title] = price
        
        self.update_table_display(tab_data, bookmakers)



    def update_table_display(self, tab_data: LeagueTabData, bookmakers):
        """Update the table display with formatted odds data"""
        table = tab_data.table_widget
        table.setRowCount(tab_data.num_rows)
        table.setColumnCount(len(bookmakers) + 1)
        
        headers = ["Market/Outcome"] + bookmakers
        table.setHorizontalHeaderLabels(headers)
        
        for row_idx, row_label in enumerate(tab_data.table_rows):
            row_data = tab_data.table_data[row_label]
            game_id = row_data.get('game_id')
            
            # Create and style the row
            item = ColoredTableItem(row_label, game_id)
            if row_data.get('is_header'):
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            
            # Set background color and ensure text is black
            color = tab_data.get_game_color(game_id)
            item.setBackground(QBrush(color))
            item.setForeground(QBrush(QColor('black')))
            table.setItem(row_idx, 0, item)
            
            # Populate odds data
            if not row_data.get('is_header'):
                for col_idx, bm_title in enumerate(bookmakers, 1):
                    price = row_data.get(bm_title, "")
                    odds_item = ColoredTableItem(price, game_id)
                    odds_item.setBackground(QBrush(color))
                    odds_item.setForeground(QBrush(QColor('black')))
                    table.setItem(row_idx, col_idx, odds_item)

        # Optimize column widths
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(1, len(bookmakers) + 1):
            table.setColumnWidth(i, 120)
        
        table.resizeRowsToContents()
        table.resizeColumnsToContents()
        table.updateGeometry()
        table.update()
        print(f"num_rows: {tab_data.num_rows}")
        print(f"length of table_rows: {len(tab_data.table_rows)}")
    
    
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
        self.update_status.setStyleSheet("background-color: #007bff; color: white;")
        self.update_status.setText("Updating odds...")
        
        try:
            selected_league = self.league_selector.currentText()
            sport_key = self.data_manager.league_map.get(selected_league)
            if not sport_key:
                print(f"No valid sport key found for the selected league: {selected_league}")
                self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
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
                self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
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
                    available_markets = REGULAR_MARKETS.copy()
                    if sport_key in MAJOR_PROP_LEAGUES:
                        available_markets |= set(MAJOR_PROP_LEAGUES[sport_key].keys())
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
