import asyncio
import aiohttp
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
import time
from collections import deque
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
        
        # Sliding window rate limiting (100 requests per minute)
        self.rate_limit_window = 60  # seconds
        self.rate_limit_max = 100   # requests per window
        self.request_times = deque()
        
        # Caching
        self._leagues_cache: Optional[List[Dict[str, Any]]] = None
        self._leagues_cache_time: Optional[datetime] = None
        self._teams_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._teams_cache_time: Dict[str, datetime] = {}
        self.cache_ttl = 3600  # 1 hour cache TTL
    
    async def __aenter__(self):
        # Simplified session for debugging
        self.session = aiohttp.ClientSession(headers=self.headers)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make async API request with sliding window rate limiting"""
        if not self.session:
            raise RuntimeError("Client must be used as async context manager")
        
        # DISABLED: Sliding window rate limiting for debugging
        # current_time = time.time()
        # self.request_times.append(current_time)
        
        url = f"{self.base_url}/{endpoint}"
        self.request_count += 1
        
        try:
            print(f"   Making request to {endpoint}... (request #{self.request_count})")
            print(f"   URL: {url}")
            print(f"   Params: {params}")
            async with self.session.get(url, params=params) as response:
                print(f"   Response status: {response.status}")
                if response.status == 200:
                    print(f"   Parsing JSON...")
                    result = await response.json()
                    print(f"   JSON parsed successfully")
                    return result
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
    
    async def get_leagues(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Get all available leagues with caching"""
        if use_cache and self._leagues_cache and self._leagues_cache_time:
            # Check if cache is still valid
            if (datetime.now() - self._leagues_cache_time).total_seconds() < self.cache_ttl:
                return self._leagues_cache
        
        response = await self._make_request("leagues")
        leagues = response.get('data', []) if response else []
        
        # Update cache
        if use_cache:
            self._leagues_cache = leagues
            self._leagues_cache_time = datetime.now()
        
        return leagues
    
    async def get_teams(self, league_id: Optional[str] = None, sport_id: Optional[str] = None, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Get teams with caching, optionally filtered by league or sport"""
        cache_key = f"{league_id or 'all'}_{sport_id or 'all'}"
        
        if use_cache and cache_key in self._teams_cache and cache_key in self._teams_cache_time:
            # Check if cache is still valid
            if (datetime.now() - self._teams_cache_time[cache_key]).total_seconds() < self.cache_ttl:
                return self._teams_cache[cache_key]
        
        params = {}
        if league_id:
            params['leagueID'] = league_id
        if sport_id:
            params['sportID'] = sport_id
        
        response = await self._make_request("teams", params)
        teams = response.get('data', []) if response else []
        
        # Update cache
        if use_cache:
            self._teams_cache[cache_key] = teams
            self._teams_cache_time[cache_key] = datetime.now()
        
        return teams
    
    async def get_events(self, league_id: str, limit: int = 10, odd_ids: Optional[List[str]] = None, 
                        include_opposing: bool = True, **kwargs) -> List[SGOEvent]:
        """Get events with optimized oddIDs targeting for entity efficiency"""
        # Optimize batch size based on requested limit
        batch_size = min(limit, 100) if limit <= 100 else 100
        
        params = {"leagueID": league_id, "limit": batch_size}
        
        # Add oddIDs optimization for entity efficiency
        if odd_ids:
            params["oddIDs"] = ','.join(odd_ids)
            if include_opposing:
                params["includeOpposingOddIDs"] = "true"
        
        # Add any additional parameters, converting booleans to strings
        for key, value in kwargs.items():
            if isinstance(value, bool):
                params[key] = str(value).lower()
            else:
                params[key] = value
        
        all_events = []
        next_cursor = None
        
        while len(all_events) < limit:
            if next_cursor:
                params['cursor'] = next_cursor
            
            # Adjust batch size for final request if needed
            remaining = limit - len(all_events)
            if remaining < batch_size:
                params['limit'] = remaining
            
            response = await self._make_request("events", params)
            
            if not response or 'data' not in response:
                break
            
            # Get the batch of events
            event_batch = response['data']
            
            # Process each event in this batch
            for event_data in event_batch:
                if len(all_events) >= limit:
                    break
                all_events.append(self._parse_event(event_data))
            
            # Check for next cursor to continue pagination
            next_cursor = response.get('nextCursor')
            if not next_cursor:
                break
        
        return all_events
    
    async def get_events_with_odds(self, league_ids: Optional[List[str]] = None, limit: int = 50) -> List[SGOEvent]:
        """Get events that have odds available"""
        if not league_ids:
            leagues = await self.get_leagues(use_cache=True)
            league_ids = [league['leagueID'] for league in leagues]
        
        events_with_odds = []
        
        for league_id in league_ids:
            # marketOddsAvailable=True is now set by default in get_events
            events = await self.get_events(league_id, limit=limit, marketOddsAvailable=True)
            events_with_odds.extend(events)  # All events should have odds due to filter
        
        return events_with_odds[:limit]  # Respect the limit across all leagues
    
    async def get_live_events(self, league_ids: Optional[List[str]] = None) -> List[SGOEvent]:
        """Get currently live events"""
        if not league_ids:
            leagues = await self.get_leagues(use_cache=True)
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
    
    async def get_historical_events(self, league_id: str, days_back: int = 7, 
                                   max_events: int = 100, require_odds: bool = True) -> List[SGOEvent]:
        """Get recent historical events using startsAfter cursor positioning"""
        from datetime import datetime, timezone, timedelta
        
        # Calculate the start date for our historical window
        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days_back)
        
        print(f"   Fetching events from {start_date.date()} to {now.date()}...")
        
        # Use startsAfter to position cursor at the beginning of our window
        # Use smaller batch sizes for better reliability
        batch_size = min(50, max_events) if days_back > 3 else min(100, max_events)
        
        params = {
            "leagueID": league_id,
            "startsAfter": start_date.isoformat(),
            "limit": batch_size
        }
        
        all_events = []
        next_cursor = None
        
        while len(all_events) < max_events:
            if next_cursor:
                params['cursor'] = next_cursor
            
            # Adjust batch size for final request if needed
            remaining = max_events - len(all_events)
            if remaining < 100:
                params['limit'] = remaining
            
            response = await self._make_request("events", params)
            
            if not response or 'data' not in response:
                break
            
            event_batch = response['data']
            
            for event_data in event_batch:
                if len(all_events) >= max_events:
                    break
                
                event = self._parse_event(event_data)
                
                # Only include events within our time window (between start_date and now)
                if event.starts_at:
                    try:
                        event_time = datetime.fromisoformat(event.starts_at.replace('Z', '')).replace(tzinfo=timezone.utc)
                        if event_time > now:
                            continue  # Skip future events
                        if event_time < start_date:
                            continue  # Skip events before our window
                    except:
                        pass
                
                # Filter for events with odds if required
                if require_odds and not event.has_odds:
                    continue
                
                all_events.append(event)
            
            # Check for next cursor
            next_cursor = response.get('nextCursor')
            if not next_cursor:
                break
            
            # Small delay between paginated requests for reliability
            if next_cursor:
                await asyncio.sleep(0.5)
        
        # Sort events by date (most recent first)
        all_events.sort(key=lambda e: e.starts_at or '', reverse=True)
        
        return all_events
    
    async def get_historical_odds_batch(self, league_id: str, days_back: int = 5, 
                                       max_events_per_day: int = 25, 
                                       essential_odds_only: bool = True) -> List[SGOEvent]:
        """Get historical odds efficiently using incremental daily collection with optimized oddIDs"""
        from datetime import datetime, timezone, timedelta
        
        print(f"📊 Collecting {days_back} days of historical events for {league_id}...")
        
        # Select appropriate essential odds based on league
        essential_odds = None
        if essential_odds_only:
            if league_id in ['MLB']:
                essential_odds = ESSENTIAL_BASEBALL
            elif league_id in ['NBA', 'WNBA', 'NCAAB']:
                essential_odds = ESSENTIAL_BASKETBALL
            elif league_id in ['NFL', 'NCAAF']:
                essential_odds = ESSENTIAL_FOOTBALL
            elif league_id in ['NHL']:
                essential_odds = ESSENTIAL_HOCKEY
            
            if essential_odds:
                print(f"   Using essential odds: {len(essential_odds)} market types")
        
        all_events = []
        now = datetime.now(timezone.utc)
        
        try:
            # Collect data day by day to avoid timeouts
            for day_offset in range(days_back, 0, -1):
                day_start = now - timedelta(days=day_offset)
                day_end = now - timedelta(days=day_offset-1)
                
                print(f"   Day {days_back-day_offset+1}: {day_start.date()}")
                
                try:
                    # Get events for this specific day with optimized oddIDs
                    day_events = await self.get_events(
                        league_id, 
                        limit=max_events_per_day, 
                        odd_ids=essential_odds,
                        startsAfter=day_start.isoformat()
                    )
                    
                    # Filter events for just this day and with odds
                    filtered_events = []
                    for event in day_events:
                        if event.starts_at:
                            event_time = datetime.fromisoformat(event.starts_at.replace('Z', '')).replace(tzinfo=timezone.utc)
                            if day_start <= event_time < day_end and event.has_odds:
                                filtered_events.append(event)
                    
                    print(f"     ✅ {len(filtered_events)} events with odds")
                    all_events.extend(filtered_events)
                    
                    # Brief pause between days for API courtesy
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    print(f"     ❌ Error for {day_start.date()}: {str(e)}")
            
            # Sort by date (most recent first)
            all_events.sort(key=lambda e: e.starts_at or '', reverse=True)
            
            print(f"✅ Total: {len(all_events)} historical events with odds")
            return all_events
            
        except Exception as e:
            print(f"❌ Error in batch collection: {e}")
            return []
    
    async def get_completed_events_with_results(self, league_id: str, days_back: int = 5, 
                                               max_events_per_day: int = 20) -> List[SGOEvent]:
        """Get completed events with final results using incremental collection"""
        
        # Use the efficient incremental batch collection
        all_events = await self.get_historical_odds_batch(
            league_id=league_id,
            days_back=days_back,
            max_events_per_day=max_events_per_day
        )
        
        # Filter for completed events with actual results data
        completed_with_results = [
            event for event in all_events 
            if event.is_completed and event.results and len(event.results) > 0
        ]
        
        print(f"📈 Found {len(completed_with_results)} completed events with results")
        return completed_with_results
    
    async def get_events_with_player_props(self, league_id: str, player_ids: List[str], 
                                          days_back: int = 3, max_events_per_day: int = 10) -> List[SGOEvent]:
        """Get events with specific player props for analysis"""
        from datetime import datetime, timezone, timedelta
        
        # SGO uses different market structure - this function needs to be adapted for SGO API
        print(f"⚠️  SGO player props functionality needs implementation for league: {league_id}")
        return []


async def test_historical_odds():
    """Test historical odds functionality"""
    print("SGO HISTORICAL ODDS TEST")
    print("=" * 40)
    
    async with SGOClient() as client:
        try:
            # Test 1: Get completed MLB games from last 5 days
            print("📊 Test 1: Getting completed MLB games from last 5 days...")
            completed_events = await client.get_completed_events_with_results('MLB', days_back=5, max_events_per_day=15)
            
            if completed_events:
                print(f"✅ Found {len(completed_events)} completed games")
                
                for i, event in enumerate(completed_events[:3]):
                    print(f"   Game {i+1}: {event.away_team.short_name} @ {event.home_team.short_name}")
                    print(f"            Final: {event.away_team.score} - {event.home_team.score}")
                    print(f"            Markets: {len(event.odds)}")
                    print(f"            Results: {len(event.results)}")
            
            # Test 2: Get historical odds batch from last 5 days  
            print(f"\n📈 Test 2: Getting historical odds from last 5 days...")
            historical_events = await client.get_historical_odds_batch('MLB', days_back=5, max_events_per_day=20)
            
            if historical_events:
                print(f"✅ Found {len(historical_events)} historical events")
                
                # Group by date
                from collections import defaultdict
                events_by_date = defaultdict(list)
                for event in historical_events:
                    if event.starts_at:
                        date_str = event.starts_at[:10]  # Extract date part
                        events_by_date[date_str].append(event)
                
                print(f"   Events by date:")
                for date, events in sorted(events_by_date.items()):
                    print(f"     {date}: {len(events)} events")
            
            print(f"\n📊 Total API requests made: {client.requests_made}")
            print("✅ Historical odds tests completed!")
            
        except Exception as e:
            print(f"❌ Test failed: {str(e)}")
            import traceback
            traceback.print_exc()


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
    # Run historical odds test instead of main
    asyncio.run(test_historical_odds())
