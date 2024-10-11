import csv
import json
from pprint import pprint
from py_clob_client.client import ClobClient
from mmKEY import pmkey
import pathlib

# Polymarket CLOB API host
host = "https://clob.polymarket.com"
chain_id = 137  # Polygon Mainnet

# Initialize the client with API key
client = ClobClient(
    host,
    key=pmkey,
    chain_id=chain_id
)

# Initialize variables for pagination
markets_list = []
next_cursor = None

limit = 10
i = 0

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
        next_cursor = response.get("next_cursor")
        
        if not next_cursor:
            break
    except Exception as e:
        print(f"Exception occurred: {e}")
        print(f"Exception details: {e.__class__.__name__}")
        print(f"Error message: {e.args}")
        break
    i += 1

# Debugging step: Print out the raw data
#print("Raw Market Data:")
#print(json.dumps(markets_list, indent=2))

# market["active"] is always True?? Even when it's closed.
print(f"\n\n returned {len(markets_list)} markets \n")
open_markets = [market for market in markets_list if ((market["active"] is True) and (not market["closed"]))]
print(f"#open_markets: {len(open_markets)}")
print("\n\n")
pprint([market["market_slug"] for market in open_markets])

wanted_fields = ("question", "description", "tokens")
# there are always two tokens. https://docs.polymarket.com/#get-markets
# "outcome" is the line the token represents. Usually "Yes/No", but sometimes not.
# (which-party-will-win-the-2024-united-states-presidential-election: "Democratic"/"Republican")
# 'winner' will always be false for open markets

def FilterData(markets) -> list[dict]:
    filtered_data = [
        { field: market[field] for field in wanted_fields }
        for market in markets
    ]
    for market in filtered_data:
        market["lines"] = [ token['outcome'] + ': ' + str(token['price']*100) + '%' for token in market["tokens"] ]
        del market["tokens"]
    return filtered_data

filtered = FilterData(markets_list)

with open((pathlib.Path.cwd() / "PMdump.json"), "w") as json_file:
    json.dump(filtered, json_file, indent=2)
    print("wrote PMdump.json")


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

GenerateHTML(filtered)

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


SaveToCSV(filtered, "all")



