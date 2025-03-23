import qasync
import aiohttp
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QProgressBar, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QPushButton,
    QHBoxLayout, QGridLayout, QCheckBox, QScrollArea, QGroupBox
)
from PropQuery import PropClient
from marketKeys import MAJOR_PROP_MARKETS
from LineCalculator import *
from TrackingStatsWidget import integrate_stats_with_props_window

#TODO: This file is massive need refactor soon or eventloop woopty is imminent 

# -----------------------------------------------------------------------------
# Helper function to extract odds and point from a given value string.
def extract_odds_point(value: str, indicator: str):
    """
    Parse a price string (e.g. "-142 O (15.5)") and extract the odds and point
    corresponding to the given indicator ('O' for over, 'U' for under).
    
    Returns:
        Tuple (odds, point) if successfully parsed, else None.
    """
    try:
        parts = value.split()
        if indicator in parts:
            idx = parts.index(indicator)
            if idx > 0:
                odds = float(parts[idx - 1])
                # Look for the first token (from idx onward) containing '('
                for i in range(idx, len(parts)):
                    if '(' in parts[i]:
                        point_str = parts[i].strip('()')
                        # Handle case where closing paren may be separate
                        if ')' not in parts[i]:
                            for j in range(i + 1, len(parts)):
                                if ')' in parts[j]:
                                    point_str += parts[j].strip(')')
                                    break
                        return odds, float(point_str)
    except Exception:
        pass
    return None

# -----------------------------------------------------------------------------
# Custom table item that stores game ID for color coordination
class ColoredTableItem(QTableWidgetItem):
    def __init__(self, text, game_id):
        super().__init__(text)
        self.game_id = game_id

# -----------------------------------------------------------------------------
# Manages data and display state for each league tab
class LeagueTabData:
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
        self.table_widget.setMinimumSize(1280, 360)
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
        # Create different color scheme for DFS site headers
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
                    parsed = extract_odds_point(current_value, 'O')  # Try parsing as over odds
                    parsed_prev = extract_odds_point(previous_value, 'O')
                    if parsed and parsed_prev:
                        current_odds, _ = parsed
                        previous_odds, _ = parsed_prev
                        # Better odds (higher value) = green, worse odds = red
                        highlight_color = QColor(0, 200, 0, 180) if current_odds > previous_odds else QColor(200, 0, 0, 180)
                        item.setBackground(highlight_color)
                        item.setForeground(QColor('black'))
                        market_color = QColor(color)
                        market_color.setAlpha(230)
                        QTimer.singleShot(5000, lambda i=item, c=market_color: (
                            i.setBackground(c),
                            i.setForeground(QColor('black'))
                        ))
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

# -----------------------------------------------------------------------------
# Base window class that provides a table and basic controls
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
        grid_layout = QGridLayout()
        
    def create_table(self):
        self.table_widget = self.tab_data.create_table_widget()
        self.table_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout.insertWidget(0, self.table_widget)

    def add_control(self, widget):
        self.layout.insertWidget(0, widget)

# -----------------------------------------------------------------------------
# PropsWindow: Displays player props and associated best lines
# Cant Believe this file works at all

class PropsWindow(BaseTableWindow):
    def __init__(self, sport_key, league_name):
        super().__init__(f"{league_name} Player Props", sport_key, league_name)
        # Define button style
        self.fetch_button_style = """
            QPushButton {
                background-color: #dc9437;
                color: white;
                border: 1px solid #0056b3;
                padding: 5px 10px; 
                margin-right: 5px;
                border-radius: 4px;
            }
        """
        self.prop_client = PropClient(sport_key)
        self.prop_types = []
        self.game_checkboxes = {}  # Store game checkboxes for easy access
        self.market_groups = {}  # Store markets grouped by player and type
        self.best_lines = {}  # Store the best lines for each market group
        self.best_lines_widget = None

        # Use a QTimer to schedule the async initialization
        self.timer = QTimer()
        self.timer.setSingleShot(True)  # Ensure the timer only fires once
        self.timer.timeout.connect(self.start_async_init)
        self.timer.start(0)  # Start the timer immediately after the constructor
        
        self.icon_frame = 0
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)
    
    def UpdateIcon(self):
        framesdir = "/home/retupmoc/Desktop/EffortScraper/OddsAPI/appicon_frames"
        next_icon = f"{framesdir}/frame{str(self.icon_frame).zfill(3)}.png"
        self.setWindowIcon(QIcon(next_icon))
        self.icon_frame = ((self.icon_frame + 1) % 200)
        #print(next_icon)
    
    def start_async_init(self):
        """Start the asynchronous initialization of the UI."""
        import asyncio
        asyncio.create_task(self.init_prop_ui())

    async def init_prop_ui(self):
        # Create controls container
        controls_widget = QWidget()
        controls_layout = QHBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)  # Remove margins for a compact layout
        controls_layout.setSpacing(5)  # Reduce spacing between widgets
        self.setWindowIcon(QIcon("/home/retupmoc/Desktop/EffortScraper/OddsAPI/AppIcon.png"))
        # Prop type label
        controls_layout.addWidget(QLabel("Select Prop Type:"))
    
        # Prop type selector
        self.prop_selector = QComboBox()
        controls_layout.addWidget(self.prop_selector)
    
        # Fetch button
        self.fetch_button = QPushButton("Fetch Props")
        self.fetch_button.setStyleSheet(self.fetch_button_style)
        self.fetch_button.clicked.connect(self.on_fetch_props_clicked)
        controls_layout.addWidget(self.fetch_button)
    
        # Progress bar (moved to the right of the controls)
        self.progress = QProgressBar()
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controls_layout.addWidget(self.progress)
    
        # Add controls to the main layout
        self.layout.addWidget(controls_widget)
    
        # Create a container for the bottom area (game selection and best lines)
        bottom_container = QWidget()
        bottom_layout = QHBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)  # Remove margins for a cleaner look
        
        # 'game_group' and checkbox-buttons need to match width
        game_selection_width = 600
        game_selection_height = 250
        
        # Add the game selection box to the left
        game_group = QGroupBox()
        game_group_layout = QVBoxLayout(game_group)
        game_group_layout.setContentsMargins(2, 2, 2, 2)  # Tighter margins (reduced from 3,3,3,3)
        game_group_layout.setSpacing(0)  # Remove spacing between elements
        game_group.setFixedWidth(game_selection_width)  # Adjust this value based on your needs
        game_group.setFixedHeight(game_selection_height) # not necessary?
        
        # Create horizontal layout for buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(2)  # Reduce spacing between buttons
        
        select_all_button = QPushButton("Select All")
        select_all_button.clicked.connect(lambda: self.set_all_game_checkboxes(True))
        select_all_button.setFixedSize(select_all_button.sizeHint().width() // 2, select_all_button.sizeHint().height())
        select_all_button.setMaximumWidth(80)  # Limit width to 80 pixels
        
        deselect_all_button = QPushButton("Deselect All")
        deselect_all_button.clicked.connect(lambda: self.set_all_game_checkboxes(False))
        deselect_all_button.setFixedSize(deselect_all_button.sizeHint().width() // 2, deselect_all_button.sizeHint().height())
        deselect_all_button.setMaximumWidth(80)  # Limit width to 80 pixels
        
        buttons_layout.addWidget(select_all_button)
        buttons_layout.addWidget(deselect_all_button)
        buttons_layout.addStretch()  # Add stretch to push buttons to the left
        game_group_layout.addLayout(buttons_layout)
    
        # Add the game selection scroll area with improved spacing
        self.game_selection_area = QScrollArea()
        self.game_selection_area.setWidgetResizable(True)
        self.game_selection_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.game_selection_area.setContentsMargins(0, 0, 0, 0)
        
        # Set a fixed width that's narrower to reduce the spacing
        self.game_selection_area.setFixedWidth(game_selection_width)  # Adjust this value based on your needs
        
        self.game_selection_widget = QWidget()
        self.game_selection_layout = QGridLayout(self.game_selection_widget)
        self.game_selection_layout.setContentsMargins(0, 0, 0, 0)  # Remove all margins
        self.game_selection_layout.setHorizontalSpacing(0)  # Set horizontal spacing to 0
        self.game_selection_layout.setVerticalSpacing(0)    # Set vertical spacing to 0
        
        self.game_selection_area.setWidget(self.game_selection_widget)
        game_group_layout.addWidget(self.game_selection_area)
    
        # Add the game selection box to the left side of the bottom container
        game_group.setFixedHeight(game_selection_height)  # Reduced from 300 to make more compact
        bottom_layout.addWidget(game_group)
    
        # Add the best lines widget to the right side of the bottom container
        self.create_best_lines_widget()
        bottom_layout.addWidget(self.best_lines_widget)
    
        # Add the bottom container to the main layout
        self.layout.addWidget(bottom_container)
    
        # Create table
        self.create_table()
    
        # Load prop markets
        self.load_prop_markets()
    
        # Fetch and populate games
        async with aiohttp.ClientSession() as session:
            games = await self.prop_client.get_games(session)
            self.populate_game_selection(games)
            integrate_stats_with_props_window(self)

    def load_prop_markets(self):
        """Load available prop markets into the dropdown"""
        prop_markets = MAJOR_PROP_MARKETS.get(self.sport_key, {})
        self.prop_types = list(prop_markets.keys())
        self.prop_selector.addItems([prop_markets[key] for key in self.prop_types])

    def populate_game_selection(self, games):
        """Populate the game selection area with checkboxes for each game."""
        # Clear existing checkboxes
        for i in reversed(range(self.game_selection_layout.count())):
            widget = self.game_selection_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        self.game_checkboxes.clear()
    
        # Use just 2 columns to match the screenshot layout
        num_columns = 2
        
        # Set zero spacing for the grid layout and remove margins
        self.game_selection_layout.setSpacing(0)
        self.game_selection_layout.setContentsMargins(0, 0, 0, 0)
        self.game_selection_layout.setHorizontalSpacing(0) # Explicitly set horizontal spacing to 0
        self.game_selection_layout.setVerticalSpacing(0)   # Explicitly set vertical spacing to 0
    
        # Add a checkbox for each game
        for idx, game in enumerate(games):
            game_id = game.get('id', '')
            home_team = game.get('home_team', 'Unknown')
            away_team = game.get('away_team', 'Unknown')
            game_label = f"{home_team} vs {away_team}"
            
            checkbox = QCheckBox(game_label)
            checkbox.setChecked(True)  # Default to selected
            
            # Tighter styling with reduced height and zero vertical margins
            checkbox.setStyleSheet("""
                QCheckBox { 
                    padding: 0px; 
                    margin: 0px; 
                    min-height: 14px; 
                    max-height: 14px; 
                    height: 14px;
                    font-size: 9pt;
                    spacing: 2px; /* Reduce space between checkbox and text */
                }
            """)
            
            self.game_checkboxes[game_id] = checkbox
    
            # Calculate row and column positions
            row = idx // num_columns
            col = idx % num_columns
            self.game_selection_layout.addWidget(checkbox, row, col)
    
        # Adjust the layout to fit the content
        self.game_selection_widget.adjustSize()

    # Update the game selection area configuration in init_prop_ui method
    # Replace the game_selection_area related code with this

    # Update the portion of init_prop_ui where you create the game selection area
    
    def set_all_game_checkboxes(self, checked: bool):
        """Set all game checkboxes to checked or unchecked state."""
        for checkbox in self.game_checkboxes.values():
            checkbox.setChecked(checked)
    
    def process_dfs_props_data(self, dfs_props):
        """Process DFS props data for display in the table"""
        if not dfs_props or 'bookmakers' not in dfs_props:
            return
        
        game_id = dfs_props.get('id', 'unknown')
        
        for bm in dfs_props['bookmakers']:
            bm_title = bm['title']
            if bm_title not in self.tab_data.bookmakers:
                self.tab_data.bookmakers.append(bm_title)
            
            for market in bm['markets']:
                for outcome in market['outcomes']:
                    player_name = outcome.get('description', outcome.get('name'))
                    label = f"{player_name} - DFS {market['key']}"
                    
                    if label not in self.tab_data.table_rows:
                        self.tab_data.table_rows.append(label)
                        self.tab_data.table_data[label] = {'game_id': game_id}
                    
                    price = f"{outcome.get('price', '')} ({outcome.get('point', '')})"
                    self.tab_data.table_data[label][bm_title] = price

    @qasync.asyncSlot()
    async def on_fetch_props_clicked(self):
        """Handle fetch button click"""
        selected_index = self.prop_selector.currentIndex()
        integrate_stats_with_props_window(self)
        if selected_index >= 0:
            selected_prop = self.prop_types[selected_index]
            await self.refresh_data({selected_prop})

    @qasync.asyncSlot()
    async def refresh_data(self, markets):
        self.progress.setValue(0)
        try:
            # Reset market groups and best lines
            self.market_groups = {}
            self.best_lines = {}
            self.raw_odds_data_by_game = {}  # Store raw odds data for each game
            
            self.tab_data.table_rows = []
            self.tab_data.table_data = {}
            
            async with aiohttp.ClientSession() as session:
                # Filter games based on selected checkboxes
                selected_games = [
                    game_id for game_id, checkbox in self.game_checkboxes.items()
                    if checkbox.isChecked()
                ]
                
                total_games = len(selected_games)
                
                # Debug logging
                selected_game_names = [
                    (checkbox.text(), checkbox.isChecked()) for game_id, checkbox in self.game_checkboxes.items()
                ]
                print(f"{len(selected_games)}/{len(selected_game_names)} selected")
                for (text, isChecked) in selected_game_names:
                    print(f"{text}: {isChecked}")
                
                # Create a consolidated odds data structure to combine data from all games
                consolidated_odds_data = {
                    'bookmakers': []
                }
                bookmakers_map = {}  # To track and merge bookmakers across games
                
                for idx, game_id in enumerate(selected_games):
                    odds = await self.prop_client.get_event_odds(
                        session, game_id, markets, region="us,us2,us_dfs,uk,au,eu"
                    )
                    
                    if not odds or 'bookmakers' not in odds:
                        continue
                        
                    # Store the raw data for this game
                    self.raw_odds_data_by_game[game_id] = odds
                    
                    # Process for table display
                    self.process_odds_data(odds)
                    
                    # Merge bookmakers data into consolidated structure
                    for bm in odds.get('bookmakers', []):
                        bm_title = bm['title']
                        if bm_title not in bookmakers_map:
                            bookmakers_map[bm_title] = {
                                'title': bm_title,
                                'markets': []
                            }
                            consolidated_odds_data['bookmakers'].append(bookmakers_map[bm_title])
                        
                        # Add markets from this game for this bookmaker
                        for market in bm.get('markets', []):
                            # Add game_id to each outcome for reference
                            for outcome in market.get('outcomes', []):
                                outcome['game_id'] = game_id
                            
                            bookmakers_map[bm_title]['markets'].append(market)
                    
                    self.progress.setValue(int((idx + 1) / total_games * 100))
                
                # Now store the consolidated data for the best lines calculator
                self.consolidated_odds_data = consolidated_odds_data
                
                # Update table display AFTER all data is processed
                self.tab_data.update_table_display()
                
                # Update the best lines widget using the consolidated raw data
                self.update_best_lines_display()
                
                # Highlight best lines in the main table
                if hasattr(self, 'highlight_best_lines'):
                    self.highlight_best_lines()
                    
        except Exception as e:
            print(f"Error fetching props: {e}")
            import traceback
            traceback.print_exc()

    def find_best_lines(self):
        """Find the best lines for each market group"""
        # Group table rows by player and market type
        market_groups = {}
        for row_label in self.tab_data.table_rows:
            # Parse player name and market type from row label
            parts = row_label.split(' - ')
            if len(parts) >= 2:
                player_name = parts[0]
                market_type = parts[1]
                # Create a unique key for this market group
                market_key = f"{player_name}:{market_type}"
                market_groups.setdefault(market_key, []).append(row_label)
        self.market_groups = market_groups
        
        # Find best lines for each market group using the helper function
        for market_key, rows in market_groups.items():
            best_over, best_under = self.find_best_market_lines(rows)
            self.best_lines[market_key] = {'over': best_over, 'under': best_under}

    def find_best_market_lines(self, market_rows):
        """Find best over and under lines for a specific market"""
        best_over = {'odds': -999999, 'point': 999999, 'bookmaker': None}
        best_under = {'odds': -999999, 'point': -999999, 'bookmaker': None}
        
        for row_label in market_rows:
            row_data = self.tab_data.table_data[row_label]
            for bm in self.tab_data.bookmakers:
                if bm not in row_data:
                    continue
                value = row_data[bm]
                if not value:
                    continue
                    
                # Parse over odds/point using helper function
                result = extract_odds_point(value, 'O')
                if result is not None:
                    over_odds, point = result
                    if (point < best_over['point'] or 
                        (point == best_over['point'] and over_odds > best_over['odds'])):
                        best_over = {'odds': over_odds, 'point': point, 'bookmaker': bm}
                
                # Parse under odds/point using helper function
                result = extract_odds_point(value, 'U')
                if result is not None:
                    under_odds, point = result
                    if (point > best_under['point'] or 
                        (point == best_under['point'] and under_odds > best_under['odds'])):
                        best_under = {'odds': under_odds, 'point': point, 'bookmaker': bm}
                        
        return best_over, best_under

    def highlight_best_lines(self):
        """
        Highlight cells with the best lines in the main table.
        Best over: lowest point with highest odds
        Best under: highest point with highest odds
        """
        if not hasattr(self, 'consolidated_odds_data') or not self.consolidated_odds_data:
            print("No consolidated odds data available for highlighting")
            return
            
        table = self.tab_data.table_widget
        
        # Define highlight colors
        best_over_color = QColor(0, 200, 0, 150)  # Green with some transparency
        best_under_color = QColor(0, 93, 167, 211)  # Blue with some transparency
        
        # Debug counters
        highlight_count = {'over': 0, 'under': 0}
        
        # First pass: Find the best lines for each market
        market_best_lines = {}
        
        for row_idx, row_label in enumerate(self.tab_data.table_rows):
            parts = row_label.split(' - ')
            if len(parts) < 2:
                continue
                
            player_name = parts[0]
            market_type = parts[1]
            row_data = self.tab_data.table_data.get(row_label, {})
            game_id = row_data.get('game_id', '')
            
            # Skip header rows
            if row_data.get('is_header', False):
                continue
            
            market_key = f"{player_name}:{market_type}"
            if market_key not in market_best_lines:
                market_best_lines[market_key] = {
                    'over': {'odds': float(-999999), 'point': float(999999), 'bookmaker': None, 'row': row_idx},
                    'under': {'odds': float(-999999), 'point': float(-999999), 'bookmaker': None, 'row': row_idx}
                }
            
            # Check each bookmaker for this row
            for col_idx, bm in enumerate(self.tab_data.bookmakers, 1):
                value = row_data.get(bm, "")
                if not value:
                    continue
                
                # Extract over odds
                over_result = extract_odds_point(value, 'O')
                if over_result:
                    over_odds, point = over_result
                    current_best = market_best_lines[market_key]['over']
                    
                    # Update if better point (lower) or same point with better odds (higher)
                    if point < current_best['point'] or (point == current_best['point'] and over_odds > current_best['odds']):
                        market_best_lines[market_key]['over'] = {
                            'odds': over_odds,
                            'point': point,
                            'bookmaker': bm,
                            'row': row_idx,
                            'col': col_idx
                        }
                
                # Extract under odds
                under_result = extract_odds_point(value, 'U')
                if under_result:
                    under_odds, point = under_result
                    current_best = market_best_lines[market_key]['under']
                    
                    # Update if better point (higher) or same point with better odds (higher)
                    if point > current_best['point'] or (point == current_best['point'] and under_odds > current_best['odds']):
                        market_best_lines[market_key]['under'] = {
                            'odds': under_odds,
                            'point': point,
                            'bookmaker': bm,
                            'row': row_idx,
                            'col': col_idx
                        }
        
        # Second pass: Apply highlighting
        for market_key, best_lines in market_best_lines.items():
            # Highlight best over
            over_data = best_lines['over']
            if over_data['bookmaker']:
                row_idx = over_data['row']
                col_idx = over_data['col']
                item = table.item(row_idx, col_idx)
                if item:
                    item.setBackground(best_over_color)
                    item.setForeground(QColor('black'))
                    highlight_count['over'] += 1
                    print(f"Highlighted OVER: {market_key} at {over_data['bookmaker']} - {over_data['odds']} O ({over_data['point']})")
            
            # Highlight best under
            under_data = best_lines['under']
            if under_data['bookmaker']:
                row_idx = under_data['row']
                col_idx = under_data['col']
                item = table.item(row_idx, col_idx)
                if item:
                    item.setBackground(best_under_color)
                    item.setForeground(QColor('black'))
                    highlight_count['under'] += 1
                    print(f"Highlighted UNDER: {market_key} at {under_data['bookmaker']} - {under_data['odds']} U ({under_data['point']})")
        
        print(f"Highlight operation complete. Highlighted {highlight_count['over']} over lines and {highlight_count['under']} under lines.")
    
    # This is semi-jenk, can likley be half as long and twice as efficient
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
            
            # Group markets by player name and market key
            grouped_markets = {}
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    player_name = outcome.get('description', outcome.get('name'))
                    # Create a unique key for grouping
                    group_key = f"{player_name} - {market_key}"
                    if group_key not in grouped_markets:
                        grouped_markets[group_key] = {'over': None, 'under': None, 'point': outcome.get('point', '')}
                    
                    # Store over/under odds
                    outcome_name = outcome.get('name', '').lower()
                    if outcome_name == 'over':
                        grouped_markets[group_key]['over'] = outcome.get('price', '')
                    elif outcome_name == 'under':
                        grouped_markets[group_key]['under'] = outcome.get('price', '')
            
            # Process the grouped markets
            for label, data in grouped_markets.items():
                if label not in self.tab_data.table_rows:
                    self.tab_data.table_rows.append(label)
                    self.tab_data.table_data[label] = {'game_id': game_id}
                
                # Format as "-142 O (15.5) +106 U"
                over_price = data['over']
                under_price = data['under']
                point = data['point']
                
                if over_price is not None and under_price is not None:
                    price = f"{over_price} O ({point}) {under_price} U"
                elif over_price is not None:
                    price = f"{over_price} O ({point})"
                elif under_price is not None:
                    price = f"{under_price} U ({point})"
                else:
                    price = f"({point})"
                    
                self.tab_data.table_data[label][bm_title] = price
    
    
    # Widget to try and calculate best lines for entire query based on deviation
    def create_best_lines_widget(self):
        """Create a widget to display the best lines and their deviations."""
        self.best_lines_widget = QTableWidget()
        self.best_lines_widget.setColumnCount(5)
        self.best_lines_widget.setHorizontalHeaderLabels(["Player","Market","Best Line","Avg Odds","Implied Prob Deviation"]) #"Avg Odds" cannot be figured out im tard boy again
        self.best_lines_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout.addWidget(self.best_lines_widget)

    def update_best_lines_display(self):
        """Update the best lines widget with the latest consolidated raw API data."""
        if not self.best_lines_widget:
            print("Best lines widget not initialized. Skipping update.")
            return
        
        # Clear existing data
        self.best_lines_widget.setRowCount(0)
        
        # Use the consolidated raw data if available
        if hasattr(self, 'consolidated_odds_data') and self.consolidated_odds_data:
            # Extract bookmakers list from the consolidated data
            bookmakers = [bm['title'] for bm in self.consolidated_odds_data.get('bookmakers', [])]
            
            # Transform the data to the format BestLinesCalculator expects
            table_data = {}
            
            # Process each bookmaker's markets
            for bm in self.consolidated_odds_data.get('bookmakers', []):
                bm_title = bm['title']
                
                for market in bm.get('markets', []):
                    market_key = market['key']
                    
                    for outcome in market.get('outcomes', []):
                        player_name = outcome.get('description', outcome.get('name', ''))
                        game_id = outcome.get('game_id', '')
                        
                        # Create a row label like "Player Name - market_key"
                        row_label = f"{player_name} - {market_key}"
                        
                        # Initialize row data if it doesn't exist
                        if row_label not in table_data:
                            table_data[row_label] = {'game_id': game_id, 'is_header': False}
                        
                        # Store outcome data
                        outcome_name = outcome.get('name', '').lower()
                        point = outcome.get('point', '')
                        price = outcome.get('price', '')
                        
                        if outcome_name == 'over':
                            value = f"{price} O ({point})"
                        elif outcome_name == 'under':
                            value = f"{price} U ({point})"
                        else:
                            value = f"{price} ({point})"
                        
                        # Store the value for this bookmaker
                        table_data[row_label][bm_title] = value
            
            # Calculate best lines using the transformed data
            calculator = BestLinesCalculator(table_data, bookmakers)
            best_lines = calculator.calculate_best_lines()
            
            # Store the best lines for highlighting in the main table
            self.best_lines = best_lines
            
            # Populate the widget with the results
            self._populate_best_lines_widget(best_lines)
        else:
            print("No consolidated odds data available for best lines calculation.")
            
            
    def _populate_best_lines_widget(self, best_lines):
        """Helper method to populate the best lines widget with calculated data."""
        # First, update column count if not already done
        self.best_lines_widget.setColumnCount(5)
        self.best_lines_widget.setHorizontalHeaderLabels(["Player", "Market", "Best Line", "Avg Odds", "Implied Prob Deviation"])
        
        # Sort markets by deviation to show the best value bets first
        sorted_markets = []
        for market_key, data in best_lines.items():
            max_deviation = 0
            if data['over'] and data['over']['count'] > 1:
                max_deviation = max(max_deviation, data['over']['deviation'])
            if data['under'] and data['under']['count'] > 1:
                max_deviation = max(max_deviation, data['under']['deviation'])
            
            sorted_markets.append((market_key, data, max_deviation))
        
        # Sort by deviation in descending order
        sorted_markets.sort(key=lambda x: x[2], reverse=True)
        
        # Add rows for each market
        for market_key, data, _ in sorted_markets:
            player_name, market_type = market_key.split(':')
            
            # Add a row for the over line if available
            if data['over']:
                over = data['over']
                row_position = self.best_lines_widget.rowCount()
                self.best_lines_widget.insertRow(row_position)
                
                self.best_lines_widget.setItem(row_position, 0, QTableWidgetItem(player_name))
                self.best_lines_widget.setItem(row_position, 1, QTableWidgetItem(f"{market_type} OVER"))
                
                over_text = f"{over['odds']} O ({over['point']}) @ {over['bookmaker']}"
                self.best_lines_widget.setItem(row_position, 2, QTableWidgetItem(over_text))
                
                if over['count'] > 1:
                    # Add average odds
                    avg_odds_text = f"{over['avg_odds']:.0f}"
                    self.best_lines_widget.setItem(row_position, 3, QTableWidgetItem(avg_odds_text))
                    
                    # Add deviation
                    deviation_item = QTableWidgetItem(f"+{over['deviation']:.2f}%")
                    
                    # Color code the deviation
                    if over['deviation'] > 10:
                        deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green
                    elif over['deviation'] > 5:
                        deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow
                    
                    self.best_lines_widget.setItem(row_position, 4, deviation_item)
                else:
                    self.best_lines_widget.setItem(row_position, 3, QTableWidgetItem("N/A"))
                    self.best_lines_widget.setItem(row_position, 4, QTableWidgetItem("Solo Line"))
            
            # Add a row for the under line if available
            if data['under']:
                under = data['under']
                row_position = self.best_lines_widget.rowCount()
                self.best_lines_widget.insertRow(row_position)
                
                self.best_lines_widget.setItem(row_position, 0, QTableWidgetItem(player_name))
                self.best_lines_widget.setItem(row_position, 1, QTableWidgetItem(f"{market_type} UNDER"))
                
                under_text = f"{under['odds']} U ({under['point']}) @ {under['bookmaker']}"
                self.best_lines_widget.setItem(row_position, 2, QTableWidgetItem(under_text))
                
                if under['count'] > 1:
                    # Add average odds
                    avg_odds_text = f"{under['avg_odds']:.0f}"
                    self.best_lines_widget.setItem(row_position, 3, QTableWidgetItem(avg_odds_text))
                    
                    # Add deviation
                    deviation_item = QTableWidgetItem(f"+{under['deviation']:.2f}")
                    
                    # Color code the deviation
                    if under['deviation'] > 10:
                        deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green
                    elif under['deviation'] > 5:
                        deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow
                    
                    self.best_lines_widget.setItem(row_position, 4, deviation_item)
                else:
                    self.best_lines_widget.setItem(row_position, 3, QTableWidgetItem("N/A"))
                    self.best_lines_widget.setItem(row_position, 4, QTableWidgetItem("Solo Line"))
        
        # Resize the columns to fit content
        self.best_lines_widget.resizeColumnsToContents()
