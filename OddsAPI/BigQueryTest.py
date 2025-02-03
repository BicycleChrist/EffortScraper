import requests
from Creds import ODDS_API_KEY
import asyncio
import aiohttp
import time

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
# TODO: YOU FUCKED UP EV CALC AGAIN FIX IT, AI doesnt understand probability
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

async def get_all_odds(session, sport_key, regions="us,us2,eu", markets="h2h,totals,spreads"):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american"
    }
    async with session.get(url, params=params) as response:
        if response.status != 200:
            print(f"Error fetching odds for {sport_key}: {response.status}")
            return None
        return await response.json()

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


def calculate_ev_for_pair(p_outcome, b_outcome):
    pinnacle_decimal = american_to_decimal(p_outcome['price'])
    bookmaker_decimal = american_to_decimal(b_outcome['price'])
    
    # Convert to implied probabilities
    p_prob = 1 / pinnacle_decimal
    b_prob = 1 / bookmaker_decimal
    # TODO: why is b_prob unused
    
    # Remove vig properly
    fair_probs = remove_vig([p_prob, 1 - p_prob])
    fair_pinnacle_prob = fair_probs[0]
    # TODO: why is 2nd number returned by 'remove_vig' ignored?
    
    ev = ((bookmaker_decimal - 1) * fair_pinnacle_prob) - (1 - fair_pinnacle_prob)
    return ev


def MoreCorrectEV(outcome_pair: list[int], keep_vig=True):
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


# TODO: preserve '"last_update":' field of each bookmakers' 'markets' data
def calculate_ev_old(pinnacle_odds, bookmaker_odds):
    if not pinnacle_odds:
        return (None, None, None)

    max_ev = -float('inf')
    best_p = None
    best_b = None
    for p_outcome in pinnacle_odds:
        for b_outcome in bookmaker_odds:
            if outcomes_match(p_outcome, b_outcome):
                current_ev = calculate_ev_for_pair(p_outcome, b_outcome)
                if current_ev > max_ev:
                    max_ev = current_ev
                    best_p = p_outcome
                    best_b = b_outcome
    if max_ev == -float('inf'):
        return None, None, None
    return (max_ev, best_p, best_b)


def calculate_ev(pinnacle_odds, bookmaker_odds):
    if not pinnacle_odds:
        return (None, None, None)

    max_ev = -float('inf')
    best_p = None
    best_b = None
    for p_outcome in pinnacle_odds:
        for b_outcome in bookmaker_odds:
            if outcomes_match(p_outcome, b_outcome):
                current_ev = MoreCorrectEV(p_outcome, b_outcome)
                if current_ev > max_ev:
                    max_ev = current_ev
                    best_p = p_outcome
                    best_b = b_outcome
    if max_ev == -float('inf'):
        return None, None, None
    return (max_ev, best_p, best_b)


async def find_ev_opportunities(threshold=0.015):
    sports = get_active_sports()
    popular_sports = [sport for sport in sports if sport['key'] in [
        'basketball_nba', 'americanfootball_nfl', 'soccer_epl', 'icehockey_nhl'
        'baseball_mlb', 'aussierules_afl', 'tennis_atp', 'golf_pga',
        'mma_mixed_martial_arts', 'boxing', 'soccer_uefa_champs_league',
        'soccer_la_liga', 'soccer_ligue_1', 'soccer_bundesliga',
        'soccer_serie_a', 'soccer_mls', 'cricket_ipl', 'rugbyleague_nrl',
        #'rugby_union', 'soccer_fifa_world_cup', 'soccer_china_superleague', 'soccer_denmark_superliga',
        #'cricket_big_bash', 'cricket_caribbean_premier_league', 'cricket_icc_world_cup',
        #'basketball_euroleague', 'basketball_wnba', 'basketball_ncaab', 'basketball_nbl'
    ]]

#TODO: Create logic for establishing which leagues are in season, API hopefully can do this for us
#popular_sports = [sport for sport in sports if sport['key'] in [
#        'basketball_nba', 'americanfootball_nfl', 'soccer_epl', 'icehockey_nhl'
#        'baseball_mlb', 'aussierules_afl', 'tennis_atp', 'golf_pga',
#        'mma_mixed_martial_arts', 'boxing', 'soccer_uefa_champs_league',
#        'soccer_la_liga', 'soccer_ligue_1', 'soccer_bundesliga',
#        'soccer_serie_a', 'soccer_mls', 'cricket_ipl', 'rugbyleague_nrl',
#        'rugby_union', 'soccer_fifa_world_cup', 'soccer_china_superleague', 'soccer_denmark_superliga',
#        'cricket_big_bash', 'cricket_caribbean_premier_league', 'cricket_icc_world_cup',
#        'basketball_euroleague', 'basketball_wnba', 'basketball_ncaab', 'basketball_nbl'
#    ]]


    opportunities = []
    EV_results_old = []
    EV_results = []

    async with aiohttp.ClientSession() as session:
        tasks = [get_all_odds(session, sport['key']) for sport in popular_sports]
        results = await asyncio.gather(*tasks)

        for sport, odds_data in zip(popular_sports, results):
            if not odds_data:
                continue

            for game in odds_data:
                pinnacle = next((b for b in game['bookmakers'] if b['title'].lower() == 'pinnacle'), None)
                if not pinnacle:
                    continue

                for bookmaker in game['bookmakers']:
                    if bookmaker == pinnacle:
                        continue

                    for market in bookmaker['markets']:
                        pinnacle_market = next((m for m in pinnacle['markets'] if m['key'] == market['key']), None)
                        if not pinnacle_market:
                            continue

                        EV_results_old.append(calculate_ev_old(pinnacle_market['outcomes'], market['outcomes']))
                        # TODO: figure out how to construct the parameters correctly here
                        #new_EV_results = calculate_ev(pinnacle_market['outcomes'], market['outcomes'])
                        #EV_results.append(new_EV_results)
                        #ev, p_outcome, b_outcome = new_EV_results
                        ev, p_outcome, b_outcome = EV_results_old[-1]
                        if ev and ev >= threshold:
                            opportunities.append({
                                'sport': sport['key'],
                                'teams': f"{game['home_team']} vs {game['away_team']}",
                                'market': market['key'],
                                'bookmaker': bookmaker['title'],
                                'ev': f"{ev:.1%}",
                                'pinnacle_odds': p_outcome['price'],
                                'bookmaker_odds': b_outcome['price'],
                                'pinnacle_line': p_outcome.get('point', 'N/A'),
                                'bookmaker_line': b_outcome.get('point', 'N/A')
                            })
    
    return sorted(opportunities, key=lambda x: x['ev'], reverse=True), EV_results, EV_results_old, results

if __name__ == "__main__":
    start_time = time.time()
    loop = asyncio.get_event_loop()
    opportunities, EV_results, EV_results_old, results = loop.run_until_complete(find_ev_opportunities())

    print("\n+EV Opportunities:")
    print("=" * 50)
    for opp in opportunities:
        print(f"\nSport: {opp['sport']}")
        print(f"Teams: {opp['teams']}")
        print(f"Market: {opp['market']}")
        print(f"Bookmaker: {opp['bookmaker']}")
        print(f"Pinnacle Line: {opp['pinnacle_line']}  Odds: {opp['pinnacle_odds']}")
        print(f"Bookmaker Line: {opp['bookmaker_line']}  Odds: {opp['bookmaker_odds']}")
        print(f"EV: {opp['ev']}")
    
    print(f"\nProcessing time: {time.time() - start_time:.2f} seconds")
    print("\n\nEV results: ")
    print(EV_results)
    print("\n\nEV results (old): ")
    print(EV_results_old)
    print("\n\nresults: ")
    print(results)

