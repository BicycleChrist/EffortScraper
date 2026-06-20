import csv
import json
import time
import threading
import orjson
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Callable, Iterable
from dataclasses import dataclass

def is_cache_fresh(cache_file="markets_cache.json", cache_hours=24):
    """Check if markets cache is still fresh"""
    try:
        if not pathlib.Path(cache_file).exists():
            return False

        # Use orjson to release GIL during parsing
        with open(cache_file, 'rb') as f:
            cache_data = orjson.loads(f.read())

        cache_time = datetime.fromisoformat(cache_data['timestamp'])
        current_time = datetime.now(timezone.utc)
        hours_old = (current_time - cache_time).total_seconds() / 3600

        return hours_old < cache_hours
    except Exception as e:
        print(f"Error checking cache freshness: {e}")
        return False

def load_cached_markets(cache_file="markets_cache.json"):
    """Load markets from cache file with GIL-releasing JSON parser"""
    try:
        # orjson releases GIL during parsing - prevents UI freeze
        with open(cache_file, 'rb') as f:
            cache_data = orjson.loads(f.read())
        return cache_data.get('markets', [])
    except Exception as e:
        print(f"Error loading cached markets: {e}")
        return []

def save_markets_cache(markets_list, cache_file="markets_cache.json", cache_hours=24):
    """Save markets to cache with timestamp - uses orjson for speed"""
    try:
        cache_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache_duration_hours": cache_hours,
            "markets": markets_list
        }
        # Use orjson for faster serialization that releases GIL
        with open(cache_file, 'wb') as f:
            f.write(orjson.dumps(cache_data))
        print(f"Saved {len(markets_list)} markets to cache")
    except Exception as e:
        print(f"Error saving markets cache: {e}")

# Shared keep-alive session for the gamma-api volume fetches. The old bare
# requests.get() opened a NEW connection per token — DNS lookup + full TLS
# handshake, thousands of times per refresh. The handshake/connection setup is
# the most GIL-expensive part of a request (pure-Python ssl/urllib3 glue), and
# with 8 workers grinding them concurrently the stall watchdog showed this
# herd starving the UI loop. Keep-alive reuses a handful of connections; the
# urllib3 pool underneath Session is thread-safe.
_GAMMA_SESSION = None


def _gamma_session():
    global _GAMMA_SESSION
    if _GAMMA_SESSION is None:
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8))
        _GAMMA_SESSION = s
    return _GAMMA_SESSION


def get_market_volume_single(token_id):
    """Fetch volume data for a single token_id from Gamma API with optimized rate limiting"""
    import time
    url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
    
    # Adaptive rate limiting for burst + throttle pattern
    max_retries = 3  # More retries for heavy throttling
    base_delay = 0.25  # Longer delay for throttled requests
    
    for attempt in range(max_retries):
        try:
            # Progressive backoff for throttled requests
            if attempt > 0:
                delay = base_delay * (2 ** attempt)  # 0.5s, 1.0s, 2.0s
                print(f"Rate limited, waiting {delay}s before retry {attempt}")
                time.sleep(delay)
            
            response = _gamma_session().get(url, timeout=8)
            
            if response.status_code == 429:
                print(f"Rate limited (429) for token {token_id[-8:]}..., attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    continue
                else:
                    print(f"Max retries exceeded for token {token_id[-8:]}...")
                    break
            
            response.raise_for_status()
            data = response.json()
            
            if data and len(data) > 0:
                market_data = data[0]
                volume_data = {
                    'volume': market_data.get('volume', 0),
                    'volume_24hr': market_data.get('volume24hr', 0),
                    'liquidity': market_data.get('liquidity', 0),
                    'volume_formatted': market_data.get('volumeNum', 0)
                }
                try:
                    vol = float(volume_data['volume']) if volume_data['volume'] else 0
                    liq = float(volume_data['liquidity']) if volume_data['liquidity'] else 0
                    print(f"✅ Token {token_id[-8:]}...: Vol=${vol:.2f}, Liq=${liq:.2f}")
                except (ValueError, TypeError):
                    print(f"✅ Token {token_id[-8:]}...: Vol=?, Liq=? (invalid data)")
                return volume_data
            else:
                print(f"❌ Token {token_id[-8:]}...: No data returned")
                return {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0}
                
        except requests.RequestException as e:
            if "429" in str(e) and attempt < max_retries - 1:
                continue
            print(f"REQUEST ERROR for token {token_id[-8:]}...: {e}")
            break
        except Exception as e:
            print(f"UNEXPECTED ERROR for token {token_id[-8:]}...: {e}")
            break
    
    return {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0}

def get_market_volume_batch(token_ids, cancellation_flag=None):
    """Fetch volume data with optimized rate limiting for maximum speed"""
    import concurrent.futures
    import time
    
    if not token_ids:
        return {}
    
    volume_map = {}
    
    print(f"\n🚀 Fetching volume data for {len(token_ids)} tokens...")
    print("Rate limiting strategy: 3 workers, keep-alive session, burst then throttle")
    
    # Adaptive strategy: burst for first 100, then throttle
    # API allows ~100 requests burst, then heavily rate limits
    # Capped at 3: this runs inside the EffortOdds process, and 8 workers'
    # worth of concurrent request glue (urllib3/http.client framing, json)
    # was enough GIL pressure to starve the UI loop (watchdog-confirmed).
    # With the keep-alive _gamma_session the per-request cost is far lower
    # anyway, so 3 workers sustain a similar request rate.
    max_workers = 3
    request_delay = 0.1  # 100ms between batches initially
    
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit with adaptive pacing - burst then throttle
        future_to_token = {}
        for i, token_id in enumerate(token_ids):
            # Check for cancellation before submitting more requests
            if cancellation_flag and cancellation_flag.get('should_stop', False):
                print(f"🚫 Volume fetch cancelled after submitting {i} requests")
                # Cancel any pending futures
                for pending_future in future_to_token.keys():
                    pending_future.cancel()
                return {}
            
            # Adaptive pacing: fast for first 100, then slower
            if i > 0 and i % max_workers == 0:
                if i < 100:
                    # Fast burst for first 100 requests
                    time.sleep(0.05)  
                else:
                    # Slower throttled rate after burst limit
                    time.sleep(0.3)
            
            future = executor.submit(get_market_volume_single, token_id)
            future_to_token[future] = token_id
        
        # Collect results with progress tracking
        completed = 0
        successful = 0
        
        for future in concurrent.futures.as_completed(future_to_token):
            # Check for cancellation during result collection
            if cancellation_flag and cancellation_flag.get('should_stop', False):
                print(f"🚫 Volume fetch cancelled during result collection (completed {completed}/{len(token_ids)})")
                # Cancel remaining futures
                for remaining_future in future_to_token.keys():
                    remaining_future.cancel()
                return volume_map  # Return partial results
            
            token_id = future_to_token[future]
            try:
                volume_data = future.result()
                volume_map[token_id] = volume_data
                completed += 1
                
                # Track success rate (handle string/int volume values)
                try:
                    volume_val = volume_data.get('volume', 0)
                    volume_num = float(volume_val) if volume_val else 0
                    if volume_num > 0:
                        successful += 1
                except (ValueError, TypeError):
                    pass  # Skip if volume value is invalid
                
                # Progress with performance stats
                if completed % 50 == 0 or completed == len(token_ids):
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    print(f"📊 Progress: {completed}/{len(token_ids)} ({completed/len(token_ids)*100:.1f}%) | "
                          f"Rate: {rate:.1f} req/s | Success: {successful} | "
                          f"Elapsed: {elapsed:.1f}s")
                    
            except Exception as exc:
                print(f"❌ Token {token_id[-8:]}... exception: {exc}")
                volume_map[token_id] = {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0}
                completed += 1
    
    # Final stats
    elapsed = time.time() - start_time
    print(f"\n✅ Volume fetch complete!")
    print(f"📈 Results: {len(volume_map)} total, {successful} with volume")
    print(f"⏱️ Performance: {elapsed:.1f}s total, {len(token_ids)/elapsed:.1f} req/s average")
    
    return volume_map

# NOTE: the polymarket.com/breaking scrape + title-matching path was removed —
# that page is now client-side JS rendered, so a plain GET returned 0 markets and
# always fell through to the gamma-API approach below.


def GetRecentCursor():
    """Get recent cursor position for tickertape data collection"""
    # Fixed cursor for recent active markets (12 blocks behind current end)
    recent_cursor = 'NTk1MDA='
    print(f"Using recent cursor: {recent_cursor}")
    return recent_cursor

# Polymarket CLOB API host
host = "https://clob.polymarket.com"
chain_id = 137  # Polygon Mainnet

# Lazy client initialization - only create when needed
_client = None

def get_client():
    """Lazy initialization of ClobClient to avoid blocking on module import"""
    global _client
    if _client is None:
        _client = ClobClient(
            host,
            key=pmkey,
            chain_id=chain_id
        )
    return _client


def FetchMarkets(next_cursor=None, recent_only=True, cancellation_flag=None):
    """Fetch markets from Polymarket CLOB API"""
    markets_list = []

    if recent_only and next_cursor is None:
        next_cursor = GetRecentCursor()
        print(f"Starting recent_only from: {next_cursor}")

    # Get client lazily (only initialized when actually needed)
    client = get_client()

    while True:
        if cancellation_flag and cancellation_flag.get('should_stop', False):
            print("🚫 FetchMarkets cancelled during pagination")
            break
        try:
            print(f"Fetching markets with next_cursor: {next_cursor}")
            response = client.get_markets(next_cursor=next_cursor) if next_cursor else client.get_markets()

            if 'data' not in response:
                print("No data found in response.")
                break

            markets_list.extend(response['data'])
            next_cursor = response.get("next_cursor")

            if not next_cursor or next_cursor.startswith('LTE='):
                if next_cursor and next_cursor.startswith('LTE='):
                    print(f"Reached endmarker cursor {next_cursor}, stopping pagination")
                break

            # Yield to the GIL so the Qt event loop can repaint between
            # back-to-back JSON-decode bursts from py-clob-client.
            time.sleep(0.02)

        except Exception as e:
            print(f"Exception occurred: {e}")
            break

    return markets_list

def get_cached_or_fresh_markets(recent_only=True, cancellation_flag=None):
    """Get markets from cache if fresh, otherwise fetch and process from API

    Returns FILTERED active markets (not raw API response) to reduce memory usage
    """
    if is_cache_fresh():
        print("📋 Using cached filtered markets (fresh)")
        return load_cached_markets()
    else:
        print("🔄 Cache stale, fetching fresh markets from CLOB API")
        # Fetch raw markets from API
        raw_markets_list = FetchMarkets(recent_only=recent_only, cancellation_flag=cancellation_flag)

        # Filter to active markets BEFORE caching (saves memory and time)
        print(f"Filtering {len(raw_markets_list)} raw markets to active only...")
        filtered_markets = process_markets_metadata(raw_markets_list)

        # Cache the FILTERED markets (much smaller than raw)
        save_markets_cache(filtered_markets)
        print(f"Cached {len(filtered_markets)} filtered active markets")

        return filtered_markets


def process_markets_metadata(markets) -> list[dict]:
    """Process markets metadata without volume data"""
    import time

    wanted_fields = ("question", "description", "tokens", "question_id", "condition_id", "tags")

    # MUCH smaller chunks to release GIL frequently and prevent UI freeze
    active_markets = []
    chunk_size = 50  # Reduced from 1000 to 50 for more frequent GIL releases

    for i in range(0, len(markets), chunk_size):
        chunk = markets[i:i + chunk_size]
        active_chunk = [market for market in chunk if ((market["active"] is True) and (not market["closed"] and (not market["archived"])))]
        active_markets.extend(active_chunk)

        # Release GIL after EVERY chunk
        time.sleep(0.0001)  # 0.1ms sleep releases GIL

    print(f"Filtered to {len(active_markets)} active markets from {len(markets)} total markets")

    # Process in chunks to release GIL
    filtered_data = []
    for i in range(0, len(active_markets), chunk_size):
        chunk = active_markets[i:i + chunk_size]
        chunk_data = [
            { field: market[field] for field in wanted_fields }
            for market in chunk
        ]
        filtered_data.extend(chunk_data)

        # Release GIL after every chunk
        time.sleep(0.0001)

    # Process market lines in chunks
    for i, market in enumerate(filtered_data):
        market["lines"] = [ token['outcome'] + ': ' + str(token['price']*100) + '%' for token in market["tokens"] ]
        market["token_ids"] = [token['token_id'] for token in market['tokens']]
        if market["tags"] is None:
            market["tags"] = []
        del market["tokens"]

        # Release GIL more frequently (every 50 items)
        if i > 0 and i % chunk_size == 0:
            time.sleep(0.0001)

    return filtered_data

def add_volume_data_to_markets(markets, token_limit=50, cancellation_flag=None) -> list[dict]:
    """Attach fresh volume data, fetching at most `token_limit` tokens.

    Tokens are taken in market order until the cap is hit (was 400 tokens / 200
    markets — way more than the ticker needs). Markets beyond the cap get zeros
    and naturally sink to the bottom of the volume sort.
    """
    all_token_ids = []
    for market in markets:
        for tid in market["token_ids"]:
            if len(all_token_ids) >= token_limit:
                break
            all_token_ids.append(tid)
        if len(all_token_ids) >= token_limit:
            break

    print(f"Fetching volume data for {len(all_token_ids)} tokens (cap {token_limit})...")
    volume_map = get_market_volume_batch(all_token_ids, cancellation_flag=cancellation_flag)

    for market in markets:
        volume_data = []
        total_volume = total_volume_24hr = total_liquidity = 0
        for token_id in market["token_ids"]:
            vol_data = volume_map.get(token_id, {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0})
            volume_data.append(vol_data)
            total_volume += float(vol_data['volume']) if vol_data['volume'] else 0
            total_volume_24hr += float(vol_data['volume_24hr']) if vol_data['volume_24hr'] else 0
            total_liquidity += float(vol_data['liquidity']) if vol_data['liquidity'] else 0

        market["volume_data"] = volume_data
        market["total_volume"] = total_volume
        market["total_volume_24hr"] = total_volume_24hr
        market["total_liquidity"] = total_liquidity

    # Sort by volume
    markets.sort(key=lambda x: x.get("total_volume", 0), reverse=True)
    print(f"Sorted {len(markets)} markets by volume (highest to lowest)")

    return markets


def FilterData(markets) -> list[dict]:
    """Legacy function for backward compatibility"""
    processed_markets = process_markets_metadata(markets)
    return add_volume_data_to_markets(processed_markets)

def WriteJsonDump(data: list[dict], filename="PMdump"):
    with open((pathlib.Path.cwd() / f"{filename}.json"), "w") as json_file:
        json.dump(data, json_file, indent=2)
        print(f"wrote {filename}.json")
    return

def LoadJsonDump():
    loaded_data = {}
    with open((pathlib.Path.cwd() / "PMdump.json"), "r") as json_file:
        loaded_data = json.load(json_file)
    return loaded_data


def GenerateHTML(markets_list: list[dict]):
    """Generate HTML ticker items for EffortOdds display"""
    cwd = pathlib.Path.cwd()
    savedir = cwd / "PolyMarketHTML"
    if not savedir.exists(): savedir.mkdir()
    html_file = savedir / "ticker_items.html"
    
    divs = [ 
        '            { title: ' + "'" + market["description"].splitlines()[0] + "', " + f'{ market["lines"][0].replace('%', '').lower() + ", " + market["lines"][1].replace('%', '').lower() }' + " },\n" 
        for market in markets_list
    ]
    
    print("writing HTML divs...")
    with open(html_file, "w", encoding="utf-8") as file:
        for div in divs: file.write(div)
    print(f"wrote to {html_file}")
    return


def SaveToCSV(marketsdata, filename):
    cwd = pathlib.Path.cwd() 
    savedir = cwd / "PolyMarketCSV"
    if not savedir.exists(): savedir.mkdir()
    csv_file = savedir/(filename + ".csv")
    
    csv_columns = marketsdata[0].keys()
    
    # Writing to CSV
    try:
        with open(csv_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
            writer.writeheader()
            for market in marketsdata:
                row = {}
                for key in csv_columns:
                    if key.startswith("token_"):
                        token_key = key[len("token_"):]
                        row[key] = ', '.join([str(token.get(token_key, 'N/A')) for token in market.get('tokens', [])])
                    else:
                        row[key] = market.get(key, 'N/A')
                writer.writerow(row)
        print(f"Data has been written to {csv_file} successfully.")
    except IOError as e:
        print(f"Error writing to CSV: {e}")

def fetch_and_process_markets(recent_only=True, cancellation_flag=None, save_full_dump=False, use_breaking=None):
    """
    Fetch recent market metadata (gamma API), attach fresh volume for the top
    `token_limit` tokens, and sort by volume.

    Args:
        recent_only: Whether to fetch only recent markets
        cancellation_flag: Dict with 'should_stop' key for cancellation
        save_full_dump: If True, saves PMdump.json (for manual runs)
        use_breaking: Deprecated/ignored — the polymarket.com/breaking scrape was
                      removed (that page is now JS-rendered).
    """
    # Get FILTERED markets from cache (metadata only, no volume yet)
    processed_markets = get_cached_or_fresh_markets(recent_only=recent_only, cancellation_flag=cancellation_flag)

    if cancellation_flag and cancellation_flag.get('should_stop', False):
        print("🚫 Market fetch cancelled before volume data")
        return []

    print("\n💰 Fetching fresh volume data (top tokens)...")
    final_data = add_volume_data_to_markets(processed_markets, cancellation_flag=cancellation_flag)

    if cancellation_flag and cancellation_flag.get('should_stop', False):
        print("🚫 Market fetch cancelled before saving")
        return []

    if save_full_dump:
        WriteJsonDump(final_data)

    return final_data

def fetch_and_process_markets_legacy(recent_only=True):
    """Original version without caching - kept for fallback"""
    markets_list = FetchMarkets(recent_only=recent_only)
    filtered_data = FilterData(markets_list)
    WriteJsonDump(filtered_data)
    WriteJsonDump(markets_list, "PMdump_all")
    return filtered_data




# ============================================================================
# Polymarket Sports Client (merged from the former polymarket_sports_client.py)
#
# Sports-event listing + orderbook/trade fetching via the modern Gamma /events
# and CLOB /book + Data /trades endpoints. Distinct from the tickertape path
# above (authenticated CLOB pagination + per-token Gamma volume): this is the
# sports-specific client consumed by HistoricalOddsClient. Only the surface
# actually used downstream is kept here; the old demo/export helpers and the
# unused get_historical_prices/get_all_sports_markets/get_game_by_teams methods
# were dropped during the merge.
# ============================================================================


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
                         max_trades: int = 100,
                         days_ahead: Optional[int] = None) -> List[Game]:
        """
        Get all active games for a specific sport with full market data

        Args:
            sport: Sport name (NFL, NBA, NHL, MLB, CFB, NCAAB)
            limit: Maximum number of games to fetch
            include_orderbook: Whether to fetch orderbook data
            include_trades: Whether to fetch trade history
            max_trades: Maximum number of trades to fetch per market
            days_ahead: When set, restrict the listing to events whose
                gamma endDate falls within now + N days. The unfiltered
                series listing is ordered by listing-creation time, so a
                bare `limit` cap returns months-out games (created early)
                while dropping games 2-3 days from now — which then show
                as Kalshi-only in the unified event list.

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
        if days_ahead is not None:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) + timedelta(days=days_ahead)
            params["end_date_max"] = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

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


# ============================================================================
# Polymarket live WebSocket feed (sub-second market channel)
#
# Mirrors KalshiStreamClient / KalshiLiveBook (KalshiClient.py) so the historical
# odds widget's live render path can be reused for Polymarket. The CLOB market
# channel is PUBLIC (no auth) and far simpler than Kalshi's: prices arrive as
# 0-1 dollar strings (×100 == implied-chance cents, the same space the widget
# renders in), the YES token's book gives bids/asks directly (no NO inversion),
# and there are NO sequence numbers — `book` is a full snapshot and `price_change`
# carries the NEW ABSOLUTE size at each level. Connection keepalive is an
# application-level "PING" string every <10s (the server replies "PONG").
#
# Docs: https://docs.polymarket.com/developers/CLOB/websocket/market-channel
# ============================================================================

import websocket  # websocket-client (same dep KalshiClient uses)
from PyQt6.QtCore import QObject, pyqtSignal


POLYMARKET_WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketStreamClient(QObject):
    """WebSocket streaming client for the Polymarket CLOB market channel.

    Emits typed Qt signals so the widget can consume them on the main thread:
      - orderbook(dict): a `book` (full snapshot) or `price_change` (level updates)
      - trade(dict):     a `last_trade_price` execution
      - best_quote(dict):a `best_bid_ask` update (only with custom_feature_enabled)
      - connected / disconnected / error

    Subscriptions are keyed by CLOB token id (asset_id). The desired set is
    resent on every (re)connect; switching markets replaces the set and bounces
    the socket so the server stops streaming the old asset. Messages for assets
    we no longer care about are also filtered downstream by asset_id.
    """

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(object)
    raw_message = pyqtSignal(object)
    orderbook = pyqtSignal(object)
    trade = pyqtSignal(object)
    best_quote = pyqtSignal(object)

    def __init__(
        self,
        url: str = POLYMARKET_WS_MARKET,
        reconnect: bool = True,
        reconnect_backoff_max: int = 60,
        ping_interval: int = 8,
        custom_feature_enabled: bool = True,
    ):
        super().__init__()
        self.url = url
        self.reconnect = reconnect
        self.reconnect_backoff_max = reconnect_backoff_max
        self.ping_interval = ping_interval  # app-level PING cadence (<10s)
        self.custom_feature_enabled = custom_feature_enabled

        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._assets = set()           # desired CLOB token ids
        self._assets_lock = threading.Lock()
        self._is_running = False
        self._running_lock = threading.Lock()

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    def start(self):
        with self._running_lock:
            if self._is_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_forever, name="PolymarketWS", daemon=True)
            self._thread.start()
            self._is_running = True

    def stop(self):
        self._stop_event.set()
        self.reconnect = False
        # Close the UNDERLYING socket directly instead of WebSocketApp.close():
        # close() performs a close handshake (blocking recv_frame on the caller),
        # which stalls the Qt main loop ~100-200ms when stop() is called from the
        # GUI thread. Closing the raw socket unblocks run_forever's recv at once
        # and is non-blocking.
        ws = self._ws_app
        if ws is not None:
            try:
                ws.keep_running = False
            except Exception:
                pass
            sock = getattr(ws, "sock", None)
            raw = getattr(sock, "sock", None) if sock is not None else None
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=2)
        with self._running_lock:
            self._is_running = False

    def send(self, message: str):
        if (self._ws_app and getattr(self._ws_app, "sock", None)
                and self._ws_app.sock.connected):
            try:
                self._ws_app.send(message)
            except Exception as e:
                self.error.emit({"action": "send_failed", "exception": e})
        else:
            self.error.emit({"action": "not_connected"})

    def set_assets(self, asset_ids: Iterable[str]):
        """Replace the desired asset set and (re)send the subscription in place.

        Does NOT close/reconnect — WebSocketApp.close() blocks the caller on the
        close handshake (a GUI stall when called from the main thread). Sending a
        fresh subscribe for the new set is non-blocking; any still-streaming old
        assets are harmless (filtered downstream by active-key) and are dropped on
        the next natural reconnect, which re-subscribes only the current set."""
        with self._assets_lock:
            self._assets = {a for a in asset_ids if a}
        self._send_subscribe()

    def subscribe(self, asset_ids: Iterable[str]):
        """Add assets to the desired set and (re)send the subscription."""
        with self._assets_lock:
            self._assets.update(a for a in asset_ids if a)
        self._send_subscribe()

    # ------------------------------------------------------------------
    # SUBSCRIPTION
    # ------------------------------------------------------------------
    def _build_subscribe(self) -> Optional[str]:
        with self._assets_lock:
            assets = sorted(self._assets)
        if not assets:
            return None
        return json.dumps({
            "assets_ids": assets,
            "type": "market",
            "custom_feature_enabled": self.custom_feature_enabled,
        })

    def _send_subscribe(self):
        payload = self._build_subscribe()
        if payload:
            self.send(payload)

    # ------------------------------------------------------------------
    # INTERNAL THREAD LOOP
    # ------------------------------------------------------------------
    def _run_forever(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._ws_app = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app.run_forever()
            except Exception as ex:
                self.error.emit({"action": "run_exception", "exception": ex})

            if self._stop_event.is_set() or not self.reconnect:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_backoff_max)

        self._ws_app = None
        self._is_running = False

    def _ping_loop(self):
        """Application-level keepalive: Polymarket wants a literal 'PING' string
        roughly every 10s (it replies 'PONG'); WS-protocol pings aren't enough."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.ping_interval):
                break
            if (self._ws_app and getattr(self._ws_app, "sock", None)
                    and self._ws_app.sock.connected):
                try:
                    self._ws_app.send("PING")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # CALLBACKS
    # ------------------------------------------------------------------
    def _on_open(self, ws):
        self.connected.emit()
        self._send_subscribe()
        # One ping thread for the client's lifetime (idempotent guard).
        if self._ping_thread is None or not self._ping_thread.is_alive():
            self._ping_thread = threading.Thread(
                target=self._ping_loop, name="PolymarketWS-ping", daemon=True)
            self._ping_thread.start()

    def _on_message(self, ws, message):
        # Keepalive ack and other bare strings: ignore.
        if message in ("PONG", "PING"):
            return
        try:
            msg = json.loads(message)
        except Exception:
            return

        self.raw_message.emit(msg)

        # The market channel may deliver a single event object OR an array of
        # them (the initial book burst arrives as a list). Normalize to a list.
        events = msg if isinstance(msg, list) else [msg]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = ev.get("event_type")
            if et in ("book", "price_change"):
                self.orderbook.emit(ev)
            elif et == "last_trade_price":
                self.trade.emit(ev)
            elif et == "best_bid_ask":
                self.best_quote.emit(ev)
            # tick_size_change / new_market / market_resolved: no live-render
            # action needed today; raw_message already exposed them.

    def _on_error(self, ws, error):
        self.error.emit({"action": "ws_error", "error": error})

    def _on_close(self, ws, code, reason):
        self.disconnected.emit()


class PolymarketLiveBook:
    """Maintains a live orderbook + last trade per Polymarket CLOB token (asset_id)
    from the market-channel `book` / `price_change` / `last_trade_price` stream.

    All prices are normalized to CENTS (0-100, == implied chance %) to match the
    widget's shared live render path. Unlike Kalshi there is no NO-side: a token's
    own bids/asks are the YES-equivalent book directly. No sequence numbers exist,
    so there is no gap handling — `book` is authoritative and `price_change` sets
    each level's new absolute size.

    Level dicts are keyed by price-in-cents rounded to 1 decimal (PM tick size can
    be 0.001 dollars = 0.1¢); sizes are share counts (float).
    """

    def __init__(self):
        # asset_id -> {"bids": {cents: size}, "asks": {cents: size},
        #              "last_trade": cents|None}
        self._books: Dict[str, Dict] = {}

    def _blank(self):
        return {"bids": {}, "asks": {}, "last_trade": None}

    @staticmethod
    def _to_cents(price):
        """Dollar price (string/float, 0-1) -> cents float rounded to 0.1¢."""
        try:
            return round(float(price) * 100.0, 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_size(size):
        try:
            return float(size)
        except (TypeError, ValueError):
            return 0.0

    def apply(self, ev: dict) -> Optional[str]:
        """Apply one market-channel event. Returns the affected asset_id, or None
        if the event was ignored / had no asset."""
        if not isinstance(ev, dict):
            return None
        et = ev.get("event_type")

        if et == "book":
            asset_id = ev.get("asset_id")
            if not asset_id:
                return None
            book = self._books.get(asset_id)
            last = book.get("last_trade") if book else None
            book = self._blank()
            book["last_trade"] = last  # preserve across snapshots
            for b in ev.get("bids", []) or []:
                c = self._to_cents(b.get("price"))
                if c is not None:
                    book["bids"][c] = self._to_size(b.get("size"))
            for a in ev.get("asks", []) or []:
                c = self._to_cents(a.get("price"))
                if c is not None:
                    book["asks"][c] = self._to_size(a.get("size"))
            self._books[asset_id] = book
            return asset_id

        if et == "price_change":
            # Each change carries its own asset_id; a message can touch several.
            touched = None
            for ch in ev.get("price_changes", []) or []:
                asset_id = ch.get("asset_id")
                if not asset_id:
                    continue
                book = self._books.setdefault(asset_id, self._blank())
                side = (ch.get("side") or "").upper()
                levels = book["bids"] if side == "BUY" else \
                    book["asks"] if side == "SELL" else None
                if levels is None:
                    continue
                c = self._to_cents(ch.get("price"))
                if c is None:
                    continue
                size = self._to_size(ch.get("size"))  # NEW absolute size
                if size <= 1e-9:
                    levels.pop(c, None)
                else:
                    levels[c] = size
                touched = asset_id
            return touched

        if et == "last_trade_price":
            asset_id = ev.get("asset_id")
            if not asset_id:
                return None
            book = self._books.setdefault(asset_id, self._blank())
            c = self._to_cents(ev.get("price"))
            if c is not None:
                book["last_trade"] = c
            return asset_id

        return None

    def state(self, asset_id: str) -> Optional[dict]:
        """Normalized state in cents: best bid/ask, mid, last trade."""
        book = self._books.get(asset_id)
        if not book:
            return None
        best_bid = max(book["bids"]) if book["bids"] else None
        best_ask = min(book["asks"]) if book["asks"] else None
        mid = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        return {
            "asset_id": asset_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "last_trade": book["last_trade"],
            "stale": False,  # no seq stream to fall behind
        }

    def ladder(self, asset_id: str, depth: int = 10) -> Optional[dict]:
        """Depth ladder in cents: bids best (highest) first, asks best (lowest)
        first. Same shape as KalshiLiveBook.ladder for the shared ladder widget."""
        book = self._books.get(asset_id)
        if not book:
            return None
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:depth]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:depth]
        return {
            "bids": [(round(p, 1), int(round(q))) for p, q in bids],
            "asks": [(round(p, 1), int(round(q))) for p, q in asks],
            "best_bid": round(bids[0][0], 1) if bids else None,
            "best_ask": round(asks[0][0], 1) if asks else None,
            "last_trade": book["last_trade"],
            "stale": False,
        }

    def reset(self, asset_id: str = None):
        if asset_id is None:
            self._books.clear()
        else:
            self._books.pop(asset_id, None)


if __name__ == "__main__":
    # Process markets with recent_only=True and volume optimization
    # Enable save_full_dump for manual runs
    filtered_data = fetch_and_process_markets(recent_only=True, save_full_dump=True)

    print(f"\nProcessed {len(filtered_data)} markets")
    markets_with_volume = [m for m in filtered_data if m.get('total_volume', 0) > 0]
    print(f"Markets with volume: {len(markets_with_volume)}")

    # Generate outputs
    GenerateHTML(filtered_data)
    SaveToCSV(filtered_data, "recent_active_markets")



