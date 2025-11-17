"""
Kalshi API Client
-----------------
Client for interacting with Kalshi's prediction markets API.
Supports fetching events, markets, and historical candlestick data.
"""

import requests
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from Creds import Kalshi_Key

# Valid historical time intervals for kalshi market data: Valid values: 1 (1 minute), 60 (1 hour), 1440 (1 day).
# Time period length of each candlestick in minutes.
# When integrating Client TO BE USED WITH histroical odds widget, be cognizant of time intervals between OddsAPI and Kalshi
# Some markets such as totals or spreads have sub markets  (Alt lines)

class KalshiClient:
    """Client for Kalshi API interactions."""

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    # Sports-related series prefixes
    SPORTS_SERIES = ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER', 'FOOTBALL', 'BASKETBALL',
                     'BASEBALL', 'HOCKEY', 'TENNIS', 'GOLF', 'UFC', 'MMA', 'MVE']

    # Game-level series tickers (for fetching individual game markets)
    GAME_SERIES = {
        # Basketball
        'NBA': [
            'KXNBAGAME',
            'KXNBASPREAD',
            'KXNBATOTAL'
        ],
    
        # Football
        'NFL': [
            'KXNFLGAME',
            'KXNFLSPREAD',
            'KXNFLTOTAL'
        ],
        # Uncomment if needed:
        # 'NFL_SINGLE_PROPS': ['KXMVENFLSINGLEGAME'],
        # 'NFL_MULTI_PROPS': ['KXMVENFLMULTIGAMEEXTENDED'],
        'NCAAF': ['KXNCAAFGAME'],
    
        # Baseball
        'MLB': [
            'KXMLBGAME',
            'KXMLBSPREAD',
            'KXMLBTOTAL'
        ],
        'MLB_SERIES': ['KXMLBSERIESGAMETOTAL'],
    
        # Hockey
        'NHL': [
            'KXNHLGAME',
            'KXNHLSPREAD',
            'KXNHLTOTAL'
        ],
    
        # Soccer
        'EPL': ['KXEPLGAME'],
        'UCL': ['KXUCLGAME'],
        'LA_LIGA': ['KXLALIGAGAME'],
        'BUNDESLIGA': ['KXBUNDESLIGAGAME'],
        'SERIE_A': ['KXSERIEAGAME'],
        'LIGUE_1': ['KXLIGUE1GAME'],
        'MLS': ['KXMLSGAME'],
    
        # Esports
        'LOL': ['KXLOLGAMES'],
    }


    # Human-readable descriptions for each series
    SERIES_DESCRIPTIONS = {
        'NBA': 'NBA Basketball',
        'NFL': 'NFL Football',
        'NFL_SINGLE_PROPS': 'NFL Single Game Props',
        'NFL_MULTI_PROPS': 'NFL Multi-Game Props',
        'NCAAF': 'NCAA College Football',
        'MLB': 'MLB Baseball',
        'MLB_SERIES': 'MLB Series Totals',
        'NHL': 'NHL Hockey',
        'EPL': 'English Premier League',
        'UCL': 'UEFA Champions League',
        'LA_LIGA': 'La Liga (Spain)',
        'BUNDESLIGA': 'Bundesliga (Germany)',
        'SERIE_A': 'Serie A (Italy)',
        'LIGUE_1': 'Ligue 1 (France)',
        'MLS': 'Major League Soccer',
        'LOL': 'League of Legends Esports',
    }

    def __init__(self, api_key: str = None):
        """
        Initialize Kalshi client.

        Args:
            api_key: Kalshi API key (defaults to Creds.Kalshi_Key)
        """
        self.api_key = api_key or Kalshi_Key
        self.session = requests.Session()

    def _make_request(self, method: str, endpoint: str, params: Dict = None, data: Dict = None) -> Dict:
        """
        Make authenticated request to Kalshi API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            params: Query parameters
            data: Request body data

        Returns:
            JSON response as dictionary
        """
        url = f"{self.BASE_URL}{endpoint}"

        headers = {
            "Content-Type": "application/json",
        }

        # Note: For now making unauthenticated requests to public endpoints
        # Full authentication requires RSA signing with private key

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=data
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error making request to {url}: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            raise

    def get_events(
        self,
        limit: int = 100,
        status: Optional[str] = None,
        series_ticker: Optional[str] = None,
        with_nested_markets: bool = False,
        cursor: Optional[str] = None,
        min_close_ts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get list of events from Kalshi.

        Args:
            limit: Number of results per page (1-200, default 100)
            status: Filter by status ('open', 'closed', 'settled')
            series_ticker: Filter by series ticker
            with_nested_markets: Include markets nested within events
            cursor: Pagination cursor from previous response
            min_close_ts: Filter events with at least one market with close timestamp
                         greater than this Unix timestamp (in seconds)

        Returns:
            Dictionary with 'events', 'cursor', and optionally 'milestones'
        """
        params = {
            "limit": limit,
            "with_nested_markets": with_nested_markets
        }

        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts

        return self._make_request("GET", "/events", params=params)

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> Dict[str, Any]:
        """
        Get details for a specific event.

        Args:
            event_ticker: Event ticker to fetch
            with_nested_markets: Include markets within event object

        Returns:
            Dictionary with 'event' and 'markets' data
        """
        params = {"with_nested_markets": with_nested_markets}
        return self._make_request("GET", f"/events/{event_ticker}", params=params)

    def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of markets from Kalshi.

        Args:
            limit: Number of results per page (1-200)
            cursor: Pagination cursor
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: Filter by market status

        Returns:
            Dictionary with 'markets' and 'cursor'
        """
        params = {"limit": limit}

        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status

        return self._make_request("GET", "/markets", params=params)

    # TODO: 'start_ts' and 'end_ts' are actually mandatory, should not have defaults
    def get_market_candlesticks(
        self,
        ticker: str,
        series_ticker: str = None,
        period_interval: int = 1440,
        start_ts: int = None,
        end_ts: int = None
    ) -> Dict[str, Any]:
        """
        Get historical candlestick data for a market.

        Args:
            ticker: Market ticker
            series_ticker: Series ticker (if None, extracted from market ticker)
            period_interval: Candlestick duration (1=minute, 60=hour, 1440=day)
            start_ts: Unix timestamp for start (not actually optional)
            end_ts: Unix timestamp for end (not actually optional)

        Returns:
            Dictionary with 'ticker' and 'candlesticks' array
        """
        # Extract series ticker from market ticker if not provided
        # Format is typically SERIES-MARKET or similar
        if not series_ticker:
            # Attempt to extract from ticker
            # This is a guess - may need adjustment based on actual ticker format
            parts = ticker.split('-')
            if len(parts) > 1:
                series_ticker = parts[0]
            else:
                raise ValueError(f"Cannot determine series_ticker from {ticker}. Please provide explicitly.")
        
        assert((start_ts is not None) and (end_ts is not None)), "missing one or more timestamp parameters"
        
        params = {
            "period_interval": period_interval,
            "start_ts": start_ts,
            "end_ts": end_ts
        }

        endpoint = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        return self._make_request("GET", endpoint, params=params)

    def print_events_summary(self, events_data: Dict[str, Any], max_events: int = 10):
        """
        Pretty print events summary.

        Args:
            events_data: Response from get_events()
            max_events: Maximum number of events to display
        """
        events = events_data.get('events', [])
        print(f"\n{'='*80}")
        print(f"KALSHI EVENTS SUMMARY ({len(events)} events)")
        print(f"{'='*80}\n")

        for i, event in enumerate(events[:max_events]):
            print(f"{i+1}. {event['title']}")
            print(f"   Ticker: {event['event_ticker']}")
            print(f"   Series: {event['series_ticker']}")
            print(f"   Category: {event.get('category', 'N/A')}")
            print(f"   Mutually Exclusive: {event.get('mutually_exclusive', 'N/A')}")

            # Show nested markets if available
            if 'markets' in event and event['markets']:
                print(f"   Markets ({len(event['markets'])}):")
                for market in event['markets'][:3]:  # Show first 3 markets
                    print(f"      - {market.get('yes_sub_title', market.get('ticker'))}")
                    print(f"        Status: {market.get('status')} | "
                          f"Last: ${market.get('last_price_dollars', 'N/A')} | "
                          f"Volume: {market.get('volume', 0)}")
                if len(event['markets']) > 3:
                    print(f"      ... and {len(event['markets']) - 3} more markets")
            print()

        if len(events) > max_events:
            print(f"... and {len(events) - max_events} more events")

        # Show cursor for pagination
        cursor = events_data.get('cursor')
        if cursor:
            print(f"\nPagination cursor available: {cursor[:50]}...")

    def print_candlesticks_summary(self, candlesticks_data: Dict[str, Any], max_candles: int = 50):
        """
        Pretty print candlestick data.

        Args:
            candlesticks_data: Response from get_market_candlesticks()
            max_candles: Maximum number of candlesticks to display
        """
        ticker = candlesticks_data.get('ticker', 'Unknown')
        candles = candlesticks_data.get('candlesticks', [])

        print(f"\n{'='*80}")
        print(f"CANDLESTICK DATA: {ticker} ({len(candles)} periods)")
        print(f"{'='*80}\n")

        if not candles:
            print("No candlestick data available.")
            return

        # Print header
        print(f"{'Date/Time':<20} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>10}")
        print(f"{'-'*20} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")

        # Print candles (most recent first)
        for candle in reversed(candles[-max_candles:]):
            ts = candle.get('end_period_ts', 0)
            dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')

            price = candle.get('price', {})
            open_price = price.get('open') or 0
            high_price = price.get('high') or 0
            low_price = price.get('low') or 0
            close_price = price.get('close') or 0
            volume = candle.get('volume') or 0

            # Handle None values gracefully
            try:
                print(f"{dt:<20} {open_price:>8.2f} {high_price:>8.2f} {low_price:>8.2f} "
                      f"{close_price:>8.2f} {volume:>10}")
            except (TypeError, ValueError):
                # If values can't be formatted, show raw
                print(f"{dt:<20} {str(open_price):>8} {str(high_price):>8} {str(low_price):>8} "
                      f"{str(close_price):>8} {str(volume):>10}")

        if len(candles) > max_candles:
            print(f"\n... and {len(candles) - max_candles} more periods")

    def get_sports_events(self, limit: int = 200, with_markets: bool = True) -> Dict[str, Any]:
        """
        Get all sports-related events.

        Args:
            limit: Number of events to fetch per request
            with_markets: Include nested market data

        Returns:
            Dictionary with sports events
        """
        all_sports_events = []
        cursor = None

        # Fetch events in batches
        while True:
            response = self.get_events(
                limit=limit,
                status='open',
                with_nested_markets=with_markets,
                cursor=cursor
            )

            events = response.get('events', [])

            # Filter for sports events
            for event in events:
                # Check category
                category = event.get('category', '')
                if category == 'Sports':
                    all_sports_events.append(event)
                    continue

                # Check event ticker for sports keywords
                ticker = event.get('event_ticker', '')
                if any(sport in ticker.upper() for sport in self.SPORTS_SERIES):
                    all_sports_events.append(event)

            # Check for more pages
            cursor = response.get('cursor')
            if not cursor or len(events) < limit:
                break

        return {'events': all_sports_events, 'count': len(all_sports_events)}

    def get_sports_markets(self, limit: int = 200) -> Dict[str, Any]:
        """
        Get all sports-related markets.

        Args:
            limit: Number of markets to fetch per request

        Returns:
            Dictionary with sports markets organized by sport
        """
        all_sports_markets = []
        cursor = None

        # Fetch markets in batches
        while True:
            response = self.get_markets(limit=limit, cursor=cursor)
            markets = response.get('markets', [])

            # Filter for sports markets
            for market in markets:
                ticker = market.get('ticker', '')
                if any(sport in ticker.upper() for sport in self.SPORTS_SERIES):
                    all_sports_markets.append(market)

            # Check for more pages
            cursor = response.get('cursor')
            if not cursor or len(markets) < limit:
                break

        # Organize by sport type
        organized = self._organize_sports_markets(all_sports_markets)
        organized['all_markets'] = all_sports_markets
        organized['total_count'] = len(all_sports_markets)

        return organized

    def _organize_sports_markets(self, markets: List[Dict]) -> Dict[str, Any]:
        """
        Organize sports markets by sport type.

        Args:
            markets: List of market dictionaries

        Returns:
            Dictionary organized by sport
        """
        organized = {
            'NFL': [],
            'NBA': [],
            'MLB': [],
            'NHL': [],
            'SOCCER': [],
            'OTHER': []
        }

        for market in markets:
            ticker = market.get('ticker', '').upper()

            categorized = False
            for sport in ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER']:
                if sport in ticker:
                    organized[sport].append(market)
                    categorized = True
                    break

            if not categorized:
                organized['OTHER'].append(market)

        return organized

    def print_sports_summary(self, sports_data: Dict[str, Any], max_per_sport: int = 5):
        """
        Pretty print sports markets summary.

        Args:
            sports_data: Response from get_sports_markets()
            max_per_sport: Maximum markets to show per sport
        """
        print(f"\n{'='*80}")
        print(f"KALSHI SPORTS MARKETS SUMMARY")
        print(f"Total Sports Markets: {sports_data.get('total_count', 0)}")
        print(f"{'='*80}\n")

        for sport in ['NFL', 'NBA', 'MLB', 'NHL', 'SOCCER', 'OTHER']:
            markets = sports_data.get(sport, [])
            if not markets:
                continue

            print(f"\n{sport} MARKETS ({len(markets)} total)")
            print(f"{'-'*80}")

            for i, market in enumerate(markets[:max_per_sport], 1):
                ticker = market.get('ticker', 'N/A')
                title = market.get('yes_sub_title', 'N/A')
                status = market.get('status', 'N/A')
                last_price = market.get('last_price_dollars', 'N/A')
                volume = market.get('volume', 0)

                print(f"{i}. {title[:100]}")
                print(f"   Ticker: {ticker}")
                print(f"   Status: {status} | Price: ${last_price} | Volume: {volume}")
                print()

            if len(markets) > max_per_sport:
                print(f"   ... and {len(markets) - max_per_sport} more {sport} markets\n")

    def get_game_markets(self, sport: str = 'NBA') -> List[Dict]:
      """
      Get all individual game markets for a specific sport.
      Supports multiple series tickers per sport.
      """
  
      series_tickers = self.GAME_SERIES.get(sport)
      if not series_tickers:
          raise ValueError(f"Unknown sport '{sport}'. Available: {list(self.GAME_SERIES.keys())}")
  
      all_game_markets = []
  
      # Loop through all series tickers belonging to this sport
      for series in series_tickers:
          cursor = None
  
          while True:
              response = self.get_events(
                  series_ticker=series,
                  status='open',
                  with_nested_markets=True,
                  limit=200,
                  cursor=cursor
              )
  
              events = response.get('events', [])
  
              # Extract markets from each event
              for event in events:
                  for market in event.get('markets', []):
                      market['event_title'] = event.get('title')
                      market['event_ticker'] = event.get('event_ticker')
                      all_game_markets.append(market)
  
              cursor = response.get('cursor')
              if not cursor or len(events) < 200:
                  break
  
      return all_game_markets


    @classmethod
    def list_available_sports(cls) -> None:
        """
        Print all available sports that can be queried for game markets.
        """
        print("="*80)
        print("AVAILABLE SPORTS FOR GAME MARKETS")
        print("="*80)
        print("\nUsage: client.get_game_events(sport='SPORT_KEY')")
        print("\nAvailable Sport Keys:\n")

        # Group by category
        categories = {
            'Basketball': ['NBA'],
            'Football': ['NFL', 'NFL_SINGLE_PROPS', 'NFL_MULTI_PROPS', 'NCAAF'],
            'Baseball': ['MLB', 'MLB_SERIES'],
            'Hockey': ['NHL'],
            'Soccer': ['EPL', 'UCL', 'LA_LIGA', 'BUNDESLIGA', 'SERIE_A', 'LIGUE_1', 'MLS'],
            'Esports': ['LOL'],
        }

        for category, sports in categories.items():
            print(f"{category}:")
            for sport_key in sports:
                description = cls.SERIES_DESCRIPTIONS.get(sport_key, sport_key)
                print(f"  '{sport_key}' - {description}")
            print()

        print("="*80)

    def get_game_events(self, sport: str = 'NBA', min_close_ts: Optional[int] = None) -> Dict[str, Any]:
      """
      Get all game events for a specific sport with nested markets.
      Supports multiple series tickers per sport.
      """
  
      series_tickers = self.GAME_SERIES.get(sport)
      if not series_tickers:
          raise ValueError(f"Unknown sport '{sport}'. Available: {list(self.GAME_SERIES.keys())}")
  
      all_events = []
  
      # Loop through every series ticker for this sport
      for series in series_tickers:
          cursor = None
  
          while True:
              response = self.get_events(
                  series_ticker=series,
                  status='open',
                  with_nested_markets=True,
                  limit=200,
                  cursor=cursor,
                  min_close_ts=min_close_ts
              )
  
              events = response.get('events', [])
              all_events.extend(events)
  
              cursor = response.get('cursor')
              if not cursor or len(events) < 200:
                  break
  
      return {'events': all_events, 'count': len(all_events)}



def main():
    """Example usage of KalshiClient."""
    client = KalshiClient()

    print("=" * 80)
    print("KALSHI API CLIENT - DEMO")
    print("=" * 80)

    # 1. Fetch open events with nested markets
    print("\n[1] Fetching open events with markets...")
    try:
        events_response = client.get_events(
            limit=10,
            status="open",
            with_nested_markets=True
        )
        client.print_events_summary(events_response, max_events=5)
    except Exception as e:
        print(f"Error fetching events: {e}")

    # 2. Get a specific event (example - you'll need to replace with actual ticker)
    print("\n[2] Fetching specific event details...")
    try:
        # Note: You need to replace this with an actual event ticker
        # event_data = client.get_event("EXAMPLE-EVENT-23")
        # print(f"Event: {event_data['event']['title']}")
        print("(Skipped - need actual event ticker)")
    except Exception as e:
        print(f"Error: {e}")

    # 3. Fetch markets (without status filter as it may not be valid)
    print("\n[3] Fetching markets...")
    try:
        markets_response = client.get_markets(limit=5)
        markets = markets_response.get('markets', [])
        print(f"Found {len(markets)} markets")
        for market in markets[:3]:
            print(f"  - {market.get('ticker')}: {market.get('yes_sub_title', 'N/A')}")
            print(f"    Status: {market.get('status')}, Last: ${market.get('last_price_dollars', 'N/A')}")
    except Exception as e:
        print(f"Error fetching markets: {e}")

    # 4. Get historical candlestick data for Mars market
    print("\n[4] Fetching candlestick data for Elon Musk Mars market...")
    try:
        # Using the Mars market we found above
        candlesticks = client.get_market_candlesticks(
            ticker="KXELONMARS-99",
            series_ticker="KXELONMARS",
            period_interval=1440,  # Daily
            days_back=30
        )
        client.print_candlesticks_summary(candlesticks, max_candles=15)
    except Exception as e:
        print(f"Error: {e}")

    print("\n" + "=" * 80)
    print("Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
