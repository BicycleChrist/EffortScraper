import requests

# oddsAPI docs https://the-odds-api.com/liveapi/guides/v4/#endpoint-8
#TODO:Implement historical odds, opportunity for some sneaky graph action
def league_query():
    API_KEY = "YOUR_API_KEY"

    response = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": API_KEY}
    )

    if response.status_code != 200:
        print(f"Failed to get sports: status_code {response.status_code}")
        print(response.text)
    else:
        sports = response.json()
        # Group sports by category
        sports_by_group = {}
        for sport in sports:
            group = sport['group']
            if group not in sports_by_group:
                sports_by_group[group] = []
            sports_by_group[group].append(sport)

        # Print formatted output
        print("\nAvailable Sports and Leagues:")
        print("=" * 50)
        for group, sports_list in sorted(sports_by_group.items()):
            print(f"\n{group}:")
            print("-" * len(group))
            for sport in sorted(sports_list, key=lambda x: x['title']):
                print(f"• {sport['title']}")
                print(f"  - Key: {sport['key']}")
                print(f"  - Description: {sport['description']}")



def NHL_query():
    API_KEY = "YOUR_API_KEY"
    SPORT = "icehockey_nhl"
    # BOOKMAKERS = "draftkings,fanduel,pinnacle,bovada,betonline,betus,betrivers,lowvig"  # Optional filter
    REGIONS = "us,us2,eu"  # Ensure 'eu' is included for Pinnacle
    MARKETS = "spreads"
    ODDS_FORMAT = "american"
    DATE_FORMAT = "iso"

    response = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds",
        params={
            "apiKey": API_KEY,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT
        }
    )

    if response.status_code != 200:
        print(f"Error: {response.status_code}\n{response.text}")
        return

    odds = response.json()

    # Optional: Filter bookmakers if BOOKMAKERS is uncommented
    if 'BOOKMAKERS' in globals():
        queried_books = [b.strip().lower() for b in BOOKMAKERS.split(',')]
    else:
        queried_books = None  # No filter, show all bookmakers

    for game in odds:
        print(f"\nGame: {game['home_team']} vs {game['away_team']}")
        print(f"Start Time: {game['commence_time']}")

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
            if queried_books and bm_title not in queried_books:
                continue

            print(f"\n\033[1mBookmaker: {bookmaker['title']}\033[0m")
            for market in bookmaker['markets']:
                if market['key'] != 'spreads':
                    continue

                for outcome in market['outcomes']:
                    team = outcome['name']
                    bm_point = outcome['point']
                    bm_price = outcome['price']

                    # Update best line for the team if this is better
                    if best_lines[team]['price'] is None or (
                        (bm_price > 0 and bm_price > best_lines[team]['price']) or
                        (bm_price < 0 and bm_price < best_lines[team]['price'])
                    ):
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

if __name__ == "__main__":
    # Uncomment the function you want to run
    #league_query()
    NHL_query()
