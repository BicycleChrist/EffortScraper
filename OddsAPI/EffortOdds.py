import qasync
import asyncio
from datetime import datetime
import aiohttp
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QIcon,QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QHBoxLayout, QFrame, QSizePolicy, QGridLayout, QSplitter
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query
from marketKeys import *
from EffortOddsPropsWindow import PropsWindow
import pandas as pd
from GUItuneinwidget import TuneInWidget
from GUIteamnewswidget import TeamNewsWidget
from GUIbestlineswidget import *
from HistoricalOddsClient import *

#TODO: MMA (Mixed Marital Arts) Markets ouput is nuked, gotta investigate that one
#TODO: Auto update cuts off last line and errors-out due to progress-bar apparently no longer existing.
# League market configurations
#TODO: Toggeling on News widget removes ability to resize odds window/Best Lines & Historical widget



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
    
    def to_dataframe(self):
        """Convert the current table data to a pandas DataFrame"""
        # Create a DataFrame with rows as indices and bookmakers as columns
        df = pd.DataFrame(index=self.table_rows, columns=self.bookmakers)
        
        # Fill the DataFrame with current odds values
        for row_label in self.table_rows:
            row_data = self.table_data.get(row_label, {})
            for bm in self.bookmakers:
                df.at[row_label, bm] = row_data.get(bm, "")
        
        # Add game_id and is_header columns for reference
        df['game_id'] = [self.table_data.get(row, {}).get('game_id', '') for row in self.table_rows]
        df['is_header'] = [self.table_data.get(row, {}).get('is_header', False) for row in self.table_rows]
        
        return df
    
    def update_from_dataframe(self, df):
        """Update table data from a pandas DataFrame"""
        # Update bookmakers list if needed
        bm_columns = [col for col in df.columns if col not in ['game_id', 'is_header']]
        for bm in bm_columns:
            if bm not in self.bookmakers:
                self.bookmakers.append(bm)
        
        # Update rows
        self.table_rows = df.index.tolist()
        
        # Update table data
        for row_label in self.table_rows:
            if row_label not in self.table_data:
                self.table_data[row_label] = {}
            
            # Set game_id and is_header
            self.table_data[row_label]['game_id'] = df.at[row_label, 'game_id']
            self.table_data[row_label]['is_header'] = df.at[row_label, 'is_header']
            
            # Set bookmaker data
            for bm in bm_columns:
                if pd.notna(df.at[row_label, bm]) and df.at[row_label, bm] != "":
                    self.table_data[row_label][bm] = df.at[row_label, bm]

    
    
    
        



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
    
    fetch_odds_button_style = """
    QPushButton {
        background-color: #dc9437;  /* Orange */
        color: white; /* text color */
        border: 1px solid #0056b3;
        padding: 5px 10px;
        margin-right: 5px;
        border-radius: 4px;
        width: 200px;
        font-size: 24px;
    }"""
    
    props_button_style = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 3px solid #0056b3;
            border-radius: 4px;
            width: 72px;
            height: 36px;
            font-size: 12px;
        }
        QPushButton:disabled {
            background-color: #909090;
        }
    """
    
    def __init__(self):
        super().__init__()
        self.region_selector = None
        self.timer = QTimer()
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.selected_markets = {"spreads"}  # Initialize with default market
        self.init_ui()
        self.connect_signals()
        self.selected_region = "us" # default region should always be a string
        
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
    

    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)
        self.setWindowIcon(QIcon("/home/retupmoc/Desktop/EffortScraper/OddsAPI/AppIcon.png"))
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.layout = QVBoxLayout(main_widget)
        
        # --------- TOP SECTION ---------
        # League selection dropdown
        league_layout = QHBoxLayout()
        league_layout.addWidget(QLabel("Select League:"))
        self.league_selector = QComboBox()
        league_layout.addWidget(self.league_selector)
        
        # Add streaming toggle button to the right of league selector
        league_layout.addStretch(1)  # Push the button to the right
        
        # Create toggle buttons
        self.stream_toggle_button = QPushButton("Show Streaming Links ▼")
        self.stream_toggle_button.setCheckable(True)
        self.stream_toggle_button.setChecked(False)
        self.stream_toggle_button.clicked.connect(self.toggle_streaming_links)
        self.stream_toggle_button.setFixedWidth(150)
        self.stream_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        # Create news toggle button
        self.news_toggle_button = QPushButton("Show Injury News ▼")
        self.news_toggle_button.setCheckable(True)
        self.news_toggle_button.setChecked(False)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        self.news_toggle_button.setFixedWidth(150)
        self.news_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        # Create historical odds toggle button
        self.history_toggle_button = QPushButton("Show Historical Odds ▼")
        self.history_toggle_button.setCheckable(True)
        self.history_toggle_button.setChecked(False)
        self.history_toggle_button.clicked.connect(self.toggle_historical_odds)
        self.history_toggle_button.setFixedWidth(150)
        self.history_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        # Create a dedicated container for the right side elements
        right_side_container = QWidget()
        right_side_layout = QVBoxLayout(right_side_container)
        right_side_layout.setContentsMargins(0, 0, 0, 0)  # No margins
        right_side_layout.setSpacing(2)  # Minimal spacing between buttons and widget
        
        # Create a horizontal layout for the buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.stream_toggle_button)
        buttons_layout.addWidget(self.news_toggle_button)
        buttons_layout.addWidget(self.history_toggle_button)  # Add historical odds toggle button
        buttons_layout.setSpacing(4)  # Small spacing between buttons
        
        # Add the buttons layout to the right side layout
        right_side_layout.addLayout(buttons_layout)
        
        # Add the right side container to the league layout
        league_layout.addWidget(right_side_container, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        
        self.layout.addLayout(league_layout)
        
        # --------- REGION SECTION ---------
        # Region selection checkboxes
        region_label = QLabel("Select Region:")
        self.layout.addWidget(region_label)
        
        region_container = QWidget()
        region_layout = QGridLayout(region_container)
        region_layout.setSpacing(10)
        
        self.region_checkboxes = {}
        regions = ['us', 'us2', 'eu', 'au', 'uk', 'global']
        
        # Create checkboxes in a 3x2 grid (3 columns, 2 rows)
        for i, region in enumerate(regions):
            checkbox = QCheckBox(region)
            if region == 'us':
                checkbox.setChecked(True)
            checkbox.stateChanged.connect(lambda state, r=region: self.handle_region_change(r))
            self.region_checkboxes[region] = checkbox
            region_layout.addWidget(checkbox, i // 3, i % 3)
        
        region_container.setFixedHeight(58)
        region_container.setFixedWidth(200)
        self.layout.addWidget(region_container)
        
        # --------- MARKET AND STREAMING SECTION ---------
        # This is where the key change happens - we put the streaming widget in the same row as the market buttons
        
        # Create a horizontal layout for markets and streaming
        market_streaming_layout = QHBoxLayout()
        
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
            btn.setStyleSheet(self.BUTTON_STYLE)
            left_layout.addWidget(btn)
        
        # Add fetch button
        self.fetch_odds_button = QPushButton("Fetch Odds 🎰")
        self.fetch_odds_button.setStyleSheet(self.fetch_odds_button_style)
        left_layout.addWidget(self.fetch_odds_button)
        
        # Add props button
        self.props_button = QPushButton("Props ➣➣")
        self.props_button.setObjectName("market_props")
        self.props_button.setEnabled(False)
        self.props_button.setStyleSheet(self.props_button_style)
        self.market_buttons["props"] = self.props_button
        left_layout.addWidget(self.props_button)
        
        # Add props availability label
        self.props_availability_label = QLabel("No Props available for this league")
        self.props_availability_label.setStyleSheet("color: #6c757d; font-style: italic;")
        self.props_availability_label.setVisible(False)
        left_layout.addWidget(self.props_availability_label)
        
        # Add the left container to the market_streaming_layout
        market_streaming_layout.addWidget(left_container)
        
        # Add a stretch to push everything to the left and right
        market_streaming_layout.addStretch(1)
        
        # Create and configure the TuneInWidget
        self.tune_in_widget = TuneInWidget()
        self.tune_in_widget.setVisible(False)  # Hidden by default
        self.tune_in_widget.setFixedWidth(650)  # Fixed width to make it compact
        market_streaming_layout.addWidget(self.tune_in_widget)
        
        # Add the market_streaming_layout to the main layout
        self.layout.addLayout(market_streaming_layout)
        
        # --------- PROGRESS BAR ---------
        # Progress bar
        self.progress = QProgressBar()
        self.layout.addWidget(self.progress)
        
        # --------- ODDS SECTION ---------
        # Tab widget for different leagues
        self.tab_widget = QTabWidget()
        self.layout.addWidget(QLabel("Odds:"))
        
        # Create the team news widget first
        self.team_news_widget = TeamNewsWidget()
        self.team_news_widget.setVisible(False)  # Hidden by default
        self.team_news_widget.setFixedWidth(650)  # Fixed width
        
        # Create news container and add the team news widget
        self.news_container = QWidget()
        self.news_container.setFixedWidth(650)  # Match the width of the widget
        news_container_layout = QVBoxLayout(self.news_container)
        news_container_layout.setContentsMargins(0, 0, 0, 0)  # Zero margins
        news_container_layout.setSpacing(0)  # Zero spacing
        news_container_layout.addWidget(self.team_news_widget)
        
        # Set initial state for the news container
        self.news_container.setVisible(False)  # Hide container initially
        
        # Create the best lines container
        self.best_lines_container = QWidget()
        best_lines_layout = QVBoxLayout(self.best_lines_container)
        best_lines_layout.setContentsMargins(0, 0, 0, 0)
        
        # Add a header
        best_lines_header = QLabel("Best Lines ⮟")
        best_lines_header.setStyleSheet("font-weight: bold; font-size: 14px; color: #7bd419")
        best_lines_layout.addWidget(best_lines_header)
        
        # Create the best lines widget
        self.best_lines_widget = BestLinesWidget()
        best_lines_layout.addWidget(self.best_lines_widget)
        
        # Create the historical odds container
        self.historical_odds_container = QWidget()
        historical_odds_layout = QVBoxLayout(self.historical_odds_container)
        historical_odds_layout.setContentsMargins(0, 0, 0, 0)
        self.historical_odds_container.setFixedWidth(750)
        
        # Create the historical odds widget
        self.historical_odds_widget = HistoricalOddsWidget()
        self.historical_odds_widget.api_key = SUPER_KEY  # Set API key
        historical_odds_layout.addWidget(self.historical_odds_widget)
        
        # Initially hide the historical container (important!)
        self.historical_odds_container.setVisible(False)
        self.historical_odds_widget.setVisible(False)
        
        # Create a container for the bottom right section (best lines + historical odds)
        right_bottom_container = QWidget()
        right_bottom_layout = QHBoxLayout(right_bottom_container)
        right_bottom_layout.setContentsMargins(0, 0, 0, 0)
        right_bottom_layout.addWidget(self.best_lines_container)
        right_bottom_layout.addWidget(self.historical_odds_container)
        
        # First, create a horizontal splitter for the bottom section
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Add the news container to the left side of horizontal splitter
        self.horizontal_splitter.addWidget(self.news_container)
        
        # Add the right bottom container to the right side of horizontal splitter  
        self.horizontal_splitter.addWidget(right_bottom_container)
        
        # Set initial sizes for horizontal splitter
        self.horizontal_splitter.setSizes([325, 325])  # Equal width initially
        
        # Now create the vertical splitter with tab widget on top and horizontal splitter on bottom
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.addWidget(self.tab_widget)
        self.vertical_splitter.addWidget(self.horizontal_splitter)
        
        # Set initial sizes for vertical splitter to show more of the tab widget
        self.vertical_splitter.setSizes([400, 200])
        
        # Add the vertical splitter to the main layout
        self.layout.addWidget(self.vertical_splitter, 1)  # The 1 gives it stretch
        
        # --------- AUTO-UPDATE CONTROLS ---------
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
        
        self.layout.addLayout(update_controls_layout)


    def update_market_selection(self):
        """Update selected markets based on button states"""
        self.selected_markets = {market for market, btn in self.market_buttons.items() 
                               if btn.isChecked() and btn.isEnabled()}
        print("Selected markets:", self.selected_markets)

    def handle_league_change(self):
        """Handle league selection changes"""
        
        # Ensure self.props_button exists before proceeding
        if not hasattr(self, "props_button"):
            print("Warning: props_button does not exist yet. Skipping handle_league_change.")
            return
        
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)
    
        has_props = sport_key in MAJOR_PROP_MARKETS  # Check if this league has props
        self.props_button.setEnabled(has_props)
        self.props_availability_label.setVisible(not has_props)
        # self.update_market_selection() # not necessary?
        if hasattr(self, 'team_news_widget'):
            self.team_news_widget.handle_league_change(sport_key)

    
    
    def handle_region_change(self, region):
        """Handle region selection changes"""
        if region == 'global' and self.region_checkboxes['global'].isChecked():
            # Check all other regions when global is selected
            for r, cb in self.region_checkboxes.items():
                if r != 'global':
                    cb.setChecked(True)
            self.selected_region = "us,us2,eu,au,uk"  # Ensure it's a string, not a set
        else:
            # If global is unchecked, uncheck it when selecting individual regions
            if region != 'global':
                self.region_checkboxes['global'].setChecked(False)
            
            # Get all selected regions except 'global'
            selected = [r for r, cb in self.region_checkboxes.items() 
                       if cb.isChecked() and r != 'global']
            
            # Join selected regions with commas or default to "us"
            self.selected_region = ",".join(selected) if selected else "us"  # Ensure string format
        
        print(f"Selected regions: {self.selected_region}")
    
    def get_valid_markets(self, sport_key):
        """Get valid markets for the selected sport"""
        valid_markets = REGULAR_MARKETS.copy()
        if sport_key in MAJOR_PROP_LEAGUES:
            valid_markets.update(MAJOR_PROP_LEAGUES[sport_key].keys())
        return valid_markets
    
    def handle_props_button(self):
        """Handle Props button click to open PropsWindow."""
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)
        
        print(f"Props button clicked. League: {selected_league}, Sport Key: {sport_key}")  # Debug print
        
        if sport_key in MAJOR_PROP_MARKETS:  # Ensure props exist for this league
            print("Props are available for this league. Creating PropsWindow...")  # Debug print
            if not hasattr(self, "props_window") or self.props_window is None:
                self.props_window = PropsWindow(sport_key, selected_league)
            self.props_window.show()
            self.props_window.activateWindow()  # Brings the window to the front if it already exists
        else:
            print("No props available for this league.")  # Debug print

    # overriding inherited method for custom keybinds
    def keyPressEvent(self, a0):
        self.clearFocus()
        print(f"Keypress: {a0.key()}")
        if (a0.key() == Qt.Key.Key_1):
            if not self.props_button.isEnabled(): return;
            self.handle_props_button()
            return
        super().keyPressEvent(a0) # delegate back to base keybind handling
        return

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.league_selector.currentTextChanged.connect(self.handle_league_change)
        for btn in self.market_buttons.values():
            btn.toggled.connect(self.update_market_selection)
        self.fetch_odds_button.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.update_interval.valueChanged.connect(self.update_timer_interval)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)
        self.timer.timeout.connect(self.refresh_data)
        self.props_button.clicked.connect(self.handle_props_button)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        


    def RestartTimer(self):
        interval_ms = self.update_interval.value() * 60 * 1000
        self.timer.start(interval_ms)
        return interval_ms
        
    def update_region(self):
        selected_region = self.region_selector.currentText()
        print(f"Selected bookmaker region: {selected_region}")
        # Modify your odds fetching logic based on selected region here

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

    async def populate_leagues(self, default_league="MLB"):
        """Fetch and populate leagues in the dropdown"""
        leagues = await self.data_manager.fetch_leagues()
        print("Fetched leagues:", leagues)
        self.league_selector.clear()
        self.data_manager.league_map.clear()
    
        for sport_category, league_list in leagues.items():
            for league in league_list:
                self.league_selector.addItem(league['title'])
                self.data_manager.league_map[league['title']] = league['key']
        
        self.league_selector.setCurrentText(default_league)
        
        # Call handle_league_change after populating
        if self.league_selector.count() > 0:
            self.handle_league_change()

    def handle_tab_change(self, index):
        """Handle tab switching events"""
        if index >= 0:
            self.current_league = self.tab_widget.tabText(index)
            # Extract the league name without the market info
            if "(" in self.current_league:
                base_league = self.current_league.split(" (")[0]
                # Optionally update league selector to match the tab
                self.league_selector.setCurrentText(base_league)

    def create_league_tab(self, league_name, sport_key, selected_markets=None):
        """Create a new tab for a league with specific markets"""
        # Create a unique tab identifier based on league name and selected markets
        markets_str = "+".join(sorted(selected_markets)) if selected_markets else "default"
        tab_id = f"{league_name} ({markets_str})"
        
        if tab_id not in self.league_tabs:
            tab_data = LeagueTabData(league_name, sport_key)
            table_widget = tab_data.create_table_widget()
            
            # Connect selection signal for the new table
            table_widget.itemSelectionChanged.connect(self.on_market_selection_changed)
            
            self.tab_widget.addTab(table_widget, tab_id)
            self.league_tabs[tab_id] = tab_data
            self.current_league = tab_id
            self.tab_widget.setCurrentIndex(self.tab_widget.count() - 1)
        return self.league_tabs[tab_id]


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
    
    def toggle_streaming_links(self):
        """Toggle visibility of the streaming links widget"""
        visible = self.stream_toggle_button.isChecked()
        self.tune_in_widget.setVisible(visible)
        
        # Update button text
        if visible:
            self.stream_toggle_button.setText("Hide Streaming Links ▲")
        else:
            self.stream_toggle_button.setText("Show Streaming Links ▼")
    
    
    def toggle_news_feed(self):
        """Toggle visibility of the news feed widget with optimized spacing"""
        visible = self.news_toggle_button.isChecked()
        
        # Hide the streaming widget if showing news
        if visible:
            self.stream_toggle_button.setChecked(False)
            self.tune_in_widget.setVisible(False)
            self.stream_toggle_button.setText("Show Streaming Links ▼")
        
        # Make the widget visible first (needed for proper layout calculations)
        self.news_container.setVisible(visible)
        self.team_news_widget.setVisible(visible)
        
        # Update button text and adjust container size
        if visible:
            self.news_toggle_button.setText("Hide Injury News ▲")
            
            # Calculate exact height for 3 articles
            article_height = 85
            container_height = (article_height * 3)
            
            # Set exact fixed height instead of min/max
            self.news_container.setFixedHeight(container_height)
            
            # KEY FIX: Set negative top margin on progress bar to pull it upward
            prog_margins = self.progress.contentsMargins()
            prog_margins.setTop(-100)  # Adjust this value as needed
            self.progress.setContentsMargins(prog_margins)
            
            # Set minimal spacing in main layout
            self.layout.setSpacing(0)
        else:
            self.news_toggle_button.setText("Show Injury News ▼")
            
            # Collapse container completely
            self.news_container.setFixedHeight(0)
            
            # Reset progress bar margins to normal
            prog_margins = self.progress.contentsMargins()
            prog_margins.setTop(0)
            self.progress.setContentsMargins(prog_margins)
            
            # Reset layout spacing
            self.layout.setSpacing(0)
        
        # Force update to apply changes
        QTimer.singleShot(10, self.update)
        
        
    def toggle_historical_odds(self):
        """Toggle visibility of the historical odds widget"""
        visible = self.history_toggle_button.isChecked()
        
        # Hide the streaming widget if showing historical odds
        if visible:
            self.stream_toggle_button.setChecked(False)
            self.tune_in_widget.setVisible(False)
            self.stream_toggle_button.setText("Show Streaming Links ▼")
            
            # Ensure news widget is also hidden to prevent UI conflicts
            self.news_toggle_button.setChecked(False)
            self.team_news_widget.setVisible(False)
            self.news_container.setVisible(False)
            self.news_toggle_button.setText("Show Injury News ▼")
        
        # Make the historical odds container visible/invisible based on toggle state
        self.historical_odds_container.setVisible(visible)
        
        # Update button text
        if visible:
            self.history_toggle_button.setText("Hide Historical Odds ▲")
        else:
            self.history_toggle_button.setText("Show Historical Odds ▼")
            
            # If we're hiding, cancel any running data loads
            if hasattr(self.historical_odds_widget, '_load_task') and self.historical_odds_widget._load_task:
                try:
                    self.historical_odds_widget._load_task.cancel()
                except:
                    pass
        
        # Force update to apply changes
        # Make sure both are visible
        self.historical_odds_container.setVisible(visible)
        self.historical_odds_widget.setVisible(visible)
        
        # Force a layout update
        self.historical_odds_container.updateGeometry()
        self.historical_odds_widget.updateGeometry()
        QTimer.singleShot(10, self.update)
    
    
    def update_best_lines_display(self):
        """Update the best lines widget with the latest consolidated raw API data."""
        if not self.best_lines_widget:
            print("Best lines widget not initialized. Skipping update.")
            return
        
        # Use the consolidated raw data if available
        if hasattr(self, 'consolidated_odds_data') and self.consolidated_odds_data:
            self.best_lines = self.best_lines_widget.update_display(self.consolidated_odds_data)
        else:
            print("No consolidated odds data available for best lines calculation.")

    
    def on_market_selection_changed(self):
        """Handle market selection in the odds table"""
        table = self.tab_widget.currentWidget()
        if not table or not isinstance(table, QTableWidget):
            return
        
        current_row = table.currentRow()
        if current_row < 0:
            return
        
        # Get event and market info from the selected row
        header_item = table.item(current_row, 0)
        if not header_item:
            return
        
        row_label = header_item.text()
        market_type = ""
        
        # Skip header rows
        if "Game:" in row_label:
            return
        
        # Try to determine market type from the row label
        if "Moneyline" in row_label:
            market_type = "h2h"
        elif "Spread" in row_label:
            market_type = "spreads"
        elif "Total" in row_label:
            market_type = "totals"
        else:
            # If we can't determine, don't update
            return
        
        # Get game info
        game_id = ""
        home_team = ""
        away_team = ""
        
        # Find the game ID from the item
        if hasattr(header_item, 'game_id'):
            game_id = header_item.game_id
            
            # Look for game header row to get teams
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item and "Game:" in item.text() and hasattr(item, 'game_id') and item.game_id == game_id:
                    # Parse team names from header
                    header_text = item.text()
                    team_part = header_text.replace("Game:", "").strip()
                    if "vs" in team_part:
                        teams = team_part.split("vs")
                        home_team = teams[0].strip()
                        away_team = teams[1].strip()
                    break
        
        if not game_id:
            return
        
        # Get league and sport info
        league_name = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(league_name)
        
        # Only update the historical odds widget if it's visible
        if self.historical_odds_container.isVisible() and hasattr(self, 'historical_odds_widget'):
            self.historical_odds_widget.set_market(sport_key, game_id, market_type, home_team, away_team)




    @qasync.asyncSlot() 
    # This function might just be too fucking much 
    async def refresh_data(self):
        """Fetch and update odds data asynchronously using pandas for comparison"""
        if not self.leagues_loaded:
            print("Leagues not loaded yet. Please wait.")
            return
    
        self.progress.setValue(0)
        self.fetch_odds_button.setEnabled(False)
        
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
    
            # Create a unique tab based on league and currently selected markets
            current_markets = self.selected_markets.copy()
            
            # Create or get existing tab (now with markets parameter)
            tab_data = self.create_league_tab(selected_league, sport_key, current_markets)
            
            # Store current table state in a DataFrame before updating
            current_df = None
            if tab_data.table_rows:
                current_df = tab_data.to_dataframe()
            
            self.data_manager.sport_key = sport_key
            self.data_manager.prop_client = PropClient(sport_key)
    
            # Create a new DataFrame for the updated data
            new_table_rows = []
            new_table_data = {}
            
            async with aiohttp.ClientSession() as session:
                games = await self.data_manager.prop_client.get_games(session)
            print(f"Fetched games response: {games}")
    
            if not isinstance(games, list):
                print(f"Unexpected response from get_games(): {games}")
                self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
                self.update_status.setText("Error: Invalid response format")
                return
    
            # Process odds data for each game
            total_games = len(games)
            bookmakers_seen = set()
            game_odds_data = {}
            
            # Create a consolidated odds data structure for the best lines widget
            consolidated_odds_data = {
                'bookmakers': []
            }
            bookmakers_map = {}  # To track and merge bookmakers across games
            
            for index, game in enumerate(games):
                game_id = game.get('id', '')
                if not game_id:
                    print("No game ID found in the response.")
                    continue
    
                # Update progress based on game processing
                progress_value = int((index + 1) / total_games * 100)
                self.progress.setValue(progress_value)
    
                async with aiohttp.ClientSession() as session:
                    available_markets = current_markets.copy()  # Use the current_markets variable
                    selected_region = self.selected_region
                    odds = await self.data_manager.prop_client.get_event_odds(
                        session, 
                        game_id, 
                        available_markets, 
                        region=selected_region
                    )
                    
                game_odds_data[game_id] = odds
                
                # Extract key info from this game
                home_team = odds.get('home_team', 'Unknown')
                away_team = odds.get('away_team', 'Unknown')
                
                # Add header row for the game
                game_header = f"Game: {home_team} vs {away_team}"
                new_table_rows.append(game_header)
                new_table_data[game_header] = {'is_header': True, 'game_id': game_id}
                
                # Process all bookmakers and markets
                for bm in odds.get('bookmakers', []):
                    bm_title = bm['title']
                    bookmakers_seen.add(bm_title)
                    
                    # Add to consolidated data for best lines widget
                    if bm_title not in bookmakers_map:
                        bookmakers_map[bm_title] = {
                            'title': bm_title,
                            'markets': []
                        }
                        consolidated_odds_data['bookmakers'].append(bookmakers_map[bm_title])
                    
                    for market in bm.get('markets', []):
                        market_key = market['key']
                        
                        # Add game_id to each outcome for reference (needed for best lines)
                        for outcome in market.get('outcomes', []):
                            outcome['game_id'] = game_id
                        
                        # Add to consolidated data
                        bookmakers_map[bm_title]['markets'].append(market)
                        
                        for outcome in market.get('outcomes', []):
                            # Create the row label
                            unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                            
                            # Add row if not already present
                            if unique_label not in new_table_rows:
                                new_table_rows.append(unique_label)
                                new_table_data[unique_label] = {'game_id': game_id}
                                
                            # Update price
                            price = self.format_price(outcome)
                            new_table_data[unique_label][bm_title] = price
            
            # Store the consolidated data for the best lines widget
            self.consolidated_odds_data = consolidated_odds_data
            
            # Create a new DataFrame from the collected data
            new_df = pd.DataFrame(index=new_table_rows, columns=list(bookmakers_seen))
            
            # Fill the new DataFrame
            for row_label in new_table_rows:
                row_data = new_table_data.get(row_label, {})
                for bm in bookmakers_seen:
                    new_df.at[row_label, bm] = row_data.get(bm, "")
                    
            # Add game_id and is_header columns
            new_df['game_id'] = [new_table_data.get(row, {}).get('game_id', '') for row in new_table_rows]
            new_df['is_header'] = [new_table_data.get(row, {}).get('is_header', False) for row in new_table_rows]
            
            # Identify changes between current and new data
            changes = {}
            if current_df is not None:
                # Look for changed odds
                for row in new_df.index:
                    if row in current_df.index:
                        for bm in bookmakers_seen:
                            if bm in current_df.columns:
                                old_val = current_df.at[row, bm]
                                new_val = new_df.at[row, bm]
                                if old_val != new_val and old_val != "" and new_val != "":
                                    if row not in changes:
                                        changes[row] = {}
                                    changes[row][bm] = (old_val, new_val)
            
            # Update tab_data with the new data
            tab_data.bookmakers = list(bookmakers_seen)
            tab_data.table_rows = new_table_rows
            tab_data.table_data = new_table_data
            
            # Update the table display with highlighting changes
            self.update_table_with_changes(tab_data, changes)
            
            # Print a debug message before updating best lines widget
            print("About to update best lines widget with consolidated data")
            print(f"Number of bookmakers in consolidated data: {len(consolidated_odds_data['bookmakers'])}")
            
            # Update best lines widget with the consolidated data
            if hasattr(self, 'best_lines_widget') and self.best_lines_widget:
                print("Updating best lines widget...")
                try:
                    self.best_lines = self.best_lines_widget.update_display(consolidated_odds_data)
                    print("Best lines widget updated successfully")
                except Exception as e:
                    print(f"Error updating best lines widget: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("Best lines widget not available")
            
            self.progress.setValue(100)
            
            # Reset status text if auto-update is enabled
            if self.auto_update_check.isChecked():
                self.update_status_text()
                self.RestartTimer()
            else:
                self.update_status.setStyleSheet("background-color: #28a745; color: white;")
                self.update_status.setText("Update complete")
                
            # Log the number of changed lines
            if changes:
                total_changes = sum(len(changes_dict) for changes_dict in changes.values())
                print(f"Total lines changed: {total_changes}")
                
        except aiohttp.ClientError as e:
            print(f"Network error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("Network error occurred")
        except Exception as e:
            print(f"Error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("An error occurred")
            import traceback
            traceback.print_exc()
        finally:
            self.fetch_odds_button.setEnabled(True)
            # Reset progress bar
            QTimer.singleShot(2000, lambda: self.progress.setValue(0))
            # Reset error messages after a delay
            if not self.auto_update_check.isChecked():
                QTimer.singleShot(5000, lambda: self.update_status.setText(""))
    
    
    # Update Odds table with changes
    
    def update_table_with_changes(self, tab_data, changes):
         """Update the table with efficient display of odds changes"""
         table = tab_data.table_widget
         
         # Count the total number of changed cells
         total_changes = sum(len(changes_dict) for changes_dict in changes.values())
         
         # Update the changes counter label
         if hasattr(self, 'changes_counter_label'):
             if total_changes > 0:
                 self.changes_counter_label.setText(f"Lines Changed: {total_changes}")
                 self.changes_counter_label.setStyleSheet("color: #dc3545; font-weight: bold;")
                 
                 # Reset the color after 5 seconds
                 QTimer.singleShot(5000, lambda: self.changes_counter_label.setStyleSheet("color: #6c757d;"))
             else:
                 self.changes_counter_label.setText("No Changes")
         
         # Update table structure if needed
         expected_cols = len(tab_data.bookmakers) + 1
         if table.columnCount() != expected_cols:
             table.setColumnCount(expected_cols)
             table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
         
         expected_rows = len(tab_data.table_rows)
         if table.rowCount() != expected_rows:
             table.setRowCount(expected_rows)
         
         # Update all rows
         for row_idx, row_label in enumerate(tab_data.table_rows):
             row_data = tab_data.table_data[row_label]
             game_id = row_data['game_id']
             color = tab_data.get_game_color(game_id)
             
             # Create or update row header
             header_item = table.item(row_idx, 0)
             if not header_item:
                 header_item = ColoredTableItem(row_label, game_id)
                 table.setItem(row_idx, 0, header_item)
             else:
                 header_item.setText(row_label)
             
             # Apply header styling
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
                 
                 item = table.item(row_idx, col_idx)
                 if not item:
                     item = ColoredTableItem(current_value, game_id)
                     table.setItem(row_idx, col_idx, item)
                 else:
                     item.setText(current_value)
                 
                 # Check if this cell has changed
                 if row_label in changes and bm in changes[row_label]:
                     old_value, new_value = changes[row_label][bm]
                     
                     # Parse the odds values for comparison
                     try:
                         current_odds = float(new_value.split()[0])
                         previous_odds = float(old_value.split()[0])
                         
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
                         pass
                 else:
                     # No change, maintain consistent background and text color
                     if not row_data.get('is_header'):
                         market_color = QColor(color)
                         market_color.setAlpha(230)
                         item.setBackground(market_color)
                         item.setForeground(QColor('black'))
         
         # Resize the table
         table.resizeColumnsToContents()
         table.resizeRowsToContents()
    
async def main():
    app = QApplication([])
    
    window = ModernOddsWindow()
    await window.initialize()
    window.show()
    
    app.dumpObjectTree()
    app.dumpObjectInfo()
    
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop) # not necessary?
    loop.run_forever() # Run event loop
    return 


if __name__ == "__main__":
    try:
        asyncio.run(main(), debug=True)
    except KeyboardInterrupt:
        pass
