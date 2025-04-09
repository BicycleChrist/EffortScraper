import aiohttp
import asyncio
import json
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Union, Any
from Creds import TT_KEY


# Table Tennis sport ID is 92
# 92_1: Match winner (1X2) odds
# 92_2: Handicap odds
# 92_3: Over/Under odds
# API DOCS: https://betsapi.com/docs/events/odds.html

class TableTennisAPI:
    BASE_URL = "https://api.b365api.com"
    SPORT_ID = 92  
    
    # Target league IDs (exact IDs for the four priority leagues)
    TARGET_LEAGUE_IDS = {
        "22307": "Setka Cup",
        "22742": "Czech Republic Liga Pro",
        "22534": "TT CUP",
        "24536": "Poland TT Elite Series"
    }
    
    # Need to discern which books carry TT from these leagues
    PRIORITY_BOOKMAKERS = [
        "bet365", "1xbet","unibet", # "pinnaclesports", "betway", "marathonbet", 
        # "bwin",  "betfair", "williamhill", "sbobet"
    ]
    
    def __init__(self, api_token: str, debug: bool = False, max_concurrent: int = 16, 
                 max_upcoming_per_league: int = 10, max_h2h_matches: int = 20):
        self.api_token = api_token
        self.debug = debug
        self.session = None
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.rate_limit_delay = 0.1
        self.last_request_time = 0
        self.league_ids = {}  # Store league IDs once retrieved
        self.max_upcoming_per_league = max_upcoming_per_league  # Max upcoming matches per league
        self.max_h2h_matches = max_h2h_matches  # Max H2H history matches
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100))
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None
    
    async def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Dict:
        """Make an asynchronous request to the BetsAPI with rate limiting."""
        if params is None:
            params = {}
        
        params['token'] = self.api_token
        url = f"{self.BASE_URL}{endpoint}"
        
        # Create session if needed
        session_created = False
        if self.session is None:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100))
            session_created = True
        
        # Apply rate limiting
        current_time = asyncio.get_event_loop().time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - time_since_last)
        
        self.last_request_time = asyncio.get_event_loop().time()
        
        if self.debug:
            print(f"Request: {url} - {params}")
            
        try:
            async with self.semaphore:
                async with self.session.get(url, params=params) as response:
                    try:
                        response_data = await response.json()
                        response.raise_for_status()
                        return response_data
                    except Exception as e:
                        if self.debug:
                            print(f"Error: {e}")
                        return {"success": 0, "error": str(e)}
        except Exception as e:
            return {"success": 0, "error": str(e)}
        finally:
            if session_created:
                await self.session.close()
                self.session = None

    async def get_target_league_ids(self) -> Dict[str, str]:
        """Get IDs for the target leagues we're interested in."""
        if self.league_ids:  # Use cached results if available
            return self.league_ids
            
        # We already have the exact league IDs, so no need to query the API
        if self.debug:
            for league_id, league_name in self.TARGET_LEAGUE_IDS.items():
                print(f"Using target league: {league_name} (ID: {league_id})")
                
        self.league_ids = {name: league_id for league_id, name in self.TARGET_LEAGUE_IDS.items()}
        return self.league_ids

    async def get_events(self, event_type: str, league_id: Optional[str] = None) -> Dict:
        """Get events of a specific type with optional league filtering."""
        params = {"sport_id": self.SPORT_ID}
        if league_id:
            params["league_id"] = league_id
            
        return await self._make_request(f"/v3/events/{event_type}", params)

    async def get_events_for_target_leagues(self, event_type: str) -> List[Dict]:
        """Get all events for our target leagues."""
        all_events = []
        tasks = []
        
        # Create tasks for each league using the direct league IDs
        for league_id, league_name in self.TARGET_LEAGUE_IDS.items():
            tasks.append((league_name, self.get_events(event_type, league_id)))
            
        # Execute all tasks concurrently
        for league_name, task in tasks:
            try:
                response = await task
                if response.get("success") == 1:
                    events = response.get("results", [])
                    
                    # Limit number of upcoming events per league if requested
                    if event_type == "upcoming" and len(events) > self.max_upcoming_per_league:
                        if self.debug:
                            print(f"Limiting from {len(events)} to {self.max_upcoming_per_league} upcoming events for {league_name}")
                        events = events[:self.max_upcoming_per_league]
                    
                    # Add league info to each event
                    for event in events:
                        event["league_name"] = league_name
                        event["league_id"] = next(lid for lid, lname in self.TARGET_LEAGUE_IDS.items() if lname == league_name)
                        
                    all_events.extend(events)
                    if self.debug:
                        print(f"Found {len(events)} {event_type} events for {league_name}")
            except Exception as e:
                if self.debug:
                    print(f"Error fetching events for {league_name}: {e}")
                    
        return all_events

    async def get_event_odds(self, event_id: str, bookmaker: str) -> Dict:
        """Get odds for an event from a specific bookmaker."""
        params = {
            "event_id": event_id,
            "source": bookmaker,
            "odds_market": "1,2,3"  # Match Winner, Spread, Total Points
        }
        return await self._make_request("/v2/event/odds", params)

    async def get_comprehensive_odds(self, event_id: str) -> Dict[str, Any]:
        """Get odds from all priority bookmakers for an event."""
        tasks = []
        for bookmaker in self.PRIORITY_BOOKMAKERS:
            tasks.append((bookmaker, self.get_event_odds(event_id, bookmaker)))
            
        odds_data = {}
        for bookmaker, task in tasks:
            try:
                response = await task
                if response.get("success") == 1:
                    results = response.get("results", {})
                    if results.get("odds", {}):
                        odds_data[bookmaker] = results
                        if self.debug:
                            print(f"Found odds from {bookmaker} for event {event_id}")
            except Exception as e:
                if self.debug:
                    print(f"Error fetching odds from {bookmaker}: {e}")
                    
        return odds_data

    async def get_event_history(self, event_id: str) -> Dict:
        """Get match history for an event."""
        params = {"event_id": event_id, "qty": self.max_h2h_matches}
        return await self._make_request("/v1/event/history", params)

    async def process_events_data(self, events_data):
        """Process events data to extract useful information."""
        result = {
            "events": events_data,
            "history": [],
            "odds": []
        }
        
        total_events = len(events_data)
        processed = 0
        
        for event in events_data:
            event_id = event.get('id')
            processed += 1
            
            # Get head-to-head history
            if event_id:
                if self.debug:
                    print(f"Processing event {processed}/{total_events} (ID: {event_id})")
                
                h2h_data = await self.get_h2h_for_event(event_id)
                if h2h_data:
                    result["history"].append({
                        "event_id": event_id,
                        "h2h": h2h_data
                    })
                
                # Get odds from MULTIPLE bookmakers
                target_bookmakers = ['bet365', 'unibet', 'bwin', 'betfair', 'williamhill', 'pinnacle']
                odds_data = await self.get_odds_for_event(event_id, target_bookmakers)
                if odds_data:
                    result["odds"].append({
                        "event_id": event_id,
                        "bookmakers": odds_data
                    })
        
        return result
    
    async def get_h2h_for_event(self, event_id):
        """Get head-to-head history for a specific event."""
        try:
            params = {
                "event_id": event_id,
                "qty": self.max_h2h_matches
            }
            
            response = await self._make_request("/v1/event/history", params)
            if response.get("success") == 1:
                return response.get("results", [])
            else:
                if self.debug:
                    print(f"Error fetching H2H data: {response.get('error')}")
                return []
        except Exception as e:
            if self.debug:
                print(f"Exception fetching H2H data: {e}")
            return []

    async def get_odds_for_event(self, event_id, target_bookmakers=None):
        """Get odds for a specific event from multiple bookmakers."""
        if target_bookmakers is None:
            target_bookmakers = self.PRIORITY_BOOKMAKERS[:3]  # Use first 3 bookmakers by default
        
        bookmakers_data = {}
        
        # Make separate requests for each bookmaker
        for bookmaker in target_bookmakers:
            try:
                params = {
                    "event_id": event_id,
                    "source": bookmaker,
                    "odds_market": "1"  # Just get match winner market (92_1)
                }
                
                response = await self._make_request("/v2/event/odds", params)
                
                if response.get("success") == 1:
                    results = response.get("results", {})
                    
                    # Format the data to match what your UI expects
                    odds_data = {
                        "odds": {
                            "92_1": []  # Table tennis match winner market
                        }
                    }
                    
                    # Extract match winner odds
                    for market_id, market_data in results.items():
                        if "92_1" in market_id:
                            odds_entry = {
                                "home_od": market_data.get("home_od", "N/A"),
                                "away_od": market_data.get("away_od", "N/A")
                            }
                            odds_data["odds"]["92_1"].append(odds_entry)
                    
                    if odds_data["odds"]["92_1"]:  # Only add if we found valid odds
                        bookmakers_data[bookmaker] = odds_data
                
                # Small delay to avoid rate limits
                await asyncio.sleep(self.rate_limit_delay)
                
            except Exception as e:
                if self.debug:
                    print(f"Error fetching odds from {bookmaker}: {e}")
        
        return bookmakers_data

    def get_bookmaker_name(self, bookmaker_id):
        """Convert bookmaker ID to name."""
        # Map of common bookmaker IDs to names
        bookmaker_map = {
            "1": "bet365",
            "2": "bwin",
            "3": "unibet",
            "8": "betfair",
            "12": "williamhill",
            "15": "pinnacle",
            # Add more as needed
        }
        
        return bookmaker_map.get(bookmaker_id, f"Bookmaker_{bookmaker_id}")


def save_to_json(data, filename="table_tennis_focused_data.json"):
    """Save data to a JSON file."""
    try:
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return str(obj)
                
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=json_serializer)
        print(f"Data successfully saved to {filename}")
        return True
    except Exception as e:
        print(f"Error saving data: {e}")
        return False


async def main():
    # Configuration parameters
    MAX_UPCOMING_PER_LEAGUE = 5  # Maximum number of upcoming matches to fetch per league
    MAX_H2H_MATCHES = 10  # Maximum number of head-to-head matches to fetch per event
    DEBUG_MODE = True
    
    print(f"Configuration: {MAX_UPCOMING_PER_LEAGUE} matches per league, {MAX_H2H_MATCHES} H2H matches per event")
    
    async with TableTennisAPI(TT_KEY, debug=DEBUG_MODE, 
                             max_upcoming_per_league=MAX_UPCOMING_PER_LEAGUE,
                             max_h2h_matches=MAX_H2H_MATCHES) as api:
        # Collect comprehensive data for upcoming and inplay events
        all_data = {
            "timestamp": datetime.now().isoformat(),
            "upcoming": {},
            "inplay": {},
            "leagues": api.TARGET_LEAGUE_IDS,
            "config": {
                "max_upcoming_per_league": MAX_UPCOMING_PER_LEAGUE,
                "max_h2h_matches": MAX_H2H_MATCHES
            }
        }
        
        # Get upcoming events for target leagues
        print("\n=== Fetching Upcoming Events for Target Leagues ===")
        print(f"Limiting to {MAX_UPCOMING_PER_LEAGUE} matches per league")
        upcoming_events = await api.get_events_for_target_leagues("upcoming")
        print(f"Found {len(upcoming_events)} upcoming events across all leagues")
        
        if upcoming_events:
            print("\n=== Processing Detailed Data for Upcoming Events ===")
            print(f"Will fetch up to {MAX_H2H_MATCHES} H2H matches per event")
            all_data["upcoming"] = await api.process_events_data(upcoming_events)
        
        # Get inplay events for target leagues
        print("\n=== Fetching Inplay Events for Target Leagues ===")
        inplay_events = await api.get_events_for_target_leagues("inplay")
        print(f"Found {len(inplay_events)} inplay events")
        
        if inplay_events:
            print("\n=== Processing Detailed Data for Inplay Events ===")
            all_data["inplay"] = await api.process_events_data(inplay_events)
        
        # Save collected data
        print("\n=== Saving Data to JSON File ===")
        save_to_json(all_data, "table_tennis_focused_data.json")
        print("Data collection complete!")


if __name__ == "__main__":
    asyncio.run(main())
