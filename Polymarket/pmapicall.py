import csv
import json
from collections import Counter
from pprint import pprint
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib
import requests
import base64

from datetime import datetime

def get_market_volume_single(token_id):
    """Fetch volume data for a single token_id from Gamma API"""
    url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
    try:
        print(f"Fetching volume for token: {token_id}")
        response = requests.get(url)
        print(f"Response status: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        print(f"Response data length: {len(data) if data else 0}")
        
        if data and len(data) > 0:
            market_data = data[0]
            volume_data = {
                'volume': market_data.get('volume', 0),
                'volume_24hr': market_data.get('volume24hr', 0),
                'liquidity': market_data.get('liquidity', 0),
                'volume_formatted': market_data.get('volumeNum', 0)
            }
            print(f"Volume data for {token_id}: {volume_data}")
            return volume_data
        else:
            print(f"No data returned for token {token_id}")
    except requests.RequestException as e:
        print(f"REQUEST ERROR for token {token_id}: {e}")
    except (KeyError, IndexError, ValueError) as e:
        print(f"PARSING ERROR for token {token_id}: {e}")
    except Exception as e:
        print(f"UNEXPECTED ERROR for token {token_id}: {e}")
    
    print(f"Returning zeros for token {token_id}")
    return {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0}

def get_market_volume_batch(token_ids):
    """Fetch volume data for multiple token_ids using parallel requests"""
    import concurrent.futures
    import threading
    
    if not token_ids:
        return {}
    
    volume_map = {}
    
    # Use ThreadPoolExecutor for parallel requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Submit all requests
        future_to_token = {executor.submit(get_market_volume_single, token_id): token_id for token_id in token_ids}
        
        # Collect results
        for future in concurrent.futures.as_completed(future_to_token):
            token_id = future_to_token[future]
            try:
                volume_data = future.result()
                volume_map[token_id] = volume_data
            except Exception as exc:
                print(f"Token {token_id} generated an exception: {exc}")
                volume_map[token_id] = {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0}
    
    return volume_map

def GetCurrentTimestamp(): return datetime.now().timestamp()


def DateFromTimestamp(timestamp: int) -> str: return datetime.fromtimestamp(timestamp).date().isoformat()

TIMESERIES_DIR = pathlib.Path.cwd() / "timeseries_cache"
if not TIMESERIES_DIR.exists(): TIMESERIES_DIR.mkdir()
TIMESERIES_CACHE = None

# counting how many markets needed to be fetched
CACHE_MISS_COUNT = 0

# cursor persistence for recent_only queries
CURSOR_FILE = pathlib.Path.cwd() / "last_cursor.txt"

def SaveLastCursor(cursor: str):
    """Save a cursor that's offset behind the last cursor for next recent_only run"""
    import base64
    try:
        # Decode cursor to get numeric value
        cursor_num = int(base64.b64decode(cursor).decode('utf-8'))
        # Subtract 6 blocks (6 * 500 = 3000) to get recent markets
        recent_cursor_num = max(cursor_num - 3000, 0)
        # Re-encode the offset cursor
        recent_cursor = base64.b64encode(str(recent_cursor_num).encode('utf-8')).decode('utf-8')
        
        print(f"Saving offset cursor: {cursor} ({cursor_num}) -> {recent_cursor} ({recent_cursor_num})")
        with open(CURSOR_FILE, 'w') as f:
            f.write(recent_cursor)
    except Exception as e:
        print(f"Error calculating cursor offset, saving original: {e}")
        with open(CURSOR_FILE, 'w') as f:
            f.write(cursor)

def GetRecentCursor():
    """Get recent cursor position (6 blocks behind estimated current end)"""
    # Based on observed data: current end ~64500, so 6 blocks behind = ~61500
    # Update this occasionally if the API advances significantly
    recent_cursor = 'NjE1MDA='  # 61500 (6 blocks behind current ~64500)
    print(f"Using fixed recent cursor: {recent_cursor} (61500)")
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

# TODO: implement api-calls for 'gamma-market' api
# https://docs.polymarket.com/#gamma-markets-api

def ConstructTimeseries(history: json):
    timeseries = [{ 
          'price': point['p'], 
          'timestamp': point['t'], 
          'date': DateFromTimestamp(point['t'])
        } for point in history]
    return timeseries

def LoadTimeseriesData():
    global TIMESERIES_CACHE
    if TIMESERIES_CACHE is not None: return TIMESERIES_CACHE;
    TIMESERIES_CACHE = {}
    print("loading timeseries cache...")
    cache_files = TIMESERIES_DIR.glob('*')
    for filepath in cache_files:
        with open(filepath, mode='r', encoding='utf-8') as file_data:
            TIMESERIES_CACHE[int(filepath.name)] = json.load(file_data)
    print(f"loaded timeseries cache ({len(TIMESERIES_CACHE)} entries)")
    return TIMESERIES_CACHE

# TODO: record the fidelity of cached timeseries
def SaveTimeseriesToCache(token_id:int, timeseries):
    # print(f"caching {token_id}")
    global CACHE_MISS_COUNT; CACHE_MISS_COUNT += 1
    cache_file = TIMESERIES_DIR / str(token_id)
    with open(cache_file, mode='w', encoding='utf-8') as new_cache_file:
        json.dump(timeseries, new_cache_file, indent=2)
    return

# default/min fidelity is 10 minutes. There's a cutoff at 12-hours where the timeseries will go back much further (~2-years further)
def GetPriceHistory(token_id:int, fidelity:int = 10, fidelity_hours:int = -1, load_cache=True):
    global TIMESERIES_CACHE
    # TODO: do not ignore the fidelity of cached timeseries
    if load_cache:
        LoadTimeseriesData()
        if token_id in TIMESERIES_CACHE: return TIMESERIES_CACHE[token_id];
    if fidelity_hours != -1: fidelity = fidelity_hours * 60
    response = requests.get(f"{host}/prices-history", params={"market": token_id, "interval": "max", "fidelity": fidelity})
    if not response.status_code == 200:
        print(f"error fetching price history: response {response.status_code}"); return None
    history = json.loads(response.content)['history']
    timeseries = ConstructTimeseries(history)
    # timeseries was not already cached; save it.
    SaveTimeseriesToCache(token_id, timeseries)
    return timeseries

# Keep pumping
def GetOrderbook(token_ids:list):
    books = client.get_order_books(token_ids)
    return books

# regex for searching "tags" including "All" in open_markets(_nl).json
# "tags"[ \t]*:[ \t]*\[[ \t\n\r]*(?:[^"\]]*"[^"]*"[ \t\n\r]*,[ \t\n\r]*)*"All"[ \t\n\r]*(?:,[ \t\n\r]*[^"\]]*"[^"]*")*[ \t\n\r]*\]
# KDE's regex engine seems to be bugged and '\s' (whitespace) doesn't include newline, which is why '[ \t\n\r]' is used instead
# not all markets have "All" tag? and occasionally "tags" is null (test markets)

def FetchMarkets(next_cursor=None, recent_only=True):
    # Initialize variables for pagination
    markets_list = []
    limit = 10
    i = 0
    last_valid_cursor = None
    
    # If recent_only is True, dynamically discover recent cursor position
    if recent_only and next_cursor is None:
        next_cursor = GetRecentCursor()
        print(f"Starting recent_only from: {next_cursor}")
    
    # Fetch all available markets using pagination
    #while i < limit:
    while True:
        try:
            print(f"Fetching markets with next_cursor: {next_cursor}")
            response = client.get_markets(next_cursor=next_cursor) if next_cursor else client.get_markets()
            
            #print(f"API Response: {json.dumps(response, indent=2)}")
            
            if 'data' not in response:
                print("No data found in response.")
                break
            
            markets_list.extend(response['data'])
            last_valid_cursor = next_cursor  # Save current cursor before getting next one
            next_cursor = response.get("next_cursor")
            
            if not next_cursor:
                break
            
            # Stop before LTE= endmarker as it's not a valid cursor
            if next_cursor.startswith('LTE='):
                print(f"Reached endmarker cursor {next_cursor}, stopping pagination")
                next_cursor = None
                break
        except Exception as e:
            print(f"Exception occurred: {e}")
            print(f"Exception details: {e.__class__.__name__}")
            print(f"Error message: {e.args}")
            break
        i += 1
    
    # No need to save cursor since we auto-discover it each time
    
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
    
    # Collect all token IDs for batch volume request
    all_token_ids = []
    for market in filtered_data:
        market["lines"] = [ token['outcome'] + ': ' + str(token['price']*100) + '%' for token in market["tokens"] ]
        market["token_ids"] = [token['token_id'] for token in market['tokens']]
        all_token_ids.extend(market["token_ids"])
    
    print(f"Fetching volume data for {len(all_token_ids)} tokens in batch...")
    # Single batch request for all token volumes
    volume_map = get_market_volume_batch(all_token_ids)
    
    print("Processing volume data for markets...")
    print(f"Volume map keys: {list(volume_map.keys())[:5]}...")  # Show first 5 keys
    
    for i, market in enumerate(filtered_data):
        # Get volume data for each token from the batch result
        volume_data = []
        total_volume = 0
        total_volume_24hr = 0
        total_liquidity = 0
        
        print(f"Processing market {i}: {market['question'][:50]}...")
        print(f"Market token_ids: {market['token_ids']}")
        
        for token_id in market["token_ids"]:
            print(f"Looking up token_id: {token_id}")
            print(f"Token in volume_map: {token_id in volume_map}")
            vol_data = volume_map.get(token_id, {'volume': 0, 'volume_24hr': 0, 'liquidity': 0, 'volume_formatted': 0})
            print(f"Vol data for {token_id}: {vol_data}")
            volume_data.append(vol_data)
            total_volume += float(vol_data['volume']) if vol_data['volume'] else 0
            total_volume_24hr += float(vol_data['volume_24hr']) if vol_data['volume_24hr'] else 0
            total_liquidity += float(vol_data['liquidity']) if vol_data['liquidity'] else 0
        
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
    cwd = pathlib.Path.cwd()
    savedir = cwd / "PolyMarketHTML"
    if not savedir.exists(): savedir.mkdir()
    html_file = savedir / "ticker_items.html"
    
    # divs = [ 
    #     '            <div class="ticker__item">\n' + 
    #     f'                 <strong>{market["description"]}</strong>: { market["lines"][0] + ' ' + market["lines"][1] }\n' +
    #     '            </div>\n'
    #     for market in markets_list
    # ]
    
    # '            { title: ' + f'\" {market["description"].splitlines()[0] } \"' + f'{ market["lines"][0] + " " + market["lines"][1] }' + ' " },\n'
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


# TODO: figure out how to return/store all this info
def GetAllTags(markets:list[dict]):
    all_tags = [market['tags'] for market in markets] # list[list[str]]
    tag_set = sorted({ tag
        for taglist in all_tags
        for tag in taglist
    })
    # relates all tags that appear together across all markets 
    tag_relations = {
        tag: { related
            for taglist in all_tags if tag in taglist
            for related in taglist if (tag != related)
        } for tag in tag_set
    }
    
    # for each tag, counts occurrences of tags appearing alongside it across all markets 
    tag_relations_counted = {
        tag: Counter([
            related
            for taglist in all_tags if tag in taglist
            for related in taglist if (tag != related) # you can remove this exclusion (allowing tag to count itself) to get a count of markets with this tag
        ]) for tag in tag_set
    }
    
    # manual count of markets including each tag (alternatively, remove the self-exclusion in 'tag_relations_counted' comprehension)
    tag_occurrence_count = {
        tag: len([taglist for taglist in all_tags if tag in taglist])
        for tag in tag_set
    }
    # verifying equivalence between occurrence counting methods (when 'tag_relations_counted' does not exclude self-counts)
    # self_count = { tag: tag_relations_counted[tag].get(tag) for tag in tag_set }
    # mismatched = {
    #     tag: [self_count[tag], tag_occurrence_count[tag]]
    #     for tag in tag_set if (self_count[tag] != tag_occurrence_count[tag])
    # }
    
    # sorted from most to least common
    tags_ordered_by_frequency = {
        tag:count for (tag,count) in reversed(sorted(tag_occurrence_count.items(), key=lambda item: item[1]))
    }
    # top-five most common related tags for top-five most common tags
    top_five_relations = {
        tag: [t for t in reversed(sorted(tag_relations_counted[tag].items(), key=lambda item: item[1]))][0:5]
        for tag in [t for t in tags_ordered_by_frequency.keys()][0:5]
    }
    return tag_set


if __name__ == "__main__":
    markets = LoadJsonDump()
    GetAllTags(markets)
    print(markets)
    fetch_and_process_markets()
    
    markets_list = FetchMarkets()
    filtered = FilterData(markets_list)
    # Debugging step: Print out the raw data
    print("Raw Market Data:")
    print(json.dumps(markets_list, indent=2))
    print(json.dumps(filtered, indent=2))
    WriteJsonDump(filtered)

    # market["active"] is always True?? Even when it's closed.
    print(f"\n\n returned {len(markets_list)} markets \n")
    open_markets = [market for market in markets_list if ((market["active"] is True) and (not market["closed"]))]
    print(f"#open_markets: {len(open_markets)}")
    print("\n\n")
    pprint([market["market_slug"] for market in open_markets])
    
    GenerateHTML(filtered)
    SaveToCSV(filtered, "all")



