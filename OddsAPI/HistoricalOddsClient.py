import qasync
import asyncio
import aiohttp
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QHBoxLayout, QScrollArea
)
from PyQt6.QtGui import QColor

from KalshiClient import KalshiClient


def kalshi_cents_to_american_odds(cents):
    """
    Convert Kalshi price in cents (0-100) to American odds.

    Args:
        cents: Price in cents (0-100), representing probability as percentage

    Returns:
        American odds as integer (e.g., -110, +150)
    """
    if cents is None:
        return None

    # Clamp extreme values to prevent division by zero
    if cents <= 1:
        cents = 1
    elif cents >= 99:
        cents = 99

    # Convert cents to probability (0.01 to 0.99)
    prob = cents / 100.0

    # Convert probability to American odds
    if prob >= 0.5:
        # Favorite: negative odds
        american = -(prob / (1 - prob)) * 100
    else:
        # Underdog: positive odds
        american = ((1 - prob) / prob) * 100

    return int(round(american))


def american_odds_to_kalshi_cents(american_odds):
    """
    Convert American odds to Kalshi price in cents (0-100).

    Args:
        american_odds: American odds (e.g., -110, +150)

    Returns:
        Price in cents (0-100)
    """
    if american_odds is None or american_odds == 0:
        return None

    # Convert American odds to probability
    if american_odds < 0:
        # Favorite
        prob = abs(american_odds) / (abs(american_odds) + 100)
    else:
        # Underdog
        prob = 100 / (american_odds + 100)

    # Convert to cents
    return int(round(prob * 100))

class HistoricalOddsClient:
    """Client for fetching historical odds data from theOddsAPI"""

    def __init__(self, api_key, interval_minutes:int):
        self.api_key = api_key
        self.base_url = "https://api.the-odds-api.com/v4/historical"
        self.cache = {}
        self.min_interval = timedelta(minutes=interval_minutes)

    async def get_historical_snapshots(self, session, sport_key, event_id, market,
                                     start_time, end_time=None, regions="us"):
        """Fetches historical odds snapshots in parallel batches"""
        if end_time is None:
            end_time = datetime.now()
        else:
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))

        start_time = datetime.fromisoformat(start_time.replace('Z', ''))
        print(f"Fetching snapshots from {start_time} to {end_time}")

        # Generate time intervals (more efficient than sequential fetching)
        time_points = []
        current_time = start_time
        while current_time < end_time:
            time_points.append(current_time)
            current_time += self.min_interval

        # Set a reasonable concurrency limit to avoid overloading the API
        # and getting rate limited
        concurrency_limit = 5

        # Split time points into batches for controlled parallelism
        snapshot_batches = []
        for i in range(0, len(time_points), concurrency_limit):
            batch = time_points[i:i+concurrency_limit]

            # Create tasks for this batch
            batch_tasks = [
                self._fetch_single_snapshot(
                    session, sport_key, event_id, market,
                    t.isoformat() + 'Z', regions
                )
                for t in batch
            ]

            # Wait for all tasks in this batch to complete
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            # Filter out errors and None results
            valid_snapshots = [
                result for result in batch_results
                if not isinstance(result, Exception) and result is not None
            ]

            snapshot_batches.extend(valid_snapshots)

            # Brief pause between batches to be nice to the API
            await asyncio.sleep(0.2)

        # Sort snapshots by timestamp
        snapshots = sorted(
            snapshot_batches,
            key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '')).timestamp()
        )

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


class KalshiHistoricalOddsClient:
    """Client for fetching historical odds data from Kalshi API"""

    def __init__(self, api_key=None):
        self.kalshi_client = KalshiClient(api_key=api_key)
        self.cache = {}

    async def get_event_markets(self, event_ticker):
        """
        Get all markets for a specific event.

        Args:
            event_ticker: Kalshi event ticker (e.g., 'KXNFLGAME-25OCT27WASKC')

        Returns:
            List of market dictionaries with market info
        """
        try:
            # Run synchronous Kalshi API call in thread pool
            loop = asyncio.get_event_loop()
            event_data = await loop.run_in_executor(
                None,
                lambda: self.kalshi_client.get_event(
                    event_ticker=event_ticker,
                    with_nested_markets=True
                )
            )
            return event_data.get('event', {}).get('markets', [])
        except Exception as e:
            print(f"Error fetching markets for event {event_ticker}: {e}")
            return []

    async def get_historical_candlesticks(self, session, market_ticker, series_ticker,
                                         start_time, end_time=None, period_interval=60):
        """
        Fetches historical candlestick data from Kalshi.

        Args:
            session: Not used for Kalshi (synchronous API), kept for interface compatibility
            market_ticker: Kalshi market ticker (e.g., 'KXNFLGAME-25OCT27WASKC-KC')
            series_ticker: Kalshi series ticker (e.g., 'KXNFLGAME')
            start_time: Start time as datetime or ISO string
            end_time: End time as datetime or ISO string
            period_interval: Candlestick interval in minutes (1, 60, or 1440)

        Returns:
            List of snapshot dictionaries formatted like TheOddsAPI for compatibility
        """
        if end_time is None:
            end_time = datetime.now()
        elif isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))

        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', ''))

        print(f"Fetching Kalshi candlesticks from {start_time} to {end_time}")
        print(f"Market: {market_ticker}, Interval: {period_interval} minutes")

        try:
            # Fetch candlestick data from Kalshi asynchronously
            # Run the synchronous call in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            candlesticks_data = await loop.run_in_executor(
                None,  # Use default executor
                lambda: self.kalshi_client.get_market_candlesticks(
                    ticker=market_ticker,
                    series_ticker=series_ticker,
                    period_interval=period_interval,
                    start_ts=int(start_time.timestamp()),
                    end_ts=int(end_time.timestamp())
                )
            )

            candles = candlesticks_data.get('candlesticks', [])
            print(f"Retrieved {len(candles)} candlesticks from Kalshi")

            # Convert Kalshi candlestick format to TheOddsAPI-like snapshot format
            snapshots = []
            skipped_none = 0
            skipped_time = 0

            for candle in candles:
                price_data = candle.get('price', {})

                # Try to get a price value - prefer close, fall back to previous
                close_price = price_data.get('close')
                if close_price is None:
                    close_price = price_data.get('previous')

                # Skip candlesticks with no price data at all
                if close_price is None:
                    skipped_none += 1
                    continue

                # Convert timestamp
                ts = candle.get('end_period_ts', 0)
                timestamp_dt = datetime.fromtimestamp(ts)

                # For Kalshi, don't filter by time range - show all available data
                # The time range will just determine how far back we fetch
                # But we display everything we get

                # Convert Kalshi price (cents) to American odds
                american_odds = kalshi_cents_to_american_odds(close_price)
                if american_odds is None:
                    continue

                # Format as TheOddsAPI-like snapshot
                snapshot = {
                    'timestamp': timestamp_dt.isoformat() + 'Z',
                    'data': {
                        'bookmakers': [{
                            'key': 'kalshi',
                            'title': 'Kalshi',
                            'markets': [{
                                'key': 'h2h',  # Generic market key for moneyline
                                'outcomes': [{
                                    'name': market_ticker.split('-')[-1],  # Extract team code
                                    'price': american_odds,
                                    'kalshi_cents': close_price,  # Store original for reference
                                }]
                            }]
                        }]
                    }
                }

                snapshots.append(snapshot)

            print(f"Converted {len(snapshots)} valid snapshots (skipped {skipped_none} with no price, {skipped_time} outside time range)")
            return snapshots

        except Exception as e:
            print(f"Error fetching Kalshi candlesticks: {e}")
            import traceback
            traceback.print_exc()
            return []


class HistoricalOddsWidget(QWidget):
    """Widget for displaying historical odds movement with point change handling"""

    def __init__(self, api_key, interval_minutes:int, parent=None, kalshi_api_key=None):
        super().__init__(parent)
        self.sport_key = None
        self.event_id = None
        self.market_key = None
        self.home_team = None
        self.away_team = None
        self.api_key = api_key

        # Initialize both clients
        self.theoddsapi_client = HistoricalOddsClient(self.api_key, interval_minutes)
        self.kalshi_client = KalshiHistoricalOddsClient(api_key=kalshi_api_key)

        # Default to Kalshi as primary
        self.client = self.kalshi_client
        self.data_source = 'kalshi'  # 'kalshi' or 'theoddsapi'

        # Kalshi-specific attributes
        self.kalshi_event_ticker = None
        self.kalshi_series_ticker = None
        self.kalshi_market_ticker = None
        self.kalshi_available_markets = []

        self.bookmaker_visible = {}
        self.current_snapshots = []
        self._load_task = None  # Track current loading task
        self.interval_minutes = interval_minutes

        # Live update functionality
        self.auto_refresh_enabled = True
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.on_auto_refresh)
        self.refresh_interval_ms = 60000  # 60 seconds

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

        # Event selector for Kalshi (populated dynamically)
        self.event_label = QLabel("Event:")
        self.event_selector = QComboBox()
        self.event_selector.setMinimumWidth(200)
        self.event_selector.currentIndexChanged.connect(self.on_event_changed)
        header_layout.addWidget(self.event_label)
        header_layout.addWidget(self.event_selector)

        # Market selector for Kalshi (populated dynamically)
        self.market_label = QLabel("Market:")
        self.market_selector = QComboBox()
        self.market_selector.setMinimumWidth(150)
        self.market_selector.currentIndexChanged.connect(self.on_market_changed)
        header_layout.addWidget(self.market_label)
        header_layout.addWidget(self.market_selector)

        # Time range selector (for TheOddsAPI only)
        self.time_label = QLabel("Time:")
        header_layout.addWidget(self.time_label)

        self.time_range = QComboBox()
        self.time_range.addItems(["1h", "3h", "6h", "12h", "24h", "7d"])
        self.time_range.setFixedWidth(60)
        self.time_range.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.time_range)
        
        # TODO: only display for kalshi datasource
        self.kalshi_interval = QComboBox()
        self.kalshi_interval.addItems([f"{M}m" for M in (1, 60, 1440)])
        self.kalshi_interval.setFixedWidth(60)
        self.kalshi_interval.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.kalshi_interval)

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
        # Create plot widget with DateAxisItem from the start to avoid scientific notation
        date_axis = pg.DateAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(
            background="#29313D",
            axisItems={'bottom': date_axis}
        )
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
        self.kalshi_interval.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.plot_widget.setEnabled(enabled)
        self.event_selector.setEnabled(enabled)
        self.market_selector.setEnabled(enabled)

    async def load_kalshi_events(self, sport=None):
        """
        Load and populate Kalshi events for all sports or a specific sport.

        Args:
            sport: Sport key ('NFL', 'NBA', 'MLB', 'NHL', etc.) - if None, loads all sports
        """
        # All available Kalshi sports including moneylines, spreads, totals
        # Note: Props are loaded separately when an event is selected (they have different ticker formats)
        all_sports = [
            # NFL
            'NFL', 'KXNFLSPREAD', 'KXNFLTOTAL',
            # NBA
            'NBA', 'KXNBASPREAD', 'KXNBATOTAL',
            # MLB
            'MLB', 'KXMLBSPREAD', 'KXMLBTOTAL', 'MLB_SERIES',
            # NHL
            'NHL', 'KXNHLSPREAD', 'KXNHLTOTAL',
            # College Football
            'NCAAF',
            # Soccer
            'EPL', 'UCL', 'LA_LIGA', 'BUNDESLIGA', 'SERIE_A', 'LIGUE_1', 'MLS',
            # Esports
            'LOL'
        ]

        sports_to_load = [sport] if sport else all_sports

        print(f"Loading Kalshi events for: {', '.join(sports_to_load)}...")

        try:
            loop = asyncio.get_event_loop()

            # Fetch events for all sports concurrently
            fetch_tasks = []
            for sport_key in sports_to_load:
                # Check if this is a direct series ticker (SPREAD/TOTAL) or a game series
                if sport_key.startswith('KX'):
                    # Direct series ticker - use get_events
                    task = loop.run_in_executor(
                        None,
                        lambda s=sport_key: self.kalshi_client.kalshi_client.get_events(
                            series_ticker=s,
                            status='open',
                            with_nested_markets=True,
                            limit=200
                        )
                    )
                else:
                    # Game series - use get_game_events
                    task = loop.run_in_executor(
                        None,
                        lambda s=sport_key: self.kalshi_client.kalshi_client.get_game_events(sport=s)
                    )
                fetch_tasks.append((sport_key, task))

            # Wait for all tasks to complete
            all_events = []
            for sport_key, task in fetch_tasks:
                try:
                    events_data = await task
                    events = events_data.get('events', [])
                    print(f"  {sport_key}: {len(events)} events")
                    all_events.extend(events)
                except Exception as e:
                    print(f"  {sport_key}: Error - {e}")
                    continue

            print(f"Total events loaded: {len(all_events)}")

            # Group events by base game (remove market type suffix like ": Spread", ": Total Points")
            # This prevents duplicate entries for the same game
            unique_events = {}
            for event in all_events:
                event_title = event.get('title', 'Unknown Event')
                event_ticker = event.get('event_ticker')
                series_ticker = event.get('series_ticker')

                # Extract base game title (remove ": Spread", ": Total Points", etc.)
                base_title = event_title.split(':')[0].strip()

                # Use base title as key to group related events
                if base_title not in unique_events:
                    unique_events[base_title] = {
                        'title': base_title,
                        'event_ticker': event_ticker,
                        'series_ticker': series_ticker,
                        'sort_key': event_ticker
                    }

            # Convert to list and sort
            unique_event_list = list(unique_events.values())
            unique_event_list.sort(key=lambda x: x.get('sort_key', ''), reverse=True)

            print(f"Unique events after grouping: {len(unique_event_list)}")

            # Populate event selector
            self.event_selector.blockSignals(True)
            self.event_selector.clear()

            for event in unique_event_list:
                event_title = event['title']
                event_ticker = event['event_ticker']
                series_ticker = event['series_ticker']

                # Extract sport from series ticker for prefix
                # KXNFLSPREAD -> NFL, KXNBATOTAL -> NBA, etc.
                sport_prefix = ''
                if series_ticker:
                    if 'NFL' in series_ticker:
                        sport_prefix = 'NFL'
                    elif 'NBA' in series_ticker:
                        sport_prefix = 'NBA'
                    elif 'MLB' in series_ticker:
                        sport_prefix = 'MLB'
                    elif 'NHL' in series_ticker:
                        sport_prefix = 'NHL'
                    elif 'NCAAF' in series_ticker:
                        sport_prefix = 'NCAAF'
                    else:
                        sport_prefix = series_ticker.replace('KX', '').replace('GAME', '')[:6]

                if sport_prefix:
                    display_title = f"[{sport_prefix}] {event_title}"
                else:
                    display_title = event_title

                # Store event info as user data
                self.event_selector.addItem(display_title, userData=(event_ticker, series_ticker, event_title))

            self.event_selector.blockSignals(False)

            # Auto-select first event if available
            if all_events:
                await self.on_event_changed()

        except Exception as e:
            print(f"Error loading Kalshi events: {e}")
            import traceback
            traceback.print_exc()

    async def load_all_markets_for_event(self, base_event_ticker, base_title, home_team, away_team):
        """
        Load all markets for an event from all related series (moneyline, spread, total).

        Args:
            base_event_ticker: Base event ticker (e.g., 'KXNFLGAME-25NOV03ARIDAL')
            base_title: Base event title (e.g., 'Arizona at Dallas')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Loading all markets for: {base_title}")

        # Extract the base ticker ID (e.g., '25NOV03ARIDAL' from 'KXNFLGAME-25NOV03ARIDAL')
        ticker_parts = base_event_ticker.split('-')
        if len(ticker_parts) >= 2:
            ticker_id = '-'.join(ticker_parts[1:])  # Everything after first dash
        else:
            ticker_id = base_event_ticker

        # Determine sport and construct series tickers to check
        # Include props series where available (currently only NFL has props)
        sport = None
        if 'NFL' in base_event_ticker:
            sport = 'NFL'
            series_to_check = [
                'KXNFLGAME',           # Moneylines
                'KXNFLSPREAD',         # Spreads
                'KXNFLTOTAL',          # Totals
                'KXMVENFLSINGLEGAME'   # Single game props
            ]
        elif 'NBA' in base_event_ticker:
            sport = 'NBA'
            series_to_check = [
                'KXNBAGAME',           # Moneylines
                'KXNBASPREAD',         # Spreads
                'KXNBATOTAL'           # Totals
                # 'KXMVENBASINGLEGAME' would go here when available
            ]
        elif 'MLB' in base_event_ticker:
            sport = 'MLB'
            series_to_check = [
                'KXMLBGAME',           # Moneylines
                'KXMLBSPREAD',         # Spreads
                'KXMLBTOTAL'           # Totals
                # 'KXMVEMLBSINGLEGAME' would go here when available
            ]
        elif 'NHL' in base_event_ticker:
            sport = 'NHL'
            series_to_check = [
                'KXNHLGAME',           # Moneylines
                'KXNHLSPREAD',         # Spreads
                'KXNHLTOTAL'           # Totals
                # 'KXMVENHLSINGLEGAME' would go here when available
            ]
        else:
            # Fallback to single event
            await self.set_kalshi_event(base_event_ticker, base_event_ticker.split('-')[0], home_team, away_team)
            return

        # Fetch markets from all related series
        all_markets = []
        loop = asyncio.get_event_loop()

        for series in series_to_check:
            # Props series have different ticker formats - they need to be fetched differently
            if series.startswith('KXMVE'):
                # For props, we need to fetch all events in the series and filter by team names
                # since they don't follow the same ticker pattern
                try:
                    props_markets = await self._fetch_props_markets_for_teams(series, home_team, away_team)
                    if props_markets:
                        all_markets.extend(props_markets)
                        print(f"  {series}: {len(props_markets)} props markets")
                except Exception as e:
                    print(f"  {series}: No props markets found ({e})")
            else:
                # Standard game/spread/total series - use ticker pattern
                event_ticker = f"{series}-{ticker_id}"
                try:
                    # Fetch event markets
                    markets = await loop.run_in_executor(
                        None,
                        lambda s=series, t=ticker_id: self._fetch_event_markets_sync(s, t)
                    )
                    if markets:
                        all_markets.extend(markets)
                        print(f"  {series}: {len(markets)} markets")
                except Exception as e:
                    print(f"  {series}: No markets found ({e})")

        print(f"Total markets loaded: {len(all_markets)}")

        # Set up the event
        self.data_source = 'kalshi'
        self.client = self.kalshi_client
        self.kalshi_event_ticker = base_event_ticker
        self.kalshi_series_ticker = series_to_check[0]  # Use primary series
        self.home_team = home_team
        self.away_team = away_team

        # Update market info
        self.market_info.setText(f"{away_team} @ {home_team}")

        # Populate market selector
        self.market_selector.blockSignals(True)
        self.market_selector.clear()

        for market in all_markets:
            market_title = market.get('yes_sub_title', market.get('subtitle', market.get('ticker')))
            market_ticker = market.get('ticker')
            self.market_selector.addItem(market_title, userData=market_ticker)

        self.market_selector.blockSignals(False)

        # Select first market and load data
        if all_markets:
            self.kalshi_market_ticker = all_markets[0].get('ticker')
            self.set_enabled(True)

            # Load data for first market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self.load_data())

    def _fetch_event_markets_sync(self, series_ticker, ticker_id):
        """Synchronous helper to fetch event markets"""
        try:
            event_ticker = f"{series_ticker}-{ticker_id}"
            event_data = self.kalshi_client.kalshi_client.get_event(
                event_ticker=event_ticker,
                with_nested_markets=True
            )
            return event_data.get('event', {}).get('markets', [])
        except:
            return []

    async def _fetch_props_markets_for_teams(self, series_ticker, home_team, away_team):
        """
        Fetch props markets for a specific game by matching team names.
        Props markets have different event structures and need to be matched by team names.
        Filters out parlay/multi-leg props (which contain multiple player names).

        Args:
            series_ticker: Props series ticker (e.g., 'KXMVENFLSINGLEGAME')
            home_team: Home team name to match
            away_team: Away team name to match

        Returns:
            List of markets for this game's props (excluding parlays)
        """
        try:
            loop = asyncio.get_event_loop()

            # Fetch all events in the props series
            events_data = await loop.run_in_executor(
                None,
                lambda: self.kalshi_client.kalshi_client.get_events(
                    series_ticker=series_ticker,
                    status='open',
                    with_nested_markets=True,
                    limit=200
                )
            )

            events = events_data.get('events', [])

            # Filter events that match our game by checking if both teams are in the title
            matching_markets = []
            parlay_keywords = ['yes', '+yes', 'and', '&', ',']  # Indicators of multi-leg parlays

            for event in events:
                event_title = event.get('title', '').lower()

                # Check if both team names appear in the event title
                # Handle various formats: "Team1 at Team2", "Team1 vs Team2", etc.
                home_match = home_team.lower() in event_title
                away_match = away_team.lower() in event_title

                if home_match and away_match:
                    # This event is for our game - filter the markets
                    markets = event.get('markets', [])

                    for market in markets:
                        # Get market subtitle/title to check for parlay indicators
                        market_subtitle = market.get('yes_sub_title', market.get('subtitle', '')).lower()
                        market_title = market.get('title', '').lower()

                        # Skip if this looks like a parlay (contains multiple "+yes" or player names separated by "yes")
                        # Count occurrences of "yes" which typically indicates multiple legs
                        yes_count = market_subtitle.count('+yes')

                        # Also check for multiple colons which often separate player props in parlays
                        colon_count = market_subtitle.count(':')

                        # Skip parlays - they have multiple "+yes" or many colons
                        if yes_count > 1 or colon_count > 2:
                            continue

                        # Skip if title explicitly mentions multiple outcomes
                        if any(keyword in market_title for keyword in ['and', ' & ', 'both']):
                            continue

                        matching_markets.append(market)

            print(f"Filtered props: {len(matching_markets)} single props (excluded parlays)")
            return matching_markets

        except Exception as e:
            print(f"Error fetching props markets: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def set_kalshi_event(self, event_ticker, series_ticker, home_team, away_team):
        """
        Set a Kalshi event and load available markets.

        Args:
            event_ticker: Kalshi event ticker (e.g., 'KXNFLGAME-25OCT27WASKC')
            series_ticker: Kalshi series ticker (e.g., 'KXNFLGAME')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Setting Kalshi event: {event_ticker}")
        self.data_source = 'kalshi'
        self.client = self.kalshi_client
        self.kalshi_event_ticker = event_ticker
        self.kalshi_series_ticker = series_ticker
        self.home_team = home_team
        self.away_team = away_team

        # Update market info
        if home_team and away_team:
            self.market_info.setText(f"{away_team} @ {home_team}")
        else:
            self.market_info.setText(f"Event: {event_ticker}")

        # Fetch available markets
        try:
            markets = await self.kalshi_client.get_event_markets(event_ticker)
            self.kalshi_available_markets = markets

            # Populate market selector
            self.market_selector.blockSignals(True)
            self.market_selector.clear()

            for market in markets:
                market_title = market.get('yes_sub_title', market.get('subtitle', market.get('ticker')))
                market_ticker = market.get('ticker')
                self.market_selector.addItem(market_title, userData=market_ticker)

            self.market_selector.blockSignals(False)

            # Select first market and load data
            if markets:
                self.kalshi_market_ticker = markets[0].get('ticker')
                self.set_enabled(True)

                # Load data for first market
                if self._load_task and not self._load_task.done():
                    self._load_task.cancel()
                self._load_task = asyncio.create_task(self.load_data())

        except Exception as e:
            print(f"Error setting Kalshi event: {e}")
            import traceback
            traceback.print_exc()

    def set_market(self, sport_key, event_id, market_key, home_team, away_team):
        """
        Set the market to display using TheOddsAPI.

        Args:
            sport_key: Sport key for TheOddsAPI
            event_id: Event ID for TheOddsAPI
            market_key: Market key (e.g., 'h2h', 'spreads')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Setting TheOddsAPI market: {sport_key}, {event_id}, {market_key}")

        self.data_source = 'theoddsapi'
        self.client = self.theoddsapi_client
        self.sport_key = sport_key
        self.event_id = event_id
        self.market_key = market_key
        self.home_team = home_team
        self.away_team = away_team

        # Show/hide appropriate controls
        self.event_selector.setVisible(False)
        self.event_label.setVisible(False)
        self.market_selector.setVisible(False)
        self.market_label.setVisible(False)
        self.time_range.setVisible(True)
        self.time_label.setVisible(True)

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
    async def on_event_changed(self):
        """Handle event selector change (Kalshi only)"""
        if self.data_source != 'kalshi':
            return

        selected_index = self.event_selector.currentIndex()
        if selected_index < 0:
            return

        event_data = self.event_selector.itemData(selected_index)
        if event_data:
            event_ticker, series_ticker, base_title = event_data

            # Remove sport prefix from display title
            display_title = self.event_selector.currentText()
            if display_title.startswith('['):
                # Remove prefix like "[NFL] "
                event_title = display_title.split('] ', 1)[-1]
            else:
                event_title = display_title

            print(f"Event changed to: {event_title} ({event_ticker})")

            # Parse team names from event title (e.g., "Washington at Kansas City")
            if ' at ' in event_title:
                away_team, home_team = [t.strip() for t in event_title.split(' at ', 1)]
            elif ' vs ' in event_title:
                home_team, away_team = [t.strip() for t in event_title.split(' vs ', 1)]
            else:
                home_team = event_title
                away_team = event_title

            # Load markets for this event from ALL related series (moneyline, spread, total)
            await self.load_all_markets_for_event(event_ticker, base_title, home_team, away_team)

    @qasync.asyncSlot()
    async def on_market_changed(self):
        """Handle market selector change (Kalshi only)"""
        if self.data_source != 'kalshi':
            return

        selected_index = self.market_selector.currentIndex()
        if selected_index < 0:
            return

        market_ticker = self.market_selector.itemData(selected_index)
        if market_ticker:
            self.kalshi_market_ticker = market_ticker
            print(f"Market changed to: {market_ticker}")

            # Reload data for new market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
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

    @qasync.asyncSlot()
    async def on_auto_refresh(self):
        """Handle automatic refresh timer"""
        if not self.auto_refresh_enabled or not self.data_source == 'kalshi':
            return

        if self._load_task and not self._load_task.done():
            return  # Skip if already loading

        print("Auto-refreshing Kalshi market data...")

        try:
            # Reload the data
            self._load_task = asyncio.create_task(self.load_data())
            await self._load_task
        except Exception as e:
            print(f"Auto-refresh error: {e}")

    def start_live_updates(self):
        """Start the auto-refresh timer"""
        if self.data_source == 'kalshi' and self.auto_refresh_enabled:
            self.refresh_timer.start(self.refresh_interval_ms)
            print(f"Live updates enabled (refresh every {self.refresh_interval_ms/1000}s)")

    def stop_live_updates(self):
        """Stop the auto-refresh timer"""
        self.refresh_timer.stop()
        print("Live updates disabled")

    async def load_data(self):
        """Load historical odds data and populate the graph"""
        # Validate required data based on source
        if self.data_source == 'kalshi':
            if not all([self.kalshi_event_ticker, self.kalshi_series_ticker, self.kalshi_market_ticker]):
                print("Missing required Kalshi market info")
                return
        else:  # theoddsapi
            if not all([self.sport_key, self.event_id, self.market_key]):
                print("Missing required TheOddsAPI market info")
                return

        if not self.client:
            print("Client not initialized!")
            return

        # Calculate time range
        end_time = datetime.now()
        start_time = self.calculate_start_time(end_time)
        kalshi_interval_value = int(self.kalshi_interval.currentText().removesuffix('m'))
        # if the interval is set to 1-minute, the start-time needs to be reduced to avoid the 5000-candlestick (API-side) limit
        if ((self.data_source == 'kalshi') and (kalshi_interval_value == 1)):
            start_time = max(start_time, (end_time - timedelta(days=2)))
            # only reduce start_time - 'max', not 'min' - because it's back in time

        self.progress_bar.setValue(10)
        self.refresh_button.setEnabled(False)

        # Make sure we remove the "no data" message if it exists
        try:
            self.plot_widget.removeItem(self.no_data_text)
        except:
            pass  # It's fine if it doesn't exist

        try:
            print(f"Fetching historical data from {start_time} to {end_time}")
            print(f"Data source: {self.data_source}")

            # Set up the session with connection pooling for better performance
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=60)  # 60 second timeout

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                if self.data_source == 'kalshi': # Fetch from Kalshi
                    snapshots = await self.client.get_historical_candlesticks(
                        session,
                        self.kalshi_market_ticker,
                        self.kalshi_series_ticker,
                        start_time,
                        end_time,
                        period_interval=kalshi_interval_value
                    )
                else:
                    # Fetch from TheOddsAPI
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
                # Process UI updates concurrently
                await asyncio.gather(
                    self.update_bookmaker_toggles(snapshots),
                    self.update_plot(snapshots)
                )
                self.progress_bar.setValue(100)

                # Start live updates for Kalshi data
                if self.data_source == 'kalshi':
                    self.start_live_updates()
            else:
                print("No valid historical data available")
                await self._show_no_data_message("No historical data available")

        except asyncio.CancelledError:
            print("Data loading was cancelled")
            # Clean exit for cancelled tasks
            await self._show_no_data_message("Loading cancelled")
        except Exception as e:
            print(f"Error loading historical odds: {str(e)}")
            import traceback
            traceback.print_exc()
            await self._show_no_data_message(f"Error: {str(e)}")
        finally:
            self.progress_bar.setValue(0)
            self.refresh_button.setEnabled(True)
            QTimer.singleShot(1000, lambda: self.progress_bar.setValue(0))

    def calculate_start_time(self, end_time):
        """
        Calculate start time based on selected time range.
        controlled by the 'Time' dropdown top-right of the graph
        """
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
        elif range_text == "7d":
            return end_time - timedelta(days=7)
        else:
            return end_time - timedelta(hours=6)  # Default

    def create_bookmaker_toggle(self, bookmaker_name):
        """Create a toggle handler for a specific bookmaker"""
        # Convert this to use qasync.asyncSlot
        @qasync.asyncSlot(int)
        async def toggle_handler(state):
            await self.on_bookmaker_toggled(bookmaker_name, state)
        return toggle_handler

    async def update_bookmaker_toggles(self, snapshots):
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

    @qasync.asyncSlot(int)
    async def toggle_all_bookmakers(self, state):
        """Toggle all bookmaker checkboxes to the given state"""
        checked = state == Qt.CheckState.Checked
        # Skip the first checkbox (which is "All") and last item (which is stretch)
        for i in range(1, self.bookmaker_layout.count() - 1):
            item = self.bookmaker_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QCheckBox):
                item.widget().setChecked(checked)

    async def on_bookmaker_toggled(self, bookmaker, state):
        """Handle bookmaker toggle checkbox changes"""
        print(f"Toggling bookmaker: {bookmaker} to {state}")
        self.bookmaker_visible[bookmaker] = (state == Qt.CheckState.Checked)
        # Refresh the plot with current visibility settings
        await self.update_plot(self.current_snapshots)

    @qasync.asyncSlot()
    async def on_time_range_changed(self):
        """Handle time range dropdown changes"""
        range_text = self.time_range.currentText()
        print(f"Time range changed to: {range_text}")

        # Only update min_interval for TheOddsAPI client
        if self.data_source == 'theoddsapi' and hasattr(self.theoddsapi_client, 'min_interval'):
            # Extract numeric value from range text
            if 'h' in range_text:
                current_time_range = int(range_text.removesuffix('h'))
                self.theoddsapi_client.min_interval = timedelta(minutes=(current_time_range * 10))
                print(f"TheOddsAPI interval: {self.theoddsapi_client.min_interval}")

        # Reload data with new time range
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self.load_data())

    async def update_plot(self, snapshots):
        """Enhanced plotting with point change visualization"""
        self.plot_widget.clear()
        self.plot_widget.addItem(pg.GridItem())
        if not snapshots:
            await self._show_no_data_message()
            return

        colors = [
            (31, 119, 180), (255, 127, 14), (44, 160, 44),
            (214, 39, 40), (148, 103, 189), (140, 86, 75)
        ]

        # Some Legend options for graph display
        #self.plot_widget.addLegend(offset=(10, 10), labelTextSize='8pt')

        # Group data by bookmaker and outcome
        plot_data = await self._organize_plot_data(snapshots)

        # Plot each series with proper point change handling
        for bm_idx, (bookmaker, outcomes) in enumerate(plot_data.items()):
            if not self.bookmaker_visible.get(bookmaker, True):
                continue

            color = colors[bm_idx % len(colors)]
            for outcome_key, points_data in outcomes.items():
                await self._plot_outcome_series(bookmaker, outcome_key, points_data, color)

        await self.configure_plot_axes(snapshots)

    async def _organize_plot_data(self, snapshots):
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

    async def _plot_outcome_series(self, bookmaker, outcome_key, points_data, color):
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

            # Add labels only when odds change from previous value
            prev_american_value = None
            for i, ts in enumerate(timestamps):
                if i < len(american_values):
                    current_value = american_values[i]
                    american = american_prices[i]

                    # Only show label if value changed from previous point
                    if prev_american_value is None or current_value != prev_american_value:
                        # Format label based on whether we have point data
                        if ('points' in points_data and
                            points_data['points'] and
                            i < len(points_data['points']) and
                            points_data['points'][i] is not None):
                            pt = points_data['points'][i]
                            label_text = f"{self._decimal_to_american(float(american))} ({pt:.1f})"
                        else:
                            label_text = f"{american}"

                        # Use black color for Kalshi labels, bookmaker color for others
                        label_color = (0, 0, 0) if bookmaker == 'kalshi' else color
                        label = pg.TextItem(label_text, anchor=(0.5, 1.5), color=label_color)
                        self.plot_widget.addItem(label)
                        label.setPos(ts, current_value)

                    prev_american_value = current_value

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

    async def configure_plot_axes(self, snapshots):
        """Configure plot axes optimized for American odds display"""
        if not snapshots:
            return

        # Collect all actual timestamps from the data to find min/max
        all_timestamps = []
        for snapshot in snapshots:
            try:
                ts = datetime.fromisoformat(snapshot['timestamp'].replace('Z', '')).timestamp()
                all_timestamps.append(ts)
            except:
                pass

        if not all_timestamps:
            return

        # Use actual data range for X-axis
        first_time = min(all_timestamps)
        last_time = max(all_timestamps)

        # Add small padding (2%) for visual clarity
        time_range = last_time - first_time
        if time_range > 0:
            padding = time_range * 0.02
        else:
            # If all timestamps are the same, add 1 hour padding on each side
            padding = 3600

        self.plot_widget.setXRange(first_time - padding, last_time + padding)

        # DateAxisItem is already set during initialization, just update the range
        # No need to re-set it here

        # Collect all American prices to determine Y-axis range
        all_american_prices = []

        for snapshot in snapshots:
            for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
                for market in bookmaker.get('markets', []):
                    # For Kalshi, market key is 'h2h', for TheOddsAPI it varies
                    # Just collect all prices regardless of market key match
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
                                elif isinstance(american_price, (int, float)):
                                    all_american_prices.append(float(american_price))
                            except (ValueError, TypeError):
                                pass  # Skip invalid values

        # Set Y-axis range for American odds with proper padding
        # American odds must never be in the range (-100, +100) as this is invalid
        if all_american_prices:
            min_price = min(all_american_prices)
            max_price = max(all_american_prices)

            # Apply padding carefully to avoid crossing into invalid range
            if min_price >= 100:
                # All underdogs (positive odds)
                min_val = max(100, min_price * 0.95)
                max_val = max_price * 1.05
            elif max_price <= -100:
                # All favorites (negative odds)
                min_val = min_price * 1.05  # Make more negative
                max_val = min(-100, max_price * 0.95)  # Less negative but not above -100
            else:
                # Mixed: both favorites and underdogs
                # Handle each side separately
                min_val = min_price * 1.05  # Make more negative
                max_val = max_price * 1.05  # Make more positive

                # Ensure we don't cross into invalid range
                if min_val > -100:
                    min_val = -100
                if max_val < 100:
                    max_val = 100

            # Ensure we don't have identical min/max which would break the axis
            if abs(min_val - max_val) < 10:
                if min_val >= 100:
                    min_val = 100
                    max_val = min_val + 50
                elif max_val <= -100:
                    max_val = -100
                    min_val = max_val - 50
                else:
                    min_val = -200
                    max_val = 200

            self.plot_widget.setYRange(min_val, max_val)

            # Set up Y-axis label and ticks
            y_axis = self.plot_widget.getAxis('left')
            y_axis.setLabel('American Odds')

            # Create appropriate Y-axis ticks for American odds
            # Must avoid the invalid range between -100 and +100
            y_ticks = []

            # Determine if we're crossing the ±100 boundary
            crosses_boundary = min_val < -100 and max_val > 100

            if crosses_boundary:
                # We have both favorites and underdogs - create ticks on both sides
                # Ticks for favorites (negative side)
                neg_range = abs(min_val) - 100
                neg_step = neg_range / 3  # 3 ticks on negative side
                for i in range(4):
                    tick_val = min_val + (i * neg_step)
                    if tick_val <= -100:
                        y_ticks.append((tick_val, f"{int(tick_val)}"))

                # Add boundary ticks at ±100
                y_ticks.append((-100, "-100"))
                y_ticks.append((100, "+100"))

                # Ticks for underdogs (positive side)
                pos_range = max_val - 100
                pos_step = pos_range / 3  # 3 ticks on positive side
                for i in range(1, 4):
                    tick_val = 100 + (i * pos_step)
                    if tick_val >= 100:
                        y_ticks.append((tick_val, f"+{int(tick_val)}"))
            else:
                # All on one side - create evenly spaced ticks
                num_ticks = 5
                step = (max_val - min_val) / num_ticks
                current = min_val

                for i in range(num_ticks + 1):
                    # Ensure tick is in valid American odds range
                    if current >= 100:
                        y_ticks.append((current, f"+{int(current)}"))
                    elif current <= -100:
                        y_ticks.append((current, f"{int(current)}"))
                    # Skip any ticks in invalid range (-100, +100)
                    current += step

            y_axis.setTicks([y_ticks])

    async def _show_no_data_message(self, message="No historical data available"):
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
