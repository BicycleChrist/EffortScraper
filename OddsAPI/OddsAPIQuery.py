import aiohttp
import asyncio
from Creds import ODDS_API_KEY
import json
import datetime
from datetime import datetime, timezone

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

async def league_query(session=None):
    """
    Fetch available sports/leagues from the API
    Args:
        session: Optional aiohttp.ClientSession. If None, creates a new session.
    Returns:
        dict: Sports grouped by category
    """
    # Create session if not provided
    session_created = False
    if session is None:
        session = aiohttp.ClientSession()
        session_created = True
    
    try:
        async with session.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": ODDS_API_KEY}
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                print(f"Failed to get sports: status_code {response.status}")
                print(error_text)
                return {}
            
            sports = await response.json()
            
        # Process the response exactly as before
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
        
    except aiohttp.ClientError as e:
        print(f"Network error fetching leagues: {e}")
        return {}
    except Exception as e:
        print(f"Error fetching leagues: {e}")
        return {}
    finally:
        # Only close session if we created it
        if session_created:
            await session.close()


async def odds_query(SPORT, REGIONS, MARKETS, ODDS_FORMAT, DATE_FORMAT, session=None):
    """
    Fetch odds for a specific sport
    Args:
        SPORT: Sport key
        REGIONS: Comma-separated region codes
        MARKETS: Comma-separated market types
        ODDS_FORMAT: Format for odds (american, decimal, etc.)
        DATE_FORMAT: Format for dates (iso, unix)
        session: Optional aiohttp.ClientSession. If None, creates a new session.
    Returns:
        dict: Odds data with processed 3-way markets
    """
    # Create session if not provided
    session_created = False
    if session is None:
        session = aiohttp.ClientSession()
        session_created = True
    
    try:
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT
        }
        
        async with session.get(
            f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds",
            params=params
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                print(f"Error: {response.status}\n{error_text}")
                return None
            
            # Save the raw response to a JSON file (exactly as before)
            raw_response = await response.json()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"odds_response_{SPORT}_{timestamp}.json"
            
            with open(filename, 'w') as f:
                json.dump(raw_response, f, indent=4)
            
            print(f"Raw JSON response saved to {filename}")
            
            # Process the odds data to ensure 3-way moneylines are handled (exactly as before)
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
            
    except aiohttp.ClientError as e:
        print(f"Network error fetching odds: {e}")
        return None
    except Exception as e:
        print(f"Error fetching odds: {e}")
        return None
    finally:
        # Only close session if we created it
        if session_created:
            await session.close()


def ParseOdds(odds):
    """
    Parse odds data for display (no changes needed - no API calls)
    Args:
        odds: Odds data from odds_query
    Returns:
        dict: Parsed results
    """
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


# From API DOCS
# "The scores endpoint applies to selected sports and is gradually being expanded to more sports." 
# https://the-odds-api.com/liveapi/guides/v4/#get-scores

async def scores_query(sport_key, days_from=None, session=None):
    """
    Fetch scores data for live game status detection
    Args:
        sport_key: Sport identifier
        days_from: Optional days from parameter; valid range is 1-3
                   if unspecified, only live and upcoming games are returned
        session: Optional aiohttp.ClientSession. If None, creates a new session.
    Returns:
        list: Games with scores, completion status, and commence times
    """
    # Create session if not provided
    session_created = False
    if session is None:
        session = aiohttp.ClientSession()
        session_created = True
    
    try:
        params = {
            "apiKey": ODDS_API_KEY,
            "dateFormat": "iso"
        }
        if days_from:
            assert((days_from > 0) and (days_from <= 3)), "days_from parameter must be 1-3";
            params["daysFrom"] = days_from
        
        async with session.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
            params=params
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                print(f"Error fetching scores: {response.status}")
                print(error_text)
                return []
                
    except aiohttp.ClientError as e:
        print(f"Network error fetching scores: {e}")
        return []
    except Exception as e:
        print(f"Exception fetching scores: {e}")
        return []
    finally:
        # Only close session if we created it
        if session_created:
            await session.close()


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
    # `or ''` — some OI sources (CRIS boards) carry commence_time=None,
    # and fromisoformat(None) raises TypeError, not ValueError.
    commence_time_str = game_data.get('commence_time') or ''
    
    # Try to find this game in scores data first
    if scores_data:
        for score_game in scores_data:
            if score_game.get('id') == game_id:
                scores = score_game.get('scores')
                
                if (scores is None) or (len(scores) == 0):
                    if commence_time_str == '':
                        return "Unknown", False, "no scores && no time"
                    else:
                        break # fallback to time-based calculation
                
                elif scores is not None:
                    # Live game with scores
                    scores_text = " | ".join([f"{s['name']}: {s['score']}" for s in scores])
                    return "🔴 LIVE", True, scores_text
    
    # Fallback to time-based detection
    if not commence_time_str:
        # No start time at all (e.g. a CRIS-only OI record) — keep the
        # game, just don't claim a state.
        return "TBD", False, ""
    try:
        commence_time = datetime.fromisoformat(commence_time_str)
        current_time = datetime.now(timezone.utc)
        time_diff = (current_time - commence_time).total_seconds()
        
        if time_diff < -1800:  # More than 30 min before
            return "Pre-Game", False, ""
        elif time_diff < 0:  # Less than 30 min before
            return "Starting Soon", False, ""
        elif time_diff < 14400:  # Less than 4 hours after (likely live)
            return "🔴LIVE", True, ""
        else:  # More than 4 hours after - skip these games
            return None
            
    except Exception as e:
        print(f"Error parsing commence time: {e}")
        return "Unknown", False, "Error"


# Async main function for testing
async def main():
    """Async main function for testing the API functions"""
    # Create a single session for all API calls
    async with aiohttp.ClientSession() as session:
        # Test league query
        print("Testing league_query...")
        leagues = await league_query(session)
        
        if not leagues:
            print("No leagues found, exiting.")
            return
        
        # Test with MLB
        SPORT = "baseball_mlb"
        BOOKMAKERS = "draftkings,fanduel,pinnacle,bovada,betonline,betus,betrivers,lowvig"  # Optional filter
        REGIONS = "us,eu"  # Ensure 'eu' is included for Pinnacle. Regions: us,us2,uk,au,eu
        MARKETS = "h2h"
        ODDS_FORMAT = "american"
        DATE_FORMAT = "iso"
        
        print(f"\nTesting odds_query for {SPORT}...")
        odds_data = await odds_query(SPORT, REGIONS, MARKETS, ODDS_FORMAT, DATE_FORMAT, session)
        
        if odds_data:
            print(f"Successfully fetched odds for {len(odds_data)} games")
            parsed_results = ParseOdds(odds_data)
            
            # Test scores query
            print(f"\nTesting scores_query for {SPORT}...")
            scores_data = await scores_query(SPORT, session=session)
            print(f"Successfully fetched scores for {len(scores_data)} games")
            
        else:
            print("Failed to retrieve odds data.")


if __name__ == "__main__":
    # Run the async main function
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error in main: {e}")
        import traceback
        traceback.print_exc()
