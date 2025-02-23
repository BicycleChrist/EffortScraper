import qasync
import aiohttp
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QFont, QPen, QPainter
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QProgressBar, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy
)
from PropQuery import PropClient
from marketKeys import MAJOR_PROP_MARKETS
# --- Begin code from EffortOdds that is needed to avoid circular imports ---

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

    def update_table_display(self):
        """Update table display with improved price change highlighting"""
        table = self.table_widget
        current_rows = table.rowCount()
        current_cols = table.columnCount()
        
        # Update table structure if needed
        expected_cols = len(self.bookmakers) + 1
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(["Market/Outcome"] + self.bookmakers)
        
        expected_rows = len(self.table_rows)
        if current_rows != expected_rows:
            table.setRowCount(expected_rows)
        
        needs_resize = False
        
        for row_idx, row_label in enumerate(self.table_rows):
            row_data = self.table_data[row_label]
            game_id = row_data['game_id']
            color = self.get_game_color(game_id)
            
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
            for col_idx, bm in enumerate(self.bookmakers, 1):
                current_value = row_data.get(bm, "")
                previous_value = self.previous_data.get((row_label, bm))
                
                item = table.item(row_idx, col_idx)
                if not item:
                    item = ColoredTableItem(current_value, game_id)
                    table.setItem(row_idx, col_idx, item)
                    needs_resize = True
                
                # Only update if value has changed
                if current_value != previous_value and previous_value is not None:
                    item.setText(current_value)
                    try:
                        current_odds = float(current_value.split()[0])
                        previous_odds = float(previous_value.split()[0])
                        
                        # Better odds (higher value) = green, worse odds = red
                        if current_odds > previous_odds:
                            highlight_color = QColor(0, 200, 0, 180)
                        else:
                            highlight_color = QColor(200, 0, 0, 180)
                        
                        item.setBackground(highlight_color)
                        item.setForeground(QColor('black'))
                        
                        market_color = QColor(color)
                        market_color.setAlpha(230)
                        QTimer.singleShot(5000, lambda i=item, c=market_color: (
                            i.setBackground(c),
                            i.setForeground(QColor('black'))
                        ))
                    except (ValueError, IndexError):
                        item.setText(current_value)
                    
                    self.previous_data[(row_label, bm)] = current_value
                    needs_resize = True
                elif current_value != previous_value:
                    item.setText(current_value)
                    self.previous_data[(row_label, bm)] = current_value
                
                if not row_data.get('is_header') and item.background().color().alpha() != 180:
                    market_color = QColor(color)
                    market_color.setAlpha(230)
                    item.setBackground(market_color)
                    item.setForeground(QColor('black'))
        
        if needs_resize:
            table.resizeColumnsToContents()
            table.resizeRowsToContents()

# --- End code from EffortOdds ---

# --- Begin combined BaseTableWindow and PropsWindow code ---

class BaseTableWindow(QMainWindow):
    def __init__(self, title, sport_key, league_name):
        super().__init__()
        self.sport_key = sport_key
        self.league_name = league_name
        self.tab_data = LeagueTabData(league_name, sport_key)
        self.init_ui(title)

    def init_ui(self, title):
        self.setWindowTitle(title)
        self.setGeometry(200, 200, 1000, 800)
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.layout = QVBoxLayout(main_widget)
        # Progress bar
        self.progress = QProgressBar()
        self.layout.addWidget(self.progress)

    def create_table(self):
        self.table_widget = self.tab_data.create_table_widget()
        self.layout.insertWidget(0, self.table_widget)

    def add_control(self, widget):
        self.layout.insertWidget(0, widget)

class PropsWindow(BaseTableWindow):
    def __init__(self, sport_key, league_name):
        super().__init__(f"{league_name} Player Props", sport_key, league_name)
        self.prop_client = PropClient(sport_key)
        self.prop_types = []
        self.init_prop_ui()

    def init_prop_ui(self):
        # Prop type selector
        self.prop_selector = QComboBox()
        self.add_control(self.prop_selector)
        self.add_control(QLabel("Select Prop Type:"))
        # Create table
        self.create_table()
        # Load prop markets
        self.load_prop_markets()

    def load_prop_markets(self):
        from marketKeys import MAJOR_PROP_MARKETS
        prop_markets = MAJOR_PROP_MARKETS.get(self.sport_key, {})
        self.prop_types = list(prop_markets.keys())
        self.prop_selector.addItems([prop_markets[key] for key in self.prop_types])
        self.prop_selector.currentIndexChanged.connect(self.prop_type_changed)

    @qasync.asyncSlot()
    async def prop_type_changed(self, index):
        selected_prop = self.prop_types[index]
        await self.refresh_data({selected_prop})

    @qasync.asyncSlot()
    async def refresh_data(self, markets):
        self.progress.setValue(0)
        try:
            async with aiohttp.ClientSession() as session:
                games = await self.prop_client.get_games(session)
                total_games = len(games)
                for idx, game in enumerate(games):
                    game_id = game.get('id', '')
                    odds = await self.prop_client.get_event_odds(
                        session, game_id, markets, region="us"
                    )
                    self.process_odds_data(odds)
                    self.progress.setValue(int((idx + 1) / total_games * 100))
                self.tab_data.update_table_display()
        except Exception as e:
            print(f"Error fetching props: {e}")

    def process_odds_data(self, odds):
        if not odds or 'bookmakers' not in odds:
            return
        game_id = odds.get('id', 'unknown')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in self.tab_data.bookmakers:
                self.tab_data.bookmakers.append(bm_title)
            for market in bm['markets']:
                for outcome in market['outcomes']:
                    player_name = outcome.get('description', outcome.get('name'))
                    label = f"{player_name} - {market['key']}"
                    if label not in self.tab_data.table_rows:
                        self.tab_data.table_rows.append(label)
                        self.tab_data.table_data[label] = {'game_id': game_id}
                    price = f"{outcome.get('price', '')} ({outcome.get('point', '')})"
                    self.tab_data.table_data[label][bm_title] = price
        self.tab_data.update_table_display()
