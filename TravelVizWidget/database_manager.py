import sqlite3
import json
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
import dataclasses
from dataclasses import dataclass
import logging
from enum import Enum

logger = logging.getLogger(__name__)



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
    division: Optional[str] = None
    league: Optional[str] = None
    conference: Optional[str] = None


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
    timezone: Optional[str] = None


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
    season: Optional[str] = None
    series_description: Optional[str] = None


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
    series_game_number: Optional[int] = None
    homestand_game_number: Optional[int] = None



class DatabaseManager:
    """Manages SQLite database for multi-league sports schedule and travel data"""
    
    def __init__(self, db_path: str = "sports_data.db"):
        # Store as absolute path string for thread-safe database access.
        # Relative paths are anchored to this module's directory, not the CWD,
        # so embedding hosts launched elsewhere reuse the same database.
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        self.db_path = str(path)
        self.db_version = "2.0"  # Updated for multi-league support
        self.setup_logging()
        self.init_database()
    
    def setup_logging(self):
        """Setup logging for database operations"""
        # No basicConfig here — the hosting application configures logging
        self.logger = logging.getLogger(__name__)
    
    def init_database(self):
        """Initialize database with required tables and indexes"""
        try:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                # Enable WAL mode for better concurrent read/write performance
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")  # 30 second busy timeout
                
                # Metadata table for tracking data freshness and versions
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Teams table - updated with league support
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS teams (
                        team_id TEXT NOT NULL,
                        league TEXT NOT NULL,
                        abbreviation TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        location TEXT,
                        color TEXT,
                        alternate_color TEXT,
                        logo_url TEXT,
                        division TEXT,
                        conference TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (team_id, league)
                    )
                """)
                
                # Venues table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS venues (
                        venue_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        city TEXT NOT NULL,
                        state TEXT,
                        country TEXT,
                        latitude REAL,
                        longitude REAL,
                        capacity INTEGER,
                        timezone TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Games table - main schedule data with league support
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS games (
                        game_id TEXT PRIMARY KEY,
                        date TIMESTAMP NOT NULL,
                        home_team_id TEXT NOT NULL,
                        away_team_id TEXT NOT NULL,
                        venue_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        week INTEGER,
                        season_type TEXT,
                        league TEXT NOT NULL,
                        season TEXT NOT NULL,
                        series_description TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (home_team_id, league) REFERENCES teams (team_id, league),
                        FOREIGN KEY (away_team_id, league) REFERENCES teams (team_id, league),
                        FOREIGN KEY (venue_id) REFERENCES venues (venue_id)
                    )
                """)
                
                # Travel data table - inferred travel patterns with league support
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS travel_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        team_name TEXT NOT NULL,
                        team_id TEXT NOT NULL,
                        league TEXT NOT NULL,
                        departure_city TEXT NOT NULL,
                        arrival_city TEXT NOT NULL,
                        game_date TIMESTAMP NOT NULL,
                        travel_date TIMESTAMP NOT NULL,
                        departure_airport TEXT,
                        arrival_airport TEXT,
                        confidence TEXT DEFAULT 'schedule_inferred',
                        game_id TEXT,
                        opponent TEXT,
                        series_game_number INTEGER,
                        homestand_game_number INTEGER,
                        season TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (team_id, league) REFERENCES teams (team_id, league),
                        FOREIGN KEY (game_id) REFERENCES games (game_id)
                    )
                """)
                
                # Season cache table - tracks what seasons have been scraped
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS season_cache (
                        season TEXT NOT NULL,
                        league TEXT NOT NULL,
                        games_count INTEGER DEFAULT 0,
                        travel_count INTEGER DEFAULT 0,
                        last_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        is_complete BOOLEAN DEFAULT FALSE,
                        scrape_hash TEXT,
                        PRIMARY KEY (season, league)
                    )
                """)
                
                # Create indexes for performance
                self.create_indexes(conn)
                
                # Set database version
                conn.execute("""
                    INSERT OR REPLACE INTO metadata (key, value, updated_at) 
                    VALUES (?, ?, ?)
                """, ("db_version", self.db_version, datetime.now().isoformat()))
                
                conn.commit()
                self.logger.info(f"Multi-league database initialized at {self.db_path}")
                
        except Exception as e:
            self.logger.error(f"Database initialization error: {e}")
            raise
    
    def create_indexes(self, conn: sqlite3.Connection):
        """Create database indexes for optimal query performance"""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_games_date ON games (date)",
            "CREATE INDEX IF NOT EXISTS idx_games_season_league ON games (season, league)",
            "CREATE INDEX IF NOT EXISTS idx_games_teams ON games (home_team_id, away_team_id)",
            "CREATE INDEX IF NOT EXISTS idx_games_league ON games (league)",
            "CREATE INDEX IF NOT EXISTS idx_travel_date ON travel_data (travel_date)",
            "CREATE INDEX IF NOT EXISTS idx_travel_team_league ON travel_data (team_id, league)",
            "CREATE INDEX IF NOT EXISTS idx_travel_season_league ON travel_data (season, league)",
            "CREATE INDEX IF NOT EXISTS idx_travel_game ON travel_data (game_id)",
            "CREATE INDEX IF NOT EXISTS idx_teams_league ON teams (league)",
            "CREATE INDEX IF NOT EXISTS idx_season_cache_league ON season_cache (league)",
        ]
        
        for index_sql in indexes:
            conn.execute(index_sql)
    
    def is_season_cached(self, season: str, league: str) -> Tuple[bool, Optional[datetime]]:
        """Check if season data is cached and get last update time"""
        try:
            # SAME FIX HERE TOO
            if league in ['NBA', 'NHL'] and season and '-' not in season:
                year = int(season)
                if year >= 2024:
                    start_year = year - 1
                    season = f"{start_year}-{str(year)[2:]}"
                    logger.debug(f"🔧 FIXED: Converted {league} season '{year}' to '{season}' in cache check")
            
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT is_complete, last_updated, games_count 
                    FROM season_cache 
                    WHERE season = ? AND league = ?
                """, (season, league))
                
                result = cursor.fetchone()
                if result:
                    is_complete, last_updated_str, games_count = result
                    last_updated = datetime.fromisoformat(last_updated_str) if last_updated_str else None
                    
                    # Consider cached if complete and has games
                    return bool(is_complete and games_count > 0), last_updated
                
                return False, None
                
        except Exception as e:
            self.logger.error(f"Error checking season cache: {e}")
            return False, None
    
    def should_refresh_season(self, season: str, league: str, force_refresh: bool = False) -> bool:
        """Determine if season data should be refreshed"""
        if force_refresh:
            return True
        
        is_cached, last_updated = self.is_season_cached(season, league)
        
        if not is_cached:
            return True
        
        # For current season, refresh if data is older than 1 day
        current_date = datetime.now()
        
        # League-specific current season logic
        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons span two years, check if we're in the current season
            if '-' in season:
                start_year = int(season.split('-')[0])
                if current_date.year == start_year or current_date.year == start_year + 1:
                    # This is the current season
                    if last_updated and (current_date - last_updated) > timedelta(days=1):
                        return True
        else:  # MLB
            current_year = str(current_date.year)
            if season == current_year and last_updated:
                time_since_update = current_date - last_updated
                return time_since_update > timedelta(days=1)
        
        # For past seasons, don't refresh unless forced
        return False
    
    def save_teams(self, teams: List[TeamInfo], league: str = None):
        """Save team information to database with league support"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                for team in teams:
                    team_league = league or getattr(team, 'league', 'MLB')
                    conn.execute("""
                        INSERT OR REPLACE INTO teams 
                        (team_id, league, abbreviation, display_name, location, color, 
                         alternate_color, logo_url, division, conference)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        team.team_id, team_league, team.abbreviation, team.display_name,
                        team.location, team.color, team.alternate_color,
                        team.logo_url, team.division, team.conference
                    ))
                
                conn.commit()
                self.logger.info(f"Saved {len(teams)} {team_league} teams to database")
                
        except Exception as e:
            self.logger.error(f"Error saving teams: {e}")
            raise
    
    def save_venues(self, venues: List[Venue]):
        """Save venue information to database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                for venue in venues:
                    conn.execute("""
                        INSERT OR REPLACE INTO venues 
                        (venue_id, name, city, state, country, latitude, longitude, capacity, timezone)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        venue.venue_id, venue.name, venue.city, venue.state,
                        venue.country, venue.latitude, venue.longitude,
                        venue.capacity, venue.timezone
                    ))
                
                conn.commit()
                self.logger.info(f"Saved {len(venues)} venues to database")
                
        except Exception as e:
            self.logger.error(f"Error saving venues: {e}")
            raise
    
    def save_games(self, games: List[GameData], season: str, league: str):
        """Save game data to database and update season cache"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Save teams and venues first (will be ignored if they exist)
                unique_teams = {}
                unique_venues = {}
                
                for game in games:
                    unique_teams[game.home_team.team_id] = game.home_team
                    unique_teams[game.away_team.team_id] = game.away_team
                    unique_venues[game.venue.venue_id] = game.venue
                
                # Save teams and venues
                if unique_teams:
                    self.save_teams(list(unique_teams.values()), league)
                if unique_venues:
                    self.save_venues(list(unique_venues.values()))
                
                # Save games
                for game in games:
                    conn.execute("""
                        INSERT OR REPLACE INTO games 
                        (game_id, date, home_team_id, away_team_id, venue_id, status,
                         week, season_type, league, season, series_description, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        game.game_id, game.date.isoformat(), game.home_team.team_id,
                        game.away_team.team_id, game.venue.venue_id, game.status.value,
                        game.week, game.season_type, game.league, game.season,
                        game.series_description, datetime.now().isoformat()
                    ))
                
                # Update season cache
                scrape_hash = self._generate_scrape_hash(games)
                conn.execute("""
                    INSERT OR REPLACE INTO season_cache 
                    (season, league, games_count, last_scraped, last_updated, is_complete, scrape_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    season, league, len(games), datetime.now().isoformat(),
                    datetime.now().isoformat(), True, scrape_hash
                ))
                
                conn.commit()
                self.logger.info(f"Saved {len(games)} games for {season} {league} season")
                
        except Exception as e:
            self.logger.error(f"Error saving games: {e}")
            raise
    
    def upsert_games(self, games: List[GameData], season: str, league: str):
        """Insert/update a subset of games (e.g. a rolling upcoming window from
        flashscore) without rewriting season-cache counts from the subset.
        Bumps last_updated so the staleness check stops demanding a re-scrape,
        and recounts games_count from the table."""
        if not games:
            return
        try:
            unique_venues = {g.venue.venue_id: g.venue for g in games}
            if unique_venues:
                self.save_venues(list(unique_venues.values()))

            with sqlite3.connect(self.db_path) as conn:
                for game in games:
                    conn.execute("""
                        INSERT OR REPLACE INTO games
                        (game_id, date, home_team_id, away_team_id, venue_id, status,
                         week, season_type, league, season, series_description, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        game.game_id, game.date.isoformat(), game.home_team.team_id,
                        game.away_team.team_id, game.venue.venue_id, game.status.value,
                        game.week, game.season_type, game.league, game.season,
                        game.series_description, datetime.now().isoformat()
                    ))

                count = conn.execute(
                    "SELECT COUNT(*) FROM games WHERE season = ? AND league = ?",
                    (season, league)).fetchone()[0]
                conn.execute("""
                    UPDATE season_cache
                    SET games_count = ?, last_updated = ?
                    WHERE season = ? AND league = ?
                """, (count, datetime.now().isoformat(), season, league))

                conn.commit()
                self.logger.info(f"Upserted {len(games)} games for {season} {league}")

        except Exception as e:
            self.logger.error(f"Error upserting games: {e}")
            raise

    def save_travel_data(self, travel_data: List[TeamTravelData], season: str, league: str):
        """Save travel data to database with league support"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Clear existing travel data for this season and league
                conn.execute("DELETE FROM travel_data WHERE season = ? AND league = ?", (season, league))
                
                # Insert new travel data
                for travel in travel_data:
                    conn.execute("""
                        INSERT INTO travel_data 
                        (team_name, team_id, league, departure_city, arrival_city, game_date,
                         travel_date, departure_airport, arrival_airport, confidence,
                         game_id, opponent, series_game_number, homestand_game_number, season)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        travel.team_name, travel.team_id, league, travel.departure_city,
                        travel.arrival_city, travel.game_date.isoformat(),
                        travel.travel_date.isoformat(), travel.departure_airport,
                        travel.arrival_airport, travel.confidence, travel.game_id,
                        travel.opponent, travel.series_game_number,
                        travel.homestand_game_number, season
                    ))
                
                # Update travel count in season cache
                conn.execute("""
                    UPDATE season_cache 
                    SET travel_count = ?, last_updated = ?
                    WHERE season = ? AND league = ?
                """, (len(travel_data), datetime.now().isoformat(), season, league))
                
                conn.commit()
                self.logger.info(f"Saved {len(travel_data)} travel records for {season} {league} season")
                
        except Exception as e:
            self.logger.error(f"Error saving travel data: {e}")
            raise
    
    def load_games(self, season: str, league: str) -> List[GameData]:
        """Load games from database for a specific season and league"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT g.*, 
                           ht.abbreviation as home_abbrev, ht.display_name as home_name, 
                           ht.location as home_location, ht.color as home_color,
                           ht.alternate_color as home_alt_color, ht.logo_url as home_logo,
                           ht.division as home_division, ht.league as home_league,
                           ht.conference as home_conference,
                           at.abbreviation as away_abbrev, at.display_name as away_name,
                           at.location as away_location, at.color as away_color,
                           at.alternate_color as away_alt_color, at.logo_url as away_logo,
                           at.division as away_division, at.league as away_league,
                           at.conference as away_conference,
                           v.name as venue_name, v.city as venue_city, v.state as venue_state,
                           v.country as venue_country, v.latitude, v.longitude,
                           v.capacity as venue_capacity, v.timezone as time_zone
                    FROM games g
                    JOIN teams ht ON g.home_team_id = ht.team_id AND g.league = ht.league
                    JOIN teams at ON g.away_team_id = at.team_id AND g.league = at.league
                    JOIN venues v ON g.venue_id = v.venue_id
                    WHERE g.season = ? AND g.league = ?
                    ORDER BY g.date
                """, (season, league))
                
                games = []
                for row in cursor.fetchall():
                    # Reconstruct team objects
                    home_team = TeamInfo(
                        team_id=row['home_team_id'],
                        abbreviation=row['home_abbrev'],
                        display_name=row['home_name'],
                        location=row['home_location'],
                        color=row['home_color'],
                        alternate_color=row['home_alt_color'],
                        logo_url=row['home_logo'],
                        division=row['home_division'],
                        league=row['home_league'],
                        conference=row['home_conference']
                    )
                    
                    away_team = TeamInfo(
                        team_id=row['away_team_id'],
                        abbreviation=row['away_abbrev'],
                        display_name=row['away_name'],
                        location=row['away_location'],
                        color=row['away_color'],
                        alternate_color=row['away_alt_color'],
                        logo_url=row['away_logo'],
                        division=row['away_division'],
                        league=row['away_league'],
                        conference=row['away_conference']
                    )
                    
                    venue = Venue(
                        venue_id=row['venue_id'],
                        name=row['venue_name'],
                        city=row['venue_city'],
                        state=row['venue_state'],
                        country=row['venue_country'],
                        latitude=row['latitude'],
                        longitude=row['longitude'],
                        capacity=row['venue_capacity'],
                        timezone=row['time_zone']
                    )
                    
                    game = GameData(
                        game_id=row['game_id'],
                        date=datetime.fromisoformat(row['date']),
                        home_team=home_team,
                        away_team=away_team,
                        venue=venue,
                        status=GameStatus(row['status']),
                        week=row['week'],
                        season_type=row['season_type'],
                        league=row['league'],
                        season=row['season'],
                        series_description=row['series_description']
                    )
                    games.append(game)
                
                self.logger.info(f"Loaded {len(games)} games for {season} {league} season")
                return games
                
        except Exception as e:
            self.logger.error(f"Error loading games: {e}")
            return []
    
    def load_travel_data(self, season: str, league: str, team_id: Optional[str] = None) -> List[TeamTravelData]:
        """Load travel data from database with league support"""
        try:
            if league in ['NBA', 'NHL'] and season and '-' not in season:
                year = int(season)
                if year >= 2024:  # Assuming this is meant to be the end year of the season
                    # Convert 2025 -> 2024-25, 2024 -> 2023-24, etc.
                    start_year = year - 1
                    season = f"{start_year}-{str(year)[2:]}"
                    logger.debug(f"🔧 FIXED: Converted {league} season '{year}' to '{season}'")
            
            # FIX: Convert team_id to lowercase for database query
            if team_id:
                team_id = team_id.lower()
            
            # Figure out where in the fuck cws abbreviation is being passed in
            # Not hope for this franchise
            TEAM_ID_FIXES = {
                "cws": "chw"
            }
            team_id = TEAM_ID_FIXES.get(team_id, team_id)
            
            logger.debug(f"🐛 DEBUG - load_travel_data called:")
            logger.debug(f"  season: '{season}' (type: {type(season)})")
            logger.debug(f"  league: '{league}'")
            logger.debug(f"  team_id: '{team_id}'")
            
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                if team_id:
                    cursor = conn.execute("""
                        SELECT * FROM travel_data 
                        WHERE season = ? AND league = ? AND team_id = ?
                        ORDER BY travel_date
                    """, (season, league, team_id))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM travel_data 
                        WHERE season = ? AND league = ?
                        ORDER BY travel_date
                    """, (season, league))
                
                travel_data = []
                for row in cursor.fetchall():
                    travel = TeamTravelData(
                        team_name=row['team_name'],
                        team_id=row['team_id'],
                        departure_city=row['departure_city'],
                        arrival_city=row['arrival_city'],
                        game_date=datetime.fromisoformat(row['game_date']),
                        travel_date=datetime.fromisoformat(row['travel_date']),
                        departure_airport=row['departure_airport'],
                        arrival_airport=row['arrival_airport'],
                        confidence=row['confidence'],
                        game_id=row['game_id'],
                        opponent=row['opponent'],
                        series_game_number=row['series_game_number'],
                        homestand_game_number=row['homestand_game_number']
                    )
                    travel_data.append(travel)
                
                filter_desc = f" for team {team_id}" if team_id else ""
                self.logger.info(f"Loaded {len(travel_data)} travel records for {season} {league}{filter_desc}")
                return travel_data
                
        except Exception as e:
            self.logger.error(f"Error loading travel data: {e}")
            return []
    
    def load_teams(self, league: str = None) -> List[TeamInfo]:
        """Load teams from database for specific league or all leagues"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                if league:
                    cursor = conn.execute("""
                        SELECT * FROM teams 
                        WHERE league = ?
                        ORDER BY display_name
                    """, (league,))
                else:
                    cursor = conn.execute("SELECT * FROM teams ORDER BY league, display_name")
                
                teams = []
                for row in cursor.fetchall():
                    team = TeamInfo(
                        team_id=row['team_id'],
                        abbreviation=row['abbreviation'],
                        display_name=row['display_name'],
                        location=row['location'],
                        color=row['color'],
                        alternate_color=row['alternate_color'],
                        logo_url=row['logo_url'],
                        division=row['division'],
                        league=row['league'],
                        conference=row['conference']
                    )
                    teams.append(team)
                
                return teams
                
        except Exception as e:
            self.logger.error(f"Error loading teams: {e}")
            return []
    
    def get_cached_seasons(self, league: str) -> List[Dict[str, Any]]:
        """Get information about cached seasons for a specific league"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT season, games_count, travel_count, last_updated, is_complete
                    FROM season_cache 
                    WHERE league = ?
                    ORDER BY season DESC
                """, (league,))
                
                seasons = []
                for row in cursor.fetchall():
                    seasons.append({
                        'season': row['season'],
                        'games_count': row['games_count'],
                        'travel_count': row['travel_count'],
                        'last_updated': datetime.fromisoformat(row['last_updated']) if row['last_updated'] else None,
                        'is_complete': bool(row['is_complete'])
                    })
                
                return seasons
                
        except Exception as e:
            self.logger.error(f"Error getting cached seasons: {e}")
            return []
    
    def clear_season_data(self, season: str, league: str):
        """Clear all data for a specific season and league"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM travel_data WHERE season = ? AND league = ?", (season, league))
                conn.execute("DELETE FROM games WHERE season = ? AND league = ?", (season, league))
                conn.execute("DELETE FROM season_cache WHERE season = ? AND league = ?", (season, league))
                
                conn.commit()
                self.logger.info(f"Cleared all data for {season} {league} season")
                
        except Exception as e:
            self.logger.error(f"Error clearing season data: {e}")
            raise
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics with league breakdown"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                stats = {}
                
                # Count tables
                cursor = conn.execute("SELECT COUNT(*) FROM teams")
                stats['teams_count'] = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM venues")
                stats['venues_count'] = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM games")
                stats['games_count'] = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM travel_data")
                stats['travel_count'] = cursor.fetchone()[0]
                
                cursor = conn.execute("SELECT COUNT(*) FROM season_cache")
                stats['cached_seasons'] = cursor.fetchone()[0]
                
                # League breakdown
                cursor = conn.execute("""
                    SELECT league, COUNT(*) as team_count 
                    FROM teams 
                    GROUP BY league
                """)
                league_teams = {row[0]: row[1] for row in cursor.fetchall()}
                stats['teams_by_league'] = league_teams
                
                cursor = conn.execute("""
                    SELECT league, COUNT(*) as game_count 
                    FROM games 
                    GROUP BY league
                """)
                league_games = {row[0]: row[1] for row in cursor.fetchall()}
                stats['games_by_league'] = league_games
                
                cursor = conn.execute("""
                    SELECT league, COUNT(*) as travel_count 
                    FROM travel_data 
                    GROUP BY league
                """)
                league_travel = {row[0]: row[1] for row in cursor.fetchall()}
                stats['travel_by_league'] = league_travel
                
                # Database size
                stats['db_size_mb'] = Path(self.db_path).stat().st_size / (1024 * 1024)
                
                # Latest data
                cursor = conn.execute("SELECT MAX(last_updated) FROM season_cache")
                latest_update = cursor.fetchone()[0]
                if latest_update:
                    stats['latest_update'] = datetime.fromisoformat(latest_update)
                
                return stats
                
        except Exception as e:
            self.logger.error(f"Error getting database stats: {e}")
            return {}
    
    def get_current_season_for_league(self, league: str) -> str:
        """Get current season string for a league based on current date"""
        now = datetime.now()
        current_year = now.year
        
        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons start in October and end in June of next year
            if now.month >= 10:  # October or later
                next_year = str(current_year + 1)[2:]  # Get last 2 digits
                return f"{current_year}-{next_year}"   # e.g., "2024-25"
            else:  # Before October
                next_year = str(current_year)[2:]  # Get last 2 digits
                return f"{current_year - 1}-{next_year}"  # e.g., "2023-24"
        else:  # MLB
            # MLB season is calendar year
            return str(current_year)
    
    def _generate_scrape_hash(self, games: List[GameData]) -> str:
        """Generate hash for tracking data changes"""
        game_ids = sorted([game.game_id for game in games])
        return hashlib.md5(''.join(game_ids).encode()).hexdigest()
    
    def vacuum_database(self):
        """Optimize database performance"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("VACUUM")
                self.logger.info("Database vacuumed successfully")
        except Exception as e:
            self.logger.error(f"Error vacuuming database: {e}")
    
    def get_league_statistics(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed statistics for each league"""
        try:
            stats = {}
            leagues = ['MLB', 'NBA', 'NHL']
            
            for league in leagues:
                league_stats = {}
                
                # Team count
                teams = self.load_teams(league)
                league_stats['teams'] = len(teams)
                
                # Seasons cached
                seasons = self.get_cached_seasons(league)
                league_stats['cached_seasons'] = len(seasons)
                
                if seasons:
                    latest_season = seasons[0]
                    league_stats['latest_season'] = latest_season['season']
                    league_stats['latest_games'] = latest_season['games_count']
                    league_stats['latest_travel'] = latest_season['travel_count']
                    league_stats['last_updated'] = latest_season['last_updated']
                
                stats[league] = league_stats
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error getting league statistics: {e}")
            return {}
    

    def get_upcoming_games(self, team_id: str, league: str, season: str = None, limit: int = 10) -> List[GameData]:
        """Get upcoming games for a specific team"""
        try:
            # Convert team_id to lowercase for consistency
            if team_id:
                team_id = team_id.lower()
            
            # Auto-detect current season if not provided
            if season is None:
                season = self.get_current_season_for_league(league)
            
            # Handle NBA/NHL season format conversion
            if league in ['NBA', 'NHL'] and season and '-' not in season:
                year = int(season)
                if year >= 2024:
                    start_year = year - 1
                    season = f"{start_year}-{str(year)[2:]}"
            
            current_datetime = datetime.now()
            
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT g.*, 
                           ht.abbreviation as home_abbrev, ht.display_name as home_name, 
                           ht.location as home_location, ht.color as home_color,
                           ht.alternate_color as home_alt_color, ht.logo_url as home_logo,
                           ht.division as home_division, ht.league as home_league,
                           ht.conference as home_conference,
                           at.abbreviation as away_abbrev, at.display_name as away_name,
                           at.location as away_location, at.color as away_color,
                           at.alternate_color as away_alt_color, at.logo_url as away_logo,
                           at.division as away_division, at.league as away_league,
                           at.conference as away_conference,
                           v.name as venue_name, v.city as venue_city, v.state as venue_state,
                           v.country as venue_country, v.latitude, v.longitude,
                           v.capacity as venue_capacity, v.timezone as time_zone
                    FROM games g
                    JOIN teams ht ON g.home_team_id = ht.team_id AND g.league = ht.league
                    JOIN teams at ON g.away_team_id = at.team_id AND g.league = at.league
                    JOIN venues v ON g.venue_id = v.venue_id
                    WHERE g.season = ? AND g.league = ? 
                    AND (g.home_team_id = ? OR g.away_team_id = ?)
                    AND datetime(g.date) >= datetime(?)
                    AND g.status IN ('scheduled', 'postponed')
                    ORDER BY g.date ASC
                    LIMIT ?
                """, (season, league, team_id, team_id, current_datetime.isoformat(), limit))
                
                games = []
                for row in cursor.fetchall():
                    # Reconstruct team objects
                    home_team = TeamInfo(
                        team_id=row['home_team_id'],
                        abbreviation=row['home_abbrev'],
                        display_name=row['home_name'],
                        location=row['home_location'],
                        color=row['home_color'],
                        alternate_color=row['home_alt_color'],
                        logo_url=row['home_logo'],
                        division=row['home_division'],
                        league=row['home_league'],
                        conference=row['home_conference']
                    )
                    
                    away_team = TeamInfo(
                        team_id=row['away_team_id'],
                        abbreviation=row['away_abbrev'],
                        display_name=row['away_name'],
                        location=row['away_location'],
                        color=row['away_color'],
                        alternate_color=row['away_alt_color'],
                        logo_url=row['away_logo'],
                        division=row['away_division'],
                        league=row['away_league'],
                        conference=row['away_conference']
                    )
                    
                    venue = Venue(
                        venue_id=row['venue_id'],
                        name=row['venue_name'],
                        city=row['venue_city'],
                        state=row['venue_state'],
                        country=row['venue_country'],
                        latitude=row['latitude'],
                        longitude=row['longitude'],
                        capacity=row['capacity'],
                        timezone=row['timezone']
                    )
                    
                    game = GameData(
                        game_id=row['game_id'],
                        date=datetime.fromisoformat(row['date']),
                        home_team=home_team,
                        away_team=away_team,
                        venue=venue,
                        status=GameStatus(row['status']),
                        week=row['week'],
                        season_type=row['season_type'],
                        league=row['league'],
                        season=row['season'],
                        series_description=row['series_description']
                    )
                    games.append(game)
                
                self.logger.info(f"Found {len(games)} upcoming games for team {team_id} in {league}")
                return games
                
        except Exception as e:
            self.logger.error(f"Error getting upcoming games for team {team_id}: {e}")
            return []
    
    def get_team_next_game(self, team_id: str, league: str, season: str = None) -> Optional[GameData]:
        """Get the very next game for a specific team"""
        upcoming_games = self.get_upcoming_games(team_id, league, season, limit=1)
        return upcoming_games[0] if upcoming_games else None

# Usage example and testing
if __name__ == "__main__":
    # Initialize database manager
    db = DatabaseManager("test_multi_sports.db")
    
    # Get database stats
    stats = db.get_database_stats()
    logger.debug("Database Stats:", stats)
    
    # Check if season is cached for different leagues
    for league in ['MLB', 'NBA', 'NHL']:
        current_season = db.get_current_season_for_league(league)
        is_cached, last_updated = db.is_season_cached(current_season, league)
        logger.debug(f"{league} {current_season} season cached: {is_cached}, last updated: {last_updated}")
    
    # Get cached seasons for each league
    for league in ['MLB', 'NBA', 'NHL']:
        seasons = db.get_cached_seasons(league)
        logger.debug(f"{league} cached seasons: {len(seasons)}")
    
    # Get league statistics
    league_stats = db.get_league_statistics()
    logger.debug("League Statistics:", league_stats)


# ===========================================================================
# Schedule-fatigue scoring engine (merged from fatigue_engine.py; Qt-free)
# ===========================================================================

"""Schedule-fatigue scoring engine.

Computes per-team travel fatigue for upcoming games from the cached schedule
database (games + venues), producing a 0-100 fatigue score per team per game
and a home/away differential — the "schedule edge" signal intended for
consumption by EffortOdds.

Deliberately Qt-free: only stdlib + database_manager, so it can be imported
by any host (panel, scripts, EffortOdds workers) without pulling in OpenGL.
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fallback coordinates for venue cities whose DB rows lack lat/lon
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "Phoenix": (33.4484, -112.0740), "Atlanta": (33.7490, -84.3880),
    "Baltimore": (39.2904, -76.6122), "Boston": (42.3601, -71.0589),
    "Chicago": (41.8781, -87.6298), "Cincinnati": (39.1031, -84.5120),
    "Cleveland": (41.4993, -81.6944), "Denver": (39.7392, -104.9903),
    "Detroit": (42.3314, -83.0458), "Houston": (29.7604, -95.3698),
    "Kansas City": (39.0997, -94.5786), "Los Angeles": (34.0522, -118.2437),
    "Miami": (25.7617, -80.1918), "Milwaukee": (43.0389, -87.9065),
    "Minneapolis": (44.9778, -93.2650), "New York": (40.7128, -74.0060),
    "Oakland": (37.8044, -122.2712), "Philadelphia": (39.9526, -75.1652),
    "Pittsburgh": (40.4406, -79.9959), "San Diego": (32.7157, -117.1611),
    "San Francisco": (37.7749, -122.4194), "Seattle": (47.6062, -122.3321),
    "St. Louis": (38.6270, -90.1994), "Tampa": (27.9506, -82.4572),
    "Dallas": (32.7767, -96.7970), "Washington": (38.9072, -77.0369),
    "Indianapolis": (39.7684, -86.1581), "Charlotte": (35.2271, -80.8431),
    "Orlando": (28.5383, -81.3792), "Portland": (45.5152, -122.6784),
    "Sacramento": (38.5816, -121.4944), "Salt Lake City": (40.7608, -111.8910),
    "Oklahoma City": (35.4676, -97.5164), "Memphis": (35.1495, -90.0490),
    "New Orleans": (29.9511, -90.0715), "San Antonio": (29.4241, -98.4936),
    "Buffalo": (42.8864, -78.8784), "Sunrise": (26.1354, -80.2373),
    "Raleigh": (35.7796, -78.6382), "Columbus": (39.9612, -82.9988),
    "Newark": (40.7357, -74.1724), "Nashville": (36.1627, -86.7816),
    "Anaheim": (33.8366, -117.9143), "Las Vegas": (36.1699, -115.1398),
    "San Jose": (37.3382, -121.8863),
    "Toronto": (43.6532, -79.3832), "Montreal": (45.5017, -73.5673),
    "Vancouver": (49.2827, -123.1207), "Calgary": (51.0447, -114.0719),
    "Edmonton": (53.5461, -113.4938), "Ottawa": (45.4215, -75.6972),
    "Winnipeg": (49.8951, -97.1384),
}

# Standard-time UTC offsets; cities not listed fall back to round(lon / 15)
CITY_TZ: Dict[str, int] = {
    "New York": -5, "Boston": -5, "Philadelphia": -5, "Washington": -5,
    "Miami": -5, "Atlanta": -5, "Detroit": -5, "Cleveland": -5,
    "Baltimore": -5, "Tampa": -5, "Pittsburgh": -5, "Charlotte": -5,
    "Orlando": -5, "Indianapolis": -5, "Buffalo": -5, "Sunrise": -5,
    "Raleigh": -5, "Columbus": -5, "Newark": -5, "Cincinnati": -5,
    "Chicago": -6, "Milwaukee": -6, "Minneapolis": -6, "Dallas": -6,
    "Houston": -6, "San Antonio": -6, "New Orleans": -6, "Memphis": -6,
    "Kansas City": -6, "St. Louis": -6, "Oklahoma City": -6, "Nashville": -6,
    "Denver": -7, "Phoenix": -7, "Salt Lake City": -7,
    "Los Angeles": -8, "San Francisco": -8, "Seattle": -8, "Portland": -8,
    "Las Vegas": -8, "Sacramento": -8, "San Diego": -8, "Oakland": -8,
    "Anaheim": -8, "San Jose": -8,
    "Toronto": -5, "Montreal": -5, "Ottawa": -5,
    "Winnipeg": -6, "Calgary": -7, "Edmonton": -7, "Vancouver": -8,
}

# Venues above ~3000 ft where arriving from sea level is itself a stressor
HIGH_ALTITUDE_CITIES = {"Denver", "Salt Lake City", "Calgary", "Mexico City"}

# Per-league scoring weights. MLB plays daily, so density terms (back-to-back,
# 3-in-4) are normal there and carry no weight; NBA/NHL punish them heavily.
LEAGUE_WEIGHTS = {
    "NBA": dict(b2b=20.0, three_in_four=14.0, games_7d_baseline=3.5,
                density=5.0, miles_div=60.0, tz=6.0, altitude=8.0, rest_relief=6.0),
    "NHL": dict(b2b=18.0, three_in_four=12.0, games_7d_baseline=3.5,
                density=5.0, miles_div=60.0, tz=6.0, altitude=7.0, rest_relief=6.0),
    "MLB": dict(b2b=0.0, three_in_four=0.0, games_7d_baseline=6.5,
                density=3.0, miles_div=55.0, tz=7.0, altitude=8.0, rest_relief=8.0),
}


@dataclass
class TeamFatigue:
    """Fatigue snapshot for one team going into one game."""
    team_id: str
    team_name: str
    league: str
    game_id: str
    game_date: datetime
    rest_days: int = 99            # full off-days since previous game
    miles_7d: float = 0.0          # venue-to-venue great-circle miles
    miles_14d: float = 0.0
    tz_hops_7d: int = 0            # sum of |tz changes| between venues
    games_7d: int = 0              # games played in prior 7 days
    back_to_back: bool = False
    three_in_four: bool = False
    altitude_shift: bool = False   # low-altitude -> high-altitude arrival
    score: float = 0.0             # 0 (fresh) .. 100 (cooked)

    def factors(self) -> List[str]:
        """Human-readable list of what is driving the score."""
        out = []
        if self.back_to_back:
            out.append("back-to-back")
        if self.three_in_four:
            out.append("3 games in 4 nights")
        if self.miles_7d >= 2000:
            out.append(f"{self.miles_7d:,.0f} mi in 7d")
        if self.tz_hops_7d >= 3:
            out.append(f"{self.tz_hops_7d} tz hops in 7d")
        if self.altitude_shift:
            out.append("altitude arrival")
        if self.rest_days >= 3 and self.rest_days < 99:
            out.append(f"{self.rest_days}d rest")
        return out


@dataclass
class GameFatigueReport:
    """Fatigue comparison for one upcoming game."""
    game_id: str
    league: str
    season: str
    game_date: datetime
    venue_city: str
    home: TeamFatigue
    away: TeamFatigue

    @property
    def differential(self) -> float:
        """away.score - home.score: positive means the road team is more tired."""
        return self.away.score - self.home.score

    def summary(self) -> str:
        return (f"{self.away.team_id.upper()} @ {self.home.team_id.upper()} "
                f"{self.game_date.strftime('%b %d')}: "
                f"away {self.away.score:.0f} vs home {self.home.score:.0f} "
                f"(diff {self.differential:+.0f})")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


class FatigueEngine:
    """Scores upcoming games from the cached schedule DB."""

    def __init__(self, db):
        self.db = db  # DatabaseManager

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _venue_coords(venue) -> Optional[Tuple[float, float]]:
        if venue.latitude and venue.longitude:
            return (venue.latitude, venue.longitude)
        return CITY_COORDS.get(venue.city)

    @staticmethod
    def _tz_offset(city: str, lon: Optional[float]) -> int:
        if city in CITY_TZ:
            return CITY_TZ[city]
        if lon is not None:
            return round(lon / 15.0)
        return 0

    def _build_team_timelines(self, games) -> Dict[str, List]:
        """team_id -> chronological list of that team's games."""
        timelines: Dict[str, List] = {}
        seen: Dict[str, set] = {}
        for game in sorted(games, key=lambda g: g.date):
            for team in (game.home_team, game.away_team):
                tid = team.team_id.lower()
                if game.game_id in seen.setdefault(tid, set()):
                    continue
                seen[tid].add(game.game_id)
                timelines.setdefault(tid, []).append(game)
        return timelines

    # ------------------------------------------------------------ scoring

    def score_team_for_game(self, team, game, timeline, league: str) -> TeamFatigue:
        tf = TeamFatigue(
            team_id=team.team_id.lower(),
            team_name=team.display_name,
            league=league,
            game_id=game.game_id,
            game_date=game.date,
        )
        weights = LEAGUE_WEIGHTS.get(league, LEAGUE_WEIGHTS["NBA"])

        # Past games strictly before this one (timeline is chronological)
        past = [g for g in timeline if g.date < game.date and g.game_id != game.game_id]
        if not past:
            return tf

        last_game = past[-1]
        tf.rest_days = max((game.date.date() - last_game.date.date()).days - 1, 0)
        tf.back_to_back = (game.date.date() - last_game.date.date()).days == 1

        window_4d = game.date - timedelta(days=3)
        games_in_4 = sum(1 for g in past if g.date >= window_4d) + 1  # incl. this game
        tf.three_in_four = games_in_4 >= 3

        window_7d = game.date - timedelta(days=7)
        window_14d = game.date - timedelta(days=14)
        tf.games_7d = sum(1 for g in past if g.date >= window_7d)

        # Travel legs: consecutive venues over the past 14 days, plus the leg
        # into this game's venue
        legs = [g for g in past if g.date >= window_14d] + [game]
        prev_coords = None
        prev_tz = None
        prev_city = None
        for g in legs:
            coords = self._venue_coords(g.venue)
            tz = self._tz_offset(g.venue.city, coords[1] if coords else None)
            if prev_coords and coords and g.venue.city != prev_city:
                miles = haversine_miles(*prev_coords, *coords)
                tf.miles_14d += miles
                if g.date >= window_7d:
                    tf.miles_7d += miles
                    if prev_tz is not None:
                        tf.tz_hops_7d += abs(tz - prev_tz)
            if coords:
                prev_coords = coords
                prev_tz = tz
                prev_city = g.venue.city

        # Altitude arrival: this venue is high, the previous one wasn't
        tf.altitude_shift = (game.venue.city in HIGH_ALTITUDE_CITIES
                             and last_game.venue.city not in HIGH_ALTITUDE_CITIES)

        score = 0.0
        score += tf.miles_7d / weights["miles_div"]
        score += tf.tz_hops_7d * weights["tz"]
        if tf.back_to_back:
            score += weights["b2b"]
        if tf.three_in_four:
            score += weights["three_in_four"]
        density_excess = max(tf.games_7d - weights["games_7d_baseline"], 0)
        score += density_excess * weights["density"]
        if tf.altitude_shift:
            score += weights["altitude"]
        score -= min(tf.rest_days, 3) * weights["rest_relief"]

        tf.score = max(0.0, min(100.0, score))
        return tf

    def score_upcoming_games(self, league: str, season: str,
                             days_ahead: int = 14,
                             now: Optional[datetime] = None) -> List[GameFatigueReport]:
        """Score every game in [now, now+days_ahead] for the given league/season."""
        now = now or datetime.now()
        cutoff = now + timedelta(days=days_ahead)

        games = self.db.load_games(season, league)
        if not games:
            return []

        timelines = self._build_team_timelines(games)
        upcoming = [g for g in games if now <= g.date <= cutoff]

        reports = []
        for game in sorted(upcoming, key=lambda g: g.date):
            home_tl = timelines.get(game.home_team.team_id.lower(), [])
            away_tl = timelines.get(game.away_team.team_id.lower(), [])
            reports.append(GameFatigueReport(
                game_id=game.game_id,
                league=league,
                season=season,
                game_date=game.date,
                venue_city=game.venue.city,
                home=self.score_team_for_game(game.home_team, game, home_tl, league),
                away=self.score_team_for_game(game.away_team, game, away_tl, league),
            ))

        return reports
