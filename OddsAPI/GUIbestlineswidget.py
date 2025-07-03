# BestLinesWidget.py - Updated with Splits Integration
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QPushButton
import LineCalculator
import json
import os
import asyncio
from datetime import datetime

class BestLinesWidget(QTableWidget):
    """Widget for displaying best betting lines and their deviations."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.best_lines = {}
        self.show_splits = False  # Flag to toggle between best lines and splits display
        self.current_sport = None  # Track the current sport for loading splits data
        self.splits_data = {}  # Cache for splits data
        self.last_splits_update = None  # Track when splits were last updated
        
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["Player/Game","Market","Best Line","Avg Odds","Implied Prob Deviation"])
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # Style the header
        header = self.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #2C3E50;
                color: white;
                padding: 4px;
                border: 1px solid #34495E;
            }
        """)
        self.best_lines = {}  # Store calculated best lines
        
        # Add a toggle button for switching between best lines and splits view
        self.toggle_button = QPushButton("Show Betting Splits")
        self.toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:hover {
                background-color: #34495E;
            }
        """)
        self.toggle_button.clicked.connect(self.toggle_splits_view)

    def american_to_decimal(self, american_odds):
        """Convert American odds to decimal odds format"""
        if american_odds > 0:
            return (american_odds / 100) + 1
        else:
            return (100 / abs(american_odds)) + 1

    def decimal_to_american(self, decimal_odds):
        """Convert decimal odds to American odds format"""
        if decimal_odds >= 2:
            return round((decimal_odds - 1) * 100)
        else:
            return round(-100 / (decimal_odds - 1))

    def calculate_implied_probability(self, odds, format='american'):
        """Calculate implied probability from odds"""
        if format == 'decimal':
            return 1 / odds
        elif format == 'american':
            if odds > 0:
                return 100 / (odds + 100)
            else:
                return abs(odds) / (abs(odds) + 100)
    
    def toggle_splits_view(self):
        """Toggle between best lines view and betting splits view"""
        self.show_splits = not self.show_splits
        
        if self.show_splits: 
            self.toggle_button.setText("Show Best Lines")
            # Update header labels for splits view
            self.setHorizontalHeaderLabels(["Game", "Market", "Option", "Handle %", "Bets %"])
            # Load/display splits data
            self.load_and_display_splits()
        else:
            self.toggle_button.setText("Show Betting Splits")
            # Restore header labels for best lines view
            self.setHorizontalHeaderLabels(["Player/Game", "Market", "Best Line", "Avg Odds", "Implied Prob Deviation"])
            
            # Redisplay best lines if they exist
            if hasattr(self, 'best_lines') and self.best_lines:
                # Clear the table first
                self.setRowCount(0)
                
                # Check if this is prop data or team-based data and call the appropriate display method
                if self.best_lines and isinstance(next(iter(self.best_lines.values()), {}), dict) and 'over' in next(iter(self.best_lines.values()), {}):
                    # This is prop data
                    self._populate_widget(self.best_lines)
                else:
                    # This is team-based market data
                    sorted_markets = sorted(
                        self.best_lines.items(),
                        key=lambda x: x[1].get('deviation', 0) if 'deviation' in x[1] else 0,
                        reverse=True
                    )
                    
                    # Track the row count
                    row = 0
                    
                    # Repopulate the table with the sorted markets
                    for market_id, market_data in sorted_markets:
                        # Only display markets with multiple bookmakers and a calculated deviation
                        if len(market_data.get('bookmakers', [])) > 1 and 'deviation' in market_data:
                            # Add a new row
                            self.insertRow(row)
                            
                            # For the game name, we use the team name as a placeholder
                            game_or_team = market_data.get('team', '')
                            self.setItem(row, 0, QTableWidgetItem(game_or_team))
                            
                            # Set the market type and point
                            market_type = market_data.get('market_type', '').capitalize()
                            point = market_data.get('point', '')
                            market_cell = f"{market_type} {point}"
                            self.setItem(row, 1, QTableWidgetItem(market_cell))
                            
                            # Set the best line
                            best_odds = market_data.get('best_odds', 0)
                            best_bm = market_data.get('best_bookmaker', '')
                            best_line = f"{best_odds} @ {best_bm}"
                            self.setItem(row, 2, QTableWidgetItem(best_line))
                            
                            # Set the average odds
                            avg_odds = market_data.get('avg_odds', 0)
                            avg_odds_str = f"{avg_odds:.0f}"
                            self.setItem(row, 3, QTableWidgetItem(avg_odds_str))
                            
                            # Set the deviation with color coding
                            deviation = market_data.get('deviation', 0)
                            deviation_item = QTableWidgetItem(f"+{deviation:.2f}%")
                            
                            # Color code based on deviation value
                            if deviation > 5:
                                deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green for good value
                            elif deviation > 2:
                                deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow for moderate value
                            
                            self.setItem(row, 4, deviation_item)
                            
                            row += 1
                    
                    # Resize columns after populating
                    self.resizeColumnsToContents()

    def set_sport(self, sport_key):
        """Set the current sport and update splits data if needed"""
        # Map from API sport key to the format used in SplitsScraper
        sport_map = {
            "basketball_nba": "nba",
            "baseball_mlb": "mlb", 
            "icehockey_nhl": "nhl",
            "football_nfl": "nfl",
            "basketball_ncaab": "ncaab",
        }
        
        # Convert from API sport key to scraper sport key
        if sport_key in sport_map:
            sport = sport_map[sport_key]
            if sport != self.current_sport:
                self.current_sport = sport
                # If currently showing splits, update the display
                if self.show_splits:
                    self.load_and_display_splits()
        else:
            self.current_sport = None
            if self.show_splits:
                # Clear the table if no valid sport
                self.setRowCount(0)

    def load_and_display_splits(self):
        """Load betting splits data from file and display it"""
        if not self.current_sport:
            self.setRowCount(0)
            return
        
        # Define the path to the latest data file
        file_path = f"SplitsData/{self.current_sport}_betting_latest.json"
        
        # Check if the directory exists, create if not
        os.makedirs("SplitsData", exist_ok=True)
        
        # Check if the file exists
        if not os.path.exists(file_path):
            self.setRowCount(0)
            return
        
        # Check file modification time
        mod_time = os.path.getmtime(file_path)
        mod_datetime = datetime.fromtimestamp(mod_time)
        
        # If we've already loaded this data and it hasn't changed, skip
        if self.current_sport in self.splits_data and self.last_splits_update and self.last_splits_update >= mod_datetime:
            # Just redisplay the cached data
            self.display_splits_data(self.splits_data[self.current_sport])
            return
        
        try:
            # Load the JSON data
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Cache the data and update the last loaded time
            self.splits_data[self.current_sport] = data
            self.last_splits_update = mod_datetime
            
            # Display the data
            self.display_splits_data(data)
            
        except Exception as e:
            print(f"Error loading betting splits data: {e}")
            self.setRowCount(0)

    def display_splits_data(self, data):
        """Display the betting splits data in the table"""
        # Clear the table
        self.setRowCount(0)
        
        if not data:
            return
        
        # Track the current row
        row = 0
        
        # Process each game
        for game in data:
            away_team = game.get('away_team', 'Unknown')
            home_team = game.get('home_team', 'Unknown')
            game_title = f"{away_team} @ {home_team}"
            
            # Process each market (Moneyline, Spread, Total)
            for market_type, market_data in game.get('markets', {}).items():
                for option in market_data:
                    # Add a new row
                    self.insertRow(row)
                    
                    # Add game info
                    self.setItem(row, 0, QTableWidgetItem(game_title))
                    
                    # Add market type
                    self.setItem(row, 1, QTableWidgetItem(market_type))
                    
                    # Add option (e.g., team name or over/under)
                    option_name = option.get('option', 'Unknown')
                    self.setItem(row, 2, QTableWidgetItem(option_name))
                    
                    # Add handle percentage with color coding
                    handle_pct = option.get('handle_percentage', '0')
                    handle_item = QTableWidgetItem(f"{handle_pct}%")
                    
                    # Color code based on handle percentage
                    try:
                        handle_value = float(handle_pct)
                        if handle_value > 70:
                            handle_item.setBackground(QColor(0, 180, 0, 100))  # Green for high percentages
                        elif handle_value < 30:
                            handle_item.setBackground(QColor(180, 0, 0, 100))  # Red for low percentages
                    except ValueError:
                        pass
                    
                    self.setItem(row, 3, handle_item)
                    
                    # Add bets percentage with color coding
                    bets_pct = option.get('bets_percentage', '0')
                    bets_item = QTableWidgetItem(f"{bets_pct}%")
                    
                    # Color code based on bets percentage
                    try:
                        bets_value = float(bets_pct)
                        if bets_value > 70:
                            bets_item.setBackground(QColor(0, 180, 0, 100))  # Green for high percentages
                        elif bets_value < 30:
                            bets_item.setBackground(QColor(180, 0, 0, 100))  # Red for low percentages
                    except ValueError:
                        pass
                    
                    self.setItem(row, 4, bets_item)
                    
                    row += 1
        
        # Resize columns to fit content
        self.resizeColumnsToContents()

    async def refresh_splits_data(self):
        """Refresh betting splits data by running the scraper"""
        if not self.current_sport:
            return
        
        try:
            # Import the scraper function
            from SplitsScraper import main_async
            
            # Run the scraper for the current sport
            success = await main_async(sport=self.current_sport, max_retries=1)
            
            if success:
                # Reset the last update time to force a reload
                self.last_splits_update = None
                
                # Reload and display the data if in splits view
                if self.show_splits:
                    self.load_and_display_splits()
                
                return True
            else:
                print(f"Failed to refresh splits data for {self.current_sport}")
                return False
        except Exception as e:
            print(f"Error refreshing betting splits data: {e}")
            return False

    def update_display(self, consolidated_odds_data):
        """Update the best lines widget with the latest consolidated raw API data.
        This method handles prop-type markets (player stats, etc.)"""
        if not consolidated_odds_data:
            print("No consolidated odds data available. Skipping update.")
            return None

        # Check if this is prop data or team-based data
        # If any market has 'player_' in the key, it's likely prop data
        is_prop_data = False
        for bm in consolidated_odds_data.get('bookmakers', []):
            for market in bm.get('markets', []):
                if 'player_' in market.get('key', ''):
                    is_prop_data = True
                    break
            if is_prop_data:
                break

        # Use the appropriate update method based on data type
        if is_prop_data:
            return self._update_display_props(consolidated_odds_data)
        else:
            return self._update_display_team_based(consolidated_odds_data)

    def _update_display_props(self, consolidated_odds_data):
        """Original update method for prop markets."""
        
        print(f"Updating display with prop data containing {len(consolidated_odds_data.get('bookmakers', []))} bookmakers")
    
        # Extract bookmakers list from the consolidated data
        bookmakers = [bm['title'] for bm in consolidated_odds_data.get('bookmakers', [])]
        
        if not bookmakers:
            print("Warning: No bookmakers found in consolidated data")
            return None
    
        # Transform the data to the format BestLinesCalculator expects
        table_data = {}
    
        # Process each bookmaker's markets
        market_count = 0
        for bm in consolidated_odds_data.get('bookmakers', []):
            bm_title = bm['title']
    
            for market in bm.get('markets', []):
                market_count += 1
                market_key = market['key']
    
                for outcome in market.get('outcomes', []):
                    player_name = outcome.get('description', outcome.get('name', ''))
                    game_id = market.get('game_id', '')  # Get game_id from market instead of outcome
    
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
        
        print(f"Processed {market_count} markets into {len(table_data)} table rows")
    
        # Calculate best lines using the transformed data
        calculator = LineCalculator.calculate_best_lines(table_data, bookmakers)
        best_lines = calculator.calculate_best_lines()
        
        print(f"Calculated {len(best_lines)} best lines")
    
        # Store the best lines for reference
        self.best_lines = best_lines
    
        # Populate the widget with the results
        self._populate_widget(best_lines)
        
        print(f"Populated widget with {len(best_lines)} best lines")
    
        return best_lines


    def _update_display_team_based(self, consolidated_odds_data):
        """Update method for team-based markets (spreads, totals, etc.) with proper odds averaging"""
        print("Starting to populate best lines widget with team-based consolidated data")
    
        # Clear the current table
        self.setRowCount(0)
    
        # Group the markets by type (spreads, totals, etc.) and game
        market_groups = {}
    
        # Loop through all bookmakers and their markets
        for bm in consolidated_odds_data.get('bookmakers', []):
            bm_title = bm['title']
    
            for market in bm.get('markets', []):
                market_key = market['key']
    
                for outcome in market.get('outcomes', []):
                    game_id = outcome.get('game_id', '')
                    team_name = outcome.get('name', '')
                    point = outcome.get('point', '')
                    price = outcome.get('price', '')
    
                    # Create a unique key for this market: game_id + market_type + team + point
                    # IMPORTANT: Changed to include point in the key to avoid overwriting different lines
                    market_id = f"{game_id}:{market_key}:{team_name}:{point}"
    
                    if market_id not in market_groups:
                        market_groups[market_id] = {
                            'game_id': game_id,
                            'market_type': market_key,
                            'team': team_name,
                            'point': point,
                            'bookmakers': [],
                            'best_odds': float(-999999),
                            'best_bookmaker': None
                        }
    
                    # Add this bookmaker's odds - convert to float for comparison
                    try:
                        odds_float = float(price)
                        market_groups[market_id]['bookmakers'].append({
                            'bookmaker': bm_title,
                            'odds': odds_float
                        })
    
                        # Update best odds if this is better
                        if odds_float > market_groups[market_id]['best_odds']:
                            market_groups[market_id]['best_odds'] = odds_float
                            market_groups[market_id]['best_bookmaker'] = bm_title
                    except ValueError:
                        # Skip this outcome if odds can't be converted to float
                        print(f"Warning: Could not convert odds '{price}' to float")
    
        # Calculate average odds and deviation for each market using decimal odds
        for market_id, market_data in market_groups.items():
            # Removed the filtering for multiple bookmakers to show more markets
            if len(market_data['bookmakers']) > 0:  # Show even single-bookmaker markets
                # Convert all American odds to decimal for proper averaging
                decimal_odds_list = [self.american_to_decimal(bm['odds']) for bm in market_data['bookmakers']]
    
                # Calculate average in decimal format
                avg_decimal_odds = sum(decimal_odds_list) / len(decimal_odds_list)
    
                # Store the average in American format for display
                avg_american_odds = self.decimal_to_american(avg_decimal_odds)
                market_data['avg_odds'] = avg_american_odds
    
                # Calculate implied probability from average decimal odds
                avg_implied_prob = 1 / avg_decimal_odds
    
                # Calculate implied probability from best American odds
                best_odds = market_data['best_odds']
                best_decimal_odds = self.american_to_decimal(best_odds)
                best_implied_prob = 1 / best_decimal_odds
    
                # Calculate deviation - higher is better value (positive EV)
                deviation = (avg_implied_prob - best_implied_prob) * 100
                market_data['deviation'] = deviation
    
        # Sort markets by deviation (highest first)
        sorted_markets = sorted(
            market_groups.items(),
            key=lambda x: x[1].get('deviation', 0) if 'deviation' in x[1] else 0,
            reverse=True
        )
    
        # Store the best lines for reference - include all markets
        self.best_lines = {market_id: data for market_id, data in sorted_markets}
    
        # Log the number of markets found and analyzed
        print(f"Found {len(market_groups)} markets and {len([m for m in market_groups.values() if len(m['bookmakers']) > 1])} with multiple bookmakers")
    
        # Track the row count
        row = 0
    
        # Populate the table with the best lines - DISPLAY MORE ROWS
        market_count = 0
        for market_id, market_data in sorted_markets:
            # Only display markets with a calculated deviation
            if market_data.get('deviation') is not None:
                # Limit to a reasonable number of rows (e.g., 250)
                if market_count >= 250:
                    break
                    
                # Add a new row
                self.insertRow(row)
    
                # For the game name, we use the team name as a placeholder since we don't have the game header
                game_or_team = market_data['team']
                self.setItem(row, 0, QTableWidgetItem(game_or_team))
    
                # Set the market type and point
                market_type = market_data['market_type'].capitalize()
                point = market_data['point']
                market_cell = f"{market_type} {point}"
                self.setItem(row, 1, QTableWidgetItem(market_cell))
    
                # Set the best line
                best_odds = market_data['best_odds']
                best_bm = market_data['best_bookmaker']
                best_line = f"{best_odds} @ {best_bm}"
                self.setItem(row, 2, QTableWidgetItem(best_line))
    
                # Set the average odds (rounded to nearest whole number for readability)
                avg_odds = market_data.get('avg_odds', 0)
                # Format with proper sign
                avg_odds_str = f"{avg_odds:.0f}"
                self.setItem(row, 3, QTableWidgetItem(avg_odds_str))
    
                # Set the deviation with color coding
                deviation = market_data.get('deviation', 0)
                deviation_item = QTableWidgetItem(f"+{deviation:.2f}%")
    
                # Color code based on deviation value
                if deviation > 5:
                    deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green for good value
                elif deviation > 2:
                    deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow for moderate value
    
                self.setItem(row, 4, deviation_item)
    
                row += 1
                market_count += 1
    
        # Resize columns to fit content
        self.resizeColumnsToContents()
        print(f"Best lines widget populated with {row} rows")
    
        return self.best_lines

    def _populate_widget(self, best_lines):
        """Helper method to populate the best lines widget with calculated data."""
        # First, update column count if not already done
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["Player", "Market", "Best Line", "Avg Odds", "Implied Prob Deviation"])

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
            try:
                player_name, market_type = market_key.split(':')

                # Add a row for the over line if available
                if data['over']:
                    over = data['over']
                    row_position = self.rowCount()
                    self.insertRow(row_position)

                    self.setItem(row_position, 0, QTableWidgetItem(player_name))
                    self.setItem(row_position, 1, QTableWidgetItem(f"{market_type} OVER"))

                    over_text = f"{over['odds']} O ({over['point']}) @ {over['bookmaker']}"
                    self.setItem(row_position, 2, QTableWidgetItem(over_text))

                    if over['count'] > 1:
                        # Add average odds
                        avg_odds_text = f"{over['avg_odds']:.0f}"
                        self.setItem(row_position, 3, QTableWidgetItem(avg_odds_text))

                        # Add deviation
                        deviation_item = QTableWidgetItem(f"+{over['deviation']:.2f}%")

                        # Color code the deviation
                        if over['deviation'] > 10:
                            deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green
                        elif over['deviation'] > 5:
                            deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow

                        self.setItem(row_position, 4, deviation_item)
                    else:
                        self.setItem(row_position, 3, QTableWidgetItem("N/A"))
                        self.setItem(row_position, 4, QTableWidgetItem("Solo Line"))

                # Add a row for the under line if available
                if data['under']:
                    under = data['under']
                    row_position = self.rowCount()
                    self.insertRow(row_position)

                    self.setItem(row_position, 0, QTableWidgetItem(player_name))
                    self.setItem(row_position, 1, QTableWidgetItem(f"{market_type} UNDER"))

                    under_text = f"{under['odds']} U ({under['point']}) @ {under['bookmaker']}"
                    self.setItem(row_position, 2, QTableWidgetItem(under_text))

                    if under['count'] > 1:
                        # Add average odds
                        avg_odds_text = f"{under['avg_odds']:.0f}"
                        self.setItem(row_position, 3, QTableWidgetItem(avg_odds_text))

                        # Add deviation
                        deviation_item = QTableWidgetItem(f"+{under['deviation']:.2f}%")

                        # Color code the deviation
                        if under['deviation'] > 10:
                            deviation_item.setBackground(QColor(0, 200, 0, 150))  # Green
                        elif under['deviation'] > 5:
                            deviation_item.setBackground(QColor(200, 200, 0, 150))  # Yellow

                        self.setItem(row_position, 4, deviation_item)
                    else:
                        self.setItem(row_position, 3, QTableWidgetItem("N/A"))
                        self.setItem(row_position, 4, QTableWidgetItem("Solo Line"))
            except Exception as e:
                print(f"Error processing market {market_key}: {e}")
                import traceback
                traceback.print_exc()
                continue

        # Resize the columns to fit content
        self.resizeColumnsToContents()
