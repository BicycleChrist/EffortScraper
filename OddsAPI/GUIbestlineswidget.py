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
            # Update header labels for splits view (adds Start time + Updated columns)
            self.setColumnCount(7)
            self.setHorizontalHeaderLabels(
                ["Game", "Start", "Market", "Option", "Handle %", "Bets %", "Updated"]
            )
            # Load/display splits data
            self.load_and_display_splits()
        else:
            self.toggle_button.setText("Show Betting Splits")
            # Restore header labels for best lines view
            self.setColumnCount(5)
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

    def _format_timestamp(self, value, fallback=""):
        """Format an ISO timestamp (or datetime) into a compact local time string.
        Returns the raw value if it can't be parsed (e.g. DK's free-text game times),
        or the fallback when empty."""
        if not value:
            return fallback
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                # Normalize trailing 'Z' (UTC) to an offset fromisoformat understands
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return str(value)
        # Convert tz-aware UTC timestamps to local time for display
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime('%b %d, %I:%M %p')

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

            # Game start time and data-freshness timestamp (per game).
            # SBD provides both; DK has no per-record update time, so fall back to
            # the file's last-modified time (when we scraped it).
            start_str = self._format_timestamp(game.get('game_time'))
            updated_str = self._format_timestamp(
                game.get('last_update'), fallback=self._format_timestamp(self.last_splits_update)
            )

            # Process each market (Moneyline, Spread, Total)
            for market_type, market_data in game.get('markets', {}).items():
                for option in market_data:
                    # Add a new row
                    self.insertRow(row)

                    # Add game info
                    self.setItem(row, 0, QTableWidgetItem(game_title))

                    # Add game start time
                    self.setItem(row, 1, QTableWidgetItem(start_str))

                    # Add market type
                    self.setItem(row, 2, QTableWidgetItem(market_type))

                    # Add option (e.g., team name or over/under)
                    option_name = option.get('option', 'Unknown')
                    self.setItem(row, 3, QTableWidgetItem(option_name))

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

                    self.setItem(row, 4, handle_item)

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

                    self.setItem(row, 5, bets_item)

                    # Add last-update timestamp
                    self.setItem(row, 6, QTableWidgetItem(updated_str))

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

        # Clear the current table and restore best-lines column layout (the splits
        # view switches to 7 columns, so reset here in case we're coming from it)
        self.setRowCount(0)
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["Player/Game", "Market", "Best Line", "Avg Odds", "Implied Prob Deviation"])
    
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
                    game_name = outcome.get('game_name', '')
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
                            'game_name': game_name,
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
    
                # Column 0 label: for totals markets the outcome 'name' is Over/Under
                # (not a team), so show the game name with the bet side appended.
                # For spreads/h2h the outcome 'name' is the team name, so use it as-is.
                team_name = market_data['team']
                game_name = market_data.get('game_name', '')
                if team_name.strip().lower() in ('over', 'under') and game_name:
                    game_or_team = f"{game_name} {team_name}"
                else:
                    game_or_team = team_name
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


# ===========================================================================
# CRT Blotter — terminal-style Best Lines / Betting Splits widget
#
# Replaces the legacy BestLinesWidget in the EffortOdds bottom panel. The
# legacy class above is kept untouched: EffortOddsPropsWindow still uses it
# as a raw QTableWidget.
#
# Public contract consumed by EffortOdds (kept identical to legacy):
#   .toggle_button (QPushButton placed in the external header)
#   .show_splits (read by EffortOdds to relabel the header)
#   .update_display(consolidated_odds_data) -> best_lines dict
#   .set_sport(sport_key)
#   async .refresh_splits_data() -> bool
#
# Backend upgrades over legacy:
#   * Real devig: pairs each outcome with its market siblings (other team /
#     mirrored spread / opposite total), strips the vig per book, and computes
#     EV% of the best price against the no-vig consensus fair probability.
#     The old avg-based 'deviation' is still computed for compatibility.
#   * Splits view merges the DK scrape and the SBD consensus feed, keeping
#     the fresher file per game and tagging the source.
#   * Splits are joined onto best-line rows (handle%/bets% per side) with a
#     reverse-line-movement proxy flag when tickets and money diverge >=15pts.
# ===========================================================================

import re
from collections import defaultdict
from PyQt6.QtCore import QAbstractListModel, QModelIndex, QSize, QRect, QEvent, QPoint
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListView, QLabel,
    QStackedWidget, QStyledItemDelegate, QStyle, QButtonGroup
)

# Clean cool/slate palette (replaces the old amber CRT look). Names kept so the
# rest of the widget is unchanged; only the values moved.
AMBER       = "#cdd6e0"   # primary text (side / best price)
AMBER_HOT   = "#7fd6a0"   # highlight / positive (green)
AMBER_DIM   = "#7d8794"   # secondary / dim
AMBER_FAINT = "#4a525c"   # faint / placeholder
CRT_BG      = "#0d1117"   # row bg
CRT_BG_ALT  = "#11161d"   # alt row bg
CRT_FRAME   = "#2b333d"   # frame / rules
RLM_WARN    = "#e06c75"   # reverse-line-movement warning (soft red)

MARKET_LABELS = {"h2h": "ML", "spreads": "SPRD", "totals": "TOT"}

BOOK_ABBREVS = {
    "draftkings": "DK", "fanduel": "FD", "betmgm": "MGM", "caesars": "CZR",
    "pinnacle": "PIN", "bovada": "BOV", "betonline": "BOL", "mybookie": "MYB",
    "betrivers": "BRV", "fliff": "FLF", "betparx": "PRX", "thescore": "SCR",
    "bally": "BLY", "betus": "BUS", "lowvig": "LV", "hard rock": "HR",
    "betanything": "BA", "espn": "ESPN", "unibet": "UNI", "fanatics": "FAN",
    "williamhill": "WH", "wynn": "WYNN", "superbook": "SUP", "circa": "CIR",
}


def _book_abbrev(title):
    t = (title or "").lower()
    for key, abbr in BOOK_ABBREVS.items():
        if key in t:
            return abbr
    return (title or "?").replace(".", " ").split()[0][:4].upper()


def _crt_font(px, bold=False):
    f = QFont()
    f.setFamilies(["JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "Monospace"])
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def _glow_text(painter, rect, flags, text, color):
    """Crisp text with a subtle drop shadow for legibility on the dark bg."""
    painter.setPen(QColor(0, 0, 0, 90))
    painter.drawText(rect.translated(1, 1), flags, text)
    painter.setPen(QColor(color))
    painter.drawText(rect, flags, text)


# ----------------------------------------------------------- odds math (pure)

def crt_american_to_decimal(odds):
    return (odds / 100.0) + 1 if odds > 0 else (100.0 / abs(odds)) + 1


def crt_implied_prob(odds):
    return 100.0 / (odds + 100.0) if odds > 0 else abs(odds) / (abs(odds) + 100.0)


def crt_prob_to_american(p):
    dec = 1.0 / p
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def _fmt_american(v):
    try:
        return f"{round(float(v)):+d}"
    except (TypeError, ValueError):
        return str(v)


def _point_or_none(p):
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def build_market_groups(consolidated_odds_data):
    """Group consolidated event-odds outcomes by (game, market, side, point),
    tracking per-book prices and the best price. Pure function."""
    groups = {}
    for bm in (consolidated_odds_data or {}).get('bookmakers', []):
        title = bm.get('title', '')
        for market in bm.get('markets', []):
            mk = market.get('key', '')
            for o in market.get('outcomes', []):
                try:
                    odds = float(o.get('price'))
                except (TypeError, ValueError):
                    continue
                gid = o.get('game_id', '')
                team = o.get('name', '')
                point = o.get('point', '')
                mid = f"{gid}:{mk}:{team}:{point}"
                g = groups.get(mid)
                if g is None:
                    g = groups[mid] = {
                        'game_id': gid, 'market_type': mk, 'team': team,
                        'game_name': o.get('game_name', ''), 'point': point,
                        'books': {}, 'best_odds': None, 'best_bookmaker': None,
                    }
                g['books'][title] = odds
                if g['best_odds'] is None or odds > g['best_odds']:
                    g['best_odds'] = odds
                    g['best_bookmaker'] = title
    return groups


def _market_siblings(g, candidates):
    """The other outcome(s) completing g's market: opposite total at the same
    point, mirrored spread, or the other side(s) of a pointless h2h market
    (3-way markets with a Draw devig across all three)."""
    p = _point_or_none(g['point'])
    name = g['team'].strip().lower()
    if name in ('over', 'under'):
        return [x for x in candidates if x is not g
                and x['team'].strip().lower() in ('over', 'under')
                and _point_or_none(x['point']) == p]
    if p is None:
        return [x for x in candidates if x is not g
                and x['team'] != g['team']
                and _point_or_none(x['point']) is None]
    return [x for x in candidates if x is not g
            and x['team'] != g['team']
            and _point_or_none(x['point']) == -p][:1]


def attach_consensus_metrics(groups):
    """Annotate every group with the legacy avg/deviation metrics AND, where
    the opposite side exists, devig fair probability + EV% of the best price.
    Books missing either side of a market don't contribute to the fair number."""
    for g in groups.values():
        decs = [crt_american_to_decimal(o) for o in g['books'].values()]
        avg_dec = sum(decs) / len(decs)
        g['avg_odds'] = crt_prob_to_american(1.0 / avg_dec)
        g['deviation'] = (1.0 / avg_dec - 1.0 / crt_american_to_decimal(g['best_odds'])) * 100
        # legacy shape consumed via self.best_lines
        g['bookmakers'] = [{'bookmaker': b, 'odds': o} for b, o in g['books'].items()]

    by_market = defaultdict(list)
    for g in groups.values():
        by_market[(g['game_id'], g['market_type'])].append(g)

    for gs in by_market.values():
        for g in gs:
            siblings = _market_siblings(g, gs)
            if not siblings:
                continue
            fair_probs = []
            for book, odds in g['books'].items():
                total = crt_implied_prob(odds)
                complete = True
                for s in siblings:
                    so = s['books'].get(book)
                    if so is None:
                        complete = False
                        break
                    total += crt_implied_prob(so)
                if complete:
                    fair_probs.append(crt_implied_prob(odds) / total)
            if fair_probs:
                fp = sum(fair_probs) / len(fair_probs)
                g['fair_prob'] = fp
                g['fair_odds'] = crt_prob_to_american(fp)
                g['ev_pct'] = (fp * crt_american_to_decimal(g['best_odds']) - 1) * 100
                g['devig_books'] = len(fair_probs)
    return groups


# ------------------------------------------------------------- splits store

def _fmt_ts(value, fallback=""):
    if not value:
        return fallback
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime('%b %d, %I:%M %p')


def _nickname(team):
    return team.split()[-1].lower() if team else ''


def _classify_split_market(name):
    n = (name or "").lower()
    if 'moneyline' in n or n == 'ml':
        return 'ml'
    if 'total' in n:
        return 'total'
    return 'spread'   # Run Line / Puck Line / Spread


_OPT_POINT_RE = re.compile(r'\s*([-+]\d+(?:\.\d+)?)\s*$')
_TOTAL_OPT_RE = re.compile(r'^\s*(over|under)\s+([\d.]+)', re.IGNORECASE)


class SplitsStore:
    """Loads + merges the DK scrape and SBD consensus splits files for one
    sport, and indexes per-side entries so best-line rows can join on them."""

    SOURCES = (("DK", "{sport}_betting_latest.json"),
               ("SBD", "{sport}_sbd_betting_latest.json"))

    def __init__(self):
        self.sport = None
        self.games = []          # merged, each game dict + 'source', '_updated'
        self._join = {}          # ('ml', nick) / ('spread', nick) / ('total', side, point)
        self._mtimes = {}

    def load(self, sport, force=False):
        """(Re)load both source files if anything changed. Returns True when
        the in-memory data was rebuilt."""
        if not sport:
            self.sport, self.games, self._join = None, [], {}
            return False
        paths = [(src, os.path.join("SplitsData", tpl.format(sport=sport)))
                 for src, tpl in self.SOURCES]
        mtimes = {p: os.path.getmtime(p) for _, p in paths if os.path.exists(p)}
        if not force and sport == self.sport and mtimes == self._mtimes:
            return False
        self.sport = sport
        self._mtimes = mtimes

        merged = {}
        for source, path in paths:
            if path not in mtimes:
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[SplitsStore] failed to read {path}: {e}")
                continue
            file_dt = datetime.fromtimestamp(mtimes[path])
            for game in data or []:
                game = dict(game)
                game['source'] = source
                updated = game.get('last_update')
                try:
                    game['_updated'] = (datetime.fromisoformat(
                        str(updated).replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
                        if updated else file_dt)
                except (ValueError, TypeError):
                    game['_updated'] = file_dt
                key = (_nickname(game.get('away_team', '')),
                       _nickname(game.get('home_team', '')))
                prev = merged.get(key)
                if prev is None or game['_updated'] >= prev['_updated']:
                    merged[key] = game
        self.games = sorted(merged.values(), key=lambda g: g['_updated'], reverse=True)
        self._build_join_index()
        return True

    def _build_join_index(self):
        self._join = {}
        for game in self.games:
            for mkt_name, options in (game.get('markets') or {}).items():
                cls = _classify_split_market(mkt_name)
                for opt in options:
                    label = opt.get('option', '')
                    try:
                        handle = float(opt.get('handle_percentage', ''))
                        bets = float(opt.get('bets_percentage', ''))
                    except (TypeError, ValueError):
                        continue
                    entry = {
                        'handle': handle, 'bets': bets,
                        'divergence': handle - bets,
                        'rlm': abs(handle - bets) >= 15,
                        'source': game['source'],
                    }
                    if cls == 'total':
                        m = _TOTAL_OPT_RE.match(label)
                        if m:
                            self._join[('total', m.group(1).lower(),
                                        float(m.group(2)))] = entry
                    else:
                        pm = _OPT_POINT_RE.search(label)
                        entry['point'] = float(pm.group(1)) if pm else None
                        team = _OPT_POINT_RE.sub('', label)
                        self._join[(cls, _nickname(team))] = entry

    def lookup_for_line(self, market_key, team, point):
        """Splits entry for one best-line side, or None. Spread joins are
        gated on the line matching the splits line (alt lines stay unjoined)."""
        tl = team.strip().lower()
        if tl in ('over', 'under'):
            p = _point_or_none(point)
            return self._join.get(('total', tl, p)) if p is not None else None
        if 'h2h' in market_key:
            return self._join.get(('ml', _nickname(team)))
        if 'spreads' in market_key:
            entry = self._join.get(('spread', _nickname(team)))
            if entry is None:
                return None
            p = _point_or_none(point)
            if entry.get('point') is None or (p is not None and abs(entry['point'] - p) < 0.01):
                return entry
        return None


# ----------------------------------------------------------- list model

class _CRTListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return self.rows[index.row()]
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()


# ------------------------------------------------- lines view (header+rows)

def _lines_columns(w):
    """Maximally condensed lines view: SIDE (slack) | BEST (price+book) | FAIR.
    BEST is sized to its content so FAIR sits right next to the book abbrev, and
    H%/B% / mark columns are gone (splits live in the dedicated Splits view)."""
    fair_w = 44
    best_w = 70
    x_fair = w - fair_w - 4   # small right margin so FAIR hugs the edge
    x_best = x_fair - best_w
    return {'side': (8, max(50, x_best - 10)), 'best': (x_best, best_w),
            'fair': (x_fair, fair_w)}


class CRTLinesHeader(QWidget):
    H = 18

    def __init__(self):
        super().__init__()
        self.setFixedHeight(self.H)

    def paintEvent(self, _):
        p = QPainter(self)
        w = self.width()
        p.fillRect(self.rect(), QColor(CRT_BG))
        cols = _lines_columns(w)
        p.setFont(_crt_font(9, True))
        vc = int(Qt.AlignmentFlag.AlignVCenter)
        for key, caption in (('side', 'SIDE'), ('best', 'BEST'), ('fair', 'FAIR')):
            x, cw = cols[key]
            _glow_text(p, QRect(x, 0, cw, self.H), vc, caption, AMBER_DIM)
        p.setPen(QPen(QColor(CRT_FRAME), 1))
        p.drawLine(6, self.H - 1, w - 6, self.H - 1)


class CRTLinesDelegate(QStyledItemDelegate):
    ROW_H = 18

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        painter.save()
        r = option.rect
        w = r.width()
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.fillRect(r, QColor("#1b2230") if hovered
                         else QColor(CRT_BG) if index.row() % 2 == 0 else QColor(CRT_BG_ALT))

        cols = _lines_columns(w)
        vc = int(Qt.AlignmentFlag.AlignVCenter)
        ev = row.get('ev')
        strong = ev is not None and ev >= 1.5

        # SIDE: name bright, market tag dim after it
        x, cw = cols['side']
        f_side = _crt_font(11, strong)
        fm = QFontMetrics(f_side)
        tag = f"  {row.get('mlabel', '')}"
        side = fm.elidedText(row['side'], Qt.TextElideMode.ElideRight,
                             cw - fm.horizontalAdvance(tag))
        painter.setFont(f_side)
        _glow_text(painter, QRect(x, r.top(), cw, self.ROW_H), vc, side, AMBER)
        sw = fm.horizontalAdvance(side)
        painter.setFont(_crt_font(9))
        _glow_text(painter, QRect(x + sw, r.top(), cw - sw, self.ROW_H), vc, tag, AMBER_DIM)

        # BEST: price bold + book abbrev dim
        x, cw = cols['best']
        painter.setFont(_crt_font(11, True))
        best_s = row['best_str']
        _glow_text(painter, QRect(x, r.top(), cw, self.ROW_H), vc, best_s, AMBER)
        bw = QFontMetrics(_crt_font(11, True)).horizontalAdvance(best_s + " ")
        painter.setFont(_crt_font(9, True))
        _glow_text(painter, QRect(x + bw, r.top(), cw - bw, self.ROW_H), vc,
                   row.get('book_abbrev', ''), AMBER_DIM)

        # FAIR
        x, cw = cols['fair']
        painter.setFont(_crt_font(10))
        _glow_text(painter, QRect(x, r.top(), cw, self.ROW_H), vc,
                   row.get('fair_str', '—'), AMBER_DIM)

        painter.restore()

    def sizeHint(self, option, index):
        return QSize(0, self.ROW_H)


# --------------------------------------------------------- splits view rows

class CRTSplitsDelegate(QStyledItemDelegate):
    GAME_H = 26
    ROW_H = 24

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        painter.save()
        r = option.rect
        w = r.width()
        vc = int(Qt.AlignmentFlag.AlignVCenter)

        if row['kind'] == 'game':
            painter.fillRect(r, QColor(CRT_BG))
            painter.setFont(_crt_font(11, True))
            title = f"├─ {row['title']} "
            _glow_text(painter, QRect(10, r.top(), w - 20, self.GAME_H), vc, title, AMBER)
            tw = QFontMetrics(_crt_font(11, True)).horizontalAdvance(title)
            painter.setFont(_crt_font(10))
            meta = f"─ {row['meta']} "
            _glow_text(painter, QRect(10 + tw, r.top(), w - tw - 20, self.GAME_H),
                       vc, meta, AMBER_DIM)
            mw = QFontMetrics(_crt_font(10)).horizontalAdvance(meta)
            painter.setPen(QPen(QColor(CRT_FRAME), 1))
            ly = r.top() + self.GAME_H // 2
            painter.drawLine(14 + tw + mw, ly, w - 14, ly)
        else:
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            painter.fillRect(r, QColor("#1b2230") if hovered
                             else QColor(CRT_BG) if index.row() % 2 == 0 else QColor(CRT_BG_ALT))
            painter.setFont(_crt_font(10, True))
            _glow_text(painter, QRect(24, r.top(), 60, self.ROW_H), vc,
                       row['mlabel'], AMBER_DIM)
            painter.setFont(_crt_font(11))
            _glow_text(painter, QRect(92, r.top(), 200, self.ROW_H), vc,
                       row['option'], AMBER)

            # handle (top) and bets (bottom) bars
            bx = 310
            bw = max(80, w - bx - 200)
            painter.setPen(Qt.PenStyle.NoPen)
            for li, (pct, alpha) in enumerate(((row['handle'], 215), (row['bets'], 110))):
                by = r.top() + 5 + li * 9
                painter.fillRect(bx, by, bw, 6, QColor("#1a2029"))
                painter.fillRect(bx, by, int(bw * min(pct, 100) / 100.0), 6,
                                 QColor(127, 214, 160, alpha))
            painter.setFont(_crt_font(10, row['rlm']))
            txt = f"{row['handle']:.0f}/{row['bets']:.0f}"
            _glow_text(painter, QRect(bx + bw + 10, r.top(), 70, self.ROW_H), vc, txt,
                       RLM_WARN if row['rlm'] else AMBER_DIM)
            if row['rlm']:
                painter.setFont(_crt_font(10, True))
                _glow_text(painter, QRect(w - 90, r.top(), 80, self.ROW_H),
                           int(vc | Qt.AlignmentFlag.AlignRight), "⚠RLM", RLM_WARN)

        painter.restore()

    def sizeHint(self, option, index):
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        return QSize(0, self.GAME_H if row.get('kind') == 'game' else self.ROW_H)


# ------------------------------------------------------------- the widget

CRT_QSS = f"""
QWidget#crtBlotter {{
    background: {CRT_BG};
    border: 1px solid {CRT_FRAME};
}}
QLineEdit#crtFilter {{
    background: {CRT_BG};
    color: {AMBER};
    border: 1px solid {CRT_FRAME};
    padding: 1px 6px;
    font-family: monospace;
    font-size: 11px;
    selection-background-color: {AMBER_FAINT};
}}
QLineEdit#crtFilter:focus {{ border-color: {AMBER}; }}
QPushButton#crtChip {{
    background: {CRT_BG};
    color: {AMBER_DIM};
    border: 1px solid {CRT_FRAME};
    padding: 1px 5px;
    font-family: monospace;
    font-size: 10px;
    font-weight: bold;
}}
QPushButton#crtChip:checked {{
    background: #16241c;
    color: {AMBER_HOT};
    border-color: {AMBER_HOT};
}}
QLabel#crtStatus {{
    color: {AMBER_DIM};
    font-family: monospace;
    font-size: 10px;
}}
QLabel#crtMode {{
    color: {AMBER_HOT};
    font-family: monospace;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
}}
QListView#crtList {{
    background: {CRT_BG};
    border: none;
    outline: none;
}}
QListView#crtList QScrollBar:vertical {{
    background: {CRT_BG}; width: 8px; margin: 0;
}}
QListView#crtList QScrollBar::handle:vertical {{
    background: {CRT_FRAME}; min-height: 24px;
}}
QListView#crtList QScrollBar::add-line:vertical,
QListView#crtList QScrollBar::sub-line:vertical {{ height: 0; }}
"""


class _CornerGrip(QWidget):
    """Bottom-right resize handle for the floating overlay."""
    def __init__(self, target):
        super().__init__(target)
        self.target = target
        self.setFixedSize(15, 15)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._drag = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(QPen(QColor(AMBER_DIM), 1))
        w, h = self.width(), self.height()
        for i in range(3):
            o = 3 + i * 4
            p.drawLine(w - o, h - 3, w - 3, h - o)

    def mousePressEvent(self, e):
        self._drag = (e.globalPosition().toPoint(), self.target.size())

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        start, sz = self._drag
        d = e.globalPosition().toPoint() - start
        self.target._resize_overlay(sz.width() + d.x(), sz.height() + d.y())

    def mouseReleaseEvent(self, _):
        self._drag = None


class CRTBestLinesWidget(QWidget):
    """Slate blotter for best lines (devig) and betting splits. Usable as a
    draggable / resizable / collapsible floating overlay (drag by the control
    bar, resize from the bottom-right grip, double-click the mode label to
    collapse to just the bar)."""

    MAX_LINE_ROWS = 250

    def __init__(self, parent=None):
        super().__init__(parent)
        self.best_lines = {}
        self.show_splits = False
        self.current_sport = None
        self.splits_store = SplitsStore()
        self._line_rows = []      # unfiltered, pre-sort
        self._props_mode = False
        self.setObjectName("crtBlotter")
        self.setStyleSheet(CRT_QSS)
        # Paint the QSS background across the whole widget (incl. the control bar
        # area). Without this a plain QWidget ignores its stylesheet `background`,
        # so as a floating overlay the table behind bled through the bar.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # External toggle button (EffortOdds places it in its header bar and
        # reads .show_splits right after our handler runs — connection order
        # matters, so connect here in __init__ like the legacy widget did).
        self.toggle_button = QPushButton("Splits")
        self.toggle_button.setObjectName("crtChip")
        self.toggle_button.setStyleSheet(f"""
            QPushButton {{
                background: {CRT_BG}; color: {AMBER};
                border: 1px solid {CRT_FRAME}; padding: 2px 6px;
                font-family: monospace; font-size: 10px; font-weight: bold;
            }}
            QPushButton:hover {{ border-color: {AMBER}; }}
        """)
        self.toggle_button.clicked.connect(self.toggle_splits_view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        # control bar: mode label + filter + sort chips + status + refresh + toggle
        # (the refresh/toggle buttons used to sit in an external EffortOdds
        # header row; they live in here now to reclaim that vertical space)
        self._ctrl_bar = bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(4, 3, 4, 3)
        bl.setSpacing(4)

        self.mode_label = QLabel("BEST")
        self.mode_label.setObjectName("crtMode")
        self.mode_label.setCursor(Qt.CursorShape.SizeAllCursor)  # drag handle hint
        self.mode_label.setToolTip("Drag to move · double-click to collapse")
        bl.addWidget(self.mode_label)

        self.filter_box = QLineEdit()
        self.filter_box.setObjectName("crtFilter")
        self.filter_box.setPlaceholderText("filter…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.setMinimumWidth(60)
        self.filter_box.textChanged.connect(self._render_current)
        bl.addWidget(self.filter_box, 1)

        # Single compact sort-cycle button (replaces the EV/DEV/SIDE chip trio to
        # reclaim horizontal space): click cycles EV% → deviation → side.
        self._sort_modes = ("EV", "DEV", "SIDE")
        self._sort_idx = 0
        self.sort_btn = QPushButton()
        self.sort_btn.setObjectName("crtChip")
        self.sort_btn.setToolTip("Sort: EV% / deviation vs avg / side — click to cycle")
        self.sort_btn.setFixedWidth(52)
        self.sort_btn.clicked.connect(self._cycle_sort)
        self._update_sort_btn()
        bl.addWidget(self.sort_btn)

        # Status label kept (other methods setText on it) but NOT shown in the bar
        # — the market/bet counts were just noise eating horizontal space.
        self.status_label = QLabel("")
        self.status_label.setObjectName("crtStatus")
        self.status_label.hide()

        # Splits refresh: EffortOdds grabs this as self.splits_refresh_button
        # and connects its own asyncSlot (it drives the ✓/✗ feedback states).
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setObjectName("crtChip")
        self.refresh_button.setToolTip("Refresh Betting Splits Data")
        self.refresh_button.setFixedWidth(26)
        bl.addWidget(self.refresh_button)
        bl.addWidget(self.toggle_button)
        layout.addWidget(bar)

        # stacked views
        self.stack = QStackedWidget()

        lines_panel = QWidget()
        lp = QVBoxLayout(lines_panel)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(0)
        self.lines_header = CRTLinesHeader()
        lp.addWidget(self.lines_header)
        self.lines_model = _CRTListModel(self)
        self.lines_view = self._make_view(self.lines_model, CRTLinesDelegate(self))
        lp.addWidget(self.lines_view)
        self.stack.addWidget(lines_panel)

        self.splits_model = _CRTListModel(self)
        self.splits_view = self._make_view(self.splits_model, CRTSplitsDelegate(self))
        self.stack.addWidget(self.splits_view)

        layout.addWidget(self.stack)

        # ---- Floating-overlay behaviour (drag / resize / collapse) ----
        self._min_w, self._min_h = 300, 120
        self._collapsed = False
        self._expanded_h = None          # remembers height to restore on un-collapse
        self._move_origin = None         # (global press pt, widget pos) while dragging
        self.grip = _CornerGrip(self)
        # The control bar is the drag handle; the mode label also doubles as a
        # collapse toggle on double-click.
        self._ctrl_bar.installEventFilter(self)
        self.mode_label.installEventFilter(self)

    # ---------------------------------------------------------- overlay glue
    def _host_rect(self):
        return self.parent().rect() if self.parent() else None

    def _resize_overlay(self, w, h):
        host = self._host_rect()
        max_w = (host.width() - self.x() - 6) if host else w
        max_h = (host.height() - self.y() - 6) if host else h
        w = max(self._min_w, min(w, max(self._min_w, max_w)))
        h = max(self._min_h, min(h, max(self._min_h, max_h)))
        self.resize(w, h)

    def _reposition_grip(self):
        if hasattr(self, 'grip'):
            self.grip.move(self.width() - self.grip.width() - 2,
                           self.height() - self.grip.height() - 2)
            self.grip.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_grip()

    def showEvent(self, e):
        super().showEvent(e)
        self._reposition_grip()

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._expanded_h = self.height()
            self.stack.hide()
            self.grip.hide()
            self.mode_label.setText(self.mode_label.text() + " ▸")
            self.setFixedHeight(self._ctrl_bar.sizeHint().height() + 2)
        else:
            self.setMinimumHeight(0); self.setMaximumHeight(16777215)
            self.stack.show()
            self.grip.show()
            self.mode_label.setText(self.mode_label.text().replace(" ▸", ""))
            if self._expanded_h:
                self.resize(self.width(), self._expanded_h)

    def eventFilter(self, obj, ev):
        if obj not in (self._ctrl_bar, self.mode_label):
            return super().eventFilter(obj, ev)
        t = ev.type()
        # Double-click the mode label -> collapse / expand.
        if obj is self.mode_label and t == QEvent.Type.MouseButtonDblClick:
            self._toggle_collapse()
            return True
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            # On the bar, only start a drag from empty space or the mode label —
            # not from the filter box / chips / buttons.
            if obj is self._ctrl_bar:
                child = self._ctrl_bar.childAt(ev.position().toPoint())
                if not (child is None or child is self.mode_label):
                    return super().eventFilter(obj, ev)
            self._move_origin = (ev.globalPosition().toPoint(), self.pos())
            # Don't consume — let the widget grab the mouse so moves flow here,
            # and so the label's double-click can still be synthesized.
        elif t == QEvent.Type.MouseMove and self._move_origin:
            start_g, start_p = self._move_origin
            delta = ev.globalPosition().toPoint() - start_g
            nx, ny = start_p.x() + delta.x(), start_p.y() + delta.y()
            host = self._host_rect()
            if host:
                nx = max(0, min(nx, host.width() - self.width()))
                ny = max(0, min(ny, host.height() - self.height()))
            self.move(nx, ny)
        elif t == QEvent.Type.MouseButtonRelease:
            self._move_origin = None
        return super().eventFilter(obj, ev)

    def _make_view(self, model, delegate):
        view = QListView()
        view.setObjectName("crtList")
        view.setModel(model)
        view.setItemDelegate(delegate)
        view.setSelectionMode(QListView.SelectionMode.NoSelection)
        view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setUniformItemSizes(False)
        view.setMouseTracking(True)
        view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        return view

    # ------------------------------------------------------------- contract

    def toggle_splits_view(self):
        self.show_splits = not self.show_splits
        if self.show_splits:
            self.mode_label.setText("SPLITS")
            self.toggle_button.setText("Lines")
            self.stack.setCurrentIndex(1)
            self.sort_btn.setVisible(False)
            self.load_and_display_splits()
        else:
            self.mode_label.setText("BEST")
            self.toggle_button.setText("Splits")
            self.stack.setCurrentIndex(0)
            self.sort_btn.setVisible(True)
            self._render_lines()

    def set_sport(self, sport_key):
        sport = SPORT_KEY_MAP.get(sport_key)
        if sport != self.current_sport:
            self.current_sport = sport
            self.splits_store.load(sport)
            if self.show_splits:
                self._render_splits()

    def load_and_display_splits(self):
        self.splits_store.load(self.current_sport)
        self._render_splits()

    async def refresh_splits_data(self):
        """Run both splits scrapers (DK page scrape + SBD API) concurrently.
        Succeeds if either source refreshed."""
        if not self.current_sport:
            return False
        try:
            from SplitsScraper import main_async, main_sbd_async
            results = await asyncio.gather(
                main_async(sport=self.current_sport, max_retries=1),
                main_sbd_async(sport=self.current_sport, max_retries=1),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Exception):
                    print(f"[CRTBestLines] splits scraper error: {r}")
            ok = any(r is True for r in results)
            if ok:
                self.splits_store.load(self.current_sport, force=True)
                if self.show_splits:
                    self._render_splits()
                else:
                    self._rejoin_splits_onto_lines()
            return ok
        except Exception as e:
            print(f"[CRTBestLines] error refreshing splits: {e}")
            return False

    def update_display(self, consolidated_odds_data):
        """Entry point from EffortOdds after every odds fetch. Returns the
        best_lines dict (legacy-compatible shape) for self.best_lines."""
        if not consolidated_odds_data:
            print("No consolidated odds data available. Skipping update.")
            return None
        is_prop_data = any(
            'player_' in m.get('key', '')
            for bm in consolidated_odds_data.get('bookmakers', [])
            for m in bm.get('markets', [])
        )
        if is_prop_data:
            return self._update_display_props(consolidated_odds_data)
        return self._update_display_team_based(consolidated_odds_data)

    # ----------------------------------------------------------- team-based

    def _update_display_team_based(self, consolidated_odds_data):
        self._props_mode = False
        groups = build_market_groups(consolidated_odds_data)
        attach_consensus_metrics(groups)
        self.best_lines = groups

        self.splits_store.load(self.current_sport)
        rows = []
        for g in groups.values():
            if g['best_odds'] is None:
                continue
            team = g['team']
            point = _point_or_none(g['point'])
            tl = team.strip().lower()
            if tl in ('over', 'under'):
                side = f"{g.get('game_name', '')} {team}"
                if point is not None:
                    side += f" {point:g}"
            elif point is not None:
                side = f"{team} {point:+g}"
            else:
                side = team
            ev = g.get('ev_pct')
            rows.append({
                '_mk': g['market_type'], '_team': team, '_point': g['point'],
                'side': side.strip(),
                'mlabel': MARKET_LABELS.get(g['market_type'],
                                            g['market_type'].upper()[:6]),
                'best_str': _fmt_american(g['best_odds']),
                'book_abbrev': _book_abbrev(g['best_bookmaker']),
                'book': g['best_bookmaker'] or '',
                'fair_str': (_fmt_american(g['fair_odds']) if 'fair_odds' in g
                             else f"≈{_fmt_american(g['avg_odds'])}"),
                'ev': ev,
                'dev': g.get('deviation', 0.0),
                'splits': self.splits_store.lookup_for_line(
                    g['market_type'], team, g['point']),
                'game': g.get('game_name', ''),
            })
        self._line_rows = rows

        paired = sum(1 for r in rows if r['ev'] is not None)
        self._lines_status = f"{len(rows)}·{paired}/{len(rows)}"
        if not self.show_splits:
            self._render_lines()
        return self.best_lines

    def _rejoin_splits_onto_lines(self):
        """Recompute the splits join on existing line rows after fresher
        splits data arrives (without waiting for the next odds fetch)."""
        if self._props_mode:
            return
        for r in self._line_rows:
            if '_mk' in r:
                r['splits'] = self.splits_store.lookup_for_line(
                    r['_mk'], r['_team'], r['_point'])
        self._render_lines()

    # ---------------------------------------------------------------- props

    def _update_display_props(self, consolidated_odds_data):
        """Prop markets keep the legacy LineCalculator pipeline; rows are
        rendered in the same blotter (no devig: O/U points differ per book)."""
        self._props_mode = True
        bookmakers = [bm['title'] for bm in consolidated_odds_data.get('bookmakers', [])]
        if not bookmakers:
            print("Warning: No bookmakers found in consolidated data")
            return None
        table_data = {}
        for bm in consolidated_odds_data.get('bookmakers', []):
            bm_title = bm['title']
            for market in bm.get('markets', []):
                market_key = market['key']
                for outcome in market.get('outcomes', []):
                    player = outcome.get('description', outcome.get('name', ''))
                    row_label = f"{player} - {market_key}"
                    if row_label not in table_data:
                        table_data[row_label] = {
                            'game_id': market.get('game_id', ''), 'is_header': False}
                    name = outcome.get('name', '').lower()
                    point = outcome.get('point', '')
                    price = outcome.get('price', '')
                    if name == 'over':
                        value = f"{price} O ({point})"
                    elif name == 'under':
                        value = f"{price} U ({point})"
                    else:
                        value = f"{price} ({point})"
                    table_data[row_label][bm_title] = value

        calculator = LineCalculator.calculate_best_lines(table_data, bookmakers)
        best_lines = calculator.calculate_best_lines()
        self.best_lines = best_lines

        rows = []
        for market_key, data in best_lines.items():
            try:
                player, market_type = market_key.split(':')
            except ValueError:
                continue
            for side_key, side_label in (('over', 'OVER'), ('under', 'UNDER')):
                entry = data.get(side_key)
                if not entry:
                    continue
                multi = entry['count'] > 1
                rows.append({
                    'side': player,
                    'mlabel': f"{market_type} {side_label}",
                    'best_str': f"{_fmt_american(entry['odds'])} ({entry['point']})",
                    'book_abbrev': _book_abbrev(entry['bookmaker']),
                    'book': entry['bookmaker'],
                    'fair_str': f"≈{entry['avg_odds']:.0f}" if multi else "—",
                    'ev': None,
                    'ev_placeholder': f"d{entry['deviation']:+.1f}" if multi else "SOLO",
                    'dev': entry['deviation'] if multi else 0.0,
                    'splits': None,
                    'game': '',
                })
        self._line_rows = rows
        self._lines_status = f"{len(rows)} PROP LINES"
        if not self.show_splits:
            self._render_lines()
        return best_lines

    # ------------------------------------------------------------ rendering

    @property
    def _sort_mode(self):
        return self._sort_modes[self._sort_idx]

    def _update_sort_btn(self):
        self.sort_btn.setText("⇅ " + self._sort_mode)

    def _cycle_sort(self):
        self._sort_idx = (self._sort_idx + 1) % len(self._sort_modes)
        self._update_sort_btn()
        self._render_current()

    def _sort_key(self):
        m = self._sort_mode
        if m == "SIDE":
            return (lambda r: r['side'].lower()), False
        if m == "DEV":
            return (lambda r: r.get('dev') or 0.0), True
        return (lambda r: r['ev'] if r['ev'] is not None
                else (r.get('dev') or 0.0) - 1000.0), True

    def _render_current(self, *_):
        if not hasattr(self, 'splits_model'):
            return  # chip/filter signal during __init__, views not built yet
        if self.show_splits:
            self._render_splits()
        else:
            self._render_lines()

    def _render_lines(self):
        rows = self._line_rows
        q = self.filter_box.text().strip().lower()
        if q:
            rows = [r for r in rows
                    if q in r['side'].lower() or q in r['book'].lower()
                    or q in r['mlabel'].lower() or q in r.get('game', '').lower()]
        key, rev = self._sort_key()
        rows = sorted(rows, key=key, reverse=rev)[:self.MAX_LINE_ROWS]
        self.lines_model.set_rows(rows)
        status = getattr(self, '_lines_status', '')
        shown = f"{len(rows)}↓ " if q else ""
        self.status_label.setText(f"{shown}{status}")

    def _render_splits(self):
        q = self.filter_box.text().strip().lower()
        rows = []
        for game in self.splits_store.games:
            away = game.get('away_team', 'Unknown')
            home = game.get('home_team', 'Unknown')
            title = f"{away} @ {home}"
            meta = " · ".join(filter(None, (
                _fmt_ts(game.get('game_time')),
                game.get('source', ''),
                f"upd {_fmt_ts(game.get('_updated'))}",
            )))
            game_rows = []
            for mkt_name, options in (game.get('markets') or {}).items():
                mlabel = {'ml': 'ML', 'total': 'TOT', 'spread': 'SPRD'}[
                    _classify_split_market(mkt_name)]
                for opt in options:
                    label = opt.get('option', '')
                    try:
                        handle = float(opt.get('handle_percentage', ''))
                        bets = float(opt.get('bets_percentage', ''))
                    except (TypeError, ValueError):
                        continue
                    if q and not (q in title.lower() or q in label.lower()
                                  or q in mkt_name.lower()):
                        continue
                    game_rows.append({
                        'kind': 'option', 'mlabel': mlabel, 'option': label,
                        'handle': handle, 'bets': bets,
                        'rlm': abs(handle - bets) >= 15,
                    })
            if game_rows:
                rows.append({'kind': 'game', 'title': title, 'meta': meta})
                rows.extend(game_rows)
        self.splits_model.set_rows(rows)
        n_games = sum(1 for r in rows if r['kind'] == 'game')
        n_opts = len(rows) - n_games
        sport = (self.current_sport or "?").upper()
        self.status_label.setText(f"{sport} · {n_games}g · {n_opts}")


SPORT_KEY_MAP = {
    "basketball_nba": "nba",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "football_nfl": "nfl",
    "basketball_ncaab": "ncaab",
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "ncaaf",
}
