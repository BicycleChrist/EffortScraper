
# Get event IDs for a sport
params={
    #"apiKey": API_KEY,
    "sport": "basketball_nba",
}

response = requests.get(
    f"https://api.the-odds-api.com/v4/sports/{params["sport"]}/events",
    params
)

events = response.json()
fields = ["home_team", "away_team"]
matchups = [ { field: event[field] for field in fields } for event in events ]
matchup_strings = [" vs ".join(matchup.values()) for matchup in matchups]

# ----------------------------------------------------------------------------------

# selecting last event for example query
event = events[-1]

# event query
# TODO: get comprehensive list of available markets for event queries
# https://the-odds-api.com/sports-odds-data/betting-markets.html
params = {
    #"apiKey": API_KEY,
    "sport": "basketball_nba",
    "eventId": event['id'],
    "regions": 'us,us2',
    "markets": 'spreads,totals,alternate_spreads,alternate_totals',
}
response = requests.get(
    f"https://api.the-odds-api.com/v4/sports/{params['sport']}/events/{event['id']}/odds",
    params
)
# the most optimal way to make queries would be to search one region/market at a time;
# because the number of regions/markets are multipliers on the cost, regardless of number of results,
# but the query is free if there are no results.
# So querying 10 markets would cost 10 credits even if 9 of those markets have no results,
# whereas 10 individual queries (1 for each market) would have only cost 1 credit (9 empty results are free)

event_odds = response.json()
cost = response.headers['x-requests-last']; print(cost)
remaining = response.headers['x-requests-remaining']
results = event_odds['bookmakers'] # all other returned data is identical to the info in 'event'; this field is the only new data
# results is a list[dict]

#############################################################################
# Querying Prizepicks

import json
import pathlib
import requests
from Creds import ODDS_API_KEY

base_url = "https://api.the-odds-api.com/v4/sports"
# "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
credits_spent = 0

def MakeRequest(url, params):
    print(f"sending request to {url}")
    response = requests.get(url, params)
    headers = response.headers
    print(f"request cost: {headers['x-requests-last']} credits [{headers['x-requests-remaining']} remaining]")
    global credits_spent; credits_spent += int(headers['x-requests-last'])
    if response.status_code != 200:
        print(f"Request Failed: {response.status_code}")
        return None
    return response.json()


# doesn't cost any credits
def GetGameList(sport_key:str):
    url = f"{base_url}/{sport_key}/events"
    params = { "apiKey": ODDS_API_KEY }
    return MakeRequest(url, params)

def PrizepicksQuery(event_id, markets, regions:list[str]):
    url = f"{base_url}/basketball_nba/events/{event_id}/odds"
    global credits_spent; credits_spent = 0
    results = {}
    
    for region in regions:
        results[region] = {}
        params = {"apiKey": ODDS_API_KEY, "oddsFormat": "american", "regions": {region}}
        for market in markets:
            params['markets'] = market
            results[region][market] = MakeRequest(url, params)
    
    print(f"\n\ntotal credits spent: {credits_spent}")
    return results


def SavePrizepicks(event_id, data):
    savedir = pathlib.Path.cwd() / "savedata"
    dumpfile = savedir / f"prizepicks_dump_{event_id}.json"
    json.dump(data, dumpfile.open('w', encoding="utf-8"), indent=2)


# 'Demons and goblins are included under "_alternate" markets'
# The 'price' field is used to differentiate: 
#   'Demons have default odds, goblins have been assigned even odds (+100)' 

if __name__ == "__main__":
    playerprop_markets = [
        #"player_points",
        "player_rebounds",
        "player_assists",
        #"player_points_alternate",
    ]
    # regions = ["us_dfs"]
    regions = ["us_dfs","eu"]
    
    gamelist = GetGameList('basketball_nba')
    # get event_id from the gamelist
    event_id = "de674935dc38d314dc4b4b0ac5c024c4"
    results = PrizepicksQuery(event_id,playerprop_markets, regions)

# interesting repo: https://github.com/acandrewchow/bet-genius/blob/main/src/client.py
