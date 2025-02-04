import requests
from Creds import ODDS_API_KEY
import time
import pathlib
import json

# credit info can be checked in 'response.headers' dict
# 'x-requests-last':   The usage cost of the last API call
# 'x-requests-used':    total usage credits used since the last quota reset
# 'x-requests-remaining': total usage credits remaining until the quota resets

# The usage quota cost = [number of markets specified] x [number of regions specified]
# For historical-odds requests:
#   cost = 10 x [number of markets specified] x [number of regions specified]
# Responses with empty data do not count towards the usage quota.
# For examples of usage quota costs, see https://the-odds-api.com/liveapi/guides/v4/#usage-quota-costs
# bookmaker lists per region: https://the-odds-api.com/sports-odds-data/bookmaker-apis.html

# rate limit (status 429) is 30/s
# https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html#exceeded-freq-limit
# TODO: get comprehensive list of available markets for event queries
# https://the-odds-api.com/sports-odds-data/betting-markets.html

# oddsAPI docs https://the-odds-api.com/liveapi/guides/v4/#endpoint-8
# TODO: Implement historical odds, opportunity for some sneaky graph action

# Cache for sports data to avoid repeated API calls
SPORTS_CACHE = None
CACHE_EXPIRY = 24 * 60 * 60

def get_active_sports():
    global SPORTS_CACHE
    if SPORTS_CACHE and (time.time() - SPORTS_CACHE['timestamp']) < CACHE_EXPIRY:
        return SPORTS_CACHE['data']

    response = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": ODDS_API_KEY}
    )
    if response.status_code != 200:
        print(f"Failed to get sports: status_code {response.status_code}")
        return []

    sports = response.json()
    active_sports = [sport for sport in sports if sport['active']]
    SPORTS_CACHE = {'data': active_sports, 'timestamp': time.time()}
    return active_sports

def get_all_odds(sport_key, regions="us,us2,eu", markets="h2h,totals,spreads"):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american"
    }
    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"Error fetching odds for {sport_key}: {response.status_code}")
        return None
    return response.json()

def outcomes_match(p_outcome, b_outcome):
    name_match = p_outcome['name'] == b_outcome['name']
    p_point = p_outcome.get('point')
    b_point = b_outcome.get('point')
    if p_point is not None and b_point is not None:
        return name_match and p_point == b_point
    else:
        return name_match and (p_point is None and b_point is None)

def american_to_decimal(american_odds:int):
    if american_odds > 0: return (american_odds / 100) + 1
    else: return (100 / abs(american_odds)) + 1

# from oddsApi github example (utilities.py)
def american_to_decimal_ref(am_odd: float) -> float:
    """ Convert American odds to decimal odds """
    if am_odd < 0: odd = 1 - 100 * 1.0 / am_odd
    else: odd = 1 + am_odd * 1.0 / 100
    return odd

# from oddsApi github example (utilities.py)
def decimal_to_american(odd: float) -> int:
    if odd == 1: return 0  # Decimal odds of 1 have no payout and are not bettable
    if odd < 2: return int(round(100 / (1 - odd), 0))
    else: return int(round(100 * (odd - 1), 0))


def ImpliedProbability(odds:int|float, isAmerican:bool=True):
    if odds == 0: print("[ImpliedProb.Calc] ERROR: odds is zero"); return 0
    if not isAmerican: return 100/odds;
    if (odds > 0): return 10000/(odds+100);
    else: odds = -odds; return (100*odds)/(odds+100);

impliedProb_testcases = [x for x in zip(*[
    (f"+{american_odds}: {ImpliedProbability(american_odds):.2f}%", 
     f"-{american_odds}: {ImpliedProbability(-american_odds):.2f}%")
    for american_odds in range(100, 1000, 25)
])]

def DecimalRange(start:float, end:float, stride:float):
    as_int = [int(F*100) for F in (start, end, stride)]
    return [(I/100) for I in range(*as_int)]

# negative decimal odds (or < 1) aren't valid
# impliedProb_testcases_dec = [x for x in zip(*[
#     (f"+{dec}: {ImpliedProbability(dec, False):.2f}%", 
#      f"-{dec}: {ImpliedProbability(-dec, False):.2f}%")
#     for dec in [*DecimalRange(2.00, 10.00, 0.25), *DecimalRange(2.00, 1.00, -0.1)]
# ])]

impliedProb_testcases_dec = [
    f"+{dec}: {ImpliedProbability(dec, False):.2f}%"
    for dec in [*DecimalRange(2.00, 10.00, 0.25), *DecimalRange(2.00, 1.00, -0.1)]
]

# vig is intrinsic to pairs of opposing lines; don't call this function on seperate bookmaker lines
def remove_vig(implied_probs:list[float]):
    """Removes the vig from implied probabilities using proportional method."""
    total_implied_prob = sum(implied_probs)
    return [p / total_implied_prob for p in implied_probs]


def MoreCorrectEV(outcome_pair: list[int], keep_vig=True):
    if len(outcome_pair) != 2: print(f"Error: cannot calc EV for {len(outcome_pair)}"); return 0.0;
    decimals = [american_to_decimal(outcome) for outcome in outcome_pair]
    probs = [(1/dec) for dec in decimals]
    novig = remove_vig(probs)
    if keep_vig: novig = probs
    
    #TODO: is this correct formula for EV calc?
    ev = ((decimals[1] - 1) * novig[1]) - (1 - novig[0])
    return ev
    # testing possible formulas for EV 
    # return [
    #     ((decimals[1] - 1) * novig[0]) - (1 - novig[0]),
    #     ((decimals[1] - 1) * novig[0]) - (1 - novig[1]),
    #     ((decimals[1] - 1) * novig[1]) - (1 - novig[0]),
    #     ((decimals[1] - 1) * novig[1]) - (1 - novig[1])
    # ]


# TODO: look into doing this calc with no-vig odds
def ExpectedCalc(outcome_pair):
    probs = [ImpliedProbability(outcome) for outcome in outcome_pair]
    return abs(probs[0] - probs[1])


# markets = [book['markets'] for book in bookmakers]
    # outcomes = zip(market['outcomes'] for market in markets)
    # prices = [[l['price'] for l in o] for o in outcomes]
# bookmakers = [bookmakers_all[0], bookmakers_all[6]]

#TODO: refactor this so you're not passing two strings just to print them
def CompareBooks(bookmakers, sportname:str, game_title:str):
    market_map = [{
    "bookmaker": bm['title'],
    **bm['markets'][0] # 'markets' is always a list[dict] of length 1?
    } for bm in bookmakers]
    
    market_map_byteam = [
        {
            'name': outcome['name'],
            'market_type': market_dict['key'],
            'bookmaker': market_dict['bookmaker'],
            'price': outcome['price'],
        }
        for market_dict in market_map
        for outcome in market_dict['outcomes']
    ]
    
    outcome_map = {
        entry['name']: {
            key: [d2[key] for d2 in market_map_byteam if d2['name'] == entry['name']]
            for key in entry.keys()
        }
        for entry in market_map_byteam
        for key in entry.keys()
    }
    
    calculated_EVs = {
        outcome: MoreCorrectEV(game['price'])
        for (outcome, game) in outcome_map.items()
        if len(game['price']) == 2
    }
    
    print(sportname)
    print(game_title)
    print(" | ".join([book['title'] for book in bookmakers]))
    for (side, entry) in outcome_map.items():
        print(f"market_type: {entry['market_type'][0]}")
        print(f"  {side}: ")
        for (bm, price) in zip(entry['bookmaker'], entry['price']):
            print(f"    {bm}: {price}")
    print("  EV:")
    for (line, ev) in calculated_EVs.items():
        print(f"    {line}: {ev:.2f}%")
    print("")
    
    return {
        "books": [book['title'] for book in bookmakers],
        "EV": calculated_EVs,
    }


def DoEverything(sport):
    savedir = pathlib.Path.cwd() / "savedata"
    start_time = time.time()
    games = get_all_odds(sport)
    
    dumpfile = savedir / f"{sport}_{int(start_time)}.json"
    json.dump(games, dumpfile.open('w'), indent=2)
    
    results = []
    for game in games:
        print(f"Sport: {sport}")
        game_title = f"{game['home_team']} vs {game['away_team']}"
        print(game_title)
        bookmakers_all = game['bookmakers']
         
        # TODO: do something even if pinnacle isn't in there
        ###########################################
        has_pinnacle = ('pinnacle' in [book['key'] for book in bookmakers_all])
        if not has_pinnacle:
            print("Ignoring game because pinnacle line isn't available")
            continue
        
        pinnacle_entry = [book for book in bookmakers_all if book['key'] == 'pinnacle'][0]
        other_books = [book for book in bookmakers_all if book['key'] != 'pinnacle']
        
        comparisons = [
            CompareBooks([pinnacle_entry, other_book], sport, game_title)
            for other_book in other_books
        ]
        results.append(comparisons)
    
    json.dumps(results, indent=2)
    
    savefile = savedir / f"pinnacle_comparisons_{sport}_{int(start_time)}.json"
    json.dump(results, savefile.open('w'), indent=2)
    
    print(f"\nProcessing time: {time.time() - start_time:.2f} seconds")


if __name__ == "__main__":
    # sports = ['icehockey_nhl', 'tennis_atp']
    # for sport in sports:
    #     DoEverything(sport)
    DoEverything('icehockey_nhl')
    print("\n\n done \n\n")
