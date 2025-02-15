import asyncio
import aiohttp
import json
import pathlib
from typing import Optional, Dict, List, Set
from Creds import ODDS_API_KEY
from marketKeys import *
from parlay_analyzer import find_best_parlays

# NBA Odds Client to handle API requests efficiently
class NBAOddsClient:
    def __init__(self):
        self.api_key = ODDS_API_KEY
        self.base_url = "https://api.the-odds-api.com/v4"
        self.request_count = 0

    async def fetch_json(self, session: aiohttp.ClientSession, url: str, params: Dict) -> Optional[Dict]:
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
                return json.loads(response_text)
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON response: {e}")
                return None

    async def get_nba_games(self, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
        """Fetch active NBA games."""
        url = f"{self.base_url}/sports/basketball_nba/events"
        params = {"apiKey": self.api_key}
        return await self.fetch_json(session, url, params)

    async def get_event_odds(self, session: aiohttp.ClientSession, event_id: str, markets: Set[str], region: str = "us") -> Optional[Dict]:
        """Fetch event odds for a specific event and markets."""
        url = f"{self.base_url}/sports/basketball_nba/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": region,
            "markets": ",".join(markets),
            "oddsFormat": "american"
        }
        return await self.fetch_json(session, url, params)

    async def get_player_props(self, session: aiohttp.ClientSession, event_id: str, markets: Set[str]) -> Optional[Dict]:
        """Fetch player prop bets from DFS sportsbooks."""
        return await self.get_event_odds(session, event_id, markets, region="us_dfs")


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


async def main():
    """Main function to fetch and process NBA odds and props."""
    client = NBAOddsClient()
    async with aiohttp.ClientSession() as session:
        print("Fetching NBA games...")
        games = await client.get_nba_games(session)
        if not games:
            print("No NBA games found")
            return

        selected_markets = {
        "player_points", 
        "player_rebounds", 
        "player_assists",
        #"player_points_rebounds_assists",
        #"player_points_rebounds",
        #"player_points_assists",
        #"player_rebounds_assists",
        #"player_double_double",
        #"player_triple_double"
     }
        
        all_market_results = {}

        # Loop through each game to fetch player props
        for game in games:
            print(f"Processing game: {game['away_team']} @ {game['home_team']}")
            event_id = game["id"]
            props_data = await client.get_player_props(session, event_id, selected_markets)
            if not props_data:
                print("No props data available for this game.")
                continue
            props_by_player = format_props_by_player(props_data)
            all_market_results.update(props_by_player)

        print("Finding best parlays...")
        prizepicks_parlays = find_best_parlays(all_market_results, "PrizePicks")
        print("PrizePicks Best Parlays:")
        print(json.dumps(prizepicks_parlays, indent=2))

        print(f"Total API requests made: {client.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
