import qasync
import aiohttp
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QProgressBar, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QPushButton,
    QHBoxLayout, QGridLayout, QCheckBox, QScrollArea, QGroupBox, QTabWidget
)
from PropQuery import PropClient
from marketKeys import MAJOR_PROP_MARKETS
import LineCalculator
from TrackingStatsWidget import integrate_stats_with_props_window
import asyncio
from GUIbestlineswidget import BestLinesWidget
#TODO: This file is massive need refactor soon or eventloop woopty is imminent 
#TODO: Best Lines widget is not populating, max tilt as its clearly a secret as to why
# -----------------------------------------------------------------------------

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
        self.last_update_time = None


# -----------------------------------------------------------------------------
# Base window class that provides a table and basic controls
class BaseTableWindow(QMainWindow):
    def __init__(self, title, sport_key, league_name):
        super().__init__()
        self.sport_key = sport_key
        self.league_name = league_name
        self.tab_data = LeagueTabData(league_name, sport_key)
        self.table_widget = None
        self.init_ui(title)

    def init_ui(self, title):
        self.setWindowTitle(title)
        self.setGeometry(200, 200, 1000, 800)
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.layout = QVBoxLayout(main_widget)
        grid_layout = QGridLayout()

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
        game_colors = self.tab_data.game_colors
        color_palette = self.tab_data.color_palette
        current_color_index = self.tab_data.current_color_index
        if game_id not in game_colors:
            game_colors[game_id] = color_palette[current_color_index]
            self.tab_data.current_color_index = (current_color_index + 1) % len(color_palette)
        return game_colors[game_id]

    def update_table_display(self):
        # Create different color scheme for DFS site headers
        table = self.table_widget
        current_rows = table.rowCount()
        current_cols = table.columnCount()
        
        # Update table structure if needed
        expected_cols = len(self.tab_data.bookmakers) + 1
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(["Market/Outcome"] + self.tab_data.bookmakers)
            
        expected_rows = len(self.tab_data.table_rows)
        if current_rows != expected_rows:
            table.setRowCount(expected_rows)
        
        needs_resize = False
        
        for row_idx, row_label in enumerate(self.tab_data.table_rows):
            row_data = self.tab_data.table_data[row_label]
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
            for col_idx, bm in enumerate(self.tab_data.bookmakers, 1):
                current_value = row_data.get(bm, "")
                previous_value = self.tab_data.previous_data.get((row_label, bm))
                
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
                    self.tab_data.previous_data[(row_label, bm)] = current_value
                    needs_resize = True
                elif current_value != previous_value:
                    item.setText(current_value)
                    self.tab_data.previous_data[(row_label, bm)] = current_value
                
                if not row_data.get('is_header') and item.background().color().alpha() != 180:
                    market_color = QColor(color)
                    market_color.setAlpha(230)
                    item.setBackground(market_color)
                    item.setForeground(QColor('black'))
        
        if needs_resize:
            table.resizeColumnsToContents()
            table.resizeRowsToContents()


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
        self.consolidated_odds_data = {'bookmakers': []}
        self.bookmakers_map = {}
        self.raw_odds_data_by_game = {}

        # Add tab-related variables
        self.props_tab_widget = QTabWidget()  # Main tab widget for the odds display
        self.current_market = None
        self.current_tab_data = self.tab_data

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
        
        
        # Line highlighting colors - use alpha 180 to prevent override by update_table_display
        self.best_over_color = QColor(0, 200, 0, 180)  # Bright Green
        self.best_under_color = QColor(0, 100, 255, 180)  # Bright Blue
        self.best_text_color = QColor(255, 255, 255) # White Text
            
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
        
        # Create main odds display tab widget and add to layout
        self.props_tab_widget = QTabWidget()
        self.props_tab_widget.currentChanged.connect(self.handle_tab_change)
        self.layout.addWidget(self.props_tab_widget)
        
        # Create a container for the bottom area (game selection and best lines)
        bottom_container = QWidget()
        bottom_layout = QHBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)  # Remove margins for a cleaner look
        
        # 'game_group' and checkbox-buttons need to match width
        game_selection_width = 600
        game_selection_height = 250
        
        
        self.tab_data_map = {}  # Maps tab index to LeagueTabData
        self.tab_market_map = {}  # Maps tab index to market key
        
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
    
        # Load prop markets
        self.load_prop_markets()
    
        # Fetch and populate games
        async with aiohttp.ClientSession() as session:
            games = await self.prop_client.get_games(session)
            self.populate_game_selection(games)
            integrate_stats_with_props_window(self)
    
    def handle_tab_change(self, index):
        """Handle tab switching events to update best lines display"""
        print("HANDLE TAB CHANGE")
        if 0 <= index < self.props_tab_widget.count():
            tab_display_name = self.props_tab_widget.tabText(index)
            print(f"Tab changed to: {tab_display_name}")
            
            # Update current tab data and market from stored mappings
            if index in self.tab_data_map:
                self.current_tab_data = self.tab_data_map[index]
                self.current_market = self.tab_market_map[index]
                
                # Sync tab_data with current_tab_data
                self.tab_data = self.current_tab_data
                
                print(f"Current market set to: {self.current_market}")
                print(f"Current tab data updated for index: {index}")
                
                # Update best lines display based on the selected tab
                self.update_best_lines_for_current_tab()
                self.highlight_best_lines()
            else:
                print(f"Warning: No data found for tab index: {index}")
        else:
            print(f"Warning: Invalid tab index: {index}")
    
    
    def update_best_lines_for_current_tab(self):
        """Update best lines display using only the current tab's data"""
        if not self.current_tab_data or not self.current_tab_data.table_data:
            print("No current tab data available for best lines calculation")
            return
        
        print(f"Updating best lines for market: {self.current_market}")
        
        # Use only the current tab's data
        table_data = self.current_tab_data.table_data
        bookmakers = self.current_tab_data.bookmakers
        
        if not table_data or not bookmakers:
            print("No table data or bookmakers for current tab")
            return
        
        # Calculate best lines using current tab data
        self.best_lines = LineCalculator.calculate_best_lines(table_data, bookmakers)
        print(f"Calculated best lines for {len(self.best_lines)} markets")
        
        # Populate the best lines widget
        self._populate_best_lines_widget(self.best_lines)
    
    
    def load_prop_markets(self):
        """Load available prop markets into the dropdown"""
        prop_markets = MAJOR_PROP_MARKETS.get(self.sport_key, {})
        self.prop_types = list(prop_markets.keys())
        
        # Store display names mapped to internal keys
        self.display_to_key = {prop_markets[key]: key for key in self.prop_types}
        self.key_to_display = {key: prop_markets[key] for key in self.prop_types}
        
        # Add display names to dropdown
        self.prop_selector.addItems([prop_markets[key] for key in self.prop_types])

    def populate_game_selection(self, games):
        """Populate the game selection area with an interactive, modern list of games."""
        # Clear existing widgets and checkboxes
        for i in reversed(range(self.game_selection_layout.count())):
            widget = self.game_selection_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        self.game_checkboxes.clear()
        
        # Set dimensions
        game_selection_width = 600
        game_selection_height = 250
        
        # Create container with modern styling
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Create header with buttons
        header = QWidget()
        header.setObjectName("gameSelectionHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 5, 10, 5)
        
        # Add title and buttons to header
        header_layout.addWidget(QLabel("Game Selection"))
        header_layout.addStretch()
        
        # Button container
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)
        
        # Add select/deselect buttons
        select_all = QPushButton("Select All")
        select_all.setObjectName("selectAllButton")
        select_all.clicked.connect(lambda: self.set_all_game_checkboxes(True))
        
        deselect_all = QPushButton("Deselect All")
        deselect_all.setObjectName("deselectAllButton")
        deselect_all.clicked.connect(lambda: self.set_all_game_checkboxes(False))
        
        button_layout.addWidget(select_all)
        button_layout.addWidget(deselect_all)
        header_layout.addWidget(button_container)
        main_layout.addWidget(header)
        
        # Create game list scroll area
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setObjectName("gameListScrollArea")
        
        # Create games container
        games_list = QWidget()
        games_layout = QVBoxLayout(games_list)
        games_layout.setContentsMargins(8, 8, 8, 8)
        games_layout.setSpacing(1)  # Minimal spacing between games
        
        # Add all games in a single list
        for game in games:
            game_id = game.get('id', '')
            home_team = game.get('home_team', 'Unknown')
            away_team = game.get('away_team', 'Unknown')
            
            # Create game item
            game_widget = QWidget()
            game_widget.setObjectName("gameItem")
            game_layout = QHBoxLayout(game_widget)
            game_layout.setContentsMargins(5, 2, 5, 2)  # Tighter margins
            
            # Add checkbox
            checkbox = QCheckBox(f"{away_team} vs {home_team}")
            checkbox.setChecked(True)
            checkbox.setObjectName("gameCheckbox")
            self.game_checkboxes[game_id] = checkbox
            
            game_layout.addWidget(checkbox)
            games_layout.addWidget(game_widget)
        
        games_layout.addStretch()  # Push everything to the top
        scroll_area.setWidget(games_list)
        main_layout.addWidget(scroll_area)
        
        # Apply styling to game checkbox
        container.setStyleSheet("""
            QWidget { color: white; font-size: 10pt; }
            #gameSelectionHeader {
                background-color: #1E2A38;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }
            #selectAllButton, #deselectAllButton {
                background-color: #2C3E50;
                border: none;
                padding: 4px 12px;
                border-radius: 3px;
            }
            #selectAllButton:hover, #deselectAllButton:hover { background-color: #34495E; }
            #selectAllButton:pressed, #deselectAllButton:pressed { background-color: #1ABC9C; }
            #gameListScrollArea {
                background-color: #2C3E50;
                border: none;
                border-bottom-left-radius: 6px;
                border-bottom-right-radius: 6px;
            }
            #gameItem { 
                padding: 1px 0px;
                border-bottom: 1px solid rgba(52, 73, 94, 0.3);
            }
            #gameItem:hover { background-color: rgba(52, 73, 94, 0.5); }
            #gameCheckbox { 
                spacing: 5px;
                font-size: 9.5pt;
            }
            #gameCheckbox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 3px;
            }
            #gameCheckbox::indicator:unchecked {
                background-color: #34495E;
                border: 1px solid #7F8C8D;
            }
            #gameCheckbox::indicator:checked {
                background-color: #1ABC9C;
                border: 1px solid #16A085;
            }
        """)
        
        # Set fixed size and update widget
        container.setFixedSize(game_selection_width, game_selection_height)
        self.game_selection_widget = container
        self.game_selection_area.setWidget(container)
        self.game_selection_layout = main_layout
    
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
            if bm_title not in self.current_tab_data.bookmakers:
                self.current_tab_data.bookmakers.append(bm_title)
            
            for market in bm['markets']:
                for outcome in market['outcomes']:
                    player_name = outcome.get('description', outcome.get('name'))
                    label = f"{player_name} - DFS {market['key']}"
                    
                    if label not in self.current_tab_data.table_rows:
                        self.current_tab_data.table_rows.append(label)
                        self.current_tab_data.table_data[label] = {'game_id': game_id}
                    
                    price = f"{outcome.get('price', '')} ({outcome.get('point', '')})"
                    self.current_tab_data.table_data[label][bm_title] = price

    @qasync.asyncSlot()
    async def on_fetch_props_clicked(self):
        """Handle fetch button click"""
        selected_index = self.prop_selector.currentIndex()
        integrate_stats_with_props_window(self)
        if selected_index >= 0:
            selected_prop = self.prop_types[selected_index]
            await self.refresh_data({selected_prop})

    def create_prop_tab(self, market_type):
        """Create a new tab for a prop market"""
        display_name = self.key_to_display.get(market_type, market_type)
        print(f"Creating new tab: {display_name} for market_type: {market_type}")
        
        tab_data = LeagueTabData(self.league_name, self.sport_key)
        self.tab_data = tab_data
        self.current_tab_data = tab_data
        self.create_table_widget()
        
        # Add the tab and store the data association
        tab_index = self.props_tab_widget.addTab(self.table_widget, display_name)
        self.tab_data_map[tab_index] = tab_data
        self.tab_market_map[tab_index] = market_type
        
        return tab_data

    @qasync.asyncSlot()
    async def refresh_data(self, markets):
        """Handle data refresh with proper error handling and state management"""
        self.progress.setValue(0)
        
        try:
            if not markets:
                return
                
            market_type = list(markets)[0]  # Get the first market type
            print(f"Selected market type: {market_type}")
            
            self.current_market = market_type
            self.create_prop_tab(market_type)
            
            # Switch to the tab we're refreshing
            tab_index = self.props_tab_widget.indexOf(self.table_widget)
            if tab_index >= 0:
                self.props_tab_widget.setCurrentIndex(tab_index)
    
            async with aiohttp.ClientSession() as session:
                selected_games = [
                    game_id for game_id, checkbox in self.game_checkboxes.items()
                    if checkbox.isChecked()
                ]
    
                if not selected_games:
                    print("No games selected - aborting refresh")
                    return
    
                for idx, game_id in enumerate(selected_games):
                    try:
                        # Show progress
                        progress_value = int((idx + 1) / len(selected_games) * 100)
                        self.progress.setValue(progress_value)                       
                        
                        # Fetch odds with timeout protection
                        try:
                            odds = await asyncio.wait_for(
                                self.prop_client.get_event_odds(
                                    session, 
                                    game_id, 
                                    markets, 
                                    region="us,us2,us_dfs,uk,au,eu"
                                ),
                                timeout=10.0
                            )
                        except asyncio.TimeoutError:
                            print(f"Timeout fetching data for game {game_id}")
                            continue
    
                        if not odds or 'bookmakers' not in odds:
                            print(f"No valid odds data for game {game_id}")
                            continue
    
                        # Store raw data by game
                        self.raw_odds_data_by_game[game_id] = odds
    
                        # Process for table display using current tab data
                        self.process_odds_data(odds)
    
                        # Merge into consolidated data
                        for bm in odds.get('bookmakers', []):
                            bm_title = bm['title']
                            if bm_title not in self.bookmakers_map:
                                self.bookmakers_map[bm_title] = {
                                    'title': bm_title,
                                    'markets': []
                                }
                                self.consolidated_odds_data['bookmakers'].append(self.bookmakers_map[bm_title])
    
                            # Add markets with game context
                            for market in bm.get('markets', []):
                                market_copy = market.copy()
                                market_copy['game_id'] = game_id
                                self.bookmakers_map[bm_title]['markets'].append(market_copy)
    
                    except Exception as e:
                        print(f"Error processing game {game_id}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        continue
    
                # Final progress update
                self.progress.setValue(100)
                
                # Update displays if we got any data
                if self.consolidated_odds_data['bookmakers']:
                    print("Updating displays with processed data")
                    self.find_best_lines()
                    self.update_table_display()
                    # Update best lines display right after processing data
                    self.update_best_lines_display()
                    # Highlight AFTER table display is updated to prevent override
                    self.highlight_best_lines()
                else:
                    print("No valid bookmaker data was processed")
    
        except Exception as e:
            print(f"Fatal error in refresh_data: {str(e)}")
            import traceback
            traceback.print_exc()
            # Ensure progress bar updates even on failure
            self.progress.setValue(0)
        
        self.progress.setValue(100)

    def find_best_lines(self):
        """Find the best lines for each market group"""
        # Clear and rebuild market groups for current tab data
        market_groups = {}  # Use local variable instead of class variable
        
        for row_label in self.current_tab_data.table_rows:
            # Parse player name and market type from row label
            parts = row_label.split(' - ')
            if len(parts) >= 2:
                player_name = parts[0]
                market_type = parts[1]
                # Create a unique key for this market group
                market_key = f"{player_name}:{market_type}"
                market_groups.setdefault(market_key, []).append(row_label)
        
        # Update the class variable with current data
        self.market_groups = market_groups
        
        # Find best lines for each market group using the helper function
        for market_key, rows in market_groups.items():
            best_over, best_under = self.find_best_market_lines(rows)
            self.best_lines[market_key] = {'over': best_over, 'under': best_under}

    def find_best_market_lines(self, market_rows):
        """Find best over and under lines for a specific market"""
        best_over = {'odds': -999999, 'point': 999999, 'bookmaker': None}
        best_under = {'odds': 999999, 'point': -999999, 'bookmaker': None}
        print("FINDING BEST MARKET LINES")
        
        for row_label in market_rows:
            row_data = self.current_tab_data.table_data[row_label]
            for bm in self.current_tab_data.bookmakers:
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
        if not hasattr(self, 'best_lines') or not self.best_lines:
            print("No best lines data available for highlighting")
            return
        
        if not self.current_tab_data:
            return 
        
        table = self.table_widget
        
        # First reset all highlighting
        for row in range(table.rowCount()):
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item:
                    # Reset to default colors but keep game-specific background
                    game_id = item.game_id if hasattr(item, 'game_id') else ''
                    if game_id:
                        bg_color = self.get_game_color(game_id)
                        item.setBackground(bg_color)
                    item.setForeground(QColor('black'))
    
        # Use LineCalculator results to highlight best lines
        highlighted_count = 0
        for market_key, best_data in self.best_lines.items():
            # Find the row for this market
            player_name, market_type = market_key.split(':', 1)
            row_label = f"{player_name} - {market_type}"
            
            if row_label not in self.current_tab_data.table_rows:
                continue
                
            row_idx = self.current_tab_data.table_rows.index(row_label)
            
            # Highlight best OVER if it exists
            if best_data['over'] and best_data['over'].get('bookmaker'):
                over_bm = best_data['over']['bookmaker']
                if over_bm in self.current_tab_data.bookmakers:
                    col_idx = self.current_tab_data.bookmakers.index(over_bm) + 1  # +1 for row header
                    if item := table.item(row_idx, col_idx):
                        item.setBackground(self.best_over_color)
                        item.setForeground(self.best_text_color)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        highlighted_count += 1
            
            # Highlight best UNDER if it exists
            if best_data['under'] and best_data['under'].get('bookmaker'):
                under_bm = best_data['under']['bookmaker']
                if under_bm in self.current_tab_data.bookmakers:
                    col_idx = self.current_tab_data.bookmakers.index(under_bm) + 1  # +1 for row header
                    if item := table.item(row_idx, col_idx):
                        item.setBackground(self.best_under_color)
                        item.setForeground(self.best_text_color)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        highlighted_count += 1
    
        print(f"Highlighted {highlighted_count} best lines (over and under) from LineCalculator results")
    
    def process_odds_data(self, odds):
        """Process odds data into table format without overwriting existing markets"""
        if not odds or 'bookmakers' not in odds:
            return
        
        if not self.current_tab_data:
            return
            
        game_id = odds.get('id', 'unknown')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')
        
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in self.current_tab_data.bookmakers:
                self.current_tab_data.bookmakers.append(bm_title)
            
            # Group markets by player name and market key
            grouped_markets = {}
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    player_name = outcome.get('description', outcome.get('name'))
                    group_key = f"{player_name} - {market_key}"
                    
                    if group_key not in grouped_markets:
                        grouped_markets[group_key] = {
                            'over': None, 
                            'under': None, 
                            'point': outcome.get('point', '')
                        }
                    
                    outcome_name = outcome.get('name', '').lower()
                    if outcome_name == 'over':
                        grouped_markets[group_key]['over'] = outcome.get('price', '')
                    elif outcome_name == 'under':
                        grouped_markets[group_key]['under'] = outcome.get('price', '')
        
            # Process the grouped markets into table data
            for label, data in grouped_markets.items():
                if label not in self.current_tab_data.table_rows:
                    self.current_tab_data.table_rows.append(label)
                    self.current_tab_data.table_data[label] = {'game_id': game_id}
                
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
                
                self.current_tab_data.table_data[label][bm_title] = price
    
    
    # Widget to try and calculate best lines for entire query based on deviation
    def create_best_lines_widget(self):
        """Create a widget to display the best lines and their deviations."""
        self.best_lines_widget = BestLinesWidget()
        self.best_lines_widget.set_sport(self.sport_key)
        self.best_lines_widget.setColumnCount(5)
        self.best_lines_widget.setHorizontalHeaderLabels(["Player","Market","Best Line","Avg Odds","Implied Prob Deviation"]) #"Avg Odds" cannot be figured out im tard boy again
        self.best_lines_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    
    # NEVER CHANGE THESE FUNCTIONS!!!
    def update_best_lines_display(self):
        """Update the best lines widget with all accumulated market data"""
        if not hasattr(self, 'consolidated_odds_data') or not self.consolidated_odds_data:
            print("No consolidated odds data available for best lines calculation")
            return
        
        if not self.current_tab_data:
            return
            
        # Use existing table data and bookmakers from current tab
        table_data = self.current_tab_data.table_data
        bookmakers = self.current_tab_data.bookmakers
        
        # Calculate and display best lines
        self.best_lines = LineCalculator.calculate_best_lines(table_data, bookmakers)
        print("BEST LINES (from LineCalculator)")
        # Print just the structure to see if we have under data
        for market_key, data in self.best_lines.items():
            over_status = "YES" if data['over'] else "NO"
            under_status = "YES" if data['under'] else "NO"
            print(f"{market_key}: OVER={over_status}, UNDER={under_status}")
        self._populate_best_lines_widget(self.best_lines)
        # self.best_lines_widget.update_display(self.consolidated_odds_data)
        return
    
    # NEVER CHANGE THESE FUNCTIONS!!!
    def _populate_best_lines_widget(self, best_lines):
        """
        Correctly populate the Best Lines tab in the bottom tab panel.
        The function directly accesses the QTableWidget inside the Best Lines tab.
        """
        print("Starting population of Best Lines tab (bottom panel left side)")
        
        # Find the correct table widget in the Best Lines tab
        best_lines_table = None
        
        # The tab is likely a direct child of a tab widget, not the best_lines_widget property
        # Let's try to find it by traversing the widget hierarchy
        
        # In the actual PropsWindow class, we can directly access it if it's already defined
        if hasattr(self, 'best_lines_table'):
            best_lines_table = self.best_lines_table
        
        # If that doesn't work, find the table in the Best Lines tab
        if not best_lines_table:
            try:
                # Assuming the tab widget is at the bottom of the window
                # This is the tab widget containing "Best Lines" and "Advanced Stats" tabs
                bottom_tab_widget = None
                
                # Check if we can access it directly
                if hasattr(self, 'bottom_tab_widget'):
                    bottom_tab_widget = self.bottom_tab_widget
                
                # If not, try another approach - look for the bottom tab container
                if not bottom_tab_widget and hasattr(self, 'layout'):
                    for i in range(self.layout.count()):
                        item = self.layout.itemAt(i)
                        if hasattr(item, 'widget') and item.widget():
                            # Look for a tab widget in the bottom part
                            for child in item.widget().findChildren(QTabWidget):
                                # Check if this tab widget has "Best Lines" tab
                                for tab_idx in range(child.count()):
                                    if child.tabText(tab_idx).lower() == "best lines":
                                        bottom_tab_widget = child
                                        break
                                if bottom_tab_widget:
                                    break
                        if bottom_tab_widget:
                            break
                
                # If we found the tab widget, get the "Best Lines" tab content
                if bottom_tab_widget:
                    for tab_idx in range(bottom_tab_widget.count()):
                        if bottom_tab_widget.tabText(tab_idx).lower() == "best lines":
                            tab_content = bottom_tab_widget.widget(tab_idx)
                            # Find the QTableWidget inside this tab
                            for child in tab_content.findChildren(QTableWidget):
                                best_lines_table = child
                                break
                            break
            except Exception as e:
                print(f"Error finding Best Lines tab: {str(e)}")
        
        # If we still don't have the table, fall back to the best_lines_widget property
        if not best_lines_table:
            print("WARNING: Could not find Best Lines tab table, using best_lines_widget directly")
            best_lines_table = self.best_lines_widget
        
        # Make sure we have a table to work with
        if not best_lines_table:
            print("ERROR: Failed to find any Best Lines table widget!")
            return
        
        print(f"Found Best Lines table widget: {best_lines_table}")
        
        # Now populate this table with the data
        try:
            # Clear existing rows
            best_lines_table.setRowCount(0)
            
            # Set up columns
            best_lines_table.setColumnCount(5)
            best_lines_table.setHorizontalHeaderLabels(["Player", "Market", "Best Line", "Avg Odds", "Implied Prob Deviation"])
            
            # Sort markets by deviation - include both over and under lines
            sorted_markets = []
            for market_key, data in best_lines.items():
                # Extract market details
                try:
                    parts = market_key.split(':')
                    if len(parts) < 2:
                        continue
                    
                    player_name = parts[0]
                    market_type = parts[1]
                    
                    # Add over line if it exists
                    if data['over'] and 'deviation' in data['over']:
                        over_dev = data['over']['deviation']
                        sorted_markets.append((player_name, market_type, data, over_dev, True))
                    
                    # Add under line if it exists  
                    if data['under'] and 'deviation' in data['under']:
                        under_dev = data['under']['deviation']
                        sorted_markets.append((player_name, market_type, data, under_dev, False))
                        
                except Exception as e:
                    print(f"Error processing market {market_key}: {str(e)}")
                    continue
            
            # Sort by deviation (highest first)
            sorted_markets.sort(key=lambda x: x[3], reverse=True)
            
            # Limit best lines results with sorted_markets for testing
            for row_idx, (player_name, market_type, data, _, use_over) in enumerate(sorted_markets):
                # Get the line data
                line_data = data['over'] if use_over else data['under']
                if not line_data:
                    continue
                
                # Insert row
                best_lines_table.insertRow(row_idx)
                
                # Column 0: Player name
                player_item = QTableWidgetItem(player_name)
                best_lines_table.setItem(row_idx, 0, player_item)
                
                # Column 1: Market type with OVER/UNDER
                line_type = "OVER" if use_over else "UNDER"
                # Clean up market name if needed
                market_name = market_type.replace('batter_', '') if 'batter_' in market_type else market_type
                market_item = QTableWidgetItem(f"{market_name} {line_type}")
                best_lines_table.setItem(row_idx, 1, market_item)
                
                # Column 2: Best Line format
                odds = line_data.get('odds', '')
                point = line_data.get('point', '')
                bookmaker = line_data.get('bookmaker', '')
                
                line_indicator = 'O' if use_over else 'U'
                best_line_text = f"{odds} {line_indicator} ({point}) @ {bookmaker}"
                line_item = QTableWidgetItem(best_line_text)
                best_lines_table.setItem(row_idx, 2, line_item)
                
                # Column 3: Average odds
                avg_odds = line_data.get('avg_odds', '')
                best_lines_table.setItem(row_idx, 3, QTableWidgetItem(str(avg_odds)))
                
                # Column 4: Deviation
                deviation = line_data.get('deviation', 0)
                deviation_text = f"+{deviation:.2f}%" if deviation > 0 else f"{deviation:.2f}%"
                deviation_item = QTableWidgetItem(deviation_text)
                
                # Color coding
                if deviation > 1.0:
                    deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green for high value
                elif deviation > 0.5:
                    deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow for medium value
                
                best_lines_table.setItem(row_idx, 4, deviation_item)
            
            # Resize columns
            best_lines_table.resizeColumnsToContents()
            
            # Make sure it's visible
            best_lines_table.setVisible(True)
            
            # Force update
            best_lines_table.update()
            
            print(f"Successfully populated Best Lines tab with {best_lines_table.rowCount()} rows")
            
        except Exception as e:
            import traceback
            print(f"ERROR in _populate_best_lines_widget: {str(e)}")
            traceback.print_exc()
        
