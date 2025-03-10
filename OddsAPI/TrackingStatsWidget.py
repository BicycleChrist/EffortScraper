import asyncio
import pandas as pd
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, 
    QWidget, QComboBox, QLabel, QHBoxLayout, QPushButton
)

class AdvancedStatsWidget(QWidget):
    """Widget to display NBA advanced statistics"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        self.stats_client = None
        self.current_tab = "passing"
        
    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        
        # Add controls at top
        controls_layout = QHBoxLayout()
        
        # Stats type selector
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
        
        # Create stats table
        self.stats_table = QTableWidget()
        self.stats_table.setSortingEnabled(True)
        self.stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.stats_table)
        
        # Initialize the NBA stats client
        QTimer.singleShot(0, self.init_stats_client)
    
    def init_stats_client(self):
        """Initialize the NBA stats client asynchronously"""
        # Import here to avoid circular imports
        try:
            from NBAtrackingstats import SimpleNBAStatsClient
            self.stats_client = SimpleNBAStatsClient()
            # Initial data load
            asyncio.create_task(self.load_stats_data(self.current_tab))
        except Exception as e:
            print(f"Error initializing NBA stats client: {e}")
    
    def on_stats_type_changed(self, index):
        """Handle stats type selection change"""
        stats_types = ["passing", "rebounding", "touches", "defense", "traditional"]
        if index < len(stats_types):
            self.current_tab = stats_types[index]
            asyncio.create_task(self.load_stats_data(self.current_tab))
    
    def on_refresh_clicked(self):
        """Handle refresh button click"""
        if self.stats_client:
            asyncio.create_task(self.load_stats_data(self.current_tab))
    
    async def load_stats_data(self, stats_type):
        """Load the specified stats data type"""
        if not self.stats_client:
            print("NBA stats client not initialized")
            return
        
        try:
            # Map stats type to client method
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
            else:
                print(f"No data returned for {stats_type} stats")
        except Exception as e:
            print(f"Error loading {stats_type} stats: {e}")
            import traceback
            traceback.print_exc()
    
    def display_stats_data(self, df):
        """Display the stats data in the table"""
        try:
            # Clear existing data
            self.stats_table.clear()
            self.stats_table.setRowCount(0)
            
            # Get columns to display
            key_cols = self.get_key_columns_for_dataframe(df)
            
            # Set column count and headers
            self.stats_table.setColumnCount(len(key_cols))
            self.stats_table.setHorizontalHeaderLabels(key_cols)
            
            # Sort by a meaningful column if possible
            if 'MIN' in df.columns:
                df = df.sort_values(by='MIN', ascending=False)
            
            # Take top players (to avoid overwhelming the table)
            df_display = df.head(50)
            
            # Add data rows
            for idx, row in df_display.iterrows():
                row_position = self.stats_table.rowCount()
                self.stats_table.insertRow(row_position)
                
                for col_idx, col_name in enumerate(key_cols):
                    value = row.get(col_name, '')
                    
                    # Format numeric values
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
                    
                    # Right-align numeric columns
                    if isinstance(value, (int, float)):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    
                    self.stats_table.setItem(row_position, col_idx, item)
            
            # Resize columns to content
            self.stats_table.resizeColumnsToContents()
            
        except Exception as e:
            print(f"Error displaying stats data: {e}")
            import traceback
            traceback.print_exc()
    
    def get_key_columns_for_dataframe(self, df):
        """Return the most important columns for the given dataframe type"""
        # Start with player identifiers
        key_cols = []
        
        # Priority player identifier columns
        id_cols = ['PLAYER_NAME', 'PLAYER', 'TEAM_ABBREVIATION', 'TEAM']
        for col in id_cols:
            if col in df.columns:
                key_cols.append(col)
        
        # Get all columns except specific ones to exclude
        exclude_cols = [
            'PLAYER_ID', 'TEAM_ID', 'GP_RANK', 'W_RANK', 'L_RANK', 'W_PCT_RANK',
            'CFID', 'CFPARAMS', 'LEAGUE_ID'
        ] + key_cols
        
        # Main stats columns - depends on the type of statistics
        if 'AGE' in df.columns:  # Traditional stats
            stat_cols = ['GP', 'MIN', 'PTS', 'FGM', 'FGA', 'FG_PCT', 'FG3M', 'FG3A', 
                       'FG3_PCT', 'FTM', 'FTA', 'FT_PCT', 'OREB', 'DREB', 'REB', 
                       'AST', 'STL', 'BLK', 'TOV', 'PF', 'PLUS_MINUS']
        elif 'POTENTIAL_AST' in df.columns:  # Passing stats
            stat_cols = ['MIN', 'PASSES_MADE', 'PASSES_RECEIVED', 'AST', 'POTENTIAL_AST', 'SECONDARY_AST',
                       'AST_POINTS_CREATED', 'AST_ADJ', 'AST_TO_PASS_PCT']
        elif 'REB_CHANCES' in df.columns:  # Rebounding stats
            stat_cols = ['MIN', 'OREB', 'DREB', 'REB', 'CONTESTED_REB', 'UNCONTESTED_REB',
                       'DEFERRED_REB', 'REB_CHANCES', 'REB_CHANCE_PCT', 'ADJ_REB_CHANCE_PCT']
        elif 'TOUCHES' in df.columns:  # Touches stats
            stat_cols = ['MIN', 'TOUCHES', 'FRONT_CT_TOUCHES', 'PAINT_TOUCHES', 'ELBOW_TOUCHES',
                       'TIME_OF_POSS', 'AVG_SEC_PER_TOUCH', 'PTS_PER_TOUCH', 'POINTS']
        elif 'STL_ADJ' in df.columns:  # Defense stats
            stat_cols = ['MIN', 'DEF_MIN', 'PARTIAL_POSS', 'STL', 'BLK', 'DEF_REB',
                       'STL_ADJ', 'BLK_ADJ', 'FOULS_DRAWN', 'DFGM', 'DFGA', 'DFG_PCT']
        else:
            # Generic case - use all numeric columns
            stat_cols = [col for col in df.columns if col not in exclude_cols]
        
        # Add available stats in preferred order
        for col in stat_cols:
            if col in df.columns and col not in key_cols:
                key_cols.append(col)
        
        # Ensure we don't have too many columns to display (practical limit)
        if len(key_cols) > 15:
            return key_cols[:15]
        return key_cols


def integrate_stats_with_props_window(props_window):
    """
    Integrates the advanced stats tab with the PropsWindow.
    
    This function creates a tab widget that contains both the best lines information
    and the advanced stats widget, using a completely new approach to avoid widget reparenting issues.
    
    Args:
        props_window: The PropsWindow instance to modify
    """
    if not hasattr(props_window, 'best_lines_widget'):
        print("Error: PropsWindow doesn't have best_lines_widget")
        return
    
    # Check if integration was already done
    if hasattr(props_window, 'tab_widget_integrated') and props_window.tab_widget_integrated:
        print("Advanced stats integration already complete")
        return
    
    try:
        # Get parent container of the best_lines_widget
        parent = props_window.best_lines_widget.parent()
        parent_layout = parent.layout()
        
        # Find the index of the best_lines_widget in its parent layout
        for i in range(parent_layout.count()):
            if parent_layout.itemAt(i).widget() == props_window.best_lines_widget:
                widget_index = i
                break
        else:
            widget_index = -1  # Not found
        
        # Create a tab widget
        tab_widget = QTabWidget()
        
        # Instead of moving the existing widget, we'll create a new table for best lines
        # that mirrors the structure of the original best_lines_widget
        best_lines_tab = QWidget()
        best_lines_layout = QVBoxLayout(best_lines_tab)
        best_lines_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create new table based on props_window.best_lines_widget (it's a QTableWidget)
        new_best_lines_table = QTableWidget()
        
        # Configure the new table to match the original
        original_table = props_window.best_lines_widget
        new_best_lines_table.setColumnCount(original_table.columnCount())
        new_best_lines_table.setHorizontalHeaderLabels(
            [original_table.horizontalHeaderItem(i).text() 
             for i in range(original_table.columnCount())]
        )
        
        # Copy data from original table to new table
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
        
        # Add the new table to the best lines tab
        best_lines_layout.addWidget(new_best_lines_table)
        
        # Add the best lines tab to the tab widget
        tab_widget.addTab(best_lines_tab, "Best Lines")
        
        # Create and add the advanced stats tab
        advanced_stats_widget = AdvancedStatsWidget()
        tab_widget.addTab(advanced_stats_widget, "Advanced Stats")
        
        # Hide the original best_lines_widget
        props_window.best_lines_widget.hide()
        
        # Insert the tab widget in the parent layout at the same position
        if widget_index >= 0:
            parent_layout.insertWidget(widget_index, tab_widget)
        else:
            # If index not found, just add it to the layout
            parent_layout.addWidget(tab_widget)
        
        # Store references
        props_window.tab_widget = tab_widget
        props_window.advanced_stats_widget = advanced_stats_widget
        props_window.best_lines_tab = best_lines_tab
        props_window.new_best_lines_table = new_best_lines_table
        
        # Mark as integrated
        props_window.tab_widget_integrated = True
        
        # Function to update the best lines table when the data changes
        def update_best_lines_table():
            try:
                # Only update if tabs exist and original widget has data
                if not hasattr(props_window, 'new_best_lines_table') or not hasattr(props_window, 'best_lines_widget'):
                    return
                
                original_table = props_window.best_lines_widget
                new_table = props_window.new_best_lines_table
                
                # Clear existing data
                new_table.setRowCount(0)
                
                # Copy data from original table to new table
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
                
                # Resize columns to fit content
                new_table.resizeColumnsToContents()
            except Exception as e:
                print(f"Error updating best lines table: {e}")
        
        # Connect the data update method to the original best_lines_widget's updates
        # We'll use a timer to periodically check for updates
        update_timer = QTimer()
        update_timer.timeout.connect(update_best_lines_table)
        update_timer.start(1000)  # Check every second
        props_window.update_timer = update_timer
        
        # Store the original update_best_lines_display method
        original_update = props_window.update_best_lines_display
        
        # Override the update method to also update our new table
        def enhanced_update_best_lines_display(*args, **kwargs):
            # Call the original method
            result = original_update(*args, **kwargs)
            # Then update our table
            update_best_lines_table()
            return result
        
        # Replace the method
        props_window.update_best_lines_display = enhanced_update_best_lines_display
        
        print("Successfully integrated advanced stats tab with new approach")
        
    except Exception as e:
        print(f"Error integrating advanced stats: {e}")
        import traceback
        traceback.print_exc()


