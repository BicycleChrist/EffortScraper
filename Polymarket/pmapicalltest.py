ximport csv
import json
from collections import Counter
from pprint import pprint
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib
import requests
from datetime import datetime

def GetCurrentTimestamp(): return datetime.now().timestamp()
def DateFromTimestamp(timestamp: int) -> str: return datetime.fromtimestamp(timestamp).date().isoformat()

# Polymarket CLOB API host
host = "https://clob.polymarket.com"
chain_id = 137  # Polygon Mainnet

# Gamma API host for liquidity data
gamma_host = "https://gamma-api.polymarket.com"

# Initialize the client with API key
client = ClobClient(
    host,
    key=pmkey,
    chain_id=chain_id
)

def GetActiveCLOBMarkets():
    """
    Get CLOB markets and filter for ACTIVE ones only (end_date > today)
    """
    print("=== GETTING ACTIVE CLOB MARKETS ===")

    try:
        # Get CLOB markets
        response = client.get_markets()
        all_clob_markets = response.get('data', [])

        # Get more pages to ensure we have enough
        next_cursor = response.get("next_cursor")
        pages_fetched = 0
        while next_cursor and pages_fetched < 10:
            response = client.get_markets(next_cursor=next_cursor)
            all_clob_markets.extend(response.get('data', []))
            next_cursor = response.get("next_cursor")
            pages_fetched += 1

        print(f"Fetched {len(all_clob_markets)} total CLOB markets")

        # Filter for ACTIVE markets only (end_date > today)
        today = datetime.now()
        active_markets = []

        for market in all_clob_markets:
            end_date_str = market.get('end_date_iso')
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    if end_date > today:  # Future end date = active
                        active_markets.append(market)
                except:
                    continue

        print(f"Filtered to {len(active_markets)} ACTIVE markets (end date > {today.strftime('%Y-%m-%d')})")

        # Show sample active markets
        print("\nSample active markets:")
        for i, market in enumerate(active_markets[:5]):
            question = market.get('question', 'N/A')[:50]
            end_date = market.get('end_date_iso', 'N/A')[:10]
            condition_id = market.get('condition_id', 'N/A')[:10] + "..."
            print(f"  {i+1}. {question}... (ends {end_date}) [{condition_id}]")

        return active_markets

    except Exception as e:
        print(f"Error getting active CLOB markets: {e}")
        return []

def GetAllGammaMarkets():
    """
    Get all Gamma markets (they should all be active)
    """
    print("\n=== GETTING GAMMA MARKETS ===")

    try:
        params = {
            'limit': 100,
            'active': True,
            'closed': False,
            'archived': False
        }

        all_gamma_markets = []
        offset = 0

        while True:
            params['offset'] = offset
            response = requests.get(f"{gamma_host}/markets", params=params, timeout=30)

            if response.status_code == 200:
                batch = response.json()
                if not batch:
                    break
                all_gamma_markets.extend(batch)

                if len(batch) < 100:
                    break
                offset += 100

                # Safety limit
                if len(all_gamma_markets) > 5000:
                    break
            else:
                break

        print(f"Fetched {len(all_gamma_markets)} Gamma markets")

        # Show sample gamma markets
        print("\nSample gamma markets:")
        for i, market in enumerate(all_gamma_markets[:5]):
            question = market.get('question', 'N/A')[:50]
            end_date = market.get('endDate', 'N/A')[:10]
            condition_id = market.get('conditionId', 'N/A')[:10] + "..."
            liquidity = float(market.get('liquidity', 0))
            print(f"  {i+1}. {question}... (ends {end_date}) [{condition_id}] ${liquidity:,.0f}")

        return all_gamma_markets

    except Exception as e:
        print(f"Error getting Gamma markets: {e}")
        return []

def MatchActiveMarkets():
    """
    Match active CLOB markets with Gamma markets and add liquidity data
    """
    print("\n=== MATCHING ACTIVE MARKETS ===")

    # Get active markets from both APIs
    active_clob_markets = GetActiveCLOBMarkets()
    gamma_markets = GetAllGammaMarkets()

    if not active_clob_markets:
        print("❌ No active CLOB markets found!")
        return []

    if not gamma_markets:
        print("❌ No Gamma markets found!")
        return []

    # Create lookup by condition_id
    gamma_by_condition = {}
    for market in gamma_markets:
        condition_id = market.get('conditionId')
        if condition_id:
            gamma_by_condition[condition_id] = market

    print(f"Created Gamma lookup with {len(gamma_by_condition)} condition IDs")

    # Match and enhance
    enhanced_markets = []
    matches_found = 0
    liquidity_found = 0

    for clob_market in active_clob_markets:
        condition_id = clob_market.get('condition_id')

        # Start with CLOB market data
        enhanced_market = clob_market.copy()
        enhanced_market['liquidity'] = 0
        enhanced_market['volume'] = 0
        enhanced_market['start_date'] = None
        enhanced_market['end_date'] = None

        # Try to find matching Gamma market
        if condition_id and condition_id in gamma_by_condition:
            gamma_market = gamma_by_condition[condition_id]

            # Add Gamma liquidity data
            liquidity = float(gamma_market.get('liquidity', 0))
            volume = float(gamma_market.get('volume', 0))

            enhanced_market['liquidity'] = liquidity
            enhanced_market['volume'] = volume
            enhanced_market['start_date'] = gamma_market.get('startDate')
            enhanced_market['end_date'] = gamma_market.get('endDate')

            matches_found += 1

            if liquidity > 0:
                liquidity_found += 1
                question = clob_market.get('question', 'N/A')[:50]
                print(f"  ✅ MATCH: {question}... -> ${liquidity:,.0f} liquidity")

        enhanced_markets.append(enhanced_market)

    print(f"\nMATCHING RESULTS:")
    print(f"  Active CLOB markets: {len(active_clob_markets)}")
    print(f"  Condition ID matches: {matches_found}")
    print(f"  Markets with liquidity: {liquidity_found}")

    return enhanced_markets

def FilterDataWithLiquidity(markets) -> list[dict]:
    """
    Filter and format the enhanced markets data
    """
    wanted_fields = ("question", "description", "tokens", "question_id", "condition_id", "tags",
                    "liquidity", "volume", "start_date", "end_date")

    # Filter for open/active markets
    open_markets = [market for market in markets if (
        (market.get("active") is True) and
        (not market.get("closed", False)) and
        (not market.get("archived", False))
    )]

    filtered_data = []
    for market in open_markets:
        filtered_market = {}

        # Copy wanted fields
        for field in wanted_fields:
            filtered_market[field] = market.get(field, None)

        # Process tokens and create lines
        if 'tokens' in market and market['tokens']:
            filtered_market["lines"] = [
                token['outcome'] + ': ' + str(token['price']*100) + '%'
                for token in market["tokens"]
            ]
            filtered_market["token_ids"] = [token['token_id'] for token in market['tokens']]

        # Ensure tags is always a list
        if filtered_market["tags"] is None:
            filtered_market["tags"] = []

        # Remove the original tokens field
        if 'tokens' in filtered_market:
            del filtered_market["tokens"]

        filtered_data.append(filtered_market)

    return filtered_data

def WriteJsonDump(data: list[dict], filename="PMdump_with_liquidity"):
    with open((pathlib.Path.cwd() / f"{filename}.json"), "w") as json_file:
        json.dump(data, json_file, indent=2)
        print(f"Wrote {filename}.json with {len(data)} markets")
    return

def SaveToCSVWithLiquidity(marketsdata, filename):
    """
    Enhanced CSV export that includes liquidity data
    """
    cwd = pathlib.Path.cwd()
    savedir = cwd / "PolyMarketCSV"
    if not savedir.exists():
        savedir.mkdir()
    csv_file = savedir/(filename + ".csv")

    csv_columns = [
        'question', 'description', 'question_id', 'condition_id', 'tags',
        'liquidity', 'volume', 'start_date', 'end_date', 'lines', 'token_ids'
    ]

    try:
        with open(csv_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
            writer.writeheader()

            for market in marketsdata:
                row = {}
                for key in csv_columns:
                    value = market.get(key, 'N/A')
                    if isinstance(value, list):
                        row[key] = ', '.join([str(item) for item in value])
                    else:
                        row[key] = value
                writer.writerow(row)

        print(f"Data with liquidity written to {csv_file}")
    except IOError as e:
        print(f"Error writing to CSV: {e}")

if __name__ == "__main__":
    print("=== ACTIVE MARKETS WITH LIQUIDITY ===")

    # Match active markets and add liquidity data
    enhanced_markets = MatchActiveMarkets()

    if enhanced_markets:
        # Filter and process the data
        filtered_data = FilterDataWithLiquidity(enhanced_markets)

        # Sort by liquidity (highest first)
        filtered_data.sort(key=lambda x: x.get('liquidity', 0), reverse=True)

        # Save results
        WriteJsonDump(filtered_data, "active_markets_with_liquidity")
        SaveToCSVWithLiquidity(filtered_data, "active_markets_with_liquidity")

        # Analysis
        total_liquidity = sum(m.get('liquidity', 0) for m in filtered_data)
        markets_with_liquidity = [m for m in filtered_data if m.get('liquidity', 0) > 0]

        print(f"\n=== FINAL RESULTS ===")
        print(f"Total active markets processed: {len(filtered_data)}")
        print(f"Markets with liquidity: {len(markets_with_liquidity)}")
        print(f"Total liquidity: ${total_liquidity:,.0f}")

        # Show top markets by liquidity
        print(f"\nTop 10 markets by liquidity:")
        for i, market in enumerate(markets_with_liquidity[:10], 1):
            liquidity = market.get('liquidity', 0)
            volume = market.get('volume', 0)
            question = market.get('question', 'N/A')[:60] + "..." if len(market.get('question', '')) > 60 else market.get('question', 'N/A')
            print(f"{i}. ${liquidity:,.0f} (Vol: ${volume:,.0f}) - {question}")

    else:
        print("❌ No enhanced markets created!")
