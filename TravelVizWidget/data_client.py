import requests
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QThread, QDateTime
from PyQt6.QtSql import QSqlDatabase, QSqlQuery


class GameStatus(Enum):
    """Game status enumeration"""
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


@dataclass
class TeamInfo:
    """Team information"""
    team_id: str
    abbreviation: str
    display_name: str
    location: str
    color: str
    alternate_color: str
    logo_url: Optional[str] = None


@dataclass
class Venue:
    """Venue information"""
    venue_id: str
    name: str
    city: str
    state: str
    country: str
    latitude: float
    longitude: float
    capacity: Optional[int] = None


@dataclass
class GameData:
    """Individual game data"""
    game_id: str
    date: datetime
    home_team: TeamInfo
    away_team: TeamInfo
    venue: Venue
    status: GameStatus
    week: Optional[int] = None
    season_type: Optional[str] = None
    league: str = "MLB"


@dataclass
class TeamTravelData:
    """Team travel data inferred from schedule"""
    team_name: str
    team_id: str
    departure_city: str
    arrival_city: str
    game_date: datetime
    travel_date: datetime
    departure_airport: str
    arrival_airport: str
    confidence: str = "schedule_inferred"
    game_id: Optional[str] = None
    opponent: Optional[str] = None


class ESPNScheduleClient:
    """ESPN API client for sports schedule data"""
    
    def __init__(self):
        self.base_url = "https://site.api.espn.com/apis/site/v2/sports"
        self.session = requests.Session()
        self.team_airports = self.load_team_airports()
        
    def load_team_airports(self) -> Dict[str, str]:
        """Load mapping of team cities to airport codes"""
        # Major team city to airport mappings
        return {
            # MLB Teams
            "Los Angeles": "LAX",
            "New York": "JFK", 
            "Boston": "BOS",
            "Chicago": "ORD",
            "Houston": "IAH",
            "Philadelphia": "PHL",
            "Phoenix": "PHX",
            "San Antonio": "SAT",
            "San Diego": "SAN",
            "Dallas": "DFW",
            "San Jose": "SJC",
            "Austin": "AUS",
            "Jacksonville": "JAX",
            "San Francisco": "SFO",
            "Columbus": "CMH",
            "Fort Worth": "DFW",
            "Charlotte": "CLT",
            "Detroit": "DTW",
            "El Paso": "ELP",
            "Memphis": "MEM",
            "Baltimore": "BWI",
            "Louisville": "SDF",
            "Milwaukee": "MKE",
            "Las Vegas": "LAS",
            "Albuquerque": "ABQ",
            "Tucson": "TUS",
            "Fresno": "FAT",
            "Sacramento": "SMF",
            "Mesa": "PHX",
            "Kansas City": "MCI",
            "Atlanta": "ATL",
            "Virginia Beach": "ORF",
            "Omaha": "OMA",
            "Colorado Springs": "COS",
            "Raleigh": "RDU",
            "Miami": "MIA",
            "Oakland": "OAK",
            "Minneapolis": "MSP",
            "Tulsa": "TUL",
            "Arlington": "DFW",
            "Tampa": "TPA",
            "New Orleans": "MSY",
            "Wichita": "ICT",
            "Cleveland": "CLE",
            "Anaheim": "LAX",
            "Honolulu": "HNL",
            "Henderson": "LAS",
            "Stockton": "SCK",
            "Corpus Christi": "CRP",
            "Lexington": "LEX",
            "Anchorage": "ANC",
            "Plano": "DFW",
            "Newark": "EWR",
            "Greensboro": "GSO",
            "Lincoln": "LNK",
            "Buffalo": "BUF",
            "Fort Wayne": "FWA",
            "Jersey City": "EWR",
            "Chula Vista": "SAN",
            "Orlando": "MCO",
            "St. Paul": "MSP",
            "Norfolk": "ORF",
            "Chandler": "PHX",
            "Laredo": "LRD",
            "Madison": "MSN",
            "Durham": "RDU",
            "Lubbock": "LBB",
            "Baton Rouge": "BTR",
            "Garland": "DFW",
            "Hialeah": "MIA",
            "Reno": "RNO",
            "Chesapeake": "ORF",
            "Gilbert": "PHX",
            "Boise": "BOI",
            "Austin": "AUS",
            # Add more mappings as needed
            "Toronto": "YYZ",
            "Montreal": "YUL",
            "Vancouver": "YVR",
            "Calgary": "YYC",
            "Edmonton": "YEG",
            "Ottawa": "YOW",
            "Winnipeg": "YWG",
            "Seattle": "SEA",
            "Portland": "PDX",
            "Denver": "DEN",
            "Salt Lake City": "SLC",
            "Nashville": "BNA",
            "Cincinnati": "CVG",
            "Pittsburgh": "PIT",
            "Washington": "DCA",
            "St. Louis": "STL",
            "Indianapolis": "IND"
        }
    
    def get_mlb_schedule(self, date_range: Optional[str] = None) -> List[GameData]:
        """Get MLB schedule data"""
        url = f"{self.base_url}/baseball/mlb/scoreboard"
        if date_range:
            url += f"?dates={date_range}"
            
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            games = []
            for event in data.get('events', []):
                game = self._parse_game_data(event, "MLB")
                if game:
                    games.append(game)
            
            return games
            
        except Exception as e:
            print(f"ESPN MLB API error: {e}")
            return []
    
    def get_nfl_schedule(self, week: Optional[int] = None, season_type: int = 2) -> List[GameData]:
        """Get NFL schedule data"""
        url = f"{self.base_url}/football/nfl/scoreboard"
        params = {"seasontype": season_type}
        if week:
            params["week"] = week
            
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            games = []
            for event in data.get('events', []):
                game = self._parse_game_data(event, "NFL")
                if game:
                    games.append(game)
            
            return games
            
        except Exception as e:
            print(f"ESPN NFL API error: {e}")
            return []
    
    def get_nba_schedule(self, date_range: Optional[str] = None) -> List[GameData]:
        """Get NBA schedule data"""
        url = f"{self.base_url}/basketball/nba/scoreboard"
        if date_range:
            url += f"?dates={date_range}"
            
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            games = []
            for event in data.get('events', []):
                game = self._parse_game_data(event, "NBA")
                if game:
                    games.append(game)
            
            return games
            
        except Exception as e:
            print(f"ESPN NBA API error: {e}")
            return []
    
    def get_nhl_schedule(self, date_range: Optional[str] = None) -> List[GameData]:
        """Get NHL schedule data"""
        url = f"{self.base_url}/hockey/nhl/scoreboard"
        if date_range:
            url += f"?dates={date_range}"
            
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            games = []
            for event in data.get('events', []):
                game = self._parse_game_data(event, "NHL")
                if game:
                    games.append(game)
            
            return games
            
        except Exception as e:
            print(f"ESPN NHL API error: {e}")
            return []
    
    def _parse_game_data(self, event_data: Dict[str, Any], league: str) -> Optional[GameData]:
        """Parse ESPN event data into GameData"""
        try:
            # Basic event info
            game_id = event_data.get('id', '')
            date_str = event_data.get('date', '')
            
            # Parse date - ensure timezone-naive datetime
            game_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            # Convert to naive datetime to avoid timezone comparison issues
            if game_date.tzinfo is not None:
                game_date = game_date.replace(tzinfo=None)
            
            # Get competition data
            competition = event_data.get('competitions', [{}])[0]
            competitors = competition.get('competitors', [])
            
            if len(competitors) != 2:
                return None
            
            # Parse teams
            home_team_data = None
            away_team_data = None
            
            for comp in competitors:
                team_data = comp.get('team', {})
                if comp.get('homeAway') == 'home':
                    home_team_data = team_data
                else:
                    away_team_data = team_data
            
            if not home_team_data or not away_team_data:
                return None
            
            # Create team objects
            home_team = TeamInfo(
                team_id=home_team_data.get('id', ''),
                abbreviation=home_team_data.get('abbreviation', ''),
                display_name=home_team_data.get('displayName', ''),
                location=home_team_data.get('location', ''),
                color=home_team_data.get('color', '#000000'),
                alternate_color=home_team_data.get('alternateColor', '#FFFFFF'),
                logo_url=home_team_data.get('logo', '')
            )
            
            away_team = TeamInfo(
                team_id=away_team_data.get('id', ''),
                abbreviation=away_team_data.get('abbreviation', ''),
                display_name=away_team_data.get('displayName', ''),
                location=away_team_data.get('location', ''),
                color=away_team_data.get('color', '#000000'),
                alternate_color=away_team_data.get('alternateColor', '#FFFFFF'),
                logo_url=away_team_data.get('logo', '')
            )
            
            # Parse venue
            venue_data = competition.get('venue', {})
            venue = Venue(
                venue_id=venue_data.get('id', ''),
                name=venue_data.get('fullName', ''),
                city=venue_data.get('address', {}).get('city', ''),
                state=venue_data.get('address', {}).get('state', ''),
                country=venue_data.get('address', {}).get('country', ''),
                latitude=float(venue_data.get('address', {}).get('latitude', 0)),
                longitude=float(venue_data.get('address', {}).get('longitude', 0)),
                capacity=venue_data.get('capacity')
            )
            
            # Parse status
            status_data = competition.get('status', {}).get('type', {})
            status_name = status_data.get('name', 'scheduled').lower()
            
            status_map = {
                'pre': GameStatus.SCHEDULED,
                'in': GameStatus.IN_PROGRESS,
                'final': GameStatus.FINAL,
                'postponed': GameStatus.POSTPONED,
                'cancelled': GameStatus.CANCELLED
            }
            
            status = GameStatus.SCHEDULED
            for key, value in status_map.items():
                if key in status_name:
                    status = value
                    break
            
            # Additional info for NFL
            week = None
            season_type = None
            if league == "NFL":
                week = event_data.get('week', {}).get('number')
                season_type = event_data.get('season', {}).get('type', {}).get('name')
            
            return GameData(
                game_id=game_id,
                date=game_date,
                home_team=home_team,
                away_team=away_team,
                venue=venue,
                status=status,
                week=week,
                season_type=season_type,
                league=league
            )
            
        except Exception as e:
            print(f"Error parsing game data: {e}")
            return None


class TravelInferenceEngine:
    """Engine to infer team travel patterns from game schedules"""
    
    def __init__(self, airport_mappings: Dict[str, str]):
        self.airport_mappings = airport_mappings
    
    def infer_travel_from_games(self, games: List[GameData]) -> List[TeamTravelData]:
        """Infer team travel patterns from game schedule"""
        travel_data = []
        
        # Group games by team
        team_schedules = {}
        for game in games:
            home_team_id = game.home_team.team_id
            away_team_id = game.away_team.team_id
            
            if home_team_id not in team_schedules:
                team_schedules[home_team_id] = []
            if away_team_id not in team_schedules:
                team_schedules[away_team_id] = []
            
            team_schedules[home_team_id].append((game, 'home'))
            team_schedules[away_team_id].append((game, 'away'))
        
        # Infer travel for each team
        for team_id, schedule in team_schedules.items():
            # Sort by date
            schedule.sort(key=lambda x: x[0].date)
            
            team_travel = self._infer_team_travel(schedule)
            travel_data.extend(team_travel)
        
        return travel_data
    
    def _infer_team_travel(self, team_schedule: List[Tuple[GameData, str]]) -> List[TeamTravelData]:
        """Infer travel for a specific team's schedule"""
        travel_data = []
        
        for i, (game, home_away) in enumerate(team_schedule):
            if home_away == 'away':
                # Team is traveling to an away game
                team_info = game.away_team
                departure_city = team_info.location
                arrival_city = game.venue.city
                
                # Estimate travel date (typically 1 day before game) - ensure naive datetime
                game_date = game.date
                if hasattr(game_date, 'tzinfo') and game_date.tzinfo is not None:
                    game_date = game_date.replace(tzinfo=None)
                
                travel_date = game_date - timedelta(days=1)
                
                # Get airport codes
                dep_airport = self.airport_mappings.get(departure_city, departure_city[:3].upper())
                arr_airport = self.airport_mappings.get(arrival_city, arrival_city[:3].upper())
                
                travel = TeamTravelData(
                    team_name=team_info.display_name,
                    team_id=team_info.team_id,
                    departure_city=departure_city,
                    arrival_city=arrival_city,
                    game_date=game_date,
                    travel_date=travel_date,
                    departure_airport=dep_airport,
                    arrival_airport=arr_airport,
                    confidence="schedule_inferred",
                    game_id=game.game_id,
                    opponent=game.home_team.display_name
                )
                travel_data.append(travel)
                
                # Return trip (day after game)
                return_date = game_date + timedelta(days=1)
                return_travel = TeamTravelData(
                    team_name=team_info.display_name,
                    team_id=team_info.team_id,
                    departure_city=arrival_city,
                    arrival_city=departure_city,
                    game_date=game_date,
                    travel_date=return_date,
                    departure_airport=arr_airport,
                    arrival_airport=dep_airport,
                    confidence="schedule_inferred",
                    game_id=game.game_id,
                    opponent=game.home_team.display_name
                )
                travel_data.append(return_travel)
        
        return travel_data


class SportsDataCache:
    """SQLite cache for sports schedule data"""
    
    def __init__(self, cache_file: str = "sports_cache.db"):
        self.cache_file = cache_file
        self.init_database()
    
    def init_database(self):
        """Initialize cache database"""
        self.db = QSqlDatabase.addDatabase("QSQLITE", "sports_cache")
        self.db.setDatabaseName(self.cache_file)
        
        if not self.db.open():
            print(f"Failed to open sports cache database: {self.db.lastError().text()}")
            return
        
        query = QSqlQuery(self.db)
        
        # Games cache
        query.exec("""
        CREATE TABLE IF NOT EXISTS games_cache (
            id INTEGER PRIMARY KEY,
            game_id TEXT UNIQUE,
            league TEXT,
            game_date TIMESTAMP,
            home_team TEXT,
            away_team TEXT,
            venue_city TEXT,
            game_data TEXT,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
        """)
        
        # Travel data cache
        query.exec("""
        CREATE TABLE IF NOT EXISTS travel_cache (
            id INTEGER PRIMARY KEY,
            team_id TEXT,
            departure_city TEXT,
            arrival_city TEXT,
            travel_date TIMESTAMP,
            travel_data TEXT,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
        """)
    
    def cache_games(self, games: List[GameData], cache_duration_hours: int = 24):
        """Cache game data"""
        query = QSqlQuery(self.db)
        expires_at = datetime.now() + timedelta(hours=cache_duration_hours)
        
        query.prepare("""
        INSERT OR REPLACE INTO games_cache 
        (game_id, league, game_date, home_team, away_team, venue_city, game_data, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """)
        
        for game in games:
            query.addBindValue(game.game_id)
            query.addBindValue(game.league)
            query.addBindValue(game.date)
            query.addBindValue(game.home_team.display_name)
            query.addBindValue(game.away_team.display_name)
            query.addBindValue(game.venue.city)
            query.addBindValue(json.dumps(self._serialize_game(game), default=str))
            query.addBindValue(expires_at)
            query.exec()
    
    def get_cached_games(self, league: Optional[str] = None) -> List[GameData]:
        """Retrieve cached game data"""
        query = QSqlQuery(self.db)
        
        if league:
            query.prepare("""
            SELECT game_data FROM games_cache 
            WHERE league = ? AND expires_at > ?
            """)
            query.addBindValue(league)
            query.addBindValue(datetime.now())
        else:
            query.prepare("""
            SELECT game_data FROM games_cache 
            WHERE expires_at > ?
            """)
            query.addBindValue(datetime.now())
        
        games = []
        if query.exec():
            while query.next():
                try:
                    game_data = json.loads(query.value(0))
                    game = self._deserialize_game(game_data)
                    if game:
                        games.append(game)
                except Exception as e:
                    print(f"Error deserializing cached game: {e}")
        
        return games
    
    def _serialize_game(self, game: GameData) -> Dict:
        """Serialize game data for caching"""
        return {
            'game_id': game.game_id,
            'date': game.date.isoformat(),
            'home_team': {
                'team_id': game.home_team.team_id,
                'abbreviation': game.home_team.abbreviation,
                'display_name': game.home_team.display_name,
                'location': game.home_team.location,
                'color': game.home_team.color,
                'alternate_color': game.home_team.alternate_color,
                'logo_url': game.home_team.logo_url
            },
            'away_team': {
                'team_id': game.away_team.team_id,
                'abbreviation': game.away_team.abbreviation,
                'display_name': game.away_team.display_name,
                'location': game.away_team.location,
                'color': game.away_team.color,
                'alternate_color': game.away_team.alternate_color,
                'logo_url': game.away_team.logo_url
            },
            'venue': {
                'venue_id': game.venue.venue_id,
                'name': game.venue.name,
                'city': game.venue.city,
                'state': game.venue.state,
                'country': game.venue.country,
                'latitude': game.venue.latitude,
                'longitude': game.venue.longitude,
                'capacity': game.venue.capacity
            },
            'status': game.status.value,
            'week': game.week,
            'season_type': game.season_type,
            'league': game.league
        }
    
    def _deserialize_game(self, data: Dict) -> Optional[GameData]:
        """Deserialize game data from cache"""
        try:
            home_team = TeamInfo(**data['home_team'])
            away_team = TeamInfo(**data['away_team'])
            venue = Venue(**data['venue'])
            
            return GameData(
                game_id=data['game_id'],
                date=datetime.fromisoformat(data['date']),
                home_team=home_team,
                away_team=away_team,
                venue=venue,
                status=GameStatus(data['status']),
                week=data.get('week'),
                season_type=data.get('season_type'),
                league=data['league']
            )
        except Exception as e:
            print(f"Error deserializing game data: {e}")
            return None


class SportsDataAggregator(QObject):
    """Sports data aggregator with ESPN API integration"""
    
    dataUpdated = pyqtSignal(list)  # Emits List[TeamTravelData]
    progressUpdated = pyqtSignal(int)
    errorOccurred = pyqtSignal(str)
    
    def __init__(self, config: Dict[str, str]):
        super().__init__()
        
        # Initialize ESPN client and travel inference engine
        self.espn_client = ESPNScheduleClient()
        self.inference_engine = TravelInferenceEngine(self.espn_client.team_airports)
        
        # Initialize cache
        self.cache = SportsDataCache()
        
        # Current data
        self.current_games = []
        self.current_travel_data = []
        self.current_league = "MLB"
        
        # Update timer
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_data)
    
    def load_league_schedule(self, league: str):
        """Load schedule for specified league"""
        self.current_league = league
        self.progressUpdated.emit(20)
        
        try:
            # Try cache first
            cached_games = self.cache.get_cached_games(league)
            
            if cached_games:
                print(f"Using cached {league} data")
                self.current_games = cached_games
                self.progressUpdated.emit(60)
            else:
                # Fetch from ESPN API
                print(f"Fetching {league} schedule from ESPN API")
                
                if league == "MLB":
                    games = self.espn_client.get_mlb_schedule()
                elif league == "NFL":
                    games = self.espn_client.get_nfl_schedule()
                elif league == "NBA":
                    games = self.espn_client.get_nba_schedule()
                elif league == "NHL":
                    games = self.espn_client.get_nhl_schedule()
                else:
                    raise ValueError(f"Unsupported league: {league}")
                
                if games:
                    self.current_games = games
                    self.cache.cache_games(games)
                    self.progressUpdated.emit(70)
                else:
                    self.errorOccurred.emit(f"No {league} games found")
                    return
            
            # Infer travel patterns
            travel_data = self.inference_engine.infer_travel_from_games(self.current_games)
            self.current_travel_data = travel_data
            
            self.progressUpdated.emit(90)
            self.dataUpdated.emit(travel_data)
            self.progressUpdated.emit(100)
            
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load {league} schedule: {str(e)}")
            self.progressUpdated.emit(0)
    
    def refresh_current_data(self):
        """Refresh current league data from API"""
        if self.current_league:
            # Force refresh by clearing cache
            # Implementation would clear relevant cache entries
            self.load_league_schedule(self.current_league)
    
    def update_data(self):
        """Periodic data update"""
        if self.current_league:
            self.load_league_schedule(self.current_league)
    
    def get_travel_by_team(self, team_id: str) -> List[TeamTravelData]:
        """Get travel data for specific team"""
        return [t for t in self.current_travel_data if t.team_id == team_id]
    
    def get_travel_by_route(self, departure: str, arrival: str) -> List[TeamTravelData]:
        """Get travel data for specific route"""
        return [t for t in self.current_travel_data 
                if t.departure_city == departure and t.arrival_city == arrival]
