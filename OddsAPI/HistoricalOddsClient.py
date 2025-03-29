import qasync
import asyncio
import aiohttp
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QHBoxLayout, QScrollArea
)

from Creds import SUPER_KEY

# TODO: Fix grapg output being erased after interacting with checboxes
# Using checkboxes to add/remove plots of bookmakers removes them permanently

class HistoricalOddsClient:
    """Client for fetching historical odds data from theOddsAPI"""
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.the-odds-api.com/v4/historical"
        self.cache = {}
        self.min_interval = timedelta(minutes=10)  # Minimum snapshot interval
    
    async def get_historical_snapshots(self, session, sport_key, event_id, market, 
                                     start_time, end_time=None, regions="us"):
        """Fetches historical odds snapshots"""
        if end_time is None:
            end_time = datetime.now()
        else:
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))
            
        start_time = datetime.fromisoformat(start_time.replace('Z', ''))
        snapshots = []
        current_time = start_time
        
        print(f"Fetching snapshots from {start_time} to {end_time}")
        
        while current_time < end_time:
            try:
                snapshot = await self._fetch_single_snapshot(
                    session, sport_key, event_id, market, 
                    current_time.isoformat() + 'Z', regions
                )
                
                if snapshot:
                    snapshots.append(snapshot)
                    next_ts = snapshot.get('next_timestamp')
                    current_time = (
                        datetime.fromisoformat(next_ts.replace('Z', '')) 
                        if next_ts 
                        else current_time + self.min_interval
                    )
                else:
                    current_time += self.min_interval
                    
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"Error processing snapshot at {current_time}: {str(e)}")
                current_time += self.min_interval
                
        print(f"Retrieved {len(snapshots)} valid snapshots")
        return snapshots
    
    async def _fetch_single_snapshot(self, session, sport_key, event_id, market, date, regions):
        """Fetch a single historical snapshot"""
        url = f"{self.base_url}/sports/{sport_key}/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": market,
            "date": date
        }
        
        try:
            async with session.get(url, params=params) as response:
                response_data = await response.json()
                
                if response.status == 200:
                    print(f"Successful snapshot fetch for {date}")
                    # Add point change detection
                    response_data['point_changes'] = self._detect_point_changes(response_data)
                    return response_data
                else:
                    error_msg = f"Error {response.status} fetching snapshot: {response_data.get('message', 'No error message')}"
                    print(error_msg)
                    return None
                    
        except Exception as e:
            print(f"Exception fetching snapshot: {str(e)}")
            return None
    
    def _detect_point_changes(self, snapshot):
        """Detect point changes across bookmakers"""
        point_changes = {}
        for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
            for market in bookmaker.get('markets', []):
                for outcome in market.get('outcomes', []):
                    if 'point' in outcome:
                        key = (outcome.get('name'), outcome.get('description', ''))
                        point_changes.setdefault(key, set()).add(outcome['point'])
        return {k: sorted(v) for k, v in point_changes.items() if len(v) > 1}


class HistoricalOddsWidget(QWidget):
    """Widget for displaying historical odds movement with point change handling"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sport_key = None
        self.event_id = None
        self.market_key = None
        self.home_team = None
        self.away_team = None
        self.api_key = SUPER_KEY
        self.client = HistoricalOddsClient(self.api_key)  # Initialize client immediately
        self.bookmaker_visible = {}
        self.current_snapshots = []
        self._load_task = None  # Track current loading task
        self.init_ui()
        
    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Header with controls
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Historical Odds")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #7bd419")
        header_layout.addWidget(self.title_label)
        
        header_layout.addStretch(1)
        
        time_label = QLabel("Time:")
        header_layout.addWidget(time_label)
        
        self.time_range = QComboBox()
        self.time_range.addItems(["1h", "3h", "6h", "12h", "24h"])
        self.time_range.setFixedWidth(60)
        self.time_range.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.time_range)
        
        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedWidth(30)
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        header_layout.addWidget(self.refresh_button)
        
        layout.addLayout(header_layout)
        
        # Market info label
        self.market_info = QLabel("Select a market to view historical odds")
        self.market_info.setStyleSheet("color: #6c757d; font-style: italic;")
        layout.addWidget(self.market_info)
        
        # Main content area
        content_layout = QHBoxLayout()
        
        # Plot section
        self.plot_panel = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_panel)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        
        # https://pyqtgraph.readthedocs.io/en/latest/api_reference/widgets/plotwidget.html
        self.plot_widget = pg.PlotWidget(background="#29313D")
        self.plot_widget.setLabel('left', 'Odds')
        self.plot_widget.setLabel('bottom', 'Time')
        #self.plot_widget.addLegend()
        self.plot_widget.addItem(pg.GridItem())
        
        self.plot_layout.addWidget(self.plot_widget)
        content_layout.addWidget(self.plot_panel, 4)
        
        # Bookmaker toggle section
        self.bookmaker_panel = QWidget()
        self.bookmaker_layout = QVBoxLayout(self.bookmaker_panel)
        self.bookmaker_layout.setContentsMargins(0, 0, 0, 0)
        self.bookmaker_layout.setSpacing(2)
        
        scroll_area = QScrollArea()
        scroll_area.setWidget(self.bookmaker_panel)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFixedWidth(100)
        content_layout.addWidget(scroll_area, 1)
        
        layout.addLayout(content_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(5)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        
        # Initial state
        self.set_enabled(False)
        
        # Add initial "no data" message
        self.no_data_text = pg.TextItem("Select a market to view historical odds", 
                                      anchor=(0.5, 0.5))
        self.plot_widget.addItem(self.no_data_text)
        self.no_data_text.setPos(0.5, 0.5)
    
    def set_enabled(self, enabled):
        """Enable or disable the widget controls"""
        self.time_range.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.plot_widget.setEnabled(enabled)
    
    def set_market(self, sport_key, event_id, market_key, home_team, away_team):
        """Set the market to display and fetch data"""
        print(f"Setting market in widget: {sport_key}, {event_id}, {market_key}, {home_team}, {away_team}")
        self.sport_key = sport_key
        self.event_id = event_id
        self.market_key = market_key
        self.home_team = home_team
        self.away_team = away_team
        
        # Update UI
        if home_team and away_team:
            self.market_info.setText(f"{home_team} vs {away_team} - {market_key}")
        else:
            self.market_info.setText(f"Market: {market_key}")
        
        self.set_enabled(True)
        
        # Cancel any existing task
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        
        # Start new data load
        self._load_task = asyncio.create_task(self.load_data())
    
    @qasync.asyncSlot()
    async def on_refresh_clicked(self):
        """Handle refresh button click"""
        if self._load_task and not self._load_task.done():
            return  # Skip if already loading
            
        try: 
            self._load_task = asyncio.create_task(self.load_data())
            await self._load_task
        except Exception as e:
            print(f"Refresh error: {e}")
    
    async def load_data(self):
        """Load historical odds data and populate the graph"""
        if not all([self.sport_key, self.event_id, self.market_key]):
            print("Missing required market info")
            return
            
        if not self.client:
            print("Client not initialized!")
            return
            
        # Calculate time range
        end_time = datetime.now()
        start_time = self.calculate_start_time(end_time)
        
        self.progress_bar.setValue(10)
        self.refresh_button.setEnabled(False)
        self.plot_widget.removeItem(self.no_data_text)
        
        try:
            print(f"Fetching historical data from {start_time} to {end_time}")
            async with aiohttp.ClientSession() as session:
                snapshots = await self.client.get_historical_snapshots(
                    session,
                    self.sport_key,
                    self.event_id,
                    self.market_key,
                    start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                
            print(f"Processing {len(snapshots)} snapshots")
            self.progress_bar.setValue(50)
            
            if snapshots:
                self.current_snapshots = snapshots
                self.update_bookmaker_toggles(snapshots)
                self.update_plot(snapshots)
                self.progress_bar.setValue(100)
            else:
                print("No valid historical data available")
                self._show_no_data_message("No historical data available")
                
        except Exception as e:
            print(f"Error loading historical odds: {str(e)}")
            import traceback
            traceback.print_exc()
            self._show_no_data_message("Error loading data")
        finally:
            self.progress_bar.setValue(0)
            self.refresh_button.setEnabled(True)
            QTimer.singleShot(1000, lambda: self.progress_bar.setValue(0))
    
    def calculate_start_time(self, end_time):
        """Calculate start time based on selected time range"""
        range_text = self.time_range.currentText()
        
        if range_text == "1h":
            return end_time - timedelta(hours=1)
        elif range_text == "3h":
            return end_time - timedelta(hours=3)
        elif range_text == "6h":
            return end_time - timedelta(hours=6)
        elif range_text == "12h":
            return end_time - timedelta(hours=12)
        elif range_text == "24h":
            return end_time - timedelta(hours=24)
        else:
            return end_time - timedelta(hours=6)  # Default
            
    def create_bookmaker_toggle(self, bookmaker_name):
        """Create a toggle handler for a specific bookmaker"""
        def toggle_handler(state):
            self.on_bookmaker_toggled(bookmaker_name, state)
        return toggle_handler
            
    def update_bookmaker_toggles(self, snapshots):
        """Update the bookmaker toggle checkboxes based on available data"""
        # Clear existing toggles
        while self.bookmaker_layout.count():
            item = self.bookmaker_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        # Extract unique bookmakers from all snapshots
        all_bookmakers = set()
        for snapshot in snapshots:
            for bookmaker in snapshot.get("data", {}).get("bookmakers", []):
                all_bookmakers.add(bookmaker["key"])
        
        print(f"Found bookmakers: {all_bookmakers}")
                
        # Add "All" checkbox
        select_all = QCheckBox("All")
        select_all.setChecked(True)
        select_all.stateChanged.connect(self.toggle_all_bookmakers)
        self.bookmaker_layout.addWidget(select_all)
        
        # Add a checkbox for each bookmaker
        for bookmaker_name in sorted(all_bookmakers):
            checkbox = QCheckBox(bookmaker_name)
            checkbox.setChecked(True)
            self.bookmaker_visible[bookmaker_name] = True
            # Use the function factory to avoid lambda capture issues
            checkbox.stateChanged.connect(self.create_bookmaker_toggle(bookmaker_name))
            self.bookmaker_layout.addWidget(checkbox)
            
        # Add stretch to push all checkboxes to the top
        self.bookmaker_layout.addStretch(1)
        
    def toggle_all_bookmakers(self, state):
        """Toggle all bookmaker checkboxes to the given state"""
        checked = state == Qt.CheckState.Checked
        # Skip the first checkbox (which is "All") and last item (which is stretch)
        for i in range(1, self.bookmaker_layout.count() - 1):
            item = self.bookmaker_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QCheckBox):
                item.widget().setChecked(checked)
                
    def on_bookmaker_toggled(self, bookmaker, state):
        """Handle bookmaker toggle checkbox changes"""
        print(f"Toggling bookmaker: {bookmaker} to {state}")
        self.bookmaker_visible[bookmaker] = (state == Qt.CheckState.Checked)
        # Refresh the plot with current visibility settings
        self.update_plot(self.current_snapshots)
        
    def on_time_range_changed(self):
        """Handle time range dropdown changes"""
        asyncio.create_task(self.load_data())
    
    def update_plot(self, snapshots):
        """Enhanced plotting with point change visualization"""
        self.plot_widget.clear()
        self.plot_widget.addItem(pg.GridItem())
        if not snapshots:
            self._show_no_data_message()
            return
            
        colors = [
            (31, 119, 180), (255, 127, 14), (44, 160, 44),
            (214, 39, 40), (148, 103, 189), (140, 86, 75)
        ]
        
        # Some Legend options for graph display
        #self.plot_widget.addLegend(offset=(10, 10), labelTextSize='8pt')
        
        # Group data by bookmaker and outcome
        plot_data = self._organize_plot_data(snapshots)
        
        # Plot each series with proper point change handling
        for bm_idx, (bookmaker, outcomes) in enumerate(plot_data.items()):
            if not self.bookmaker_visible.get(bookmaker, True):
                continue
                
            color = colors[bm_idx % len(colors)]
            for outcome_key, points_data in outcomes.items():
                self._plot_outcome_series(bookmaker, outcome_key, points_data, color)
        
        self.configure_plot_axes(snapshots)
    
    def _organize_plot_data(self, snapshots):
        """Organize snapshot data using American odds directly"""
        plot_data = {}
        
        for snapshot in snapshots:
            timestamp = datetime.fromisoformat(snapshot['timestamp'].replace('Z', '')).timestamp()
            
            for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
                bm_key = bookmaker['key']
                
                if bm_key not in plot_data:
                    plot_data[bm_key] = {}
                
                for market in bookmaker.get('markets', []):
                    for outcome in market.get('outcomes', []):
                        outcome_key = (outcome.get('name'), outcome.get('description', ''))
                        
                        if outcome_key not in plot_data[bm_key]:
                            plot_data[bm_key][outcome_key] = {
                                'timestamps': [],
                                'american_prices': [],
                                'points': []
                            }
                        
                        # Add timestamp
                        plot_data[bm_key][outcome_key]['timestamps'].append(timestamp)
                        
                        # Add American price exactly as it comes from the API
                        if 'price' in outcome:
                            american_price = outcome['price']
                            # Format with sign if it's a number
                            if isinstance(american_price, (int, float)):
                                if american_price > 0:
                                    american_price = f"+{american_price}"
                                else:
                                    american_price = f"{american_price}"
                            plot_data[bm_key][outcome_key]['american_prices'].append(american_price)
                        else:
                            plot_data[bm_key][outcome_key]['american_prices'].append(None)
                        
                        # Add point if available
                        if 'point' in outcome:
                            plot_data[bm_key][outcome_key]['points'].append(outcome['point'])
                        else:
                            plot_data[bm_key][outcome_key]['points'].append(None)
        
        # Clean up data structure - remove None values
        for bm_key, outcomes in plot_data.items():
            for outcome_key, data in outcomes.items():
                # Keep only entries with valid American prices
                valid_indices = []
                for i, price in enumerate(data['american_prices']):
                    if price is not None:
                        valid_indices.append(i)
                
                if valid_indices:
                    data['timestamps'] = [data['timestamps'][i] for i in valid_indices]
                    data['american_prices'] = [data['american_prices'][i] for i in valid_indices]
                    
                    # Only keep points that have corresponding valid prices
                    if 'points' in data:
                        data['points'] = [
                            data['points'][i] if i < len(data['points']) else None 
                            for i in valid_indices
                        ]
        
        return plot_data
    
    def _plot_outcome_series(self, bookmaker, outcome_key, points_data, color):
        """Plot a single outcome series using American odds directly"""
        if not points_data['timestamps'] or len(points_data['timestamps']) == 0:
            return
        
        timestamps = np.array(points_data['timestamps'])
        
        # Check if we have price data
        if 'american_prices' in points_data and points_data['american_prices'] and len(points_data['american_prices']) > 0:
            american_prices = np.array(points_data['american_prices'])
            
            # Convert to numeric values for plotting
            american_values = []
            for price in american_prices:
                try:
                    if isinstance(price, str) and price.startswith('+'):
                        american_values.append(float(price[1:]))
                    elif isinstance(price, str) and price.startswith('-'):
                        american_values.append(float(price))
                    else:
                        american_values.append(float(price))
                except (ValueError, TypeError):
                    # Default to a safe value if conversion fails
                    american_values.append(-110.0)
            
            american_values = np.array(american_values)
            
            name = f"{bookmaker} - {outcome_key[0]}"
            if outcome_key[1]:
                name += f" ({outcome_key[1]})"
            
            # Plot using the American odds values directly
            line = self.plot_widget.plot(
                timestamps, 
                american_values,
                pen=pg.mkPen(color=color, width=2),
                name=name,
                symbol='o',
                symbolSize=6,
                symbolBrush=color
            )
            
            # Add labels that show both American odds and points if available
            for i, ts in enumerate(timestamps):
                if i < len(american_values):
                    american = american_prices[i]
                    
                    # Format label based on whether we have point data
                    if ('points' in points_data and 
                        points_data['points'] and 
                        i < len(points_data['points']) and 
                        points_data['points'][i] is not None):
                        pt = points_data['points'][i]
                        # 'american' here is actually in a string in decimal (with leading sign)
                        label_text = f"{self._decimal_to_american(float(american))} ({pt:.1f})"
                    else:
                        label_text = f"{american}"
                    
                    label = pg.TextItem(label_text, anchor=(0.5, 1.5), color=color)
                    self.plot_widget.addItem(label)
                    label.setPos(ts, american_values[i])
        
        # If we only have points data (no prices), plot those instead
        elif 'points' in points_data and points_data['points'] and len(points_data['points']) > 0:
            points = np.array(points_data['points'])
            name = f"{bookmaker} - {outcome_key[0]} (Points)"
            if outcome_key[1]:
                name += f" ({outcome_key[1]})"
            
            line = self.plot_widget.plot(
                timestamps, 
                points,
                pen=pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine),
                name=name,
                symbol='s',
                symbolSize=6,
                symbolBrush=color
            )
    
    def configure_plot_axes(self, snapshots):
        """Configure plot axes optimized for American odds display"""
        if not snapshots:
            return
        
        # Set up the time axis
        first_time = datetime.fromisoformat(snapshots[0]['timestamp'].replace('Z', '')).timestamp()
        last_time = datetime.fromisoformat(snapshots[-1]['timestamp'].replace('Z', '')).timestamp()
        
        # X-axis range
        self.plot_widget.setXRange(first_time, last_time)
        
        # Create time formatter for X-axis
        time_axis = pg.AxisItem(orientation='bottom')
        
        def timestamp_formatter(values, scale, spacing):
            result = []
            for value in values:
                try:
                    dt = datetime.fromtimestamp(value)
                    if dt.minute == 0:
                        # For whole hours, show date and hour
                        label = dt.strftime('%m-%d %H:%M')
                    else:
                        # For other times, just show the time
                        label = dt.strftime('%H:%M')
                    
                    result.append(label)
                except:
                    result.append('')
            
            return result
        
        time_axis.tickStrings = timestamp_formatter
        self.plot_widget.setAxisItems({'bottom': time_axis})
        self.plot_widget.setLabel('bottom', 'Time')
        
        # Collect all American prices to determine Y-axis range
        all_american_prices = []
        
        for snapshot in snapshots:
            for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
                for market in bookmaker.get('markets', []):
                    if market['key'] == self.market_key:
                        for outcome in market.get('outcomes', []):
                            if 'price' in outcome:
                                american_price = outcome['price']
                                # Convert to numeric for min/max calculations
                                try:
                                    if isinstance(american_price, str):
                                        if american_price.startswith('+'):
                                            all_american_prices.append(float(american_price[1:]))
                                        elif american_price.startswith('-'):
                                            all_american_prices.append(float(american_price))
                                    else:
                                        all_american_prices.append(float(american_price))
                                except (ValueError, TypeError):
                                    pass  # Skip invalid values
        
        # Set Y-axis range for American odds with proper padding
        if all_american_prices:
            min_val = min(all_american_prices) * 1.05  # More padding for negative values
            max_val = max(all_american_prices) * 1.05
            
            # Ensure we don't have identical min/max which would break the axis
            if min_val == max_val:
                if min_val > 0:
                    min_val = min_val * 0.9
                    max_val = max_val * 1.1
                else:
                    min_val = min_val * 1.1
                    max_val = max_val * 0.9
            
            self.plot_widget.setYRange(min_val, max_val)
            
            # Set up Y-axis label and ticks
            y_axis = self.plot_widget.getAxis('left')
            y_axis.setLabel('American Odds')
            
            # Create appropriate Y-axis ticks
            y_ticks = []
            num_ticks = 5
            step = (max_val - min_val) / num_ticks
            current = min_val
            
            for i in range(num_ticks + 1):
                # Format as American odds with +/- sign
                if current >= 0:
                    y_ticks.append((current, f"+{int(current)}"))
                else:
                    y_ticks.append((current, f"{int(current)}"))
                current += step
                
            y_axis.setTicks([y_ticks])
    
    def _show_no_data_message(self, message="No historical data available"):
        """Show a message when no data is available"""
        self.no_data_text = pg.TextItem(message, anchor=(0.5, 0.5))
        self.plot_widget.addItem(self.no_data_text)
        self.no_data_text.setPos(0.5, 0.5)

    def _american_to_decimal(self, american_odds):
        """Convert American odds to decimal odds with proper handling of extreme values"""
        try:
            if american_odds == 0:
                return 1.0  # Handle zero case
                
            if american_odds > 0:
                return (american_odds / 100) + 1
            else:
                return (100 / abs(american_odds)) + 1
        except Exception as e:
            print(f"Error converting American odds {american_odds} to decimal: {e}")
            return 1.01  # Return a safe default
    
    def _decimal_to_american(self, decimal_odds):
        """Convert decimal odds to American odds with safeguards"""
        try:
            if decimal_odds < 1.01:
                return -10000  # Cap at -10000 for very low decimal odds
            
            if decimal_odds >= 2.0:
                american = round((decimal_odds - 1) * 100)
                return f"+{min(american, 10000)}"  # Cap at +10000
            else:
                # For favorites (decimal odds < 2.0)
                american = round(100 / (decimal_odds - 1))
                return f"-{min(american, 10000)}"  # Cap at -10000
        except Exception as e:
            print(f"Error converting decimal odds {decimal_odds} to American: {e}")
            return "-110"  # Return a safe default
    
    def _decimal_to_american_int(self, decimal_odds):
        """Convert decimal odds to American odds format as int with safeguards"""
        try:
            if decimal_odds < 1.01:
                return -10000  # Cap at -10000 for very low decimal odds
                
            if decimal_odds >= 2.0:
                american = round((decimal_odds - 1) * 100)
                return min(american, 10000)  # Cap at +10000
            else:
                american = round(100 / (decimal_odds - 1))
                return -min(american, 10000)  # Cap at -10000
        except Exception as e:
            print(f"Error converting decimal odds {decimal_odds} to American int: {e}")
            return -110  # Return a safe default
