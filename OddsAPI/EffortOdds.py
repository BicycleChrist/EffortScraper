import qasync
import asyncio
import json  # Added for JSON parsing
import aiohttp  # Added for HTTP session handling
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query
from marketKeys import *

MAJOR_PROP_LEAGUES = {
    "basketball_nba": NBA_MARKETS,
    "baseball_mlb": MLB_MARKETS,
    "icehockey_nhl": NHL_MARKETS,
    "football_nfl": NFL_MARKETS,
    "aussierules_afl": AFL_MARKETS,
    "soccer_usa_mls": SOCCER_MARKETS
}

REGULAR_MARKETS = {"h2h", "spreads", "totals"}

class DataManager(QObject):
    games_updated = pyqtSignal(list)
    odds_updated = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.sport_key = None
        self.prop_client = None
        self.league_map = {}

    async def fetch_leagues(self):
        """Fetch available leagues from the API."""
        return await asyncio.to_thread(league_query)

    async def fetch_odds(self, sport, region, markets, odds_format, date_format):
        """Fetch odds for a specific sport, region, and markets."""
        return await asyncio.to_thread(odds_query, sport, region, markets, odds_format, date_format)


# TODO: create tabs for each league/query
# TODO: save data
class ModernOddsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.data_manager = DataManager()
        self.init_ui()
        self.connect_signals()
        self.timer = QTimer()
        self.leagues_loaded = False
        self.num_rows = 0
        self.num_cols = 0
        self.table_rows = []
        self.table_data = {}

    async def initialize(self):
        await self.populate_leagues()
        self.leagues_loaded = True

    async def populate_leagues(self):
        """Fetch and populate leagues in the dropdown."""
        leagues = await self.data_manager.fetch_leagues()
        print("Fetched leagues:", leagues)
        self.league_selector.clear()
        self.data_manager.league_map.clear()

        for sport_category, league_list in leagues.items():
            for league in league_list:
                self.league_selector.addItem(league['title'])
                self.data_manager.league_map[league['title']] = league['key']

    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("SharpBook Pro")
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

        # Odds display area
        self.odds_display = QTableWidget()
        self.odds_display.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("Odds:"))
        layout.addWidget(self.odds_display)
        self.odds_display.setMinimumSize(1000, 800)

        # Auto-update checkbox and interval
        self.auto_update_check = QCheckBox("Auto-Update Odds")
        layout.addWidget(self.auto_update_check)

        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(5)
        self.update_interval.setEnabled(False)  # Disabled by default
        layout.addWidget(QLabel("Update Interval:"))
        layout.addWidget(self.update_interval)

    def connect_signals(self):
        """Connect UI signals to their respective slots."""
        self.refresh_btn.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.data_manager.odds_updated.connect(self.display_odds)

    def display_odds(self, odds: dict):
        """Display fetched odds in the QTableWidget."""
        # self.odds_display.clear()
        # self.odds_display.setRowCount(0)
        # self.odds_display.setColumnCount(0)
        print(odds)
        print("-"*40)
        
        if not odds or 'bookmakers' not in odds:
            return

        # Extract unique bookmakers in order
        bookmakers = []
        for bm in odds['bookmakers']:
            if bm['title'] not in bookmakers:
                bookmakers.append(bm['title'])
        self.num_cols += len(bookmakers)

        # Collect all row labels and map bookmaker odds
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            print(bm_title)
            for market in bm['markets']:
                print(market)
                market_key = market['key']
                for outcome in market['outcomes']:
                    print(outcome)
                    # Generate row label based on market type
                    if market_key == 'h2h':
                        row_label = f"Moneyline: {outcome['name']}"
                    elif market_key == 'spreads':
                        point = outcome.get('point', '')
                        row_label = f"Spread: {outcome['name']} {point}"
                    elif market_key == 'totals':
                        point = outcome.get('point', '')
                        row_label = f"Total {outcome['name']} {point}"
                    else:
                        row_label = f"{market_key}: {outcome['name']}"

                    if row_label not in self.table_rows:
                        self.table_rows.append(row_label)
                        self.table_data[row_label] = {}
                        self.num_rows += 1

                    # Store price for this bookmaker
                    price = str(outcome.get('price', ''))
                    if 'point' in outcome:
                        price += f" ({outcome['point']})"
                    self.table_data[row_label][bm_title] = price
        
        print(self.table_rows)
        print("-"*40)
        print(self.table_data)
        # Configure table dimensions
        self.odds_display.setRowCount(self.num_rows)
        self.odds_display.setColumnCount(self.num_cols + 1)  # +1 for market column

        # Set headers
        headers = ["Market/Outcome"] + bookmakers
        self.odds_display.setHorizontalHeaderLabels(headers)

        # Populate rows
        for row_idx, row_label in enumerate(self.table_rows):
            self.odds_display.setItem(row_idx, 0, QTableWidgetItem(row_label))
            for col_idx, bm_title in enumerate(bookmakers, 1):
                price = self.table_data[row_label].get(bm_title, "")
                self.odds_display.setItem(row_idx, col_idx, QTableWidgetItem(price))

        # self.odds_display.resizeColumnsToContents()
        for i in range(len(self.table_rows)):
            self.odds_display.setColumnWidth(i, 250)
    
    def toggle_auto_update(self, state):
        """Enable or disable auto-update functionality."""
        if state == Qt.CheckState.Checked:
            self.update_interval.setEnabled(True)
            self.timer.timeout.connect(self.refresh_data)
            self.timer.start(self.update_interval.value() * 60 * 1000)
        else:
            self.update_interval.setEnabled(False)
            self.timer.stop()
    
    
    @qasync.asyncSlot()
    async def refresh_data(self):
        """Fetch and update odds data."""
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

            self.data_manager.sport_key = sport_key
            self.data_manager.prop_client = PropClient(sport_key)

            async with aiohttp.ClientSession() as session:
                games = await self.data_manager.prop_client.get_games(session)
            print(f"Fetched games response: {games}")

            if not isinstance(games, list):
                print(f"Unexpected response from get_games(): {games}")
                return
            
            # need to add each game to display
            # TODO: reset the table dimensions and display for different queries
            for game in games:
                game_id = game.get('id', '')
                if not game_id:
                    print("No game ID found in the response.")
                    return

                async with aiohttp.ClientSession() as session:
                    available_markets = REGULAR_MARKETS.copy()
                    if sport_key in MAJOR_PROP_LEAGUES:
                        available_markets |= set(MAJOR_PROP_LEAGUES[sport_key].keys())
                    # odds = await self.data_manager.fetch_odds(sport_key, "us", available_markets, "american", "iso")
                    odds = await self.data_manager.prop_client.get_event_odds(session, game_id, available_markets, region="us,us2,eu,au")
                
                self.data_manager.odds_updated.emit(odds)
                # if isinstance(odds, dict):
                #     self.data_manager.odds_updated.emit(odds)
                # else:
                #     print(f"Unexpected response from get_event_odds(): {odds}")
            self.progress.setValue(100)
        except Exception as e:
            print(f"Error: {e}")
        finally:
            self.refresh_btn.setEnabled(True)



async def main():
    """Main function to start the application."""
    app = QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ModernOddsWindow()
    window.show()

    await window.initialize()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
