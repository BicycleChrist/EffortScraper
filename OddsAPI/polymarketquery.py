import csv
import json
import orjson
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

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

def get_market_volume_single(token_id):
    """Fetch volume data for a single token_id from Gamma API with optimized rate limiting"""
    import time
    url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
    
    # Adaptive rate limiting for burst + throttle pattern
    max_retries = 3  # More retries for heavy throttling
    base_delay = 0.5  # Longer delay for throttled requests
    
    for attempt in range(max_retries):
        try:
            # Progressive backoff for throttled requests
            if attempt > 0:
                delay = base_delay * (2 ** attempt)  # 0.5s, 1.0s, 2.0s
                print(f"Rate limited, waiting {delay}s before retry {attempt}")
                time.sleep(delay)
            
            response = requests.get(url, timeout=8)
            
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
    print("Rate limiting strategy: 8 workers, burst then throttle, adaptive pacing")
    
    # Adaptive strategy: burst for first 100, then throttle
    # API allows ~100 requests burst, then heavily rate limits
    max_workers = 8  # Moderate concurrency to handle burst + throttle
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

def scrape_breaking_markets():
    """
    Scrape breaking markets from Polymarket's breaking page.
    Returns list of market question strings found on the page.
    """
    url = "https://polymarket.com/breaking"
    print(f"🌐 Scraping breaking markets from {url}...")

    try:
        # Use a browser-like user agent to avoid blocks
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all market title paragraphs with the specific class structure
        # Target: <p class="text-[15px] font-medium mb-0.5 text-pretty line-clamp-3 hover:underline underline-offset-2">
        market_titles = []

        # Look for p tags with these specific classes
        for p_tag in soup.find_all('p', class_='text-[15px]'):
            # Check if it has the other required classes
            classes = p_tag.get('class', [])
            if ('font-medium' in classes and
                'mb-0.5' in classes and
                'text-pretty' in classes and
                'line-clamp-3' in classes):

                title_text = p_tag.get_text(strip=True)
                if title_text and len(title_text) > 10:  # Sanity check
                    market_titles.append(title_text)

        print(f"✅ Found {len(market_titles)} breaking markets")
        for i, title in enumerate(market_titles[:5], 1):
            print(f"  {i}. {title[:60]}{'...' if len(title) > 60 else ''}")

        if len(market_titles) > 5:
            print(f"  ... and {len(market_titles) - 5} more")

        return market_titles

    except requests.RequestException as e:
        print(f"❌ Error fetching breaking markets page: {e}")
        return []
    except Exception as e:
        print(f"❌ Error parsing breaking markets: {e}")
        return []


def match_breaking_markets_to_tokens(breaking_titles, cached_markets):
    """
    Match breaking market titles to cached market data and extract token IDs.

    Args:
        breaking_titles: List of market question strings from breaking page
        cached_markets: List of market dicts from cache

    Returns:
        List of matched market dicts with token_ids
    """
    print(f"\n🔍 Matching {len(breaking_titles)} breaking markets against {len(cached_markets)} cached markets...")

    matched_markets = []
    unmatched_titles = []

    for breaking_title in breaking_titles:
        # Try exact match first
        found = False
        for market in cached_markets:
            if market.get('question', '') == breaking_title:
                matched_markets.append(market)
                found = True
                print(f"  ✓ Exact match: {breaking_title[:50]}...")
                break

        if not found:
            # Try fuzzy match (case-insensitive, strip whitespace)
            breaking_normalized = breaking_title.lower().strip()
            for market in cached_markets:
                market_question = market.get('question', '').lower().strip()
                if market_question == breaking_normalized:
                    matched_markets.append(market)
                    found = True
                    print(f"  ✓ Fuzzy match: {breaking_title[:50]}...")
                    break

        if not found:
            unmatched_titles.append(breaking_title)
            print(f"  ✗ No match: {breaking_title[:50]}...")

    print(f"\n📊 Matching results:")
    print(f"  ✓ Matched: {len(matched_markets)} markets")
    print(f"  ✗ Unmatched: {len(unmatched_titles)} markets")

    if unmatched_titles:
        print(f"\n⚠️  Unmatched markets (may need to refresh cache):")
        for title in unmatched_titles[:3]:
            print(f"    - {title[:60]}{'...' if len(title) > 60 else ''}")

    return matched_markets


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


def FetchMarkets(next_cursor=None, recent_only=True):
    """Fetch markets from Polymarket CLOB API"""
    markets_list = []

    if recent_only and next_cursor is None:
        next_cursor = GetRecentCursor()
        print(f"Starting recent_only from: {next_cursor}")

    # Get client lazily (only initialized when actually needed)
    client = get_client()

    while True:
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
                
        except Exception as e:
            print(f"Exception occurred: {e}")
            break
    
    return markets_list

def get_cached_or_fresh_markets(recent_only=True):
    """Get markets from cache if fresh, otherwise fetch and process from API

    Returns FILTERED active markets (not raw API response) to reduce memory usage
    """
    if is_cache_fresh():
        print("📋 Using cached filtered markets (fresh)")
        return load_cached_markets()
    else:
        print("🔄 Cache stale, fetching fresh markets from CLOB API")
        # Fetch raw markets from API
        raw_markets_list = FetchMarkets(recent_only=recent_only)

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

def add_volume_data_to_markets(markets, volume_limit=200, cancellation_flag=None) -> list[dict]:
    """Add fresh volume data to markets (supports both old and new breaking markets flow)"""
    all_token_ids = []
    for i, market in enumerate(markets):
        if i < volume_limit:
            all_token_ids.extend(market["token_ids"])

    print(f"Fetching volume data for {len(all_token_ids)} tokens from first {min(len(markets), volume_limit)} markets...")
    volume_map = get_market_volume_batch(all_token_ids, cancellation_flag=cancellation_flag)

    for i, market in enumerate(markets):
        volume_data = []
        total_volume = 0
        total_volume_24hr = 0
        total_liquidity = 0

        if i < volume_limit:
            for token_id in market["token_ids"]:
                vol_data = volume_map.get(token_id, {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0})
                volume_data.append(vol_data)
                total_volume += float(vol_data['volume']) if vol_data['volume'] else 0
                total_volume_24hr += float(vol_data['volume_24hr']) if vol_data['volume_24hr'] else 0
                total_liquidity += float(vol_data['liquidity']) if vol_data['liquidity'] else 0
        else:
            volume_data = [{'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0} for _ in market["token_ids"]]

        market["volume_data"] = volume_data
        market["total_volume"] = total_volume
        market["total_volume_24hr"] = total_volume_24hr
        market["total_liquidity"] = total_liquidity

    # Sort by volume
    markets.sort(key=lambda x: x.get("total_volume", 0), reverse=True)
    print(f"Sorted {len(markets)} markets by volume (highest to lowest)")

    return markets


def add_volume_data_to_breaking_markets(markets, cancellation_flag=None) -> list[dict]:
    """
    Optimized version: Add volume data ONLY to matched breaking markets.
    No volume_limit needed since we're only processing ~15-20 markets.
    """
    # Collect ALL token IDs from the breaking markets
    all_token_ids = []
    for market in markets:
        all_token_ids.extend(market["token_ids"])

    print(f"💰 Fetching volume data for {len(all_token_ids)} tokens from {len(markets)} breaking markets...")
    print(f"   (Previously would fetch 400+ tokens - now fetching {len(all_token_ids)}!)")

    # Fetch volume data for all tokens
    volume_map = get_market_volume_batch(all_token_ids, cancellation_flag=cancellation_flag)

    # Add volume data to each market
    for market in markets:
        volume_data = []
        total_volume = 0
        total_volume_24hr = 0
        total_liquidity = 0

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

    # Sort by volume (highest first)
    markets.sort(key=lambda x: x.get("total_volume", 0), reverse=True)
    print(f"✅ Sorted {len(markets)} breaking markets by volume")

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

def fetch_and_process_markets(recent_only=True, cancellation_flag=None, save_full_dump=False, use_breaking=True):
    """
    Optimized version using breaking markets from polymarket.com/breaking page.

    Args:
        recent_only: Whether to fetch only recent markets (used as fallback if breaking scrape fails)
        cancellation_flag: Dict with 'should_stop' key for cancellation
        save_full_dump: If True, saves PMdump.json (for manual runs)
                       If False, skips file writes (for ticker worker to avoid blocking)
        use_breaking: If True, scrapes breaking markets page (default, much faster)
                     If False, uses old behavior (fallback)
    """
    # Get FILTERED markets from cache (metadata only, no volume yet)
    processed_markets = get_cached_or_fresh_markets(recent_only=recent_only)

    # Check for cancellation
    if cancellation_flag and cancellation_flag.get('should_stop', False):
        print("🚫 Market fetch cancelled before volume data")
        return []

    # NEW APPROACH: Use breaking markets page to identify which markets to fetch
    if use_breaking:
        print("\n🚀 Using BREAKING MARKETS approach (optimized)...")

        # Scrape breaking markets page
        breaking_titles = scrape_breaking_markets()

        if not breaking_titles:
            print("⚠️  No breaking markets found, falling back to old approach...")
            use_breaking = False  # Fall through to old approach
        else:
            # Match breaking market titles to cached markets
            matched_markets = match_breaking_markets_to_tokens(breaking_titles, processed_markets)

            if not matched_markets:
                print("⚠️  No matches found, falling back to old approach...")
                use_breaking = False  # Fall through to old approach
            else:
                # Check for cancellation
                if cancellation_flag and cancellation_flag.get('should_stop', False):
                    print("🚫 Market fetch cancelled after matching")
                    return []

                # Fetch volume data ONLY for matched breaking markets (much faster!)
                print(f"\n💰 Fetching volume for {len(matched_markets)} breaking markets only...")
                print(f"   📉 Token reduction: ~400+ → {sum(len(m['token_ids']) for m in matched_markets)}")
                final_data = add_volume_data_to_breaking_markets(matched_markets, cancellation_flag=cancellation_flag)

                # Check for cancellation before saving
                if cancellation_flag and cancellation_flag.get('should_stop', False):
                    print("🚫 Market fetch cancelled before saving")
                    return []

                # Only save dumps if explicitly requested
                if save_full_dump:
                    WriteJsonDump(final_data)

                return final_data

    # FALLBACK: Old approach if breaking scrape failed
    if not use_breaking:
        print("\n📦 Using OLD approach (fallback - slower)...")
        print("💰 Fetching fresh volume data for first 200 markets...")
        final_data = add_volume_data_to_markets(processed_markets, cancellation_flag=cancellation_flag)

        # Check for cancellation before saving
        if cancellation_flag and cancellation_flag.get('should_stop', False):
            print("🚫 Market fetch cancelled before saving")
            return []

        # Only save dumps if explicitly requested
        if save_full_dump:
            WriteJsonDump(final_data)
            print("Skipping PMdump_all.json write (not needed for ticker)")

        return final_data

def fetch_and_process_markets_legacy(recent_only=True):
    """Original version without caching - kept for fallback"""
    markets_list = FetchMarkets(recent_only=recent_only)
    filtered_data = FilterData(markets_list)
    WriteJsonDump(filtered_data)
    WriteJsonDump(markets_list, "PMdump_all")
    return filtered_data




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



