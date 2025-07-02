import asyncio
import aiohttp
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import time
from Creds import SGO_KEY


@dataclass
class SGOTeam:
    team_id: str
    name: str
    short_name: str
    score: Optional[int] = None
    colors: Optional[Dict[str, str]] = None


@dataclass
class SGOEvent:
    event_id: str
    sport_id: str
    league_id: str
    home_team: SGOTeam
    away_team: SGOTeam
    odds: Dict[str, Any]
    status: Dict[str, Any]
    players: Dict[str, Any]
    results: Dict[str, Any]
    starts_at: Optional[str] = None
    
    @property
    def is_live(self) -> bool:
        return self.status.get('started', False) and not self.status.get('ended', False)
    
    @property
    def is_completed(self) -> bool:
        return self.status.get('ended', False)
    
    @property
    def has_odds(self) -> bool:
        return bool(self.odds) and (self.status.get('oddsPresent', False) or self.status.get('oddsAvailable', False))


class SGOClient:
    """Async client for SportsGameOdds API"""
    
    def __init__(self, api_key: str = SGO_KEY):
        self.api_key = api_key
        self.base_url = "https://api.sportsgameodds.com/v2" 
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        self.session: Optional[aiohttp.ClientSession] = None
        self.request_count = 0
        self.last_request_time = 0
        self.rate_limit_delay = 2.0  # Reduced for testing
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers=self.headers)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make async API request with rate limiting"""
        if not self.session:
            raise RuntimeError("Client must be used as async context manager")
        
        # Rate limiting: ensure we don't exceed 10 requests per minute
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.rate_limit_delay:
            sleep_time = self.rate_limit_delay - time_since_last
            print(f"   Rate limiting: sleeping {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
        
        url = f"{self.base_url}/{endpoint}"
        self.request_count += 1
        self.last_request_time = time.time()
        
        try:
            timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout
            async with self.session.get(url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    # Rate limit hit, wait longer and retry once
                    await asyncio.sleep(60)  # Wait 1 minute
                    async with self.session.get(url, params=params) as retry_response:
                        if retry_response.status == 200:
                            return await retry_response.json()
                        else:
                            error_text = await retry_response.text()
                            raise Exception(f"API Error {retry_response.status}: {error_text}")
                else:
                    error_text = await response.text()
                    raise Exception(f"API Error {response.status}: {error_text}")
        
        except Exception as e:
            if "API Error" in str(e):
                raise
            raise Exception(f"Request failed for {endpoint}: {str(e)}")
    
    async def get_sports(self) -> List[Dict[str, Any]]:
        """Get all available sports"""
        response = await self._make_request("sports")
        return response.get('data', []) if response else []
    
    async def get_leagues(self) -> List[Dict[str, Any]]:
        """Get all available leagues"""
        response = await self._make_request("leagues")
        return response.get('data', []) if response else []
    
    async def get_events(self, league_id: str, limit: int = 10, **kwargs) -> List[SGOEvent]:
        """Get events for a specific league with optional parameters"""
        # Build URL like their GitHub example: /events?leagueID=MLB&startsAfter=...
        params = {"leagueID": league_id}
        
        # Add any additional parameters
        for key, value in kwargs.items():
            params[key] = value
            
        response = await self._make_request("events", params)
        
        if not response or 'data' not in response:
            return []
        
        events = []
        event_data_list = response['data']
        
        # Limit results if needed
        if limit and len(event_data_list) > limit:
            event_data_list = event_data_list[:limit]
            
        for event_data in event_data_list:
            events.append(self._parse_event(event_data))
        
        return events
    
    async def get_events_with_odds(self, league_ids: Optional[List[str]] = None, limit: int = 50) -> List[SGOEvent]:
        """Get events that have odds available"""
        if not league_ids:
            leagues = await self.get_leagues()
            league_ids = [league['leagueID'] for league in leagues]
        
        events_with_odds = []
        
        for league_id in league_ids:
            events = await self.get_events(league_id, limit=limit)
            for event in events:
                if event.has_odds:
                    events_with_odds.append(event)
        
        return events_with_odds
    
    async def get_live_events(self, league_ids: Optional[List[str]] = None) -> List[SGOEvent]:
        """Get currently live events"""
        if not league_ids:
            leagues = await self.get_leagues()
            league_ids = [league['leagueID'] for league in leagues]
        
        live_events = []
        
        for league_id in league_ids:
            events = await self.get_events(league_id, limit=20)
            for event in events:
                if event.is_live:
                    live_events.append(event)
        
        return live_events
    
    def _parse_event(self, event_data: Dict[str, Any]) -> SGOEvent:
        """Parse raw event data into SGOEvent object"""
        teams = event_data.get('teams', {})
        
        # Parse home team
        home_data = teams.get('home', {})
        home_team = SGOTeam(
            team_id=home_data.get('teamID', ''),
            name=home_data.get('names', {}).get('long', 'Unknown'),
            short_name=home_data.get('names', {}).get('short', 'UNK'),
            score=home_data.get('score'),
            colors=home_data.get('colors', {})
        )
        
        # Parse away team
        away_data = teams.get('away', {})
        away_team = SGOTeam(
            team_id=away_data.get('teamID', ''),
            name=away_data.get('names', {}).get('long', 'Unknown'),
            short_name=away_data.get('names', {}).get('short', 'UNK'),
            score=away_data.get('score'),
            colors=away_data.get('colors', {})
        )
        
        return SGOEvent(
            event_id=event_data.get('eventID', ''),
            sport_id=event_data.get('sportID', ''),
            league_id=event_data.get('leagueID', ''),
            home_team=home_team,
            away_team=away_team,
            odds=event_data.get('odds', {}),
            status=event_data.get('status', {}),
            players=event_data.get('players', {}),
            results=event_data.get('results', {}),
            starts_at=event_data.get('status', {}).get('startsAt')
        )
    
    def parse_odds_markets(self, odds: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Parse flat odds structure into organized markets"""
        markets = {}
        
        for odds_key, odds_value in odds.items():
            # Parse odds key format: points-{team}-{period}-{market}-{side}
            parts = odds_key.split('-')
            if len(parts) >= 5:
                stat_type = parts[0]  # points, assists, etc.
                team = parts[1]       # home, away, all
                period = parts[2]     # game, 1q, 2q, etc.
                market_type = parts[3] # ml, sp, ou (moneyline, spread, over/under)
                side = parts[4]       # home, away, over, under
                
                market_key = f"{stat_type}_{team}_{period}_{market_type}"
                
                if market_key not in markets:
                    markets[market_key] = {
                        'stat_type': stat_type,
                        'team': team,
                        'period': period,
                        'market_type': market_type,
                        'odds': {}
                    }
                
                markets[market_key]['odds'][side] = odds_value
        
        return markets
    
    def get_moneyline_odds(self, event: SGOEvent) -> Dict[str, Any]:
        """Extract moneyline odds from event"""
        moneyline = {}
        
        for key, value in event.odds.items():
            if 'game-ml-' in key:
                if 'home' in key:
                    moneyline['home'] = value
                elif 'away' in key:
                    moneyline['away'] = value
        
        return moneyline
    
    def get_spread_odds(self, event: SGOEvent) -> Dict[str, Any]:
        """Extract spread odds from event"""
        spread = {}
        
        for key, value in event.odds.items():
            if 'game-sp-' in key:
                if 'home' in key:
                    spread['home'] = value
                elif 'away' in key:
                    spread['away'] = value
        
        return spread
    
    def get_total_odds(self, event: SGOEvent) -> Dict[str, Any]:
        """Extract over/under odds from event"""
        totals = {}
        
        for key, value in event.odds.items():
            if 'game-ou-' in key:
                if 'over' in key:
                    totals['over'] = value
                elif 'under' in key:
                    totals['under'] = value
        
        return totals
    
    @property 
    def requests_made(self) -> int:
        """Get number of API requests made"""
        return self.request_count
    
    async def get_usage(self) -> Dict[str, Any]:
        """Get current rate limit and usage information"""
        response = await self._make_request("account/usage")
        return response if response else {}


async def main():
    """Main function to test SGO API usage and credits"""
    print("SportsGameOdds API - USAGE TRACKING TEST")
    print("=" * 50)
    
    async with SGOClient() as client:
        try:
            from datetime import datetime, timedelta, timezone
            
            # Check initial usage
            print("📊 Checking initial usage...")
            initial_usage = await client.get_usage()
            print(f"Initial usage data: {initial_usage}")
            
            # Get current time for upcoming games query
            today = datetime.now(timezone.utc)
            starts_after = today.isoformat()
            
            print(f"\n🎯 Making controlled query for upcoming MLB games...")
            print(f"   startsAfter: {starts_after}")
            
            # Make the successful query that you found works
            mlb_events = await client.get_events('MLB', limit=5, startsAfter=starts_after)
            
            print(f"✅ Query completed!")
            print(f"   Found {len(mlb_events)} events")
            
            if mlb_events:
                # Show first upcoming game details
                first_game = mlb_events[0]
                print(f"\n🏆 First upcoming game:")
                print(f"   {first_game.away_team.name} @ {first_game.home_team.name}")
                print(f"   Event ID: {first_game.event_id}")
                print(f"   Starts: {first_game.starts_at}")
                print(f"   Total markets: {len(first_game.odds)}")
                print(f"   Has live odds: {first_game.has_odds}")
                
                # Check for live odds
                if first_game.odds:
                    sample_odd = next(iter(first_game.odds.values()))
                    if isinstance(sample_odd, dict):
                        book_available = sample_odd.get('bookOddsAvailable', False)
                        print(f"   bookOddsAvailable: {book_available}")
            
            # Check usage after query
            print(f"\n📊 Checking usage after query...")
            final_usage = await client.get_usage()
            print(f"Final usage data: {final_usage}")
            
            # Calculate credits used
            if initial_usage and final_usage and 'data' in initial_usage and 'data' in final_usage:
                print(f"\n💰 CREDIT USAGE ANALYSIS:")
                
                initial_data = initial_usage['data']['rateLimits']
                final_data = final_usage['data']['rateLimits']
                
                # Check entities used (this is the "credits" in SGO)
                initial_entities = initial_data['per-month']['current-entities']
                final_entities = final_data['per-month']['current-entities']
                entities_used = final_entities - initial_entities
                
                print(f"   🎯 Entities (credits) used for this query: {entities_used}")
                print(f"   📊 Events returned: {len(mlb_events) if mlb_events else 0}")
                
                if mlb_events and len(mlb_events) > 0:
                    total_markets = sum(len(event.odds) for event in mlb_events)
                    print(f"   📈 Total markets returned: {total_markets}")
                    
                    if entities_used > 0:
                        print(f"   💡 Entities per event: {entities_used / len(mlb_events):.1f}")
                        print(f"   💡 Entities per market: {entities_used / total_markets:.3f}")
                
                # Show rate limit changes
                print(f"\n📊 RATE LIMIT CHANGES:")
                periods = ['per-minute', 'per-hour', 'per-day', 'per-month']
                
                for period in periods:
                    if period in initial_data and period in final_data:
                        initial_period = initial_data[period]
                        final_period = final_data[period]
                        
                        print(f"   {period.upper()}:")
                        for metric in ['current-requests', 'current-entities']:
                            if metric in initial_period and metric in final_period:
                                initial_val = initial_period[metric]
                                final_val = final_period[metric]
                                if final_val != initial_val:
                                    change = final_val - initial_val
                                    print(f"     {metric}: {initial_val} → {final_val} (+{change})")
                
                # Show monthly limits
                monthly = final_data.get('per-month', {})
                entities_limit = monthly.get('max-entities', 'unlimited')
                entities_current = monthly.get('current-entities', 0)
                
                print(f"\n📈 MONTHLY USAGE SUMMARY:")
                print(f"   Entities used: {entities_current} / {entities_limit}")
                if entities_limit != 'unlimited':
                    remaining = entities_limit - entities_current
                    print(f"   Entities remaining: {remaining}")
                    if entities_used > 0:
                        queries_remaining = remaining // entities_used
                        print(f"   Similar queries remaining: ~{queries_remaining}")
            
            # Dump all data to JSON file for inspection
            print(f"\n💾 Saving data to JSON file...")
            
            output_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query_parameters": {"leagueID": "MLB", "startsAfter": starts_after, "limit": 5},
                "initial_usage": initial_usage,
                "final_usage": final_usage,
                "api_requests_made": client.requests_made,
                "events_count": len(mlb_events) if mlb_events else 0,
                "events": []
            }
            
            if mlb_events:
                for event in mlb_events:
                    event_data = {
                        "event_id": event.event_id,
                        "sport_id": event.sport_id,
                        "league_id": event.league_id,
                        "starts_at": event.starts_at,
                        "home_team": {
                            "team_id": event.home_team.team_id,
                            "name": event.home_team.name,
                            "short_name": event.home_team.short_name,
                            "score": event.home_team.score,
                            "colors": event.home_team.colors
                        },
                        "away_team": {
                            "team_id": event.away_team.team_id,
                            "name": event.away_team.name,
                            "short_name": event.away_team.short_name,
                            "score": event.away_team.score,
                            "colors": event.away_team.colors
                        },
                        "status": event.status,
                        "is_live": event.is_live,
                        "is_completed": event.is_completed,
                        "has_odds": event.has_odds,
                        "odds_count": len(event.odds),
                        "players_count": len(event.players) if event.players else 0,
                        "odds": event.odds,
                        "players": event.players,
                        "results": event.results
                    }
                    output_data["events"].append(event_data)
            
            # Save to file with timestamp
            filename = f"sgo_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            filepath = f"/home/retupmoc/Desktop/EffortScraper/OddsAPI/{filename}"
            
            with open(filepath, 'w') as f:
                json.dump(output_data, f, indent=2, default=str)
            
            print(f"✅ Data saved to: {filename}")
            print(f"   File size: {len(json.dumps(output_data, default=str)) / 1024:.1f} KB")
            print(f"   Contains: {len(mlb_events) if mlb_events else 0} events with full odds and player data")
            
            print(f"\n📊 Total API requests made: {client.requests_made}")
            print(f"💡 Usage endpoint helps track credit consumption per query")
            
        except Exception as e:
            print(f"❌ Error: {str(e)}")


if __name__ == "__main__":
    asyncio.run(main())
