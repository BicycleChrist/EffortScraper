import asyncio
import json

import pandas as pd
from PyQt6.QtCore import Qt, QTimer, QEvent, QObject
from PyQt6.QtGui import QColor, QFont, QBrush
from PyQt6.QtWidgets import (
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, 
    QWidget, QComboBox, QLabel, QHBoxLayout, QPushButton,
    QHeaderView, QScrollBar, QAbstractItemView, QSplitter, QLineEdit
)
import traceback

import update_importpaths
from MLBAnalytics import parkfactors
from MLBpercentilerankings import fetch_leaderboard_data, PITCHER_URL, HITTER_URL
import requests
from bs4 import BeautifulSoup
from GUIMLBlineups import *

class FrozenTableWidget(QTableWidget):
    """Widget for displaying frozen columns"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.verticalHeader().hide()
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(240, 240, 245))
        self.setPalette(palette)

class AdvancedStatsWidget(QWidget):
    """Widget to display advanced statistics for NBA and MLB"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        self.stats_client = None
        self.current_tab = "passing"
        self.current_sport = None
        self.loading_task = None
        
        # Cache variables
        self.cached_data = {}  # Dictionary to store cached data
        self.cache_timestamp = {}  # Dictionary to store when data was last fetched
        self.stored_pitchers = None
        
    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        
        # Add controls at top
        controls_layout = QHBoxLayout()
        
        # Stats type selector (NBA only)
        controls_layout.addWidget(QLabel("Stats Type:"))
        self.stats_selector = QComboBox()
        self.stats_selector.addItems([
            "Passing", "Rebounding", "Touches", "Defense", "Traditional"
        ])
        self.stats_selector.currentIndexChanged.connect(self.on_stats_type_changed)
        controls_layout.addWidget(self.stats_selector)
        
        # Refresh button
        self.refresh_button = QPushButton("Refresh Stats")
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        controls_layout.addWidget(self.refresh_button)
        
        # View Selector
        self.view_selector = QComboBox()
        self.view_selector.addItems(["Percentile Stats", "Stuff+ Stats", "SP Climate"])
        self.view_selector.currentIndexChanged.connect(self.on_view_mode_changed)
        controls_layout.addWidget(QLabel("View:"))
        controls_layout.addWidget(self.view_selector)
        
        
        
        controls_layout.addStretch()
        layout.addLayout(controls_layout)
        
        # Create a splitter to hold the frozen and scrollable tables
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Create frozen columns table
        self.frozen_table = FrozenTableWidget()
        self.frozen_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        
        
        
        
        # Search bar
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Filter by name...")
        self.search_field.textChanged.connect(self.filter_data)
        self.search_field.setClearButtonEnabled(True)
        search_layout.addWidget(self.search_field)
        layout.insertLayout(1, search_layout)  # Insert between controls and tables
        
        
        # Create main stats table
        self.stats_table = QTableWidget()
        self.stats_table.setSortingEnabled(True)
        self.stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        # Add tables to splitter
        self.splitter.addWidget(self.frozen_table)
        self.splitter.addWidget(self.stats_table)
        
        # Set splitter stretch factors
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        
        layout.addWidget(self.splitter)
        
        # Connect vertical scrollbars
        self.stats_table.verticalScrollBar().valueChanged.connect(
            self.sync_frozen_table_scroll
        )
        
        # Install event filters
        self.stats_table.viewport().installEventFilter(self)
        self.frozen_table.viewport().installEventFilter(self)
        self.stats_table.horizontalHeader().installEventFilter(self)
        
        # Loading indicator
        self.loading_label = QLabel("Loading data...")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.hide()
        layout.addWidget(self.loading_label)
        
        # Initialize with empty data
        self.clear_tables()
    
    def set_sport(self, sport_key):
        """Set the current sport and load appropriate data"""
        self.current_sport = sport_key
        nba_mode = sport_key == 'basketball_nba'
        
        # Show/hide the NBA-specific controls
        self.stats_selector.setVisible(nba_mode)
        
        # Show/hide the team sidebar (frozen table with team names)
        if hasattr(self, 'frozen_table'):
            self.frozen_table.setVisible(nba_mode)
            print(f"Set team sidebar visibility to {nba_mode} for sport: {sport_key}")
        
        # For MLB, fetch pitchers once and store them
        if sport_key == 'baseball_mlb' and self.stored_pitchers is None:
            try:
                pitchers, _ = get_todays_pitchers()
                self.stored_pitchers = pitchers
                print(f"Cached {len(pitchers)} pitchers for highlighting")
            except Exception as e:
                print(f"Error fetching pitchers: {e}")
                self.stored_pitchers = []
        
        # Check if we have cached data for this sport and view mode
        cache_key = self.get_cache_key()
        if cache_key in self.cached_data:
            print(f"Using cached data for {cache_key}")
            self.display_stats_data(self.cached_data[cache_key])
            return
        
        # Cancel any existing loading task
        if self.loading_task and not self.loading_task.done():
            self.loading_task.cancel()
        
        if nba_mode:
            self.init_nba_stats_client()
        else:
            self.show_loading_state()
            if self.is_stuffplus_mode():
                self.loading_task = asyncio.create_task(self.load_stuffplus_data())
            else:
                self.loading_task = asyncio.create_task(self.load_mlb_percentile_data())
    
    def filter_data(self, text):
        """Filter the table data based on search text"""
        search_text = text.lower()
        
        # If search text is empty, show all rows
        if not search_text:
            for row in range(self.stats_table.rowCount()):
                self.stats_table.setRowHidden(row, False)
                self.frozen_table.setRowHidden(row, False)
            return
            
        # Get the name column index from frozen table
        name_col_idx = -1
        for col in range(self.frozen_table.columnCount()):
            header = self.frozen_table.horizontalHeaderItem(col)
            if header and (header.text().upper().strip() in ['PLAYER', 'NAME', 'LAST NAME', 'FIRST NAME', 'PLAYER_NAME']):
                name_col_idx = col
                break
        
        # If name column not found, search all columns
        if name_col_idx == -1:
            print("Name column not found, searching all columns")
            for row in range(self.stats_table.rowCount()):
                row_visible = False
                
                # Search in frozen table
                for col in range(self.frozen_table.columnCount()):
                    item = self.frozen_table.item(row, col)
                    if item and search_text in item.text().lower():
                        row_visible = True
                        break
                        
                # Search in main table if not found in frozen table
                if not row_visible:
                    for col in range(self.stats_table.columnCount()):
                        item = self.stats_table.item(row, col)
                        if item and search_text in item.text().lower():
                            row_visible = True
                            break
                
                self.stats_table.setRowHidden(row, not row_visible)
                self.frozen_table.setRowHidden(row, not row_visible)
        else:
            # Search only in the name column for efficiency
            for row in range(self.frozen_table.rowCount()):
                item = self.frozen_table.item(row, name_col_idx)
                row_visible = item and search_text in item.text().lower()
                self.stats_table.setRowHidden(row, not row_visible)
                self.frozen_table.setRowHidden(row, not row_visible)
        
        # Sync row heights and scrollbars after filtering
        self.sync_frozen_table_scroll(self.stats_table.verticalScrollBar().value())
    
    
    
    def get_cache_key(self):
        """Generate a unique key for the current sport and view configuration"""
        if self.current_sport == 'basketball_nba':
            return f"{self.current_sport}_{self.current_tab}"
        else:  # MLB
            view_text = self.view_selector.currentText()
            if view_text == "Stuff+ Stats":
                view_mode = "stuffplus"
            elif view_text == "Percentile Stats":
                view_mode = "percentile"
            elif view_text == "SP Climate":
                view_mode = "parkfactors"
            else:
                view_mode = "unknown"
            return f"{self.current_sport}_{view_mode}"
    
    
    def is_stuffplus_mode(self):
        return self.view_selector.currentText() == "Stuff+ Stats"
    
    def on_view_mode_changed(self, index):
        if self.current_sport == 'baseball_mlb':
            # Check if we have cached data for this view mode
            cache_key = self.get_cache_key()
            if index == 2:  # SP Climate view
                cache_key = "parkfactors"
            
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                df = self.cached_data[cache_key]
                self.display_stats_data(df)
                
                # Apply highlighting with cached pitchers
                if self.stored_pitchers and index != 2:  # Don't highlight park factors
                    self.highlight_todays_pitchers(self.stored_pitchers)
                return
                
            # If no cached data, load it
            self.show_loading_state()
            if self.loading_task and not self.loading_task.done():
                self.loading_task.cancel()
                
            if index == 0:  # Stuff+ Stats
                self.loading_task = asyncio.create_task(self.load_stuffplus_data())
            elif index == 1:  # Percentile Stats
                self.loading_task = asyncio.create_task(self.load_mlb_percentile_data())
            elif index == 2:  # SP Climate
                # Load park factors data directly as DataFrame
                try:
                    df = parkfactors.main(single_year=1)[0]
                    
                    # Cache the data
                    self.cached_data[cache_key] = df
                    self.cache_timestamp[cache_key] = pd.Timestamp.now()
                    
                    # Display the data
                    self.display_stats_data(df)
                    self.hide_loading_state()
                except Exception as e:
                    print(f"Error loading park factors: {e}")
                    self.hide_loading_state()

        
    
    def fetch_stuffplus_data(self):
        url = 'https://www.fangraphs.com/leaders/major-league?type=36&pos=all&stats=pit&sortcol=3&sortdir=default&qual=1&pagenum=1&pageitems=2000000000'
        response = requests.get(url)
        if response.status_code != 200:
            raise Exception("Failed to fetch Fangraphs Stuff+ page")
        soup = BeautifulSoup(response.content, 'lxml')
        table_wrapper = soup.find('div', class_="fg-data-grid table-type").find('div', class_='table-wrapper-inner')
        dfs = pd.read_html(table_wrapper.encode(), encoding="utf-8")
        df = dfs[0]
    
        # Clean columns
        df = df.loc[:, ~df.columns.str.contains('Line Break', na=False)]
        df = df.rename(columns={
            'Name': 'Name','Team': 'Team', '#': 'Index',            
            'IPIP - Innings Pitched': 'IP',
            'Stf+ FA': 'FA', 'Stf+ SI': 'SI', 'Stf+ FC': 'FC', 'Stf+ FS': 'FS',
            'Stf+ SL': 'SL', 'Stf+ CU': 'CU', 'Stf+ CH': 'CH', 'Stf+ KC': 'KC', 'Stf+ FO': 'FO',
            'Stuff+': 'Stuff+', 'Location+': 'Location+', 'Pitching+': 'Pitching+'
        })
        df = df.drop(columns=[col for col in df.columns if col.lower() in ['#', 'rk']], errors='ignore')
        return df

    
    async def load_stuffplus_data(self):
        try:
            if self.current_sport != 'baseball_mlb':
                return
                
            # Check cache first
            cache_key = self.get_cache_key()
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                df = self.cached_data[cache_key]
            else:
                df = await asyncio.get_event_loop().run_in_executor(None, self.fetch_stuffplus_data)
                df['type'] = 'Pitcher'
                if df is None or df.empty:
                    raise Exception("Failed to fetch Stuff+ data")
                    
                # Store in cache
                self.cached_data[cache_key] = df
                self.cache_timestamp[cache_key] = pd.Timestamp.now()
                print(f"Cached Stuff+ data at {self.cache_timestamp[cache_key]}")
            
            # Display the data
            self.display_stats_data(df)
            
            # Highlight pitchers using cached pitcher list
            if self.stored_pitchers is None:
                self.stored_pitchers, _ = get_todays_pitchers()
                print(f"Fetched and cached {len(self.stored_pitchers)} pitchers")
            
            self.highlight_todays_pitchers(self.stored_pitchers)
        except asyncio.CancelledError:
            print("Stuff+ loading cancelled")
        except Exception as e:
            print(f"Error loading Stuff+ data: {e}")
            self.clear_tables()
            self.stats_table.setRowCount(1)
            self.stats_table.setItem(0, 0, QTableWidgetItem(f"Error loading data: {str(e)}"))
        finally:
            self.hide_loading_state()
            
    
    
    def highlight_todays_pitchers(self, pitcher_names):
        """Highlight today's starting pitchers in the stats table."""
        if self.current_sport != 'baseball_mlb':
            return
    
        if self.stats_table.rowCount() == 0:
            return
    
        if not pitcher_names:
            print("No pitchers to highlight")
            return
    
        def normalize_name(name):
            if ',' in name:
                parts = [part.strip() for part in name.split(',')]
                if len(parts) == 2:
                    return f"{parts[1]} {parts[0]}"
            return name.strip()
    
        name_column = 'player_name'
        name_column_idx = -1
    
        for i in range(self.stats_table.columnCount()):
            header = self.stats_table.horizontalHeaderItem(i)
            if header and header.text() == name_column:
                name_column_idx = i
                break
    
        if name_column_idx == -1:
            print(f"Could not find column '{name_column}' to highlight pitchers")
            return
    
        highlight_color = QBrush(QColor(97,95,2))  # Light yellow
        rows_highlighted = []
    
        normalized_pitchers = set(p.lower().strip() for p in pitcher_names)
    
        for row in range(self.stats_table.rowCount()):
            item = self.stats_table.item(row, name_column_idx)
            if not item:
                continue
    
            raw_name = item.text()
            player_name = normalize_name(raw_name).lower()
    
            if player_name in normalized_pitchers:
                for col in range(self.stats_table.columnCount()):
                    cell = self.stats_table.item(row, col)
                    if cell:
                        cell.setBackground(highlight_color)
                rows_highlighted.append(row)
    
        # Sort highlighted rows to the top
        temp_col = self.stats_table.columnCount()
        self.stats_table.insertColumn(temp_col)
        self.stats_table.setHorizontalHeaderItem(temp_col, QTableWidgetItem("SP_Sort"))
    
        for row in range(self.stats_table.rowCount()):
            flag = "1" if row in rows_highlighted else "0"
            self.stats_table.setItem(row, temp_col, QTableWidgetItem(flag))
    
        self.stats_table.sortItems(temp_col, Qt.SortOrder.DescendingOrder)
        self.stats_table.removeColumn(temp_col)
    
        print(f"Highlighted and sorted {len(rows_highlighted)} pitchers out of {len(pitcher_names)} probable pitchers")
    
    def show_loading_state(self):
        """Show loading state in the tables"""
        self.clear_tables()
        self.loading_label.show()
        self.stats_table.setRowCount(1)
        self.stats_table.setItem(0, 0, QTableWidgetItem("Loading data..."))
    
    def hide_loading_state(self):
        """Hide loading state"""
        self.loading_label.hide()
    
    def init_nba_stats_client(self):
        """Initialize the NBA stats client asynchronously"""
        try:
            from NBAtrackingstats import SimpleNBAStatsClient
            self.stats_client = SimpleNBAStatsClient()
            
            # Check cache first for the current tab
            cache_key = self.get_cache_key()
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                self.display_stats_data(self.cached_data[cache_key])
            else:
                asyncio.create_task(self.load_stats_data(self.current_tab))
        except Exception as e:
            print(f"Error initializing NBA stats client: {e}")
    
    async def load_mlb_percentile_data(self):
        """Load MLB percentile data from Baseball Savant"""
        try:
            if self.current_sport != 'baseball_mlb':
                return
                
            # Check cache first
            cache_key = self.get_cache_key()
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                combined_df = self.cached_data[cache_key]
            else:
                # Fetch data using run_in_executor for synchronous requests
                pitcher_df = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: fetch_leaderboard_data(PITCHER_URL)
                )
                hitter_df = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: fetch_leaderboard_data(HITTER_URL)
                )
                
                if pitcher_df is None or hitter_df is None:
                    raise Exception("Failed to fetch MLB percentile data")
                
                # Combine dataframes
                pitcher_df['type'] = 'Pitcher'
                hitter_df['type'] = 'Hitter'
                combined_df = pd.concat([pitcher_df, hitter_df])
                
                # Store in cache
                self.cached_data[cache_key] = combined_df
                self.cache_timestamp[cache_key] = pd.Timestamp.now()
                print(f"Cached percentile data at {self.cache_timestamp[cache_key]}")
            
            # Actually display the data (was missing in original code!)
            self.display_stats_data(combined_df)
            
            # Highlight today's pitchers using cached list
            if self.stored_pitchers is None:
                self.stored_pitchers, _ = get_todays_pitchers()
                print(f"Fetched and cached {len(self.stored_pitchers)} pitchers")
            
            self.highlight_todays_pitchers(self.stored_pitchers)
        except asyncio.CancelledError:
            print("MLB data loading cancelled")
        except Exception as e:
            print(f"Error loading MLB percentile data: {e}")
            self.clear_tables()
            self.stats_table.setRowCount(1)
            self.stats_table.setItem(0, 0, QTableWidgetItem(f"Error loading data: {str(e)}"))
        finally:
            self.hide_loading_state()

    
    def clear_tables(self):
        """Clear both tables"""
        self.stats_table.clear()
        self.stats_table.setRowCount(0)
        self.stats_table.setColumnCount(0)
        self.frozen_table.clear()
        self.frozen_table.setRowCount(0)
        self.frozen_table.setColumnCount(0)
    
    def clear_cache(self):
        """Clear all cached data"""
        self.cached_data = {}
        self.cache_timestamp = {}
        print("Cache cleared")
    
    def eventFilter(self, obj, event):
        """Event filter for tables and headers"""
        if obj == self.stats_table.horizontalHeader() and event.type() == QEvent.Type.Resize:
            self.update_frozen_table_geometry()
        elif event.type() == QEvent.Type.Wheel:
            if obj == self.frozen_table.viewport():
                self.stats_table.verticalScrollBar().setValue(
                    self.stats_table.verticalScrollBar().value() - event.angleDelta().y()
                )
                return True
        return super().eventFilter(obj, event)
    
    def sync_frozen_table_scroll(self, value):
        """Sync the vertical scrolling of the frozen table"""
        self.frozen_table.verticalScrollBar().setValue(value)
        for row in range(min(self.frozen_table.rowCount(), self.stats_table.rowCount())):
            height = self.stats_table.rowHeight(row)
            if self.frozen_table.rowHeight(row) != height:
                self.frozen_table.setRowHeight(row, height)
    
    def update_frozen_table_geometry(self):
        """Update the frozen table's geometry"""
        width = 0
        for col in range(self.frozen_table.columnCount()):
            width += self.frozen_table.columnWidth(col)
        width += self.frozen_table.verticalHeader().width() + 4
        self.splitter.setSizes([width, self.width() - width])
        
        for row in range(min(self.stats_table.rowCount(), self.frozen_table.rowCount())):
            self.frozen_table.setRowHeight(row, self.stats_table.rowHeight(row))
    
    def on_stats_type_changed(self, index):
        """Handle stats type selection change"""
        stats_types = ["passing", "rebounding", "touches", "defense", "traditional"]
        if index < len(stats_types):
            self.current_tab = stats_types[index]
            
            # Check if we have cached data for this tab
            cache_key = self.get_cache_key()
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                self.display_stats_data(self.cached_data[cache_key])
            else:
                asyncio.create_task(self.load_stats_data(self.current_tab))
    
    def on_refresh_clicked(self):
        """Handle refresh button click - force refresh regardless of cache"""
        # Clear cache for the current view
        cache_key = self.get_cache_key()
        if cache_key in self.cached_data:
            del self.cached_data[cache_key]
            if cache_key in self.cache_timestamp:
                del self.cache_timestamp[cache_key]
            print(f"Cleared cache for {cache_key}")
            
        if self.current_sport == 'basketball_nba' and self.stats_client:
            asyncio.create_task(self.load_stats_data(self.current_tab))
        elif self.current_sport == 'baseball_mlb':
            self.show_loading_state()
            if self.is_stuffplus_mode():
                self.loading_task = asyncio.create_task(self.load_stuffplus_data())
            else:
                self.loading_task = asyncio.create_task(self.load_mlb_percentile_data())
    
    async def load_stats_data(self, stats_type):
        """Load the specified stats data type (NBA only)"""
        if not self.stats_client or self.current_sport != 'basketball_nba':
            return
        
        try:
            # Check cache first
            cache_key = self.get_cache_key()
            if cache_key in self.cached_data:
                print(f"Using cached data for {cache_key}")
                df = self.cached_data[cache_key]
            else:
                df = None
                if stats_type == "passing":
                    df = self.stats_client.get_passing_stats()
                elif stats_type == "rebounding":
                    df = self.stats_client.get_rebounding_stats()
                elif stats_type == "touches":
                    df = self.stats_client.get_touches_stats()
                elif stats_type == "defense":
                    df = self.stats_client.get_defense_stats()
                elif stats_type == "traditional":
                    df = self.stats_client.get_traditional_stats()
                
                if df is not None and not df.empty:
                    # Store in cache
                    self.cached_data[cache_key] = df
                    self.cache_timestamp[cache_key] = pd.Timestamp.now()
                    print(f"Cached {stats_type} data at {self.cache_timestamp[cache_key]}")
            
            if df is not None and not df.empty:
                self.display_stats_data(df)
        except Exception as e:
            print(f"Error loading {stats_type} stats: {e}")
            traceback.print_exc()
    
    def display_stats_data(self, df):
        """Display the stats data in the table with frozen columns for NBA data"""
        try:
            self.clear_tables()
            
            # Only proceed with frozen columns for NBA data
            if self.current_sport == 'basketball_nba':
                # Define which columns to freeze for NBA - specifically PLAYER_NAME and MIN
                frozen_columns = []
                if 'PLAYER_NAME' in df.columns:
                    frozen_columns.append('PLAYER_NAME')
                if 'MIN' in df.columns:
                    frozen_columns.append('MIN')
                
                # Get all columns not in frozen columns
                main_columns = [col for col in df.columns if col not in frozen_columns and col != 'Index']
                
                # Set up frozen table
                self.frozen_table.setColumnCount(len(frozen_columns))
                frozen_headers = []
                for col in frozen_columns:
                    if col == 'PLAYER_NAME':
                        frozen_headers.append('Player')
                    else:
                        frozen_headers.append(col)
                self.frozen_table.setHorizontalHeaderLabels(frozen_headers)
                
                # Set up main table
                self.stats_table.setColumnCount(len(main_columns))
                self.stats_table.setHorizontalHeaderLabels(main_columns)
                
                # Make frozen table visible for NBA data
                self.frozen_table.setVisible(True)
                
                # Disable sorting temporarily for faster loading
                self.frozen_table.setSortingEnabled(False)
                self.stats_table.setSortingEnabled(False)
                
                # Load the data row by row
                for idx, row in df.iterrows():
                    frozen_row_pos = self.frozen_table.rowCount()
                    self.frozen_table.insertRow(frozen_row_pos)
                    main_row_pos = self.stats_table.rowCount()
                    self.stats_table.insertRow(main_row_pos)
                    
                    # Fill frozen table columns
                    for col_idx, col_name in enumerate(frozen_columns):
                        value = row.get(col_name, '')
                        item = self.create_table_item(value)
                        self.frozen_table.setItem(frozen_row_pos, col_idx, item)
                    
                    # Fill main table columns
                    for col_idx, col_name in enumerate(main_columns):
                        value = row.get(col_name, '')
                        item = self.create_table_item(value)
                        self.stats_table.setItem(main_row_pos, col_idx, item)
            else:
                # For MLB and other sports, use the existing implementation
                # Just load everything directly in the main table
                all_columns = [col for col in df.columns if col != 'Index']
                
                # Set up the table with all columns
                self.stats_table.setColumnCount(len(all_columns))
                self.stats_table.setHorizontalHeaderLabels(all_columns)
                
                # Set the frozen table to have 0 columns
                self.frozen_table.setColumnCount(0)
                self.frozen_table.setVisible(False)
                
                # Disable sorting temporarily for faster loading
                self.stats_table.setSortingEnabled(False)
                
                # Load the data row by row
                for idx, row in df.iterrows():
                    row_pos = self.stats_table.rowCount()
                    self.stats_table.insertRow(row_pos)
                    
                    # Fill all columns in the main table
                    for col_idx, col_name in enumerate(all_columns):
                        value = row.get(col_name, '')
                        item = self.create_table_item(value)
                        self.stats_table.setItem(row_pos, col_idx, item)
            
            # Re-enable sorting
            self.stats_table.setSortingEnabled(True)
            self.stats_table.verticalHeader().hide()
            if self.frozen_table.isVisible():
                self.frozen_table.verticalHeader().hide()
            
            # Resize columns to fit content
            self.stats_table.resizeColumnsToContents()
            if self.frozen_table.isVisible():
                self.frozen_table.resizeColumnsToContents()
                self.update_frozen_table_geometry()
                
                # Connect sort indicator change signal
                self.stats_table.horizontalHeader().sortIndicatorChanged.connect(
                    self.on_main_table_sort
                )
            
            # For MLB data, handle highlighting pitchers
            if self.current_sport == 'baseball_mlb' and self.stored_pitchers:
                self.highlight_todays_pitchers(self.stored_pitchers)
            
        except Exception as e:
            print(f"Error displaying stats data: {e}")
            traceback.print_exc()
    
    def create_table_item(self, value):
        """Create a table widget item with appropriate formatting"""
        if isinstance(value, (int, float)):
            if abs(value) >= 1000:
                text = f"{value:,.0f}"
            elif abs(value) >= 100:
                text = f"{value:.1f}"
            else:
                text = f"{value:.1f}"
        else:
            text = str(value)
        
        item = QTableWidgetItem(text)
        
        if isinstance(value, (int, float)):
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        return item
    
    def on_main_table_sort(self, column, order):
        """Handle sorting in the main table"""
        self.stats_table.verticalScrollBar().valueChanged.disconnect(self.sync_frozen_table_scroll)
        
        rows_mapping = {}
        for row in range(self.stats_table.rowCount()):
            logical_index = self.stats_table.verticalHeader().logicalIndex(row)
            rows_mapping[row] = logical_index
        
        for old_row, new_row in rows_mapping.items():
            if old_row != new_row:
                old_row_data = []
                for col in range(self.frozen_table.columnCount()):
                    item = self.frozen_table.takeItem(old_row, col)
                    old_row_data.append(item)
                
                new_row_data = []
                for col in range(self.frozen_table.columnCount()):
                    item = self.frozen_table.takeItem(new_row, col)
                    new_row_data.append(item)
                    if item is not None:
                        self.frozen_table.setItem(old_row, col, item)
                
                for col, item in enumerate(old_row_data):
                    if item is not None:
                        self.frozen_table.setItem(new_row, col, item)
        
        self.stats_table.verticalScrollBar().valueChanged.connect(self.sync_frozen_table_scroll)
        
        for row in range(self.stats_table.rowCount()):
            self.frozen_table.setRowHeight(row, self.stats_table.rowHeight(row))
    
    def resizeEvent(self, event):
        """Handle resize events"""
        super().resizeEvent(event)
        self.update_frozen_table_geometry()
    
    def get_key_columns_for_dataframe(self, df):
        """Return the most important columns for the given dataframe type"""
        if self.current_sport == 'baseball_mlb':
            if self.is_stuffplus_mode():
                # Just use ALL actual columns from the dataframe
                # Filter out any Index/# columns if needed
                columns = [col for col in df.columns if not col.startswith('#') and col != 'Index' and col != '']
                # Make sure Name and Team are first
                if 'Name' in columns:
                    columns.remove('Name')
                    columns = ['Name'] + columns
                if 'Team' in columns:
                    columns.remove('Team')
                    columns = columns[:1] + ['Team'] + columns[1:]
                # Add type column which we know is added
                if 'type' in df.columns and 'type' not in columns:
                    columns.append('type')
                # Return all available columns
                return columns
            
            # Your existing code for percentile mode
            key_cols = ['player_name','xwoba', 'xba', 'xslg', 'xiso', 'xobp', 'brl', 'brl_percent', 'exit_velocity', 'max_ev', 'hard_hit_percent', 'k_percent', 
                        'bb_percent', 'whiff_percent', 'chase_percent', 'arm_strength', 'xera', 'fb_velocity', 'fb_spin', 'curve_spin']
            percentile_cols = [col for col in df.columns if col.endswith('_percentile')]
            key_cols.extend(percentile_cols)
            return key_cols[:20]
        
        # Rest of your existing function remains unchanged
        key_cols = []
        id_cols = ['PLAYER_NAME', 'PLAYER', 'TEAM_ABBREVIATION', 'TEAM']
        for col in id_cols:
            if col in df.columns:
                key_cols.append(col)
        
        exclude_cols = [
            'PLAYER_ID', 'TEAM_ID', 'GP_RANK', 'W_RANK', 'L_RANK', 'W_PCT_RANK',
            'CFID', 'CFPARAMS', 'LEAGUE_ID'
        ] + key_cols
        
        if 'AGE' in df.columns:
            stat_cols = ['GP', 'MIN', 'PTS', 'FGM', 'FGA', 'FG_PCT', 'FG3M', 'FG3A', 
                       'FG3_PCT', 'FTM', 'FTA', 'FT_PCT', 'OREB', 'DREB', 'REB', 
                       'AST', 'STL', 'BLK', 'TOV', 'PF', 'PLUS_MINUS']
        elif 'POTENTIAL_AST' in df.columns:
            stat_cols = ['MIN', 'PASSES_MADE', 'PASSES_RECEIVED', 'AST', 'POTENTIAL_AST', 'SECONDARY_AST',
                       'AST_POINTS_CREATED', 'AST_ADJ', 'AST_TO_PASS_PCT']
        elif 'REB_CHANCES' in df.columns:
            stat_cols = ['MIN','REB', 'OREB', 'OREB_CONTEST', 'OREB_UNCONTEST', 'OREB_CONTEST_PCT', 
                         'OREB_CHANCES', 'OREB_CHANCE_PCT', 'OREB_CHANCE_DEFER', 'OREB_CHANCE_PCT_ADJ', 
                         'AVG_OREB_DIST', 'DREB', 'DREB_CONTEST', 'DREB_UNCONTEST', 'DREB_CONTEST_PCT',
                         'DREB_CHANCES', 'DREB_CHANCE_PCT', 'DREB_CHANCE_DEFER', 'DREB_CHANCE_PCT_ADJ',
                         'AVG_DREB_DIST', 'REB', 'REB_CONTEST', 'REB_UNCONTEST', 'REB_CONTEST_PCT',
                         'REB_CHANCES', 'REB_CHANCE_PCT', 'REB_CHANCE_DEFER', 'REB_CHANCE_PCT_ADJ',
                         'AVG_REB_DIST']
        elif 'TOUCHES' in df.columns:
            stat_cols = ['MIN', 'TOUCHES', 'FRONT_CT_TOUCHES', 'PAINT_TOUCHES', 'ELBOW_TOUCHES',
                 'TIME_OF_POSS', 'AVG_SEC_PER_TOUCH', 'PTS_PER_TOUCH', 'POINTS']
        elif 'STL_ADJ' in df.columns:
            stat_cols = ['MIN', 'DEF_MIN', 'PARTIAL_POSS', 'STL', 'BLK', 'DEF_REB',
                       'STL_ADJ', 'BLK_ADJ', 'FOULS_DRAWN', 'DFGM', 'DFGA', 'DFG_PCT']
        else:
            stat_cols = [col for col in df.columns if col not in exclude_cols]
        
        for col in stat_cols:
            if col in df.columns and col not in key_cols:
                key_cols.append(col)
        
        if len(key_cols) > 20:
            return key_cols[:20]
        return key_cols

def integrate_stats_with_props_window(props_window):
    """
    Integrates the advanced stats tab with the PropsWindow.
    """
    if not hasattr(props_window, 'best_lines_widget'):
        print("Error: PropsWindow doesn't have best_lines_widget")
        return
    
    if hasattr(props_window, 'tab_widget_integrated') and props_window.tab_widget_integrated:
        if hasattr(props_window, 'advanced_stats_widget'):
            props_window.advanced_stats_widget.set_sport(props_window.sport_key)
        return
    
    try:
        parent = props_window.best_lines_widget.parent()
        parent_layout = parent.layout()
        
        for i in range(parent_layout.count()):
            if parent_layout.itemAt(i).widget() == props_window.best_lines_widget:
                widget_index = i
                break
        else:
            widget_index = -1
        
        tab_widget = QTabWidget()
        
        best_lines_tab = QWidget()
        best_lines_layout = QVBoxLayout(best_lines_tab)
        best_lines_layout.setContentsMargins(0, 0, 0, 0)
        
        new_best_lines_table = QTableWidget()
        original_table = props_window.best_lines_widget
        new_best_lines_table.setColumnCount(original_table.columnCount())
        new_best_lines_table.setHorizontalHeaderLabels(
            [original_table.horizontalHeaderItem(i).text() 
             for i in range(original_table.columnCount())]
        )
        
        for row in range(original_table.rowCount()):
            new_best_lines_table.insertRow(row)
            for col in range(original_table.columnCount()):
                item = original_table.item(row, col)
                if item:
                    new_item = QTableWidgetItem(item.text())
                    new_item.setBackground(item.background())
                    new_item.setForeground(item.foreground())
                    new_item.setTextAlignment(item.textAlignment())
                    new_best_lines_table.setItem(row, col, new_item)
        
        best_lines_layout.addWidget(new_best_lines_table)
        tab_widget.addTab(best_lines_tab, "Best Lines")
        
        advanced_stats_widget = AdvancedStatsWidget()
        tab_widget.addTab(advanced_stats_widget, "Advanced Stats")
        
        props_window.best_lines_widget.hide()
        
        if widget_index >= 0:
            parent_layout.insertWidget(widget_index, tab_widget)
        else:
            parent_layout.addWidget(tab_widget)
        
        props_window.tab_widget = tab_widget
        props_window.advanced_stats_widget = advanced_stats_widget
        props_window.best_lines_tab = best_lines_tab
        props_window.new_best_lines_table = new_best_lines_table
        props_window.tab_widget_integrated = True
        
        def update_best_lines_table():
            if not hasattr(props_window, 'new_best_lines_table') or not hasattr(props_window, 'best_lines_widget'): return;
            if ((props_window.new_best_lines_table is None) or (props_window.best_lines_widget is None)): return;
            print("updating best lines table...")
            try:
                original_table = props_window.best_lines_widget
                new_table = props_window.new_best_lines_table
                
                new_table.setRowCount(0)
                
                for row in range(original_table.rowCount()):
                    new_table.insertRow(row)
                    for col in range(original_table.columnCount()):
                        item = original_table.item(row, col)
                        if item:
                            new_item = QTableWidgetItem(item.text())
                            new_item.setBackground(item.background())
                            new_item.setForeground(item.foreground())
                            new_item.setTextAlignment(item.textAlignment())
                            new_table.setItem(row, col, new_item)
                
                new_table.resizeColumnsToContents()
                props_window.new_best_lines_table = new_table
            except Exception as e:
                print(f"Error updating best lines table: {e}")
            return
        
        props_window.update_timer = QTimer.singleShot(1000, update_best_lines_table)
        
        # Set initial sport for the stats widget
        advanced_stats_widget.set_sport(props_window.sport_key)
        
        print("Successfully integrated advanced stats tab with new approach")
        
    except Exception as e:
        print(f"Error integrating advanced stats: {e}")
        traceback.print_exc()
