import sqlite3
import json
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
from dataclasses import asdict
import logging

from data_client import GameData, TeamTravelData, TeamInfo, Venue, GameStatus


class DatabaseManager:
    """Manages SQLite database for sports schedule and travel data"""
    
    def __init__(self, db_path: str = "sports_data.db"):
        self.db_path = Path(db_path)
        self.db_version = "1.0"
        self.setup_logging()
        self.init_database()
    
    def setup_logging(self):
        """Setup logging for database operations"""
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def init_database(self):
        """Initialize database with required tables and indexes"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                
                # Metadata table for tracking data freshness and versions
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Teams table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS teams (
                        team_id TEXT PRIMARY KEY,
                        abbreviation TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        location TEXT,
                        color TEXT,
                        alternate_color TEXT,
                        logo_url TEXT,
                        division TEXT,
                        league TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                
                # Games table - main schedule data
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
                        league TEXT DEFAULT 'MLB',
                        season TEXT NOT NULL,
                        series_description TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (home_team_id) REFERENCES teams (team_id),
                        FOREIGN KEY (away_team_id) REFERENCES teams (team_id),
                        FOREIGN KEY (venue_id) REFERENCES venues (venue_id)
                    )
                """)
                
                # Travel data table - inferred travel patterns
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS travel_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        team_name TEXT NOT NULL,
                        team_id TEXT NOT NULL,
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
                        FOREIGN KEY (team_id) REFERENCES teams (team_id),
                        FOREIGN KEY (game_id) REFERENCES games (game_id)
                    )
                """)
                
                # Season cache table - tracks what seasons have been scraped
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS season_cache (
                        season TEXT PRIMARY KEY,
                        league TEXT NOT NULL,
                        games_count INTEGER DEFAULT 0,
                        travel_count INTEGER DEFAULT 0,
                        last_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        is_complete BOOLEAN DEFAULT FALSE,
                        scrape_hash TEXT
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
                self.logger.info(f"Database initialized at {self.db_path}")
                
        except Exception as e:
            self.logger.error(f"Database initialization error: {e}")
            raise
    
    def create_indexes(self, conn: sqlite3.Connection):
        """Create database indexes for optimal query performance"""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_games_date ON games (date)",
            "CREATE INDEX IF NOT EXISTS idx_games_season ON games (season)",
            "CREATE INDEX IF NOT EXISTS idx_games_teams ON games (home_team_id, away_team_id)",
            "CREATE INDEX IF NOT EXISTS idx_travel_date ON travel_data (travel_date)",
            "CREATE INDEX IF NOT EXISTS idx_travel_team ON travel_data (team_id)",
            "CREATE INDEX IF NOT EXISTS idx_travel_season ON travel_data (season)",
            "CREATE INDEX IF NOT EXISTS idx_travel_game ON travel_data (game_id)",
        ]
        
        for index_sql in indexes:
            conn.execute(index_sql)
    
    def is_season_cached(self, season: str, league: str = "MLB") -> Tuple[bool, Optional[datetime]]:
        """Check if season data is already cached and when it was last updated"""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
    
    def should_refresh_season(self, season: str, force_refresh: bool = False) -> bool:
        """Determine if season data should be refreshed"""
        if force_refresh:
            return True
        
        is_cached, last_updated = self.is_season_cached(season)
        
        if not is_cached:
            return True
        
        # For current season, refresh if data is older than 1 day
        current_year = str(datetime.now().year)
        if season == current_year and last_updated:
            time_since_update = datetime.now() - last_updated
            return time_since_update > timedelta(days=1)
        
        # For past seasons, don't refresh unless forced
        return False
    
    def save_teams(self, teams: List[TeamInfo]):
        """Save team information to database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                for team in teams:
                    conn.execute("""
                        INSERT OR REPLACE INTO teams 
                        (team_id, abbreviation, display_name, location, color, 
                         alternate_color, logo_url, division, league)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        team.team_id, team.abbreviation, team.display_name,
                        team.location, team.color, team.alternate_color,
                        team.logo_url, team.division, team.league
                    ))
                
                conn.commit()
                self.logger.info(f"Saved {len(teams)} teams to database")
                
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
    
    def save_games(self, games: List[GameData], season: str, league: str = "MLB"):
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
                    self.save_teams(list(unique_teams.values()))
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
    
    def save_travel_data(self, travel_data: List[TeamTravelData], season: str):
        """Save travel data to database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Clear existing travel data for this season
                conn.execute("DELETE FROM travel_data WHERE season = ?", (season,))
                
                # Insert new travel data
                for travel in travel_data:
                    conn.execute("""
                        INSERT INTO travel_data 
                        (team_name, team_id, departure_city, arrival_city, game_date,
                         travel_date, departure_airport, arrival_airport, confidence,
                         game_id, opponent, series_game_number, homestand_game_number, season)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        travel.team_name, travel.team_id, travel.departure_city,
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
                    WHERE season = ?
                """, (len(travel_data), datetime.now().isoformat(), season))
                
                conn.commit()
                self.logger.info(f"Saved {len(travel_data)} travel records for {season} season")
                
        except Exception as e:
            self.logger.error(f"Error saving travel data: {e}")
            raise
    
    def load_games(self, season: str, league: str = "MLB") -> List[GameData]:
        """Load games from database for a specific season"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("""
                    SELECT g.*, 
                           ht.abbreviation as home_abbrev, ht.display_name as home_name, 
                           ht.location as home_location, ht.color as home_color,
                           ht.alternate_color as home_alt_color, ht.logo_url as home_logo,
                           ht.division as home_division, ht.league as home_league,
                           at.abbreviation as away_abbrev, at.display_name as away_name,
                           at.location as away_location, at.color as away_color,
                           at.alternate_color as away_alt_color, at.logo_url as away_logo,
                           at.division as away_division, at.league as away_league,
                           v.name as venue_name, v.city as venue_city, v.state as venue_state,
                           v.country as venue_country, v.latitude, v.longitude,
                           v.capacity, v.timezone
                    FROM games g
                    JOIN teams ht ON g.home_team_id = ht.team_id
                    JOIN teams at ON g.away_team_id = at.team_id
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
                        league=row['home_league']
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
                        league=row['away_league']
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
                
                self.logger.info(f"Loaded {len(games)} games for {season} {league} season")
                return games
                
        except Exception as e:
            self.logger.error(f"Error loading games: {e}")
            return []
    
    def load_travel_data(self, season: str, team_id: Optional[str] = None) -> List[TeamTravelData]:
        """Load travel data from database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                if team_id:
                    cursor = conn.execute("""
                        SELECT * FROM travel_data 
                        WHERE season = ? AND team_id = ?
                        ORDER BY travel_date
                    """, (season, team_id))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM travel_data 
                        WHERE season = ?
                        ORDER BY travel_date
                    """, (season,))
                
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
                self.logger.info(f"Loaded {len(travel_data)} travel records for {season}{filter_desc}")
                return travel_data
                
        except Exception as e:
            self.logger.error(f"Error loading travel data: {e}")
            return []
    
    def load_teams(self) -> List[TeamInfo]:
        """Load all teams from database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                cursor = conn.execute("SELECT * FROM teams ORDER BY display_name")
                
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
                        league=row['league']
                    )
                    teams.append(team)
                
                return teams
                
        except Exception as e:
            self.logger.error(f"Error loading teams: {e}")
            return []
    
    def get_cached_seasons(self, league: str = "MLB") -> List[Dict[str, Any]]:
        """Get information about cached seasons"""
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
    
    def clear_season_data(self, season: str, league: str = "MLB"):
        """Clear all data for a specific season"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM travel_data WHERE season = ?", (season,))
                conn.execute("DELETE FROM games WHERE season = ? AND league = ?", (season, league))
                conn.execute("DELETE FROM season_cache WHERE season = ? AND league = ?", (season, league))
                
                conn.commit()
                self.logger.info(f"Cleared all data for {season} {league} season")
                
        except Exception as e:
            self.logger.error(f"Error clearing season data: {e}")
            raise
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
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
                
                # Database size
                stats['db_size_mb'] = self.db_path.stat().st_size / (1024 * 1024)
                
                # Latest data
                cursor = conn.execute("SELECT MAX(last_updated) FROM season_cache")
                latest_update = cursor.fetchone()[0]
                if latest_update:
                    stats['latest_update'] = datetime.fromisoformat(latest_update)
                
                return stats
                
        except Exception as e:
            self.logger.error(f"Error getting database stats: {e}")
            return {}
    
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


# Usage example and testing
if __name__ == "__main__":
    # Initialize database manager
    db = DatabaseManager("test_sports.db")
    
    # Get database stats
    stats = db.get_database_stats()
    print("Database Stats:", stats)
    
    # Check if season is cached
    is_cached, last_updated = db.is_season_cached("2025")
    print(f"2025 season cached: {is_cached}, last updated: {last_updated}")
    
    # Get cached seasons
    seasons = db.get_cached_seasons()
    print("Cached seasons:", seasons)
