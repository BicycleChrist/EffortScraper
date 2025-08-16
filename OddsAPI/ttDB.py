import sqlite3
import os
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import asyncio
import aiohttp
from pathlib import Path

# You'll need to modify this import path as appropriate
from TableTennisClient import get_event_view_async, get_history_async, TT_KEY

# Constants
DB_PATH = "tt_tracker.db"
BASE_URL = "https://api.b365api.com"

class TTDatabase:
    def __init__(self, db_path: str = DB_PATH):
        """Initialize the database connection and create tables if they don't exist."""
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.init_db()

    def init_db(self):
        """Initialize the database and create tables if they don't exist."""
        self.conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # This enables column access by name
        # Enable WAL mode for better concurrent access
        self.conn.execute('PRAGMA journal_mode=WAL')
        # Reduce lock timeout issues
        self.conn.execute('PRAGMA busy_timeout=30000')
        self.cursor = self.conn.cursor()

        # Create players table with a composite primary key
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER,
            name TEXT NOT NULL,
            league_id INTEGER,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            PRIMARY KEY (id, league_id)
        )
        ''')

        # Create matches table
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY,
            league_id INTEGER,
            home_player_id INTEGER,
            away_player_id INTEGER,
            home_player_name TEXT,
            away_player_name TEXT,
            home_score INTEGER,
            away_score INTEGER,
            match_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            home_odds REAL,
            away_odds REAL,
            spread_handicap REAL,
            spread_home_odds REAL,
            spread_away_odds REAL,
            total_points REAL,
            total_over_odds REAL,
            total_under_odds REAL,
            odds_updated_at TIMESTAMP,
            elo_processed BOOLEAN DEFAULT FALSE,
            elo_processed_at TIMESTAMP
        )
        ''')

        # Create sets table
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT,
            set_number INTEGER,
            home_score INTEGER,
            away_score INTEGER,
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(match_id, set_number)
        )
        ''')

        # Create points table (for detailed point progression)
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT,
            set_number INTEGER,
            point_number INTEGER,
            home_score INTEGER,
            away_score INTEGER,
            server INTEGER,  -- 0 for home, 1 for away
            winner INTEGER,  -- 0 for home, 1 for away
            FOREIGN KEY (match_id) REFERENCES matches(id),
            UNIQUE(match_id, set_number, point_number)
        )
        ''')

        self.conn.commit()
        
        # Handle database migrations for ELO tracking
        self.migrate_database()

    def migrate_database(self):
        """Handle database migrations for new features."""
        try:
            # Check if elo_processed column exists
            self.cursor.execute("PRAGMA table_info(matches)")
            columns = [column[1] for column in self.cursor.fetchall()]
            
            if 'elo_processed' not in columns:
                print("Adding ELO tracking columns to matches table...")
                self.cursor.execute('ALTER TABLE matches ADD COLUMN elo_processed BOOLEAN DEFAULT FALSE')
                self.cursor.execute('ALTER TABLE matches ADD COLUMN elo_processed_at TIMESTAMP')
                self.conn.commit()
                print("ELO tracking columns added successfully.")
        except Exception as e:
            print(f"Error during database migration: {e}")

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def add_player(self, player_id: int, name: str, league_id: int, timestamp: Optional[int] = None) -> bool:
        """Add a player to the database if they don't exist, or update if they do."""
        if timestamp is None:
            timestamp = int(datetime.now().timestamp())

        time_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

        try:
            # Check if player exists
            self.cursor.execute(
                "SELECT id FROM players WHERE id = ? AND league_id = ?", 
                (player_id, league_id)
            )
            exists = self.cursor.fetchone() is not None
            
            if exists:
                # Update existing player
                self.cursor.execute(
                    "UPDATE players SET name = ?, last_seen = ? WHERE id = ? AND league_id = ?",
                    (name, time_str, player_id, league_id)
                )
            else:
                # Insert new player
                print("Oh fucking no player entry error. Player may not exist?")
                self.cursor.execute(
                    "INSERT INTO players (id, name, league_id, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                    (player_id, name, league_id, time_str, time_str)
                )
            
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            print(f"Error adding player {name} (ID: {player_id}): {e}")
            return False

    def add_match(self, match_data: Dict) -> bool:
        """
        Add a match to the database along with sets and points data.
        Returns True if successful, False otherwise.
        """
        try:
            match_id = match_data.get('id')
            if not match_id:
                print("Match data missing ID")
                return False

            # Check if match already exists
            self.cursor.execute("SELECT id FROM matches WHERE id = ?", (match_id,))
            if self.cursor.fetchone():
                # Match already exists, silently skip
                return True

            # Extract basic match information
            league_id = match_data.get('league', {}).get('id')

            home_player = match_data.get('home', {})
            away_player = match_data.get('away', {})

            home_player_id = home_player.get('id')
            away_player_id = away_player.get('id')

            home_player_name = home_player.get('name', '')
            away_player_name = away_player.get('name', '')

            match_time = match_data.get('time')

            # Extract score
            score_str = match_data.get('ss', '')
            home_score, away_score = 0, 0

            if score_str and '-' in score_str:
                try:
                    home_score, away_score = map(int, score_str.split('-'))
                except ValueError:
                    print(f"Invalid score format: {score_str}")

            # Add players to database
            if home_player_id:
                self.add_player(home_player_id, home_player_name, league_id, match_time)
            if away_player_id:
                self.add_player(away_player_id, away_player_name, league_id, match_time)

            # Add match to database
            self.cursor.execute('''
            INSERT INTO matches
            (id, league_id, home_player_id, away_player_id, home_player_name, away_player_name,
             home_score, away_score, match_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id, league_id, home_player_id, away_player_id,
                home_player_name, away_player_name, home_score, away_score,
                datetime.fromtimestamp(int(match_time)).strftime('%Y-%m-%d %H:%M:%S') if match_time else None
            ))

            # Process set scores if available
            set_scores = match_data.get('detailed_scores', {})
            if not set_scores and 'scores' in match_data:
                set_scores = match_data.get('scores', {})

            for set_num, set_data in set_scores.items():
                home_set_score = set_data.get('home', 0)
                away_set_score = set_data.get('away', 0)

                # Ensure scores are integers
                try:
                    home_set_score = int(home_set_score)
                    away_set_score = int(away_set_score)
                except (ValueError, TypeError):
                    home_set_score = 0
                    away_set_score = 0

                self.cursor.execute('''
                INSERT INTO sets (match_id, set_number, home_score, away_score)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(match_id, set_number) DO UPDATE SET
                    home_score = EXCLUDED.home_score,
                    away_score = EXCLUDED.away_score
                ''', (match_id, set_num, home_set_score, away_set_score))

            # Process point progression if available
            timeline_data = match_data.get('processed_timeline', {})
            for set_num, points in timeline_data.items():
                for point_data in points:
                    point_num = point_data.get('point_num')
                    home_score = point_data.get('home_score')
                    away_score = point_data.get('away_score')
                    winner = point_data.get('team')  # 0 for home, 1 for away

                    # Server information might not be available
                    server = point_data.get('server', None)

                    self.cursor.execute('''
                    INSERT INTO points
                    (match_id, set_number, point_number, home_score, away_score, server, winner)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id, set_number, point_number) DO UPDATE SET
                        home_score = EXCLUDED.home_score,
                        away_score = EXCLUDED.away_score,
                        server = EXCLUDED.server,
                        winner = EXCLUDED.winner
                    ''', (match_id, set_num, point_num, home_score, away_score, server, winner))

            self.conn.commit()
            return True
            
        except sqlite3.IntegrityError as e:
            # Handle integrity errors (like duplicates) silently
            if "UNIQUE constraint failed" in str(e):
                return True
            print(f"SQL integrity error for match {match_data.get('id', 'unknown')}: {e}")
            return False
            
        except Exception as e:
            self.conn.rollback()
            print(f"Error adding match {match_data.get('id', 'unknown')}: {e}")
            return False

    def get_player_matches(self, player_id: int, limit: int = 100) -> List[Dict]:
        """Get a player's match history."""
        try:
            self.cursor.execute('''
            SELECT * FROM matches
            WHERE home_player_id = ? OR away_player_id = ?
            ORDER BY match_time DESC
            LIMIT ?
            ''', (player_id, player_id, limit))

            matches = []
            for row in self.cursor.fetchall():
                match_dict = dict(row)

                # Get set scores
                self.cursor.execute('''
                SELECT set_number, home_score, away_score
                FROM sets
                WHERE match_id = ?
                ORDER BY set_number
                ''', (row['id'],))

                sets = {}
                for set_row in self.cursor.fetchall():
                    sets[set_row['set_number']] = {
                        'home': set_row['home_score'],
                        'away': set_row['away_score']
                    }

                match_dict['sets'] = sets
                matches.append(match_dict)

            return matches
        except sqlite3.Error as e:
            print(f"Error fetching matches for player {player_id}: {e}")
            return []

    def get_players_by_league(self, league_id: int) -> List[Dict]:
        """Get all players in a specific league."""
        try:
            self.cursor.execute('''
            SELECT p.id, p.name, p.first_seen, p.last_seen
            FROM players p
            WHERE p.league_id = ?
            ORDER BY p.name
            ''', (league_id,))

            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error fetching players for league {league_id}: {e}")
            return []

    def search_players(self, name_fragment: str) -> List[Dict]:
        """Search for players by name."""
        try:
            self.cursor.execute('''
            SELECT p.*
            FROM players p
            WHERE p.name LIKE ?
            ORDER BY p.name
            ''', (f'%{name_fragment}%',))

            return [dict(row) for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error searching for players with name '{name_fragment}': {e}")
            return []

    async def fetch_player_history(self, player_id: int, player_name: str, league_id: int,
                            session: aiohttp.ClientSession, limit: int = 50) -> List[Dict]:
        """
        Fetch a player's match history from the API.
        This uses the endpoint to search for player's name across events.
        """
        print(f"Fetching history for player {player_name} (ID: {player_id})")

        # First search all events mentioning this player
        params = {
            "token": TT_KEY,
            "search": player_name
        }

        matched_events = []

        try:
            async with session.get(f"{BASE_URL}/v2/events/search", params=params) as response:
                if response.status != 200:
                    print(f"Error fetching events for player {player_name}: {response.status}")
                    return []

                data = await response.json()

                if data.get("success") != 1:
                    print(f"Unsuccessful request for player search!")
                    return []

                results = data.get("results", [])
                print(f"Found {len(results)} events for player {player_name}")

                # Filter results by league and ensure player is actually in the match
                for event in results:
                    # Check if the event is from the correct league
                    event_league = event.get("league", {}).get("id")
                    if event_league != league_id:
                        continue

                    # Check if the player is really in this match
                    home_player = event.get("home", {})
                    away_player = event.get("away", {})

                    home_id = home_player.get("id")
                    away_id = away_player.get("id")

                    if home_id == player_id or away_id == player_id:
                        # Filter completed matches only
                        if event.get("time_status") == 3:  # 3 means the match is finished
                            # Get the match ID
                            event_id = event.get("id")
                            if event_id:
                                matched_events.append(event_id)

                                # Limit the number of matches to fetch
                                if len(matched_events) >= limit:
                                    break

                print(f"Filtered to {len(matched_events)} completed matches in league {league_id}")

                # Now fetch detailed data for each event
                event_details = []
                for event_id in matched_events:
                    detail = await get_event_view_async(session, event_id)
                    if detail:
                        event_details.append(detail)
                        # Add match to database
                        self.add_match(detail)
                        print(f"Added match {event_id} to database")

                return event_details
        except Exception as e:
            print(f"Exception fetching history for player {player_name}: {str(e)}")
            return []

    def does_player_exist(self, player_id: int, league_id: int) -> bool:
        """Check if a player exists in the database."""
        try:
            self.cursor.execute("SELECT id FROM players WHERE id = ? AND league_id = ?", 
                             (player_id, league_id))
            return self.cursor.fetchone() is not None
        except sqlite3.Error as e:
            print(f"Error checking if player {player_id} exists: {e}")
            return False

    
    def get_match_count(self) -> int:
        """Get the total number of matches in the database."""
        try:
            self.cursor.execute("SELECT COUNT(*) as count FROM matches")
            result = self.cursor.fetchone()
            return result['count'] if result else 0
        except sqlite3.Error as e:
            print(f"Error counting matches: {e}")
            return 0

    def get_player_count(self) -> int:
        """Get the total number of players in the database."""
        try:
            self.cursor.execute("SELECT COUNT(*) as count FROM players")
            result = self.cursor.fetchone()
            return result['count'] if result else 0
        except sqlite3.Error as e:
            print(f"Error counting players: {e}")
            return 0

   

    def add_sets_from_match_data(self, match: dict) -> int:
        """
        Add set score data from a match object.
        Returns the number of new sets committed.
        """
        match_id = match.get("id")
        set_data = match.get("detailed_scores", match.get("scores", {}))
        new_sets = 0
    
        if not match_id or not set_data:
            return 0
    
        for set_num, scores in set_data.items():
            try:
                set_number = int(set_num)
                home_score = int(scores.get("home", 0))
                away_score = int(scores.get("away", 0))
    
                # Check if set already exists
                self.cursor.execute('''
                    SELECT 1 FROM sets WHERE match_id = ? AND set_number = ?
                ''', (match_id, set_number))
                if self.cursor.fetchone():
                    continue  # Skip duplicate
    
                self.cursor.execute('''
                    INSERT INTO sets (match_id, set_number, home_score, away_score)
                    VALUES (?, ?, ?, ?)
                ''', (match_id, set_number, home_score, away_score))
                new_sets += 1
            except Exception as e:
                print(f"Error adding set {set_num} for match {match_id}: {e}")
    
        return new_sets

    async def get_match_odds_async(self, session: aiohttp.ClientSession, match_id: str, source: str = "bet365") -> Dict:
        """Fetch odds for a specific match"""
        print(f"Fetching odds for match_id: {match_id}")
        
        params = {
            "token": TT_KEY,
            "event_id": match_id,
            "source": source,
        }
        
        try:
            async with session.get(f"{BASE_URL}/v2/event/odds", params=params) as response:
                if response.status != 200:
                    print(f"Error fetching odds for match {match_id}: {response.status}")
                    return None
                
                result = await response.json()
                if result.get("success") != 1:
                    print(f"Unsuccessful request for odds! Match ID: {match_id}")
                    return None
                
                return result.get("results", {})
        except Exception as e:
            print(f"Exception fetching odds for match {match_id}: {str(e)}")
            return None






    def extract_odds_data(self, odds_data: Dict) -> Dict:
        """Extract relevant pre-game odds data from the API response"""
        if not odds_data:
            return None
        
        # Function to get pre-game odds from a list of odds entries
        def get_pregame_odds(keys:tuple, odds_list:list[dict]):
            # Filter for pre-game odds (where ss is null or empty)
            pregame_odds = [
                [odds[key] for key in keys] for odds in odds_list
                if ((odds.get('ss') is None) or (odds.get('ss') == '') or (odds.get('ss') == '-'))
            ]
            
            for odd_tuple in pregame_odds:
                for odd in odd_tuple:
                    if (odd is None) or (not (odd.replace('.','').isdigit())):
                        print("BULLSHIT ODDS DETECTED!!!")
                        print(odds_list)
                        return [None for _ in odd_tuple]
            
            if (len(pregame_odds) == 0): return [None for _ in keys];
            return [[float(odd) for odd in odd_tuple] for odd_tuple in pregame_odds][0] # returning the earliest entry
        
        # Table tennis uses sport_id 92, so markets are 92_1 (Money Line), 92_2 (Spread), 92_3 (Total Points)
        odds_info = {
            'home_odds': None,
            'away_odds': None,
            'spread_handicap': None,
            'spread_home_odds': None,
            'spread_away_odds': None,
            'total_points': None,
            'total_over_odds': None,
            'total_under_odds': None
        }
        
        # moneyline odds (92_1)
        # spread odds (92_2)
        # total points odds (92_3)
        if 'odds' not in odds_data.keys(): return {}
        odds_dict = odds_data['odds']
        if ('92_1' in odds_dict): (odds_info['home_odds'], odds_info['away_odds']) = get_pregame_odds(('home_od', 'away_od'), odds_dict['92_1']);
        if ('92_2' in odds_dict): (odds_info['spread_home_odds'], odds_info['spread_away_odds'], odds_info['spread_handicap']) = get_pregame_odds(('home_od', 'away_od', 'handicap'), odds_dict['92_2']);
        if ('92_3' in odds_dict): (odds_info['total_over_odds'], odds_info['total_under_odds'], odds_info['total_points']) = get_pregame_odds(('over_od', 'under_od', 'handicap'), odds_dict['92_3']);
        return odds_info

    def update_match_odds(self, match_id: str, odds_info: Dict) -> bool:
        """Update a match with odds data"""
        if not odds_info:
            return False
        
        try:
            # Prepare update query
            fields = []
            values = []
            
            for field, value in odds_info.items():
                if value is not None:
                    fields.append(f"{field} = ?")
                    values.append(value)
            
            # Add timestamp
            fields.append("odds_updated_at = ?")
            values.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            
            # Add match_id for WHERE clause
            values.append(match_id)
            
            if not fields:
                print(f"No valid odds data to update for match {match_id}")
                return False
            
            # Execute update
            query = f"UPDATE matches SET {', '.join(fields)} WHERE id = ?"
            self.cursor.execute(query, values)
            self.conn.commit()
            
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            print(f"Error updating odds for match {match_id}: {e}")
            return False

    def mark_match_elo_processed(self, match_id: str) -> bool:
        """Mark a match as having been processed for ELO calculation."""
        try:
            self.cursor.execute('''
            UPDATE matches 
            SET elo_processed = TRUE, elo_processed_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            ''', (match_id,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            print(f"Error marking match {match_id} as ELO processed: {e}")
            return False

    def get_unprocessed_elo_matches(self, league_id: Optional[int] = None, limit: Optional[int] = None) -> List[str]:
        """Get match IDs that haven't been processed for ELO calculation, in chronological order."""
        try:
            query = '''
            SELECT id FROM matches 
            WHERE (elo_processed IS NULL OR elo_processed = FALSE)
            AND home_score IS NOT NULL AND away_score IS NOT NULL
            '''
            params = []
            
            if league_id is not None:
                query += ' AND league_id = ?'
                params.append(league_id)
                
            query += ' ORDER BY match_time ASC'
            
            if limit is not None:
                query += ' LIMIT ?'
                params.append(limit)
            
            self.cursor.execute(query, params)
            return [row[0] for row in self.cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error getting unprocessed ELO matches: {e}")
            return []

    def get_elo_processing_stats(self) -> Dict[str, int]:
        """Get statistics about ELO processing status."""
        try:
            stats = {}
            
            # Total matches
            self.cursor.execute("SELECT COUNT(*) FROM matches")
            stats['total_matches'] = self.cursor.fetchone()[0]
            
            # Processed matches
            self.cursor.execute("SELECT COUNT(*) FROM matches WHERE elo_processed = TRUE")
            stats['processed_matches'] = self.cursor.fetchone()[0]
            
            # Unprocessed matches
            self.cursor.execute('''
            SELECT COUNT(*) FROM matches 
            WHERE (elo_processed IS NULL OR elo_processed = FALSE)
            AND home_score IS NOT NULL AND away_score IS NOT NULL
            ''')
            stats['unprocessed_matches'] = self.cursor.fetchone()[0]
            
            return stats
        except sqlite3.Error as e:
            print(f"Error getting ELO processing stats: {e}")
            return {}

    def sync_elo_processed_status(self) -> int:
        """
        Sync existing ELO history with the new elo_processed tracking system.
        Marks matches as elo_processed if they exist in elo_history table.
        Returns the number of matches synced.
        """
        try:
            # Find matches that are in elo_history but not marked as processed
            self.cursor.execute('''
            UPDATE matches 
            SET elo_processed = TRUE, elo_processed_at = CURRENT_TIMESTAMP
            WHERE id IN (
                SELECT DISTINCT match_id FROM elo_history
            ) AND (elo_processed IS NULL OR elo_processed = FALSE)
            ''')
            
            synced_count = self.cursor.rowcount
            self.conn.commit()
            
            print(f"Synced {synced_count} matches with existing ELO history")
            return synced_count
            
        except sqlite3.Error as e:
            print(f"Error syncing ELO processed status: {e}")
            return 0

