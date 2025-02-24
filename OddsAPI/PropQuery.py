import asyncio
import aiohttp
import json
import pathlib
from typing import Optional, Dict, List, Set
from Creds import ODDS_API_KEY
from marketKeys import SPORTS_MARKETS, NBA_MARKETS, MLB_MARKETS, NHL_MARKETS, AFL_MARKETS, SOCCER_MARKETS
from parlay_analyzer import find_best_parlays
import requests

# Define save directory
SAVE_DIR = pathlib.Path.cwd() / "savedata"
SAVE_DIR.mkdir(exist_ok=True)

# PropClient to handle API requests efficiently
class PropClient:
    def __init__(self, sport_key: str):
        self.sport_key = sport_key
        self.api_key = ODDS_API_KEY
        self.base_url = "https://api.the-odds-api.com/v4"
        self.request_count = 0

    async def fetch_json(self, session: aiohttp.ClientSession, url: str, params: dict) -> dict|None:
        """Helper function to make GET requests and return JSON response."""
        print(f"Fetching data from: {url}")
        async with session.get(url, params=params) as response:
            self.request_count += 1
            response_text = await response.text()
            print(f"Response Status: {response.status}")
            if response.status != 200:
                print(f"Error fetching {url}: {response.status}\nResponse: {response_text}")
                return None
            try:
                return await response.json()
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON response: {e}")
                return None

    

    async def get_games(self, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
        """Fetch active games for the selected sport."""
        url = f"{self.base_url}/sports/{self.sport_key}/events"
        params = {"apiKey": self.api_key}
        return await self.fetch_json(session, url, params)

    async def get_event_odds(self, session: aiohttp.ClientSession, event_id: str, markets: set[str], region: str = "us") -> dict:
        """Fetch event odds for a specific event and markets."""
        url = f"{self.base_url}/sports/{self.sport_key}/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": region,
            "markets": ",".join(markets),
            "oddsFormat": "american"
        }
        return await self.fetch_json(session, url, params)
    
    async def get_dfs_props(self, session: aiohttp.ClientSession, event_id: str) -> dict:
        """Fetch DFS props for a specific event."""
        # This method would query DFS props from your data source
        # Since the original code doesn't show a specific DFS endpoint, we'll need to adapt
        # based on your specific API requirements
        
        url = f"{self.base_url}/sports/{self.sport_key}/events/{event_id}/dfs/props"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "oddsFormat": "american"
        }
        return await self.fetch_json(session, url, params)

    def get_dfs_props_no_async(self, event_id: str) -> dict:
        """Non-async version of get_dfs_props."""
        url = f"{self.base_url}/sports/{self.sport_key}/events/{event_id}/dfs/props"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "oddsFormat": "american"
        }
        return self.fetch_json_no_async(url, params)
    
    
    
    
    def fetch_json_no_async(self, url: str, params: dict) -> dict|None:
        """Helper function to make GET requests and return JSON response."""
        print(f"Fetching data from: {url}")
        response = requests.get(url, params)
        self.request_count += 1
        response_text = response.text
        print(f"Response Status: {response.status_code}")
        if response.status_code != 200:
            print(f"Error fetching {url}: {response.status_code}\nResponse: {response_text}")
            return None
        return response.json()
    
    def get_event_odds_no_async(self, event_id: str, markets: set[str], region: str = "us") -> dict:
        """Fetch event odds for a specific event and markets."""
        url = f"{self.base_url}/sports/{self.sport_key}/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": region,
            "markets": ",".join(markets),
            "oddsFormat": "american"
        }
        return self.fetch_json_no_async(url, params)


def format_props_by_player(props_data: Dict) -> Dict[str, Dict[str, List[Dict]]]:
    """Reorganize props data by player for each bookmaker."""
    props_by_player = {}
    for bookmaker in props_data.get("bookmakers", []):
        book_name = bookmaker["title"]
        for market in bookmaker.get("markets", []):
            for outcome in market["outcomes"]:
                player_name = outcome.get("description", outcome.get("name"))
                if not player_name or player_name in ["Over", "Under"]:
                    continue
                if player_name not in props_by_player:
                    props_by_player[player_name] = {}
                if book_name not in props_by_player[player_name]:
                    props_by_player[player_name][book_name] = []
                prop_data = {
                    "market": market["key"],
                    "type": outcome["name"],
                    "odds": outcome["price"],
                    "line": outcome.get("point")
                }
                props_by_player[player_name][book_name].append(prop_data)
    return props_by_player


def save_data(event_id: str, data: dict, filename_prefix: str):
    """Save data to JSON file in the specified directory."""
    file_path = SAVE_DIR / f"{filename_prefix}_{event_id}.json"
    with file_path.open('w', encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Data saved to {file_path}")


async def main():
    """Main function to fetch and process odds and props based on user selection."""
    print("Select a league:")
    leagues = list(SPORTS_MARKETS["Basketball"].keys()) + list(SPORTS_MARKETS["Baseball"].keys()) + list(SPORTS_MARKETS["Ice Hockey"].keys()) + list(SPORTS_MARKETS["Aussie Rules"].keys()) + list(SPORTS_MARKETS["Soccer"].keys())
    for i, league in enumerate(leagues, 1):
        print(f"{i}. {league}")
    league_index = int(input("Enter the number corresponding to your league: ")) - 1
    selected_league = leagues[league_index]
    sport_key = None
    for category, leagues in SPORTS_MARKETS.items():
        if selected_league in leagues:
            sport_key = leagues[selected_league]
            break

    if not sport_key:
        print(f"Invalid sport key: {selected_league}")
        return
    market_map = {
        "basketball_nba": NBA_MARKETS,
        "baseball_mlb": MLB_MARKETS,
        "icehockey_nhl": NHL_MARKETS,
        "aussierules_afl": AFL_MARKETS,
        "soccer_mexico_ligamx": SOCCER_MARKETS
    }
    
    available_markets = market_map.get(sport_key, {})
    if not available_markets:
        print(f"No available markets for: {sport_key}")
        return

    client = PropClient(sport_key)
    async with aiohttp.ClientSession() as session:
        print(f"Fetching {sport_key} games...")
        games = await client.get_games(session)
        if not games:
            print("No games found")
            return
        save_data(sport_key, games, "raw_gameslist")
        
        print("Available games:")
        for i, game in enumerate(games):
            print(f"{i + 1}. {game['away_team']} @ {game['home_team']}")
        selected_games = input("Enter the numbers of the games you want to check (comma-separated, or 'all' for all games): ").strip()
        if selected_games.lower() == 'all':
            game_ids = [game["id"] for game in games]
        else:
            game_ids = [games[int(i) - 1]["id"] for i in selected_games.split(',')]

        print("Available props:")
        for i, (key, name) in enumerate(available_markets.items()):
            print(f"{i + 1}. {name} ({key})")
        selected_props = input("Enter the numbers of the props you want to see (comma-separated, or 'all' for all props): ").strip()
        if selected_props.lower() == 'all':
            selected_markets = set(available_markets.keys())
        else:
            selected_markets = {list(available_markets.keys())[int(i) - 1] for i in selected_props.split(',')}
        
        for event_id in game_ids:
            print(event_id)
            props_data = await client.get_event_odds(session, event_id, selected_markets)
            if not props_data:
                print(f"No props data available for game ID {event_id}.")
                continue
            save_data(event_id, props_data, "raw_props_data")
            props_by_player = format_props_by_player(props_data)
            save_data(event_id, props_by_player, "player_props")

            print("Finding best parlays...")
            prizepicks_parlays = find_best_parlays({event_id: props_by_player}, "PrizePicks")
            underdog_parlays = find_best_parlays({event_id: props_by_player}, "Underdog")
            print("PrizePicks Best Parlays:")
            print(json.dumps(prizepicks_parlays, indent=2))
            print("Underdog Best Parlays:")
            print(json.dumps(underdog_parlays, indent=2))
            save_data(event_id, prizepicks_parlays, "prizepicks_parlay_results")
            save_data(event_id, underdog_parlays, "underdog_parlay_results")
        
        print(f"Total API requests made: {client.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
