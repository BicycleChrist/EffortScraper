import asyncio
import aiohttp
import json
from typing import Optional, Dict, List, Set
from Creds import ODDS_API_KEY
from marketKeys import *


class NBAPropsClient:
    def __init__(self):
        self.api_key = ODDS_API_KEY
        self.base_url = "https://api.the-odds-api.com/v4"
        self.request_count = 0
    
    async def get_nba_games(self, session: aiohttp.ClientSession) -> Optional[List[Dict]]:
        """Fetch active NBA games."""
        url = f"{self.base_url}/sports/basketball_nba/events"
        params = {"apiKey": self.api_key}
        
        async with session.get(url, params=params) as response:
            self.request_count += 1
            response_text = await response.text()
            print(f"\nGames API Response Status: {response.status}")
            
            if response.status != 200:
                print(f"Error fetching NBA games: {response.status}")
                print(f"Response text: {response_text}")
                return None
            return json.loads(response_text)
    
    async def get_props(self, session: aiohttp.ClientSession, event_id: str, markets: Set[str]) -> Optional[Dict]:
        """Fetch player props for specific markets in an NBA game."""
        url = f"{self.base_url}/sports/basketball_nba/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": ",".join(markets),
            "oddsFormat": "american",
            "bookmakers": "bovada,fanduel,draftkings,pinnacle,betonline,betus"
        }
        
        print(f"\nMaking request to: {url}")
        
        async with session.get(url, params=params) as response:
            self.request_count += 1
            response_text = await response.text()
            
            if response.status == 404:
                print(f"\nProps not available for event {event_id}")
                return None
            elif response.status != 200:
                print(f"\nError fetching props for event {event_id}: {response.status}")
                return None
            
            try:
                return json.loads(response_text)
            except json.JSONDecodeError as e:
                print(f"\nError decoding JSON response: {e}")
                return None


def format_props_by_player(props_data: Dict, sport: str = "basketball_nba") -> Dict[str, Dict[str, List[Dict]]]:
    """Reorganize props data by player for each bookmaker."""
    props_by_player = {}
    sport_markets = SPORTS_MARKETS.get(sport, {})
    
    for bookmaker in props_data.get("bookmakers", []):
        book_name = bookmaker["title"]
        
        for market in bookmaker.get("markets", []):
            market_name = sport_markets.get(market["key"], market["key"])
            market_key = market["key"]
            
            for outcome in market["outcomes"]:
                player_name = outcome.get("description", outcome.get("name"))
                if not player_name or player_name in ["Over", "Under", "Yes", "No"]:
                    continue
                
                if player_name not in props_by_player:
                    props_by_player[player_name] = {}
                
                if book_name not in props_by_player[player_name]:
                    props_by_player[player_name][book_name] = []
                
                # Handle different market types
                prop_data = {
                    "market": market_name,
                    "type": outcome["name"],
                    "odds": outcome["price"]
                }
                
                # Add line only for Over/Under markets
                if "point" in outcome and outcome["name"] in ["Over", "Under"]:
                    prop_data["line"] = outcome["point"]
                
                # For Yes/No markets, use the type as the line
                if outcome["name"] in ["Yes", "No"]:
                    prop_data["line"] = outcome["name"]
                
                props_by_player[player_name][book_name].append(prop_data)
    
    return props_by_player


async def select_markets() -> Set[str]:
    """Allow user to select which markets to query."""
    print("\nAvailable NBA Player Props Markets:")
    print("=" * 50)
    for i, (key, name) in enumerate(NBA_MARKETS.items(), 1):
        print(f"{i}. {name} ({key})")
    
    selected_markets = set()
    while True:
        try:
            choices = input("\nEnter the numbers of the markets you want to check (comma-separated, or 'all' for all markets): ").strip()
            if choices.lower() == 'all':
                return set(NBA_MARKETS.keys())
            
            for choice in choices.split(','):
                num = int(choice.strip())
                if 1 <= num <= len(NBA_MARKETS):
                    selected_markets.add(list(NBA_MARKETS.keys())[num-1])
                else:
                    print(f"Invalid choice: {num}. Please enter numbers between 1 and {len(NBA_MARKETS)}")
                    selected_markets.clear()
                    break
            if selected_markets:
                return selected_markets
        except ValueError:
            print("Please enter valid numbers separated by commas")


async def select_games(games: List[Dict]) -> List[Dict]:
    """Allow user to select which games to query."""
    print("\nAvailable NBA Games:")
    print("=" * 50)
    for i, game in enumerate(games, 1):
        print(f"{i}. {game['away_team']} @ {game['home_team']} (ID: {game['id']})")
    
    selected_games = []
    while True:
        try:
            choices = input("\nEnter the numbers of the games you want to check (comma-separated, or 'all' for all games): ").strip()
            if choices.lower() == 'all':
                return games
            
            for choice in choices.split(','):
                num = int(choice.strip())
                if 1 <= num <= len(games):
                    selected_games.append(games[num-1])
                else:
                    print(f"Invalid choice: {num}. Please enter numbers between 1 and {len(games)}")
                    selected_games.clear()
                    break
            if selected_games:
                return selected_games
        except ValueError:
            print("Please enter valid numbers separated by commas")


async def main():
    client = NBAPropsClient()
    
    async with aiohttp.ClientSession() as session:
        # Get NBA games
        print("\nFetching NBA games...")
        games = await client.get_nba_games(session)
        if not games:
            print("No NBA games found")
            return
        
        # Let user select games
        selected_games = await select_games(games)
        if not selected_games:
            print("No games selected")
            return
        
        # Let user select markets
        selected_markets = await select_markets()
        if not selected_markets:
            print("No markets selected")
            return
        
        # Process each selected game
        for game in selected_games:
            print(f"\nFetching props for: {game['away_team']} @ {game['home_team']}")
            print(f"Game ID: {game['id']}")
            print("=" * 50)
            
            # Get props for the game
            props_data = await client.get_props(session, game['id'], selected_markets)
            if not props_data:
                print("No props data available")
                continue
            
            # Process and display props organized by player
            props_by_player = format_props_by_player(props_data)
            
            # Display props for each player
            for player_name, bookmaker_props in sorted(props_by_player.items()):
                print(f"\n{player_name}:")
                print("-" * 50)
                
                for book_name, props in bookmaker_props.items():
                    print(f"\n{book_name}:")
                    for prop in props:
                        # Adjust display format based on whether it's a Yes/No market
                        if isinstance(prop.get('line'), str):
                            print(f"  {prop['market']:<30} {prop['type']:<5} @ {prop['odds']:>4}")
                        else:
                            print(f"  {prop['market']:<30} {prop['type']:<5} {prop['line']:>5.1f} @ {prop['odds']:>4}")
        
        print(f"\nTotal API requests made: {client.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
# Claude has such a hard on for async, even more so than for writing the react componet
