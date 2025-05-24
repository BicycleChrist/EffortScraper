import requests
from Creds import ODDS_API_KEY
import json
import datetime
from datetime import datetime, timezone
import dateutil.parser
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
#TODO:Implement historical odds, opportunity for some sneaky graph action
def league_query():
    response = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": ODDS_API_KEY}
    )
    if response.status_code != 200:
        print(f"Failed to get sports: status_code {response.status_code}")
        print(response.text)
        return
    
    sports = response.json()
    sports_by_group = {
        group: [sport for sport in sports if (sport['group'] == group)]
        for group in { sport['group'] for sport in sports }
    }
    
    print("\nAvailable Sports and Leagues:")
    print("=" * 50)
    for group, sports_list in sorted(sports_by_group.items()):
        print(f"\n{group}:")
        print("-" * len(group))
        for sport in sorted(sports_list, key=lambda x: x['title']):
            print(f"• {sport['title']}")
            print(f"  - Key: {sport['key']}")
            print(f"  - Description: {sport['description']}")
    
    return sports_by_group


def odds_query(SPORT, REGIONS, MARKETS, ODDS_FORMAT, DATE_FORMAT):
    response = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT
        }
    )
    # Optional: Filter bookmakers if BOOKMAKERS is uncommented
    # if 'BOOKMAKERS' in globals():
    #     queried_books = [b.strip().lower() for b in BOOKMAKERS.split(',')]
    # else:
    #     queried_books = None  # No filter, show all bookmakers
    if response.status_code != 200:
        print(f"Error: {response.status_code}\n{response.text}")
        return None
    
    # Save the raw response to a JSON file
    raw_response = response.json()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"odds_response_{SPORT}_{timestamp}.json"
    
    with open(filename, 'w') as f:
        json.dump(raw_response, f, indent=4)
    
    print(f"Raw JSON response saved to {filename}")
    
    # Process the odds data to ensure 3-way moneylines are handled
    odds_data = raw_response
    for game in odds_data:
        for bookmaker in game['bookmakers']:
            for market in bookmaker['markets']:
                if market['key'] == 'h2h':
                    # Check if this is a 3-way moneyline (i.e., has a "Draw" outcome)
                    is_three_way = any(outcome['name'].lower() == 'draw' for outcome in market['outcomes'])
                    if is_three_way:
                        # Ensure the market is labeled as a 3-way moneyline
                        market['key'] = 'h2h_3way'
    
    return odds_data


def ParseOdds(odds):
    results = {}
    for game in odds:
        game_title = f"{game['home_team']} vs {game['away_team']}"
        print(f"\nGame: {game_title}")
        print(f"Start Time: {game['commence_time']}")
        results[game_title] = {
            "start_time": {game['commence_time']}
        }

        # Extract Pinnacle's odds first (for +EV calculation)
        pinnacle_odds = None
        for bookmaker in game['bookmakers']:
            if bookmaker['title'].lower() == "pinnacle":
                pinnacle_odds = bookmaker
                break

        if not pinnacle_odds:
            print("  \033[91mPinnacle odds not available - skipping EV calculation\033[0m")

        # Store Pinnacle's spreads for comparison
        pinnacle_spreads = {}
        if pinnacle_odds:
            for market in pinnacle_odds['markets']:
                if market['key'] == 'spreads':
                    for outcome in market['outcomes']:
                        team = outcome['name']
                        pinnacle_spreads[team] = {
                            'point': outcome['point'],
                            'price': outcome['price']
                        }
                    break
        
        results[game_title]['pinnacle_spreads'] = pinnacle_spreads 
        results[game_title]['pinnacle_odds'] = pinnacle_odds 
        
        # Track the best lines for each team
        best_lines = {
            game['home_team']: {'point': None, 'price': None, 'bookmaker': None},
            game['away_team']: {'point': None, 'price': None, 'bookmaker': None}
        }

        # Process all bookmakers
        print("\nOdds from Bookmakers (+EV Calculation vs Pinnacle):")
        for bookmaker in game['bookmakers']:
            bm_title = bookmaker['title'].lower()

            # Skip if BOOKMAKERS is defined and this bookmaker isn't in the list
            # if queried_books and bm_title not in queried_books:
            #     continue

            print(f"\n\033[1mBookmaker: {bookmaker['title']}\033[0m")
            for market in bookmaker['markets']:
                if market['key'] != 'spreads': # TODO: don't limit parsing to only spreads
                    continue

                for outcome in market['outcomes']:
                    team = outcome['name']
                    bm_point = outcome['point']
                    bm_price = outcome['price']

                    # Update best line for the team if this is better
                    if best_lines[team]['price'] is None or (bm_price > best_lines[team]['price']):
                        best_lines[team]['point'] = bm_point
                        best_lines[team]['price'] = bm_price
                        best_lines[team]['bookmaker'] = bookmaker['title']

                    # Calculate +EV if Pinnacle odds are available
                    if pinnacle_odds:
                        pinnacle_outcome = pinnacle_spreads.get(team)
                        if pinnacle_outcome and pinnacle_outcome['point'] == bm_point:
                            # Calculate implied probabilities
                            def implied_prob(odds):
                                if odds > 0:
                                    return 100 / (odds + 100)
                                else:
                                    return -odds / (-odds + 100)

                            pinnacle_prob = implied_prob(pinnacle_outcome['price'])

                            # Convert American odds to decimal
                            if bm_price > 0:
                                decimal_odds = (bm_price / 100) + 1
                            else:
                                decimal_odds = (100 / abs(bm_price)) + 1

                            # Calculate EV
                            ev = (decimal_odds * pinnacle_prob) - 1
                            ev_percent = ev * 100

                            # Format output
                            ev_color = "\033[92m" if ev > 0 else "\033[91m"
                            print(f"  {team} Spread: {bm_point} ({bm_price})")
                            print(f"    Pinnacle Reference: {pinnacle_outcome['price']}")
                            print(f"    +EV: {ev_color}{ev_percent:+.2f}%\033[0m")
                        else:
                            print(f"  {team} Spread: {bm_point} ({bm_price})")
                            print("    \033[91mNo matching Pinnacle line for EV calculation\033[0m")
                    else:
                        print(f"  {team} Spread: {bm_point} ({bm_price})")

        # Print the best lines for each team
        print("\n\033[1mBest Lines:\033[0m")
        for team, line in best_lines.items():
            if line['price'] is not None:
                print(f"  {team} Spread: {line['point']} ({line['price']}) at {line['bookmaker']}")
            else:
                print(f"  {team}: No lines available")
    # end of game for-loop
    return results


def scores_query(sport_key, days_from=None):
    """
    Fetch scores data for live game status detection
    Returns list of games with scores, completion status, and commence times
    """
    params = {
        "apiKey": ODDS_API_KEY,
        "dateFormat": "iso"
    }
    if days_from:
        params["daysFrom"] = days_from
    
    try:
        response = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
            params=params
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Error fetching scores: {response.status_code}")
            return []
            
    except Exception as e:
        print(f"Exception fetching scores: {e}")
        return []

def get_game_status(game_data, scores_data=None):
    """
    Determine game status from odds and optional scores data
    Args:
        game_data: dict with 'id' and 'commence_time' 
        scores_data: optional dict from scores API
    Returns:
        tuple: (status_text, is_live_bool, scores_text)
    """
    game_id = game_data.get('id')
    commence_time_str = game_data.get('commence_time', '')
    
    # Try to find this game in scores data first
    if scores_data:
        for score_game in scores_data:
            if score_game.get('id') == game_id:
                completed = score_game.get('completed', False)
                scores = score_game.get('scores')
                
                if completed:
                    return "Finished", False, ""
                elif scores and len(scores) > 0:
                    # Live game with scores
                    scores_text = " | ".join([f"{s['name']}: {s['score']}" for s in scores])
                    return "🔴 LIVE", True, scores_text
                else:
                    # Game exists in scores but no scores yet (pre-game)
                    break
    
    # Fallback to time-based detection
    try:
        commence_time = dateutil.parser.parse(commence_time_str)
        current_time = datetime.now(timezone.utc)
        time_diff = (current_time - commence_time).total_seconds()
        
        if time_diff < -1800:  # More than 30 min before
            return "Pre-Game", False, ""
        elif time_diff < 0:  # Less than 30 min before
            return "Starting Soon", False, ""
        elif time_diff < 14400:  # Less than 4 hours after (likely live)
            return "🔴 LIVE", True, ""
        else:  # More than 4 hours after (likely finished)
            return "Finished", False, ""
            
    except Exception as e:
        print(f"Error parsing commence time: {e}")
        return "Unknown", False, ""



if __name__ == "__main__":
    league_query()
    
    SPORT = "baseball_mlb"
    BOOKMAKERS = "draftkings,fanduel,pinnacle,bovada,betonline,betus,betrivers,lowvig"  # Optional filter
    REGIONS = "us,eu"  # Ensure 'eu' is included for Pinnacle. Regions: us,us2,uk,au,eu
    MARKETS = "h2h"
    ODDS_FORMAT = "american"
    DATE_FORMAT = "iso"
     
    odds_data = odds_query(SPORT, REGIONS, MARKETS, ODDS_FORMAT, DATE_FORMAT)
    
    if odds_data:
        parsed_results = ParseOdds(odds_data)
    else:
        print("Failed to retrieve odds data.")
