import qasync
import aiohttp
import pathlib
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QProgressBar, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QPushButton,
    QHBoxLayout, QGridLayout, QCheckBox, QScrollArea, QGroupBox, QTabWidget,
    QSplitter, QFrame
)
from propQuery import PropClient
from marketKeys import MAJOR_PROP_MARKETS
import LineCalculator
from TrackingStatsWidget import AdvancedStatsWidget
import asyncio
import statistics
from GUIbestlineswidget import BestLinesWidget
from PropWindowUtils import (MLBPropStats, market_stat_for, PlayerDetailPanel,
                             BullpenPanel)
#TODO: This file is massive need refactor soon or eventloop woopty is imminent 
#TODO: Best Lines widget is not populating, max tilt as its clearly a secret as to why
# -----------------------------------------------------------------------------

def extract_odds_point(value: str, indicator: str):
    """
    Parse a price string and extract the odds and point for the given indicator.

    Supported formats:
        "-142 O (15.5)" / "+120 U (15.5)"  -> indicator 'O' or 'U', point parsed
        "+150 Yes -180 No"                 -> indicator 'Yes' or 'No', point is None

    Returns:
        Tuple (odds, point) if successfully parsed, else None. For Yes/No
        markets point is None.
    """
    try:
        parts = value.split()
        if indicator in parts:
            idx = parts.index(indicator)
            if idx > 0:
                odds = float(parts[idx - 1])
                # Yes/No markets carry no point.
                if indicator in ('Yes', 'No'):
                    return odds, None
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
        self.row_stats = {}   # row_label -> PropStatSummary (filled async)
        self.row_points = {}  # row_label -> [point, ...] across books
        self.row_lineup = {}  # row_label -> slot int | -1 (posted, out) | "SP"
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
    # Stat columns rendered between Market/Outcome and the bookmaker columns.
    # Filled from tab_data.row_stats (PropStatSummary) when a stats backend
    # exists for the sport; left blank otherwise.
    STAT_COLUMNS = ["Szn", "L5", "L10", "Hit%", "L10 Hit%"]
    BOOK_COL_OFFSET = 1   # bookmakers directly after the label column;
                          # stat columns trail after the last bookmaker

    def stat_col_start(self, tab_data=None):
        td = tab_data if tab_data is not None else self.tab_data
        return self.BOOK_COL_OFFSET + len(td.bookmakers)

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
        self.table_widget.setMinimumSize(640, 360)
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

    def format_stat_values(self, summary):
        """PropStatSummary -> display strings for STAT_COLUMNS."""
        if summary is None:
            return [""] * len(self.STAT_COLUMNS)
        fmt = lambda v: f"{v:.2f}".rstrip('0').rstrip('.') if v else "0"
        pct = lambda v: "" if v is None else f"{v:.0%}"
        return [fmt(summary.season_avg), fmt(summary.l5_avg),
                fmt(summary.l10_avg), pct(summary.hit_rate),
                pct(summary.hit_rate_l10)]

    def update_table_display(self, table=None, tab_data=None):
        # Operates on the current tab by default; async stat fills pass their
        # captured table/tab_data so a mid-load tab switch can't cross wires.
        table = table if table is not None else self.table_widget
        tab_data = tab_data if tab_data is not None else self.tab_data
        self_tab_data, self.tab_data = self.tab_data, tab_data
        try:
            self._update_table_display(table, tab_data)
        finally:
            self.tab_data = self_tab_data

    def _update_table_display(self, table, tab_data):
        current_rows = table.rowCount()
        current_cols = table.columnCount()

        # Update table structure if needed
        expected_cols = 1 + len(self.tab_data.bookmakers)
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(
                ["Market/Outcome"] + self.tab_data.bookmakers)

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

            # Lineup flag: batting-order slot prefix, grey-out confirmed-out
            flag = self.tab_data.row_lineup.get(row_label)
            display = row_label
            if flag == "SP":
                display = f"[SP] {row_label}"
            elif flag == -1:
                display = f"✗ {row_label}"
                header_item.setForeground(QColor(145, 145, 150))
                font = header_item.font()
                font.setItalic(True)
                header_item.setFont(font)
            elif isinstance(flag, int) and flag > 0:
                display = f"[{flag}] {row_label}"

            # Compact stat suffix in the label cell (Szn/L5/L10 · Hit rates)
            summary = self.tab_data.row_stats.get(row_label)
            if summary is not None:
                szn, l5, l10, hit, hit10 = self.format_stat_values(summary)
                stats_txt = f"{szn} {l5} {l10}"
                rates = "/".join(x for x in (hit, hit10) if x)
                if rates:
                    stats_txt += f" · {rates}"
                display += f"   [{stats_txt}]"
            if header_item.text() != display:
                header_item.setText(display)
                needs_resize = True

            # Update bookmaker columns
            for col_idx, bm in enumerate(self.tab_data.bookmakers, self.BOOK_COL_OFFSET):
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
                    # Compare odds on whichever side both old and new values share
                    # (Over/Under/Yes/No) so the change flash works for every market.
                    current_odds = previous_odds = None
                    for ind in ('O', 'U', 'Yes', 'No'):
                        parsed = extract_odds_point(current_value, ind)
                        parsed_prev = extract_odds_point(previous_value, ind)
                        if parsed and parsed_prev:
                            current_odds = parsed[0]
                            previous_odds = parsed_prev[0]
                            break
                    if current_odds is not None:
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
        # Per-sport stats backend (MLB StatsAPI game logs for now; other
        # sports plug in here as their resolvers get built)
        self.is_mlb = sport_key.startswith("baseball_mlb")
        self.mlb_stats = MLBPropStats() if self.is_mlb else None
        self.player_detail_panel = None
        self.advanced_stats_widget = None
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
        
        # Cap the window to the monitor: async-loaded content (detail panel
        # tables, matchup strip) grows size hints and would otherwise push
        # the window off screen. Re-capped on move (monitor changes).
        self._cap_window_to_screen()

        self.icon_frame = 0
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)
        
        
        # Line highlighting colors - use alpha 180 to prevent override by update_table_display
        self.best_over_color = QColor(0, 200, 0, 180)  # Bright Green
        self.best_under_color = QColor(0, 100, 255, 180)  # Bright Blue
        self.best_text_color = QColor(255, 255, 255) # White Text
            
    def _cap_window_to_screen(self):
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.setMaximumSize(geo.width(), geo.height())
        if (self.width() > geo.width()) or (self.height() > geo.height()):
            self.resize(min(self.width(), geo.width()),
                        min(self.height(), geo.height()))

    def moveEvent(self, a0):
        # Re-cap when dragged to a different monitor
        self._cap_window_to_screen()
        super().moveEvent(a0)

    def UpdateIcon(self):
        framesdir = pathlib.Path(__file__).parent / "appicon_frames"
        next_icon = framesdir / f"frame{str(self.icon_frame).zfill(3)}.png"
        self.setWindowIcon(QIcon(str(next_icon)))
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
        
        self.tab_data_map = {}  # Maps tab index to LeagueTabData
        self.tab_market_map = {}  # Maps tab index to market key

        # Main area: horizontal splitter — odds grid on the left, side panel
        # (Games / Best Lines / Advanced Stats / Player Detail) on the right.
        # The old bottom strip is gone; the grid gets the full height.
        self.props_tab_widget = QTabWidget()
        self.props_tab_widget.currentChanged.connect(self.handle_tab_change)

        # Game selection lives in the side panel's first tab now
        self.game_selection_area = QScrollArea()
        self.game_selection_area.setWidgetResizable(True)
        self.game_selection_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.game_selection_area.setContentsMargins(0, 0, 0, 0)

        self.game_selection_widget = QWidget()
        self.game_selection_layout = QGridLayout(self.game_selection_widget)
        self.game_selection_layout.setContentsMargins(0, 0, 0, 0)
        self.game_selection_layout.setHorizontalSpacing(0)
        self.game_selection_layout.setVerticalSpacing(0)
        self.game_selection_area.setWidget(self.game_selection_widget)

        # Side tab panel. BestLinesWidget lives in a tab directly — no more
        # clone-table mirroring via integrate_stats_with_props_window.
        self.create_best_lines_widget()
        # _populate_best_lines_widget writes to best_lines_table when set;
        # pointing it at the real widget short-circuits its hierarchy walk.
        self.best_lines_table = self.best_lines_widget

        # Wrap BestLinesWidget in a container page: _populate_best_lines_widget
        # calls setVisible(True) on it, which would force it to paint over the
        # active tab if the widget itself were the page. As a child of a
        # hidden page, that setVisible is inert.
        best_lines_page = QWidget()
        best_lines_page_layout = QVBoxLayout(best_lines_page)
        best_lines_page_layout.setContentsMargins(0, 0, 0, 0)
        best_lines_page_layout.addWidget(self.best_lines_widget)

        self.bottom_tab_widget = QTabWidget()
        self.bottom_tab_widget.addTab(self.game_selection_area, "Games")
        self.bottom_tab_widget.addTab(best_lines_page, "Best Lines")
        self.advanced_stats_widget = AdvancedStatsWidget()
        self.bottom_tab_widget.addTab(self.advanced_stats_widget, "Advanced Stats")
        self.player_detail_panel = PlayerDetailPanel()
        self.player_detail_panel.stat_requested.connect(self._on_detail_stat_requested)
        # Scroll-wrap the panel: its async-populated tables/labels grow the
        # layout's MINIMUM size, and Qt resizes a window past maximumSize to
        # honor minimums — the scroll area's tiny minimum breaks that chain.
        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._detail_scroll.setWidget(self.player_detail_panel)
        self.bottom_tab_widget.addTab(self._detail_scroll, "Player Detail")
        self.advanced_stats_widget.set_sport(self.sport_key)

        # Left column: odds grid on top, dedicated bullpen section below
        # (MLB only — other sports just get the grid)
        self.bullpen_panel = None
        if self.is_mlb:
            left_splitter = QSplitter(Qt.Orientation.Vertical)
            left_splitter.addWidget(self.props_tab_widget)
            self.bullpen_panel = BullpenPanel(self.mlb_stats)
            left_splitter.addWidget(self.bullpen_panel)
            left_splitter.setStretchFactor(0, 3)
            left_splitter.setStretchFactor(1, 1)
            left_splitter.setCollapsible(0, False)
            left_widget = left_splitter
            asyncio.create_task(self._populate_bullpen_teams())
        else:
            left_widget = self.props_tab_widget

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(left_widget)
        self.main_splitter.addWidget(self.bottom_tab_widget)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setCollapsible(0, False)
        # Default split sized so BOTH sides clear their content widths on a
        # ~1920px window: bullpen table ~860px, detail panel ~820px minimum.
        # Ratio applies proportionally at first layout; draggable.
        self.main_splitter.setSizes([460, 540])
        self.layout.addWidget(self.main_splitter, stretch=1)

        # Load prop markets
        self.load_prop_markets()

        # Savant percentile leaderboards for the detail panel (MLB only)
        if self.is_mlb:
            asyncio.create_task(self._load_percentile_data())

        # Fetch and populate games
        async with aiohttp.ClientSession() as session:
            games = await self.prop_client.get_games(session)
            self.populate_game_selection(games)
    
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
        
        # Add all games in a single list. The events endpoint returns every
        # upcoming event (today AND later days, plus already-started games),
        # so each row needs its start time to be tellable apart — and only
        # today's not-yet-started games default to checked, since started
        # games have their props pulled and future games rarely have props
        # posted yet.
        from datetime import datetime as _dt
        now = _dt.now().astimezone()

        def _parse_commence(game):
            try:
                return _dt.fromisoformat(
                    (game.get('commence_time') or '').replace('Z', '+00:00')
                ).astimezone()
            except ValueError:
                return None

        for game in sorted(games, key=lambda g: g.get('commence_time') or ''):
            game_id = game.get('id', '')
            home_team = game.get('home_team', 'Unknown')
            away_team = game.get('away_team', 'Unknown')
            dt = _parse_commence(game)

            label = f"{away_team} vs {home_team}"
            checked = True
            if dt is not None:
                is_today = dt.date() == now.date()
                started = dt <= now
                when = (dt.strftime("%-I:%M %p") if is_today
                        else dt.strftime("%a %-m/%-d %-I:%M %p"))
                label += f"   ·  {when}"
                if started:
                    label += "  · STARTED"
                checked = is_today and not started

            # Create game item
            game_widget = QWidget()
            game_widget.setObjectName("gameItem")
            game_layout = QHBoxLayout(game_widget)
            game_layout.setContentsMargins(5, 2, 5, 2)  # Tighter margins

            checkbox = QCheckBox(label)
            checkbox.setChecked(checked)
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
        
        # Fills the Games tab of the side panel
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
        self.table_widget.cellClicked.connect(
            lambda row, _col, td=tab_data: self._on_prop_row_clicked(td, row))

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
                
                # If this tab ended up with no rows, say so in the table
                # instead of leaving a silent empty grid
                if not self.current_tab_data.table_rows:
                    self._show_table_message(
                        "No props returned for the selected game(s).\n"
                        "Likely causes: the game already started (books pull "
                        "props at first pitch), the market isn't posted yet "
                        "(common for tomorrow's games), or no book offers "
                        "this market. Check the start times in the Games tab.")

                # Update displays if we got any data
                if self.consolidated_odds_data['bookmakers']:
                    print("Updating displays with processed data")
                    self.find_best_lines()
                    self.update_table_display()
                    # Update best lines display right after processing data
                    self.update_best_lines_display()
                    # Highlight AFTER table display is updated to prevent override
                    self.highlight_best_lines()
                    # Kick off async per-player stat resolution for the grid
                    if self.mlb_stats is not None:
                        asyncio.create_task(self._load_prop_stats(
                            self.current_tab_data, self.table_widget, market_type))
                else:
                    print("No valid bookmaker data was processed")
    
        except Exception as e:
            print(f"Fatal error in refresh_data: {str(e)}")
            import traceback
            traceback.print_exc()
            # Ensure progress bar updates even on failure
            self.progress.setValue(0)
        
        self.progress.setValue(100)

    # ------------------------------------------------------------------
    # Per-row stats resolution (MLB StatsAPI game logs)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_row_market(row_label):
        """Split a row label into (player, market_key, rung_line|None).

        Labels are "{player} - {market_key}" or, for alternate ladders,
        "{player} - {market_key} {point}+" where the rung means >= point
        (so the effective over/under line is point - 0.5).
        """
        parts = row_label.split(' - ')
        if len(parts) < 2:
            return None, None, None
        player = parts[0]
        market = parts[1]
        tokens = market.split()
        if len(tokens) > 1 and tokens[-1].endswith('+'):
            try:
                rung = float(tokens[-1][:-1])
                return player, ' '.join(tokens[:-1]), rung - 0.5
            except ValueError:
                pass
        return player, market, None

    async def _load_prop_stats(self, tab_data, table, market_type):
        """Resolve game-log summaries for every row of a freshly filled tab
        and repaint its stat columns."""
        rows = []
        for row_label in tab_data.table_rows:
            row_data = tab_data.table_data.get(row_label, {})
            if row_data.get('is_header'):
                continue
            player, market_key, rung_line = self._parse_row_market(row_label)
            if not player or market_stat_for(market_key) is None:
                continue
            line = rung_line
            if line is None:
                points = tab_data.row_points.get(row_label)
                if points:
                    line = statistics.median(points)
            rows.append((row_label, player, market_key, line))

        if not rows:
            return
        print(f"Loading prop stats for {len(rows)} rows ({market_type})")
        try:
            async with aiohttp.ClientSession() as session:
                summaries = await self.mlb_stats.summarize_many(session, rows)
        except Exception as e:
            print(f"Prop stats load failed: {e}")
            return

        resolved = {k: v for k, v in summaries.items() if v is not None}
        tab_data.row_stats.update(resolved)
        print(f"Prop stats resolved for {len(resolved)}/{len(rows)} rows")

        # Lineup flags per row: batting-order slot, confirmed-out, probable SP
        try:
            async with aiohttp.ClientSession() as session:
                lineup_maps = await self.mlb_stats.get_lineup_maps(session)
        except Exception as e:
            print(f"Lineup maps load failed: {e}")
            lineup_maps = {}
        for label, summ in resolved.items():
            m = lineup_maps.get(summ.team)
            if not m:
                continue
            if summ.market_key.startswith("pitcher"):
                if m.get("probable") == summ.player_id:
                    tab_data.row_lineup[label] = "SP"
            elif m.get("posted"):
                slot = m["slots"].get(summ.player_id)
                tab_data.row_lineup[label] = slot if slot else -1
        try:
            self.update_table_display(table, tab_data)
        except RuntimeError:
            pass  # table was deleted (tab/window closed mid-fetch)

    def _on_prop_row_clicked(self, tab_data, row):
        """Prop row click -> populate the Player Detail tab."""
        if self.player_detail_panel is None or row >= len(tab_data.table_rows):
            return
        row_label = tab_data.table_rows[row]
        summary = tab_data.row_stats.get(row_label)
        if summary is not None:
            self._show_player_detail(summary)
        elif self.mlb_stats is not None:
            # Stats not resolved yet (or row was skipped) — fetch on demand
            player, market_key, rung_line = self._parse_row_market(row_label)
            if not player or market_stat_for(market_key) is None:
                return
            line = rung_line
            if line is None:
                points = tab_data.row_points.get(row_label)
                if points:
                    line = statistics.median(points)
            asyncio.create_task(
                self._load_single_summary(tab_data, row_label, player,
                                          market_key, line))

    async def _load_single_summary(self, tab_data, row_label, player,
                                   market_key, line):
        try:
            async with aiohttp.ClientSession() as session:
                summary = await self.mlb_stats.summarize(
                    session, player, market_key, line)
        except Exception as e:
            print(f"On-demand stat fetch failed for {player}: {e}")
            return
        if summary is None:
            return
        tab_data.row_stats[row_label] = summary
        self._show_player_detail(summary)

    def _show_player_detail(self, summary, switch_tab=True):
        """Show a summary in the detail panel and kick the async context
        fetches (matchup + pitch-level splits — both cached in MLBPropStats)."""
        self.player_detail_panel.show_summary(summary)
        if switch_tab:
            self.bottom_tab_widget.setCurrentWidget(self._detail_scroll)
        if self.mlb_stats is not None:
            asyncio.create_task(self._load_matchup(summary))
            asyncio.create_task(self._load_pitch_splits(summary))
            asyncio.create_task(self._load_traditional_stats(summary))

    async def _load_traditional_stats(self, summary):
        group = ("pitching" if summary.market_key.startswith("pitcher")
                 else "hitting")
        try:
            async with aiohttp.ClientSession() as session:
                pairs = await self.mlb_stats.get_traditional_stats(
                    session, summary.player_id, group)
        except Exception as e:
            print(f"Traditional stats load failed for {summary.player_name}: {e}")
            return
        if (pairs and self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_traditional(pairs)
        # FG batting join for hitters: wOBA/wRC+/WAR onto the strip and the
        # swing-tracking line (first call boots the headless fetch; cached 6h)
        if group == "hitting":
            try:
                fgb = await self.mlb_stats.get_fg_batting(summary.player_id)
            except Exception as e:
                print(f"FG batting load failed: {e}")
                return
            if (fgb and self.player_detail_panel.current_player_name()
                    == summary.player_name):
                extra = []
                if fgb.get("woba") is not None:
                    extra.append(("wOBA", f"{fgb['woba']:.3f}".lstrip("0")))
                if fgb.get("wrcplus") is not None:
                    extra.append(("wRC+", f"{fgb['wrcplus']:.0f}"))
                if fgb.get("war") is not None:
                    extra.append(("WAR", f"{fgb['war']:.1f}"))
                self.player_detail_panel.show_traditional(pairs + extra)
                self.player_detail_panel.show_swing(fgb)

    async def _load_pitch_splits(self, summary):
        player_type = ("pitcher" if summary.market_key.startswith("pitcher")
                       else "batter")
        try:
            async with aiohttp.ClientSession() as session:
                splits = await self.mlb_stats.get_pitch_splits(
                    session, summary.player_id, player_type)
                velo_splits = await self.mlb_stats.get_velo_splits(
                    session, summary.player_id, player_type)
        except Exception as e:
            print(f"Pitch splits load failed for {summary.player_name}: {e}")
            return
        if self.player_detail_panel.current_player_name() == summary.player_name:
            self.player_detail_panel.show_pitch_splits(
                splits, player_type, velo_splits)
            try:
                async with aiohttp.ClientSession() as session:
                    spray = await self.mlb_stats.get_spray_points(
                        session, summary.player_id, player_type)
            except Exception as e:
                print(f"Spray load failed: {e}")
                spray = []
            if self.player_detail_panel.current_player_name() == summary.player_name:
                self.player_detail_panel.set_spray(spray)

    async def _load_matchup(self, summary):
        is_pitcher_prop = summary.market_key.startswith("pitcher")
        try:
            async with aiohttp.ClientSession() as session:
                ctx = await self.mlb_stats.get_matchup(
                    session, summary.team,
                    include_opp_batting=is_pitcher_prop,
                    batter_id=None if is_pitcher_prop else summary.player_id,
                    pitcher_id=summary.player_id if is_pitcher_prop else None)
        except Exception as e:
            print(f"Matchup load failed for {summary.team}: {e}")
            return
        # Only render if the panel still shows the same player
        if self.player_detail_panel.current_player_name() == summary.player_name:
            self.player_detail_panel.show_matchup(ctx)
        # Bullpen fatigue: opposing pen for batters (they face it late),
        # own pen for pitcher props (win/outs context)
        # Sync the dedicated bullpen section: opposing pen for batters,
        # own pen for pitcher props, own pen fallback on off days
        if self.bullpen_panel is not None:
            pen_team = (summary.team if (is_pitcher_prop or ctx is None)
                        else ctx.opponent)
            context = (f"{summary.player_name} — "
                       + ("opponent pen" if (not is_pitcher_prop and ctx)
                          else "own pen"))
            self.bullpen_panel.show_team(pen_team, context)

        # SP deep card: opposing SP for batter props, own arsenal for
        # pitcher props. Also feeds arm angle + Stuff+ back into the
        # matchup line and the arsenal-highlighted splits rows.
        if is_pitcher_prop:
            card_pid, card_name, card_hand = (
                summary.player_id, summary.player_name, None)
        elif ctx is not None and ctx.opp_pitcher_id:
            card_pid, card_name, card_hand = (
                ctx.opp_pitcher_id, ctx.opp_pitcher_name, ctx.opp_pitcher_hand)
        else:
            return
        try:
            async with aiohttp.ClientSession() as session:
                arsenal = None
                if not is_pitcher_prop:
                    arsenal = await self.mlb_stats.get_pitch_arsenal(
                        session, card_pid)
                    if (arsenal and self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.set_opposing_arsenal(
                            card_name, arsenal)
                card = await self.mlb_stats.get_sp_deep_card(session, card_pid)
        except Exception as e:
            print(f"SP card load failed for {card_name}: {e}")
            return
        if self.player_detail_panel.current_player_name() != summary.player_name:
            return
        self.player_detail_panel.set_sp_card(card, card_name, card_hand)
        sp_stuff = (card or {}).get("fg")
        if not is_pitcher_prop and ctx is not None:
            if card is not None:
                ctx.opp_pitcher_arm = card.get("arm_angle")
            if sp_stuff:
                ctx.opp_pitcher_stuff = sp_stuff
            self.player_detail_panel.show_matchup(ctx)
            if arsenal and sp_stuff:
                self.player_detail_panel.set_opposing_arsenal(
                    card_name, arsenal, sp_stuff)

    async def _populate_bullpen_teams(self):
        """Fill the bullpen panel's team combo once the team list is known."""
        try:
            async with aiohttp.ClientSession() as session:
                if await self.mlb_stats.ensure_roster(session):
                    self.bullpen_panel.set_teams(
                        list(self.mlb_stats._teams.values()))
        except Exception as e:
            print(f"Bullpen team list load failed: {e}")

    def _on_detail_stat_requested(self, market_key, line):
        """Detail panel stat/line switch -> re-summarize the shown player.
        Game logs are cached in MLBPropStats so this is usually instant."""
        if self.mlb_stats is None:
            return
        player = self.player_detail_panel.current_player_name()
        if not player:
            return
        asyncio.create_task(self._load_detail_summary(player, market_key, line))

    async def _load_detail_summary(self, player, market_key, line):
        try:
            async with aiohttp.ClientSession() as session:
                summary = await self.mlb_stats.summarize(
                    session, player, market_key, line)
        except Exception as e:
            print(f"Detail stat switch failed for {player}: {e}")
            return
        if summary is not None:
            self._show_player_detail(summary, switch_tab=False)

    async def _load_percentile_data(self):
        """Fetch Savant percentile leaderboards off-thread for the detail
        panel's skill bars."""
        try:
            from MLBpercentilerankings import (fetch_leaderboard_data,
                                               PITCHER_URL, HITTER_URL)
            loop = asyncio.get_event_loop()
            hitters = await loop.run_in_executor(
                None, fetch_leaderboard_data, HITTER_URL)
            pitchers = await loop.run_in_executor(
                None, fetch_leaderboard_data, PITCHER_URL)
            if self.player_detail_panel is not None:
                self.player_detail_panel.set_percentile_data(hitters, pitchers)
                print("Savant percentile data loaded for detail panel")
        except Exception as e:
            print(f"Percentile data load failed: {e}")

    def _show_table_message(self, msg):
        """Show an informational message inside the current (empty) tab."""
        table = self.table_widget
        if table is None:
            return
        table.setColumnCount(1)
        table.setHorizontalHeaderLabels(["No data"])
        table.setRowCount(1)
        item = QTableWidgetItem(msg)
        item.setForeground(QColor(160, 160, 165))
        table.setItem(0, 0, item)
        table.resizeRowsToContents()
        table.resizeColumnsToContents()

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
        
        # First reset all highlighting (skip label + trailing stat columns —
        # they manage their own colors in update_table_display)
        book_end = self.stat_col_start(self.current_tab_data)
        for row in range(table.rowCount()):
            for col in range(self.BOOK_COL_OFFSET, book_end):
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
                    col_idx = self.current_tab_data.bookmakers.index(over_bm) + self.BOOK_COL_OFFSET
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
                    col_idx = self.current_tab_data.bookmakers.index(under_bm) + self.BOOK_COL_OFFSET
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

        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in self.current_tab_data.bookmakers:
                self.current_tab_data.bookmakers.append(bm_title)

            # Group outcomes by row label. Handles three outcome shapes:
            #   - Over/Under markets   (name == "Over"/"Under", shared point)
            #   - Yes/No markets       (name == "Yes"/"No", e.g. anytime TD,
            #                           goal scorer, double-double, pitcher win)
            #   - bare selections      (no over/under/yes/no label -> treated as Yes)
            # Alternate markets are ladders: each point becomes its own row so
            # successive rungs don't overwrite one another.
            grouped_markets = {}
            for market in bm['markets']:
                market_key = market['key']
                is_alternate = market_key.endswith('_alternate')
                for outcome in market['outcomes']:
                    name_raw = outcome.get('name', '') or ''
                    side = name_raw.lower()
                    description = outcome.get('description')
                    point = outcome.get('point')
                    price = outcome.get('price', '')

                    if side in ('over', 'under', 'yes', 'no'):
                        player_name = description or name_raw
                    else:
                        # No standard side label: the outcome name is the
                        # selection itself (player/team). Treat as an implicit Yes.
                        player_name = description or name_raw
                        side = 'yes'

                    if is_alternate and point is not None:
                        label = f"{player_name} - {market_key} {point}+"
                    else:
                        label = f"{player_name} - {market_key}"

                    slot = grouped_markets.setdefault(label, {
                        'over': None, 'under': None,
                        'yes': None, 'no': None,
                        'point': point,
                    })
                    if point is not None:
                        slot['point'] = point
                        # Track every book's point so the stats layer can use
                        # a consensus line for hit-rate calculations.
                        self.current_tab_data.row_points.setdefault(label, []).append(point)
                    slot[side] = price

            # Process the grouped markets into table data
            for label, data in grouped_markets.items():
                if label not in self.current_tab_data.table_rows:
                    self.current_tab_data.table_rows.append(label)
                    self.current_tab_data.table_data[label] = {'game_id': game_id}

                over_price = data['over']
                under_price = data['under']
                yes_price = data['yes']
                no_price = data['no']
                point = data['point'] if data['point'] is not None else ''

                if over_price is not None and under_price is not None:
                    price = f"{over_price} O ({point}) {under_price} U"
                elif over_price is not None:
                    price = f"{over_price} O ({point})"
                elif under_price is not None:
                    price = f"{under_price} U ({point})"
                elif yes_price is not None and no_price is not None:
                    price = f"{yes_price} Yes {no_price} No"
                elif yes_price is not None:
                    price = f"{yes_price} Yes"
                elif no_price is not None:
                    price = f"{no_price} No"
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


# -----------------------------------------------------------------------------
# Standalone launcher (development convenience — normally opened from
# EffortOdds via the Props button):
#   python EffortOddsPropsWindow.py            -> MLB
#   python EffortOddsPropsWindow.py NBA        -> any league in SPORTS_MARKETS
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    from marketKeys import SPORTS_MARKETS

    league = sys.argv[1] if len(sys.argv) > 1 else "MLB"
    sport_key = None
    for leagues in SPORTS_MARKETS.values():
        if league in leagues:
            sport_key = leagues[league]
            break
    if sport_key is None or sport_key not in MAJOR_PROP_MARKETS:
        with_props = sorted(
            name for leagues in SPORTS_MARKETS.values()
            for name, key in leagues.items() if key in MAJOR_PROP_MARKETS)
        print(f"No prop markets for '{league}'. Leagues with props: "
              f"{', '.join(with_props)}")
        sys.exit(1)

    app = QApplication(sys.argv)
    # qasync loop must exist before PropsWindow's deferred async init fires
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = PropsWindow(sport_key, league)
    window.show()
    with loop:
        loop.run_forever()

