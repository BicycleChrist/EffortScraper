"""
Polymarket Sports Client
A comprehensive client for fetching sports markets data from Polymarket

Usage:
    from polymarket_sports_client import PolymarketSportsClient

    client = PolymarketSportsClient()

    # Get all NFL games with full market data
    nfl_data = client.get_sport_markets("NFL")

    # Get specific game
    bears_vikings = client.get_game_by_teams("NFL", "Bears", "Vikings")

    # Get all major sports at once
    all_sports = client.get_all_sports_markets()
"""

import requests
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
import time


@dataclass
class Order:
    """Represents a single orderbook order"""
    price: float
    size: float
    timestamp: Optional[str] = None


@dataclass
class Trade:
    """Represents a single trade"""
    timestamp: int
    side: str
    price: float
    size: float
    wallet: str
    transaction_hash: Optional[str] = None


@dataclass
class Market:
    """Represents a single market within a game"""
    id: str
    question: str
    slug: str
    outcomes: List[str]
    outcome_prices: List[float]
    volume: float
    volume_24hr: float
    liquidity: float
    clob_token_ids: List[str]
    active: bool
    closed: bool
    end_date: str

    # Orderbook data
    bids: List[Order]
    asks: List[Order]

    # Trade data
    recent_trades: List[Trade]
    largest_trades: List[Trade]


@dataclass
class Game:
    """Represents a single game/event"""
    id: str
    title: str
    slug: str
    description: str
    sport: str
    start_time: str
    end_time: str
    volume: float
    volume_24hr: float
    liquidity: float
    active: bool
    closed: bool

    # Nested markets
    markets: List[Market]


class PolymarketSportsClient:
    """Client for fetching Polymarket sports data"""

    # API endpoints
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"

    # Sports series IDs
    SPORTS_SERIES = {
        "NFL": 10187,
        "NBA": 10345,
        "NHL": 10346,
        "MLB": 3,
        "CFB": 10210,
        "NCAAB": 39
    }

    def __init__(self, rate_limit_delay: float = 0.01, max_workers: int = 20):
        """
        Initialize the client (optimized for maximum speed)

        Default settings: 20 workers, 0.01s delay (~30-50 markets/sec)
        Includes automatic retry on rate limit errors
        """
        self.rate_limit_delay = rate_limit_delay
        self.max_workers = max_workers
        self.session = requests.Session()

        # Rate limits (based on Polymarket docs):
        # - Gamma /events: 10 req/s
        # - Gamma /markets: 12.5 req/s
        # - CLOB /book: 20 req/s
        # - Data API /trades: 15 req/s
        # Using max_workers=10 with 0.05s delay = ~10 req/s average

    def _rate_limit(self):
        """Simple rate limiting"""
        time.sleep(self.rate_limit_delay)

    def _parse_json_field(self, value: Any) -> Any:
        """Parse JSON string fields"""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except:
                return value
        return value

    def get_sport_markets(self, sport: str, limit: int = 50,
                         include_orderbook: bool = True,
                         include_trades: bool = True,
                         max_trades: int = 100) -> List[Game]:
        """
        Get all active games for a specific sport with full market data

        Args:
            sport: Sport name (NFL, NBA, NHL, MLB, CFB, NCAAB)
            limit: Maximum number of games to fetch
            include_orderbook: Whether to fetch orderbook data
            include_trades: Whether to fetch trade history
            max_trades: Maximum number of trades to fetch per market

        Returns:
            List of Game objects with nested Market data
        """
        series_id = self.SPORTS_SERIES.get(sport)
        if not series_id:
            raise ValueError(f"Unknown sport: {sport}. Available: {list(self.SPORTS_SERIES.keys())}")

        # Fetch events
        url = f"{self.GAMMA_API}/events"
        params = {
            "series_id": series_id,
            "closed": False,
            "limit": limit
        }

        print(f"📡 Fetching {sport} games (Series ID: {series_id})...")
        response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        events = response.json()

        print(f"✅ Found {len(events)} {sport} games")

        # Process each game
        games = []
        for idx, event in enumerate(events, 1):
            print(f"📊 Processing game {idx}/{len(events)}: {event['title']}")
            game = self._process_game(event, sport, include_orderbook, include_trades, max_trades)
            games.append(game)
            self._rate_limit()

        return games

    def _process_game(self, event: Dict, sport: str,
                     include_orderbook: bool,
                     include_trades: bool,
                     max_trades: int) -> Game:
        """Process a single game event with parallel market processing"""
        import concurrent.futures

        # Extract markets from event
        markets_data = event.get('markets', [])
        markets = []

        print(f"   Processing {len(markets_data)} markets in parallel...")

        # Process markets in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all market processing tasks
            future_to_market = {
                executor.submit(
                    self._process_market,
                    market_data,
                    include_orderbook,
                    include_trades,
                    max_trades
                ): market_data
                for market_data in markets_data
            }

            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_market):
                try:
                    market = future.result()
                    markets.append(market)
                except Exception as e:
                    market_data = future_to_market[future]
                    print(f"   ⚠️  Error processing market {market_data.get('question', 'Unknown')[:30]}: {e}")

        print(f"   ✅ Processed {len(markets)}/{len(markets_data)} markets")

        # Create Game object
        game = Game(
            id=str(event.get('id', '')),
            title=event.get('title', ''),
            slug=event.get('slug', ''),
            description=event.get('description', ''),
            sport=sport,
            start_time=event.get('startTime', ''),  # Actual game start time (UTC)
            end_time=event.get('endDate', ''),
            volume=float(event.get('volume', 0)),
            volume_24hr=float(event.get('volume24hr', 0)),
            liquidity=float(event.get('liquidity', 0)),
            active=event.get('active', False),
            closed=event.get('closed', False),
            markets=markets
        )

        return game

    def _process_market(self, market_data: Dict,
                       include_orderbook: bool,
                       include_trades: bool,
                       max_trades: int) -> Market:
        """Process a single market"""

        # Parse JSON fields
        outcomes = self._parse_json_field(market_data.get('outcomes', '[]'))
        outcome_prices = self._parse_json_field(market_data.get('outcomePrices', '[]'))
        outcome_prices = [float(p) for p in outcome_prices] if outcome_prices else []
        clob_token_ids = self._parse_json_field(market_data.get('clobTokenIds', '[]'))

        # Initialize orderbook and trades
        bids = []
        asks = []
        recent_trades = []
        largest_trades = []

        # Fetch orderbook if requested
        if include_orderbook and clob_token_ids:
            token_id = clob_token_ids[0]
            bids, asks = self._fetch_orderbook(token_id)

        # Fetch trades if requested
        if include_trades and clob_token_ids:
            token_id = clob_token_ids[0]
            recent_trades, largest_trades = self._fetch_trades(token_id, max_trades)

        market = Market(
            id=str(market_data.get('id', '')),
            question=market_data.get('question', ''),
            slug=market_data.get('slug', ''),
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            volume=float(market_data.get('volumeNum', market_data.get('volume', 0))),
            volume_24hr=float(market_data.get('volume24hr', 0)),
            liquidity=float(market_data.get('liquidityNum', market_data.get('liquidity', 0))),
            clob_token_ids=clob_token_ids,
            active=market_data.get('active', False),
            closed=market_data.get('closed', False),
            end_date=market_data.get('endDate', ''),
            bids=bids,
            asks=asks,
            recent_trades=recent_trades,
            largest_trades=largest_trades
        )

        return market

    def _fetch_orderbook(self, token_id: str) -> tuple[List[Order], List[Order]]:
        """
        Fetch orderbook for a token with retry on rate limit

        Returns:
            Tuple of (bids, asks)
        """
        import time as time_module

        max_retries = 2
        for attempt in range(max_retries):
            try:
                url = f"{self.CLOB_API}/book"
                response = self.session.get(url, params={"token_id": token_id}, timeout=10)

                # Handle rate limiting with retry
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (attempt + 1)  # 0.5s, 1.0s
                        time_module.sleep(wait_time)
                        continue
                    else:
                        return [], []  # Give up after retries

                response.raise_for_status()
                orderbook = response.json()

                bids = [Order(price=float(b.get('price', 0)),
                             size=float(b.get('size', 0)))
                       for b in orderbook.get('bids', [])]

                asks = [Order(price=float(a.get('price', 0)),
                             size=float(a.get('size', 0)))
                       for a in orderbook.get('asks', [])]

                return bids, asks

            except Exception as e:
                if attempt < max_retries - 1 and "429" in str(e):
                    time_module.sleep(0.5)
                    continue
                else:
                    print(f"      ⚠️  Error fetching orderbook: {e}")
                    return [], []

    def get_historical_prices(self, token_id: str, start_ts: int, end_ts: int, fidelity: int = 60) -> List[Dict]:
        """
        Fetch historical price data for a token from CLOB API.

        Args:
            token_id: CLOB token ID
            start_ts: Start timestamp (Unix seconds)
            end_ts: End timestamp (Unix seconds)
            fidelity: Resolution in minutes (default 60)

        Returns:
            List of price points: [{'t': timestamp, 'p': price}, ...]
        """
        try:
            url = f"{self.CLOB_API}/prices-history"
            params = {
                'market': token_id,
                'startTs': start_ts,
                'endTs': end_ts,
                'fidelity': fidelity
            }

            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            history = data.get('history', [])
            print(f"      ✅ Retrieved {len(history)} price points")
            return history

        except Exception as e:
            print(f"      ⚠️  Error fetching price history: {e}")
            return []

    def _fetch_trades(self, token_id: str, limit: int) -> tuple[List[Trade], List[Trade]]:
        """
        Fetch trade history for a token with retry on rate limit

        Returns:
            Tuple of (recent_trades, largest_trades)
        """
        import time as time_module

        max_retries = 2
        for attempt in range(max_retries):
            try:
                url = f"{self.DATA_API}/trades"
                response = self.session.get(url, params={
                    "asset_id": token_id,
                    "limit": limit
                }, timeout=10)

                # Handle rate limiting with retry
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (attempt + 1)  # 0.5s, 1.0s
                        time_module.sleep(wait_time)
                        continue
                    else:
                        return [], []  # Give up after retries

                response.raise_for_status()
                trades_data = response.json()
                break  # Success

            except Exception as e:
                if attempt < max_retries - 1 and "429" in str(e):
                    time_module.sleep(0.5)
                    continue
                else:
                    return [], []

        try:

            # Process trades
            trades = []
            for trade_data in trades_data:
                wallet = trade_data.get('proxyWallet', 'N/A')

                trade = Trade(
                    timestamp=trade_data.get('timestamp', 0),
                    side=trade_data.get('side', 'N/A'),
                    price=float(trade_data.get('price', 0)),
                    size=float(trade_data.get('size', 0)),
                    wallet=wallet,
                    transaction_hash=trade_data.get('transactionHash')
                )
                trades.append(trade)

            # Get recent trades (already in chronological order)
            recent_trades = trades[:20]

            # Get largest trades (sort by size)
            largest_trades = sorted(trades, key=lambda t: t.size, reverse=True)[:5]

            return recent_trades, largest_trades

        except Exception as e:
            print(f"      ⚠️  Error fetching trades: {e}")
            return [], []

    def get_all_sports_markets(self, sports: Optional[List[str]] = None,
                               limit_per_sport: int = 20,
                               include_orderbook: bool = True,
                               include_trades: bool = True) -> Dict[str, List[Game]]:
        """
        Get markets for all major sports

        Args:
            sports: List of sports to fetch (default: all major sports)
            limit_per_sport: Maximum games per sport
            include_orderbook: Whether to include orderbook data
            include_trades: Whether to include trade history

        Returns:
            Dictionary mapping sport name to list of Game objects
        """
        if sports is None:
            sports = ["NFL", "NBA", "NHL"]  # Default to major sports in season

        all_data = {}
        for sport in sports:
            print(f"\n{'='*80}")
            print(f"Fetching {sport} markets...")
            print(f"{'='*80}\n")

            try:
                games = self.get_sport_markets(
                    sport=sport,
                    limit=limit_per_sport,
                    include_orderbook=include_orderbook,
                    include_trades=include_trades
                )
                all_data[sport] = games
            except Exception as e:
                print(f"❌ Error fetching {sport}: {e}")
                all_data[sport] = []

        return all_data

    def get_game_by_teams(self, sport: str, team1: str, team2: str) -> Optional[Game]:
        """
        Find a specific game by team names

        Args:
            sport: Sport name
            team1: First team name (partial match)
            team2: Second team name (partial match)

        Returns:
            Game object if found, None otherwise
        """
        games = self.get_sport_markets(sport, limit=50)

        team1_lower = team1.lower()
        team2_lower = team2.lower()

        for game in games:
            title_lower = game.title.lower()
            if team1_lower in title_lower and team2_lower in title_lower:
                return game

        return None

    def export_to_json(self, data: Any, filename: str):
        """
        Export data to JSON file

        Args:
            data: Data to export (Game, List[Game], or Dict)
            filename: Output filename
        """
        # Convert dataclasses to dicts
        if isinstance(data, Game):
            data = asdict(data)
        elif isinstance(data, list):
            data = [asdict(g) for g in data]
        elif isinstance(data, dict):
            data = {k: [asdict(g) for g in v] for k, v in data.items()}

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"💾 Data exported to: {filename}")

    def print_game_summary(self, game: Game):
        """Print a formatted summary of a game"""
        print(f"\n{'='*80}")
        print(f"GAME: {game.title}")
        print(f"{'='*80}")
        print(f"Sport: {game.sport}")
        print(f"Start Time: {game.start_time}")
        print(f"Volume: ${game.volume:,.2f} (24h: ${game.volume_24hr:,.2f})")
        print(f"Liquidity: ${game.liquidity:,.2f}")
        print(f"Active: {game.active} | Closed: {game.closed}")

        print(f"\n📊 {len(game.markets)} Markets:")
        for idx, market in enumerate(game.markets, 1):
            print(f"\n  {idx}. {market.question}")
            print(f"     Outcomes: {', '.join([f'{o}: {p*100:.1f}%' for o, p in zip(market.outcomes, market.outcome_prices)])}")
            print(f"     Volume: ${market.volume:,.2f} | Liquidity: ${market.liquidity:,.2f}")

            if market.bids or market.asks:
                print(f"     Orderbook: {len(market.bids)} bids, {len(market.asks)} asks")
                if market.bids:
                    best_bid = market.bids[0]
                    print(f"       Best Bid: ${best_bid.price:.4f} x {best_bid.size:,.0f}")
                if market.asks:
                    best_ask = market.asks[0]
                    print(f"       Best Ask: ${best_ask.price:.4f} x {best_ask.size:,.0f}")

            if market.largest_trades:
                print(f"     Largest Trades:")
                for trade in market.largest_trades[:3]:
                    trade_time = datetime.fromtimestamp(trade.timestamp).strftime('%Y-%m-%d %H:%M')
                    wallet_short = trade.wallet[:8] + "..." + trade.wallet[-6:] if len(trade.wallet) > 20 else trade.wallet
                    print(f"       {trade_time} | {trade.side:4} ${trade.price:.4f} x {trade.size:,.0f} | {wallet_short}")


def main():
    """Example usage"""
    client = PolymarketSportsClient()

    # Example 1: Get all NFL games
    print("="*80)
    print("EXAMPLE 1: Fetching all NFL games with full market data")
    print("="*80)
    nfl_games = client.get_sport_markets("NFL", limit=5)

    if nfl_games:
        client.print_game_summary(nfl_games[0])

    # Example 2: Get specific game
    print("\n\n" + "="*80)
    print("EXAMPLE 2: Finding Bears vs Vikings game")
    print("="*80)
    bears_vikings = client.get_game_by_teams("NFL", "Bears", "Vikings")

    if bears_vikings:
        client.print_game_summary(bears_vikings)
    else:
        print("Game not found")

    # Example 3: Get all major sports
    print("\n\n" + "="*80)
    print("EXAMPLE 3: Fetching all major sports")
    print("="*80)
    all_sports = client.get_all_sports_markets(
        sports=["NFL", "NBA"],
        limit_per_sport=3,
        include_orderbook=True,
        include_trades=True
    )

    # Print summary
    print("\n\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    for sport, games in all_sports.items():
        total_volume = sum(g.volume_24hr for g in games)
        print(f"{sport}: {len(games)} games, ${total_volume:,.2f} 24h volume")

    # Export to JSON
    client.export_to_json(all_sports, "/home/retupmoc/Desktop/EffortScraper/OddsAPI/sports_markets_complete.json")


if __name__ == "__main__":
    main()
