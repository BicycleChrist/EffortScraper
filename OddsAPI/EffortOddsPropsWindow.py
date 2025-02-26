import qasync
import aiohttp
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QFont, QPen, QPainter
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel, QProgressBar, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QPushButton, 
    QHBoxLayout, QGridLayout, QCheckBox
)
from PropQuery import PropClient
from marketKeys import MAJOR_PROP_MARKETS


# Cant Believe this file works at all
#TODO: Right now the table only displays overs, need to get unders in there for player_props
# Ideally we have a structure "-142 O (15.5) +106 U" for each cell 

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
        
        needs_resize = True
        
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
        # Progress bar
        self.progress = QProgressBar()
        self.layout.addWidget(self.progress)

    def create_table(self):
        self.table_widget = self.tab_data.create_table_widget()
        self.layout.insertWidget(0, self.table_widget)

    def add_control(self, widget):
        self.layout.insertWidget(0, widget)


from PyQt6.QtWidgets import QScrollArea, QGroupBox

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

        # Use a QTimer to schedule the async initialization
        self.timer = QTimer()
        self.timer.setSingleShot(True)  # Ensure the timer only fires once
        self.timer.timeout.connect(self.start_async_init)
        self.timer.start(0)  # Start the timer immediately after the constructor

    def start_async_init(self):
        """Start the asynchronous initialization of the UI."""
        import asyncio
        asyncio.create_task(self.init_prop_ui())

    async def init_prop_ui(self):
        # Create controls container
        controls_widget = QWidget()
        controls_layout = QHBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
    
        # Prop type label
        controls_layout.addWidget(QLabel("Select Prop Type:"))
    
        # Prop type selector
        self.prop_selector = QComboBox()
        controls_layout.addWidget(self.prop_selector)
    
        # Fetch button
        self.fetch_button = QPushButton("Fetch Props")
        self.fetch_button.setStyleSheet(self.fetch_button_style)
        self.fetch_button.clicked.connect(self.on_fetch_props_clicked)  # Connect to the method
        controls_layout.addWidget(self.fetch_button)
    
        # Add controls to the main layout
        self.layout.addWidget(controls_widget)
    
        # Create a collapsible group box for game selection
        game_group = QGroupBox("Select Games")
        game_group.setCheckable(True)  # Make the group box collapsible
        game_group.setChecked(True)  # Default to expanded
        game_group_layout = QVBoxLayout(game_group)
    
        # Add "Select All" and "Deselect All" buttons
        select_all_button = QPushButton("Select All")
        select_all_button.clicked.connect(lambda: self.set_all_game_checkboxes(True))
        deselect_all_button = QPushButton("Deselect All")
        deselect_all_button.clicked.connect(lambda: self.set_all_game_checkboxes(False))
        game_group_layout.addWidget(select_all_button)
        game_group_layout.addWidget(deselect_all_button)
    
        # Create a scrollable area for game selection
        self.game_selection_area = QScrollArea()
        self.game_selection_area.setWidgetResizable(True)
        self.game_selection_widget = QWidget()
        self.game_selection_layout = QGridLayout(self.game_selection_widget)
        self.game_selection_area.setWidget(self.game_selection_widget)
    
        # Add the scrollable area to the group box
        game_group_layout.addWidget(self.game_selection_area)
    
        # Add the collapsible group box to the main layout
        self.layout.addWidget(game_group)
    
        # Create table
        self.create_table()
    
        # Load prop markets
        self.load_prop_markets()
        
        # Fetch and populate games
        async with aiohttp.ClientSession() as session:
            games = await self.prop_client.get_games(session)
            self.populate_game_selection(games)

    def load_prop_markets(self):
        """Load available prop markets into the dropdown"""
        from marketKeys import MAJOR_PROP_MARKETS
        prop_markets = MAJOR_PROP_MARKETS.get(self.sport_key, {})
        self.prop_types = list(prop_markets.keys())
        self.prop_selector.addItems([prop_markets[key] for key in self.prop_types])

    def populate_game_selection(self, games):
        """Populate the game selection area with checkboxes for each game."""
        # Clear existing checkboxes
        for i in reversed(range(self.game_selection_layout.count())):
            self.game_selection_layout.itemAt(i).widget().setParent(None)
        self.game_checkboxes.clear()
    
        # Add a checkbox for each game
        for game in games:
            game_id = game.get('id', '')
            home_team = game.get('home_team', 'Unknown')
            away_team = game.get('away_team', 'Unknown')
            game_label = f"{home_team} vs {away_team}"
            checkbox = QCheckBox(game_label)
            checkbox.setChecked(True)  # Default to selected
            self.game_checkboxes[game_id] = checkbox
            self.game_selection_layout.addWidget(checkbox)
    
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
        if selected_index >= 0:
            selected_prop = self.prop_types[selected_index]
            await self.refresh_data({selected_prop})

    @qasync.asyncSlot()
    async def refresh_data(self, markets):
        """Fetch props only for selected games"""
        self.progress.setValue(0)
        try:
            # Reset market groups and best lines
            self.market_groups = {}
            self.best_lines = {}
            
            async with aiohttp.ClientSession() as session:
                games = await self.prop_client.get_games(session)
                # Populate game selection checkboxes
                self.populate_game_selection(games)
    
                # Filter games based on selected checkboxes
                selected_games = [
                    game_id for game_id, checkbox in self.game_checkboxes.items()
                    if checkbox.isChecked()
                ]
    
                total_games = len(selected_games)
                for idx, game_id in enumerate(selected_games):
                    odds = await self.prop_client.get_event_odds(
                        session, game_id, markets, region="us,eu,us_dfs"
                    )
                    self.process_odds_data(odds)
                    self.progress.setValue(int((idx + 1) / total_games * 100))
                
                # Find best lines for each market group
                self.find_best_lines()
                
                # Update table display with best lines highlighted
                self.tab_data.update_table_display()
                self.highlight_best_lines()
        except Exception as e:
            print(f"Error fetching props: {e}")

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
                
                if market_key not in market_groups:
                    market_groups[market_key] = []
                
                market_groups[market_key].append(row_label)
        
        self.market_groups = market_groups
        
        # Find best lines for each market group
        for market_key, rows in market_groups.items():
            best_over, best_under = self.find_best_market_lines(rows)
            self.best_lines[market_key] = {
                'over': best_over,
                'under': best_under
            }
    
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
                    
                # Try to parse the value (e.g., "-142 O (15.5) +106 U")
                try:
                    parts = value.split()
                    # Check for over odds
                    if 'O' in parts:
                        over_idx = parts.index('O')
                        if over_idx > 0:  # Ensure there's an odds value before 'O'
                            over_odds = float(parts[over_idx-1])
                            # Find the point value (in parentheses)
                            for i in range(over_idx, len(parts)):
                                if '(' in parts[i]:
                                    point_str = parts[i].strip('()')
                                    if ')' not in point_str:  # Handle case where closing paren is separate
                                        for j in range(i+1, len(parts)):
                                            if ')' in parts[j]:
                                                point_str = point_str + parts[j].strip(')')
                                                break
                                    try:
                                        point = float(point_str)
                                        # For overs: lower point and higher odds is better
                                        if (point < best_over['point'] or 
                                            (point == best_over['point'] and over_odds > best_over['odds'])):
                                            best_over = {'odds': over_odds, 'point': point, 'bookmaker': bm}
                                        break
                                    except ValueError:
                                        continue
                    
                    # Check for under odds
                    if 'U' in parts:
                        under_idx = parts.index('U')
                        if under_idx > 0:  # Ensure there's an odds value before 'U'
                            under_odds = float(parts[under_idx-1])
                            # The point value should be the same as for overs
                            for i in range(0, len(parts)):
                                if '(' in parts[i]:
                                    point_str = parts[i].strip('()')
                                    if ')' not in point_str:  # Handle case where closing paren is separate
                                        for j in range(i+1, len(parts)):
                                            if ')' in parts[j]:
                                                point_str = point_str + parts[j].strip(')')
                                                break
                                    try:
                                        point = float(point_str)
                                        # For unders: higher point and higher odds is better
                                        if (point > best_under['point'] or 
                                            (point == best_under['point'] and under_odds > best_under['odds'])):
                                            best_under = {'odds': under_odds, 'point': point, 'bookmaker': bm}
                                        break
                                    except ValueError:
                                        continue
                except (ValueError, IndexError) as e:
                    print(f"Error parsing value '{value}': {e}")
                    continue
        
        return best_over, best_under

    def highlight_best_lines(self):
        """Highlight cells with the best lines"""
        table = self.tab_data.table_widget
        
        # Define highlight colors
        best_over_color = QColor(0, 200, 0, 150)  # Green with some transparency
        best_under_color = QColor(0, 93, 167, 211)  # Blue with some transparency
        
        # First reset all highlights to normal
        for row_idx, row_label in enumerate(self.tab_data.table_rows):
            row_data = self.tab_data.table_data[row_label]
            game_id = row_data.get('game_id')
            normal_color = self.tab_data.get_game_color(game_id)
            normal_color.setAlpha(230)
            
            # Skip header rows
            if row_data.get('is_header'):
                continue
                
            # Identify the market group this row belongs to
            parts = row_label.split(' - ')
            if len(parts) < 2:
                continue
                
            player_name = parts[0]
            market_type = parts[1]
            market_key = f"{player_name}:{market_type}"
            
            # Skip if we don't have best lines for this market
            if market_key not in self.best_lines:
                continue
                
            best_over = self.best_lines[market_key]['over']
            best_under = self.best_lines[market_key]['under']
            
            # Check each bookmaker column
            for col_idx, bm in enumerate(self.tab_data.bookmakers, 1):
                if bm not in row_data:
                    continue
                    
                item = table.item(row_idx, col_idx)
                if not item:
                    continue
                    
                value = row_data[bm]
                
                # Check if this cell has the best over odds
                if best_over['bookmaker'] == bm:
                    try:
                        parts = value.split()
                        if 'O' in parts:
                            over_idx = parts.index('O')
                            if over_idx > 0:
                                over_odds = float(parts[over_idx-1])
                                
                                # Find the point value
                                for i in range(over_idx, len(parts)):
                                    if '(' in parts[i]:
                                        point_str = parts[i].strip('()')
                                        if ')' not in point_str:  # Handle case where closing paren is separate
                                            for j in range(i+1, len(parts)):
                                                if ')' in parts[j]:
                                                    point_str = point_str + parts[j].strip(')')
                                                    break
                                        try:
                                            point = float(point_str)
                                            if point == best_over['point'] and over_odds == best_over['odds']:
                                                # This cell has the best over line - highlight it
                                                item.setBackground(best_over_color)
                                                item.setForeground(QColor('black'))
                                            break
                                        except ValueError:
                                            continue
                    except (ValueError, IndexError):
                        pass
                
                # Check if this cell has the best under odds
                if best_under['bookmaker'] == bm:
                    try:
                        parts = value.split()
                        if 'U' in parts:
                            under_idx = parts.index('U')
                            if under_idx > 0:
                                under_odds = float(parts[under_idx-1])
                                
                                # Find the point value
                                for i in range(0, len(parts)):
                                    if '(' in parts[i]:
                                        point_str = parts[i].strip('()')
                                        if ')' not in point_str:  # Handle case where closing paren is separate
                                            for j in range(i+1, len(parts)):
                                                if ')' in parts[j]:
                                                    point_str = point_str + parts[j].strip(')')
                                                    break
                                        try:
                                            point = float(point_str)
                                            if point == best_under['point'] and under_odds == best_under['odds']:
                                                # This cell has the best under line - highlight it
                                                item.setBackground(best_under_color)
                                                item.setForeground(QColor('black'))
                                            break
                                        except ValueError:
                                            continue
                    except (ValueError, IndexError):
                        pass
    
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
                    if outcome.get('name', '').lower() == 'over':
                        grouped_markets[group_key]['over'] = outcome.get('price', '')
                    elif outcome.get('name', '').lower() == 'under':
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
        
        self.tab_data.update_table_display()

