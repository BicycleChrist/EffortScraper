import json
import pathlib
from datetime import datetime
from typing import Optional

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    import requests
    HAS_CLOUDSCRAPER = False
    print("WARNING: cloudscraper not installed. Install with: pip install cloudscraper")
    print("Falling back to regular requests (may not work due to Cloudflare protection)")


class ParlayPlayClient:
    """Client for ParlayPlay API - fetches player props and odds data"""

    BASE_URL = "https://parlayplay.io"

    def __init__(self):
        if HAS_CLOUDSCRAPER:
            self.session = cloudscraper.create_scraper(
                browser={
                    'browser': 'firefox',
                    'platform': 'linux',
                    'mobile': False
                }
            )
        else:
            self.session = requests.Session()

        # Set base headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Referer': 'https://parlayplay.io/',
            'X-Requested-With': 'XMLHttpRequest',
            'X-Parlay-Request': '1',
            'X-ParlayPlay-Native-Platform': 'web',
            'X-ParlayPlay-Platform': 'web',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        })

        # Initialize session by visiting main page to get cookies
        self._initialize_session()

    def _initialize_session(self):
        """Initialize session by visiting main page to obtain cookies"""
        try:
            # Visit main page to get cookies
            response = self.session.get(self.BASE_URL, timeout=30)

            # Extract CSRF token from cookies and add to headers
            csrf_token = self.session.cookies.get('csrftoken')
            if csrf_token:
                self.session.headers['X-CSRFToken'] = csrf_token
                print(f"Session initialized. CSRF token obtained.")
            else:
                print("Warning: No CSRF token found in cookies")

        except Exception as e:
            print(f"Warning: Failed to initialize session: {e}")

    def search(self,
               sport: str = "Football",
               league: str = "NFL",
               period: str = "FG",
               include_alt: bool = True,
               include_boost: bool = True,
               include_sports: bool = True,
               version: int = 2) -> Optional[dict]:
        """
        Search for player props by sport and league

        Args:
            sport: Sport name (e.g., "Football", "Basketball", "Soccer", "Baseball")
            league: League code (e.g., "NFL", "NBA", "MLB", "CFB", "NCAAB", "LaLiga")
            period: Match period (e.g., "FG" for full game, "H1" for first half)
            include_alt: Include alternate lines
            include_boost: Include boosted payouts
            include_sports: Include sports data
            version: API version

        Returns:
            JSON response with player props data
        """
        endpoint = f"{self.BASE_URL}/api/v1/crossgame/search"

        params = {
            'sport': sport,
            'league': league,
            'period': period,
            'includeAlt': str(include_alt).lower(),
            'includeBoost': str(include_boost).lower(),
            'includeSports': str(include_sports).lower(),
            'version': version
        }

        try:
            response = self.session.get(endpoint, params=params, timeout=30)

            # Check for Cloudflare challenge
            if response.status_code == 403:
                print(f"Error: Cloudflare protection detected for {league}")
                print("  Install cloudscraper: pip install cloudscraper")
                return None

            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching {league} data: {e}")
            return None

    def get_all_sports(self) -> list[dict]:
        """
        Fetch all available sports/leagues
        Common leagues include:
        - Football: NFL, CFB
        - Basketball: NBA, NCAAB, WNBA
        - Baseball: MLB
        - Hockey: NHL
        - Soccer: EPL, LaLiga, etc.
        """
        # This is a helper method to define common leagues
        # You can expand this based on what's available
        leagues = [
            {'sport': 'Football', 'league': 'NFL'},
            {'sport': 'Football', 'league': 'CFB'},
            {'sport': 'Basketball', 'league': 'NBA'},
            {'sport': 'Basketball', 'league': 'NCAAB'},
            {'sport': 'Basketball', 'league': 'WNBA'},
            {'sport': 'Baseball', 'league': 'MLB'},
            {'sport': 'Hockey', 'league': 'NHL'},
            {'sport': 'Soccer', 'league': 'EPL'},
            {'sport': 'Soccer', 'league': 'LaLiga'},
            {'sport': 'Soccer', 'league': 'UCL'},
        ]
        return leagues

    def save_response(self, data: dict, filename: str, subdir: str = "parlayplay_dumps"):
        """Save API response to JSON file"""
        cwd = pathlib.Path.cwd()
        savedir = cwd / subdir
        if not savedir.exists():
            savedir.mkdir()

        filepath = savedir / f"{filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        print(f"Saving to: {filepath}")

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        return filepath


def fetch_league(client: ParlayPlayClient, sport: str, league: str) -> Optional[dict]:
    """Fetch and save data for a specific league"""
    print(f"Fetching {sport} - {league}...")
    data = client.search(sport=sport, league=league)

    if data and 'players' in data:
        player_count = len(data['players'])
        print(f"  ✓ Found {player_count} player props")
        client.save_response(data, f"{sport}_{league}")
        return data
    else:
        print(f"  ✗ No data returned")
        return None


def fetch_all_leagues(client: ParlayPlayClient) -> dict:
    """Fetch data for all available leagues"""
    results = {}
    leagues = client.get_all_sports()

    for league_info in leagues:
        sport = league_info['sport']
        league = league_info['league']

        data = fetch_league(client, sport, league)
        if data:
            results[f"{sport}_{league}"] = data

    return results


if __name__ == "__main__":
    client = ParlayPlayClient()

    # Example 1: Fetch specific league
    print("=" * 60)
    print("Fetching NFL player props...")
    print("=" * 60)
    nfl_data = client.search(sport="Football", league="NFL")
    if nfl_data:
        client.save_response(nfl_data, "NFL_props")
        print(f"Total players: {len(nfl_data.get('players', []))}")

    # Example 2: Fetch multiple leagues
    print("\n" + "=" * 60)
    print("Fetching all major leagues...")
    print("=" * 60)
    all_data = fetch_all_leagues(client)
    print(f"\nSuccessfully fetched {len(all_data)} leagues")

    # Save combined results
    if all_data:
        client.save_response(all_data, "All_Leagues_Combined")

    print("\nDone!")
