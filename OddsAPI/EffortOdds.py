import qasync
import asyncio
import json
import aiohttp
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query
from marketKeys import *

#TODO: 3 way moneylines and the consequent draws need to be properly handled for the GUI
#TODO: Call function from BigQueryTest that contains this logic instead of OddsAPIQuery



# League market configurations
MAJOR_PROP_LEAGUES = {
    "basketball_nba": NBA_MARKETS,
    "baseball_mlb": MLB_MARKETS,
    "icehockey_nhl": NHL_MARKETS,
    "football_nfl": NFL_MARKETS,
    "aussierules_afl": AFL_MARKETS,
    "soccer_usa_mls": SOCCER_MARKETS
}

REGULAR_MARKETS = {"h2h", "spreads", "totals"} # "h2h", "spreads", "totals"

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
            QColor(232, 240, 254),  # Lighter blue
            QColor(255, 240, 240),  # Lighter red
            QColor(240, 255, 240),  # Lighter green
            QColor(255, 250, 240),  # Lighter yellow
            QColor(240, 240, 255),  # Lighter purple
        ]
        self.table_widget = None
        self.last_update_time = None

    def get_game_color(self, game_id):
        """Get or assign a color for a specific game"""
        if game_id not in self.game_colors:
            self.game_colors[game_id] = self.color_palette[self.current_color_index]
            self.current_color_index = (self.current_color_index + 1) % len(self.color_palette)
        return self.game_colors[game_id]

    def create_table_widget(self):
        """Create and configure a new table widget for this league"""
        self.table_widget = QTableWidget()
        self.table_widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_widget.setMinimumSize(1000, 800)
        
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
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.init_ui()
        self.connect_signals()
        self.timer = QTimer()

    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)

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
        self.auto_update_check = QCheckBox("Auto-Update Odds")
        layout.addWidget(self.auto_update_check)

        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(5)
        self.update_interval.setEnabled(False)
        layout.addWidget(QLabel("Update Interval:"))
        layout.addWidget(self.update_interval)

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.refresh_btn.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)

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

    def toggle_auto_update(self, state):
        """Enable or disable auto-update functionality"""
        if state == Qt.CheckState.Checked:
            self.update_interval.setEnabled(True)
            self.timer.timeout.connect(self.refresh_data)
            self.timer.start(self.update_interval.value() * 60 * 1000)
        else:
            self.update_interval.setEnabled(False)
            self.timer.stop()

    @qasync.asyncSlot()
    async def refresh_data(self):
        """Fetch and update odds data asynchronously"""
        if not self.leagues_loaded:
            print("Leagues not loaded yet. Please wait.")
            return

        self.progress.setValue(0)
        self.refresh_btn.setEnabled(False)
        try:
            selected_league = self.league_selector.currentText()
            sport_key = self.data_manager.league_map.get(selected_league)
            if not sport_key:
                print(f"No valid sport key found for the selected league: {selected_league}")
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
                return

            for game in games:
                game_id = game.get('id', '')
                if not game_id:
                    print("No game ID found in the response.")
                    return

                async with aiohttp.ClientSession() as session:
                    available_markets = REGULAR_MARKETS.copy()
                    if sport_key in MAJOR_PROP_LEAGUES:
                        available_markets |= set(MAJOR_PROP_LEAGUES[sport_key].keys())
                    odds = await self.data_manager.prop_client.get_event_odds(
                        session, game_id, available_markets, region="us,eu" # us,us2,eu,au,uk
                    )
                
                self.data_manager.odds_updated.emit(odds, selected_league)

            self.progress.setValue(100)
        except Exception as e:
            print(f"Error: {e}")
        finally:
            self.refresh_btn.setEnabled(True)


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
    
    # Run event loop
    return await loop.run_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Handle clean shutdown
        pass
