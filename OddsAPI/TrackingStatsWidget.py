import asyncio
import pandas as pd
from PyQt6.QtCore import Qt, QTimer, QEvent, QObject
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, 
    QWidget, QComboBox, QLabel, QHBoxLayout, QPushButton,
    QHeaderView, QScrollBar, QAbstractItemView, QSplitter
)
import traceback
from MLBpercentilerankings import fetch_leaderboard_data, PITCHER_URL, HITTER_URL

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
        
        controls_layout.addStretch()
        layout.addLayout(controls_layout)
        
        # Create a splitter to hold the frozen and scrollable tables
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Create frozen columns table
        self.frozen_table = FrozenTableWidget()
        self.frozen_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        
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
        # This is what you're seeing in your screenshots
        if hasattr(self, 'frozen_table'):
            self.frozen_table.setVisible(nba_mode)  # Only show team sidebar for NBA
            print(f"Set team sidebar visibility to {nba_mode} for sport: {sport_key}")
        
        # Cancel any existing loading task
        if self.loading_task and not self.loading_task.done():
            self.loading_task.cancel()
        
        if nba_mode:
            self.init_nba_stats_client()
        else:
            self.show_loading_state()
            self.loading_task = asyncio.create_task(self.load_mlb_percentile_data())
    
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
            asyncio.create_task(self.load_stats_data(self.current_tab))
        except Exception as e:
            print(f"Error initializing NBA stats client: {e}")
    
    async def load_mlb_percentile_data(self):
        """Load MLB percentile data from Baseball Savant"""
        try:
            if self.current_sport != 'baseball_mlb':
                return
                
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
            
            # Display the data
            self.display_stats_data(combined_df)
            
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
            asyncio.create_task(self.load_stats_data(self.current_tab))
    
    def on_refresh_clicked(self):
        """Handle refresh button click"""
        if self.current_sport == 'basketball_nba' and self.stats_client:
            asyncio.create_task(self.load_stats_data(self.current_tab))
        elif self.current_sport == 'baseball_mlb':
            self.show_loading_state()
            self.loading_task = asyncio.create_task(self.load_mlb_percentile_data())
    
    async def load_stats_data(self, stats_type):
        """Load the specified stats data type (NBA only)"""
        if not self.stats_client or self.current_sport != 'basketball_nba':
            return
        
        try:
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
                self.display_stats_data(df)
        except Exception as e:
            print(f"Error loading {stats_type} stats: {e}")
            traceback.print_exc()
    
    def display_stats_data(self, df):
        """Display the stats data in the table"""
        try:
            self.clear_tables()
            
            key_cols = self.get_key_columns_for_dataframe(df)
            frozen_cols = []
            frozen_headers = []
            
            for col in ['PLAYER_NAME', 'PLAYER', 'TEAM_ABBREVIATION', 'TEAM', 'MIN', 'last_name', 'first_name', 'type', 'team_name']:
                if col in key_cols:
                    frozen_cols.append(col)
                    if col == 'PLAYER_NAME' or col == 'PLAYER':
                        frozen_headers.append('Player')
                    elif col == 'TEAM_ABBREVIATION' or col == 'TEAM':
                        frozen_headers.append('Team')
                    elif col == 'last_name':
                        frozen_headers.append('Last Name')
                    elif col == 'first_name':
                        frozen_headers.append('First Name')
                    elif col == 'type':
                        frozen_headers.append('Type')
                    elif col == 'team_name':
                        frozen_headers.append('Team')
                    else:
                        frozen_headers.append(col)
            
            main_cols = [col for col in key_cols if col not in frozen_cols]
            
            self.frozen_table.setColumnCount(len(frozen_cols))
            self.frozen_table.setHorizontalHeaderLabels(frozen_headers)
            self.stats_table.setColumnCount(len(main_cols))
            self.stats_table.setHorizontalHeaderLabels(main_cols)
            
            if 'MIN' in df.columns:
                df = df.sort_values(by='MIN', ascending=False)
            
            df_display = df
            
            self.frozen_table.setSortingEnabled(False)
            self.stats_table.setSortingEnabled(False)
            
            for idx, row in df_display.iterrows():
                frozen_row_pos = self.frozen_table.rowCount()
                self.frozen_table.insertRow(frozen_row_pos)
                main_row_pos = self.stats_table.rowCount()
                self.stats_table.insertRow(main_row_pos)
                
                for col_idx, col_name in enumerate(frozen_cols):
                    value = row.get(col_name, '')
                    item = self.create_table_item(value)
                    self.frozen_table.setItem(frozen_row_pos, col_idx, item)
                
                for col_idx, col_name in enumerate(main_cols):
                    value = row.get(col_name, '')
                    item = self.create_table_item(value)
                    self.stats_table.setItem(main_row_pos, col_idx, item)
            
            self.stats_table.setSortingEnabled(True)
            self.stats_table.verticalHeader().hide()
            self.frozen_table.resizeColumnsToContents()
            self.stats_table.resizeColumnsToContents()
            self.update_frozen_table_geometry()
            
            self.stats_table.horizontalHeader().sortIndicatorChanged.connect(
                self.on_main_table_sort
            )
            
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
        # Hitter columns to include: hitter_cols = ['xwoba', 'xba', 'xslg', 'xiso', 'xobp', 'brl', 'brl_percent', 'exit_velocity', 'max_ev', 
        # 'hard_hit_percent', 'k_percent', 'bb_percent', 'whiff_percent', 'chase_percent', 'arm_strength', 'sprint_speed', 'oaa', 'bat_speed', 'squared_up_rate', 'swing_length']
        if self.current_sport == 'baseball_mlb':
            key_cols = ['player_name','xwoba', 'xba', 'xslg', 'xiso', 'xobp', 'brl', 'brl_percent', 'exit_velocity', 'max_ev', 'hard_hit_percent', 'k_percent', 
                        'bb_percent', 'whiff_percent', 'chase_percent', 'arm_strength', 'xera', 'fb_velocity', 'fb_spin', 'curve_spin']
            percentile_cols = [col for col in df.columns if col.endswith('_percentile')]
            key_cols.extend(percentile_cols)
            return key_cols[:20]
        
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
            try:
                if not hasattr(props_window, 'new_best_lines_table') or not hasattr(props_window, 'best_lines_widget'):
                    return
                
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
            except Exception as e:
                print(f"Error updating best lines table: {e}")
        
        update_timer = QTimer()
        update_timer.timeout.connect(update_best_lines_table)
        update_timer.start(1000)
        props_window.update_timer = update_timer
        
        original_update = props_window.update_best_lines_display
        
        def enhanced_update_best_lines_display(*args, **kwargs):
            result = original_update(*args, **kwargs)
            update_best_lines_table()
            return result
        
        props_window.update_best_lines_display = enhanced_update_best_lines_display
        
        # Set initial sport for the stats widget
        advanced_stats_widget.set_sport(props_window.sport_key)
        
        print("Successfully integrated advanced stats tab with new approach")
        
    except Exception as e:
        print(f"Error integrating advanced stats: {e}")
        traceback.print_exc()
