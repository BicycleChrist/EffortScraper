import csv
import json
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib
import requests
from datetime import datetime

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

def get_market_volume_batch(token_ids):
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

def GetRecentCursor():
    """Get recent cursor position for tickertape data collection"""
    # Fixed cursor for recent active markets (12 blocks behind current end)
    recent_cursor = 'NTk1MDA='
    print(f"Using recent cursor: {recent_cursor}")
    return recent_cursor

# Polymarket CLOB API host
host = "https://clob.polymarket.com"
chain_id = 137  # Polygon Mainnet

# Initialize the client with API key
client = ClobClient(
    host,
    key=pmkey,
    chain_id=chain_id
)


def FetchMarkets(next_cursor=None, recent_only=True):
    """Fetch markets from Polymarket CLOB API"""
    markets_list = []
    
    if recent_only and next_cursor is None:
        next_cursor = GetRecentCursor()
        print(f"Starting recent_only from: {next_cursor}")
    
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


def FilterData(markets) -> list[dict]:
    wanted_fields = ("question", "description", "tokens", "question_id", "condition_id", "tags")
    # there are always two tokens. https://docs.polymarket.com/#get-markets
    # "outcome" is the line the token represents. Usually "Yes/No", but sometimes not.
    # (which-party-will-win-the-2024-united-states-presidential-election: "Democratic"/"Republican")
    # 'winner' will always be false for open markets
    active_markets = [market for market in markets if ((market["active"] is True) and (not market["closed"] and (not market["archived"])))]
    print(f"Filtered to {len(active_markets)} active markets from {len(markets)} total markets")
    markets = active_markets
    filtered_data = [
        { field: market[field] for field in wanted_fields }
        for market in markets
    ]
    
    # Optimize volume queries - only query first 200 markets where volume is concentrated
    volume_limit = min(200, len(filtered_data))
    
    all_token_ids = []
    for market in filtered_data:
        market["lines"] = [ token['outcome'] + ': ' + str(token['price']*100) + '%' for token in market["tokens"] ]
        market["token_ids"] = [token['token_id'] for token in market['tokens']]
        # Only collect token IDs for volume query if in first 200 markets
        if filtered_data.index(market) < volume_limit:
            all_token_ids.extend(market["token_ids"])
    
    print(f"Fetching volume data for {len(all_token_ids)} tokens from first {volume_limit} markets...")
    volume_map = get_market_volume_batch(all_token_ids)
    
    print("Processing volume data for markets...")
    print(f"Volume map keys: {list(volume_map.keys())[:5]}...")
    
    for i, market in enumerate(filtered_data):
        # Get volume data for each token from the batch result
        volume_data = []
        total_volume = 0
        total_volume_24hr = 0
        total_liquidity = 0
        
        # Only process volume data for markets in the volume_limit range
        if i < volume_limit:
            print(f"Processing market {i}: {market['question'][:50]}...")
            
            for token_id in market["token_ids"]:
                vol_data = volume_map.get(token_id, {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0})
                volume_data.append(vol_data)
                total_volume += float(vol_data['volume']) if vol_data['volume'] else 0
                total_volume_24hr += float(vol_data['volume_24hr']) if vol_data['volume_24hr'] else 0
                total_liquidity += float(vol_data['liquidity']) if vol_data['liquidity'] else 0
        else:
            # For markets beyond volume_limit, set zero volume (they likely have none anyway)
            volume_data = [{'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0} for _ in market["token_ids"]]
        
        # Add volume fields to market data
        market["volume_data"] = volume_data
        market["total_volume"] = total_volume
        market["total_volume_24hr"] = total_volume_24hr
        market["total_liquidity"] = total_liquidity
        
        if market["tags"] is None: market["tags"] = []; # ensure 'tags' is always a list (handling case where it was null in JSON)
        del market["tokens"]
    
    # Sort markets by total volume (highest to lowest)
    filtered_data.sort(key=lambda x: x.get("total_volume", 0), reverse=True)
    print(f"Sorted {len(filtered_data)} markets by volume (highest to lowest)")
    
    return filtered_data

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

def fetch_and_process_markets(recent_only=True):
    markets_list = FetchMarkets(recent_only=recent_only)
    filtered_data = FilterData(markets_list)
    WriteJsonDump(filtered_data)
    WriteJsonDump(markets_list, "PMdump_all")
    return filtered_data




if __name__ == "__main__":
    # Process markets with recent_only=True and volume optimization
    filtered_data = fetch_and_process_markets(recent_only=True)
    
    print(f"\nProcessed {len(filtered_data)} markets")
    markets_with_volume = [m for m in filtered_data if m.get('total_volume', 0) > 0]
    print(f"Markets with volume: {len(markets_with_volume)}")
    
    # Generate outputs
    GenerateHTML(filtered_data)
    SaveToCSV(filtered_data, "recent_active_markets")



