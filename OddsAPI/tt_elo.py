import math
import sqlite3
from typing import Dict, List, Any, Optional, Tuple
import asyncio
import datetime
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from ttDB import TTDatabase
import os
import time

# Constants for ELO calculation
DEFAULT_K_FACTOR = 32  # Standard K-factor for chess ELO
PROVISIONAL_K_FACTOR = 64  # Higher K-factor for players with few matches
PROVISIONAL_MATCH_THRESHOLD = 30  # Number of matches to be considered non-provisional
DEFAULT_ELO = 1500  # Starting ELO for new players

# Set score importance weighting
SET_SCORE_WEIGHT = 0.3  # Weight for set score difference (adjust as needed)
MATCH_OUTCOME_WEIGHT = 0.7  # Weight for match outcome (adjust as needed)

class ELOCalculator:
    def __init__(self, database: TTDatabase):
        self.db = database
        self._db_lock = threading.Lock()
        # Ensure the current_elo table exists
        self.ensure_elo_table_exists()
        
    def ensure_elo_table_exists(self):
        """Create the current_elo table if it doesn't exist"""
        self.db.cursor.execute('''
        CREATE TABLE IF NOT EXISTS current_elo (
            player_id INTEGER,
            league_id INTEGER,
            elo INTEGER DEFAULT 1500,
            matches_played INTEGER DEFAULT 0,
            last_match_id TEXT,
            last_updated TIMESTAMP,
            PRIMARY KEY (player_id, league_id)
        )
        ''')
        
        self.db.cursor.execute('''
        CREATE TABLE IF NOT EXISTS elo_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            league_id INTEGER,
            match_id TEXT,
            old_elo INTEGER,
            new_elo INTEGER,
            match_date TIMESTAMP,
            FOREIGN KEY (match_id) REFERENCES matches(id)
        )
        ''')
        
        self.db.conn.commit()

    def calculate_expected_score(self, player_elo: int, opponent_elo: int) -> float:
        """
        Calculate the expected score for a player against an opponent.
        The expected score is a value between 0 and 1 representing the probability of winning.
        """
        return 1 / (1 + math.pow(10, (opponent_elo - player_elo) / 400))

    def get_k_factor(self, player_id: int, league_id: int, db: TTDatabase = None) -> int:
        """
        Get the appropriate K-factor for a player based on their match history.

        Players with fewer matches have a higher K-factor to allow their rating
        to adjust more quickly in the early stages.
        """
        if db is None:
            db = self.db
            
        # Get the number of matches the player has played
        db.cursor.execute('''
        SELECT matches_played FROM current_elo
        WHERE player_id = ? AND league_id = ?
        ''', (player_id, league_id))

        result = db.cursor.fetchone()
        if not result or not result['matches_played']:
            return PROVISIONAL_K_FACTOR

        matches_played = result['matches_played']

        # Use provisional K-factor for players with few matches
        if matches_played < PROVISIONAL_MATCH_THRESHOLD:
            return PROVISIONAL_K_FACTOR

        return DEFAULT_K_FACTOR

    def calculate_set_score_factor(self, home_sets: int, away_sets: int) -> float:
        """
        Calculate a factor based on the set score difference.
        This gives more weight to decisive victories and less to narrow wins.

        Returns a value between 0 and 1.
        """
        # Total number of sets played
        total_sets = home_sets + away_sets

        if total_sets == 0:
            return 0.5  # No information about sets

        # Set difference normalized to a scale of 0 to 1
        set_diff = abs(home_sets - away_sets) / total_sets

        # Scale it slightly to not be too extreme
        # A 3-0 win gives 0.75, a 3-2 win gives 0.55
        return 0.5 + (set_diff * 0.5)

    def calculate_elo_change(self, match_data: Dict, db: TTDatabase = None) -> Dict[str, Any]:
        """
        Calculate ELO changes for both players based on a match result.
        Takes into account both the match outcome and the set score.

        Returns a dictionary with ELO changes for both players.
        """
        if db is None:
            db = self.db
            
        # Extract player IDs and current ratings
        home_player_id = match_data.get('home_player_id')
        away_player_id = match_data.get('away_player_id')

        if not home_player_id or not away_player_id:
            return {'error': 'Missing player IDs'}

        league_id = match_data.get('league_id')

        # Get current ELO ratings
        home_elo = self.get_player_elo(home_player_id, league_id, db)
        away_elo = self.get_player_elo(away_player_id, league_id, db)

        # Get appropriate K-factors
        home_k = self.get_k_factor(home_player_id, league_id, db)
        away_k = self.get_k_factor(away_player_id, league_id, db)

        # Calculate expected scores
        home_expected = self.calculate_expected_score(home_elo, away_elo)
        away_expected = 1 - home_expected

        # Determine the actual match outcome (1 for win, 0.5 for draw, 0 for loss)
        home_score = match_data.get('home_score', 0)
        away_score = match_data.get('away_score', 0)

        if home_score > away_score:
            home_actual = 1.0
            away_actual = 0.0
        elif home_score < away_score:
            home_actual = 0.0
            away_actual = 1.0
        else:
            home_actual = 0.5
            away_actual = 0.5

        # Now factor in the set scores for more nuanced ELO adjustments
        set_scores = match_data.get('sets', {})
        home_sets = 0
        away_sets = 0

        for set_data in set_scores.values():
            home_set_score = set_data.get('home', 0)
            away_set_score = set_data.get('away', 0)

            if home_set_score > away_set_score:
                home_sets += 1
            elif away_set_score > home_set_score:
                away_sets += 1

        # Calculate the set score factor (how decisive was the victory)
        set_factor = self.calculate_set_score_factor(home_sets, away_sets)

        # Adjust the actual score based on set factor
        # For the winner, increase the score if the win was decisive
        # For the loser, decrease the penalty if they won some sets
        if home_actual > away_actual:  # Home player won
            home_actual_adjusted = MATCH_OUTCOME_WEIGHT * home_actual + SET_SCORE_WEIGHT * set_factor
            away_actual_adjusted = MATCH_OUTCOME_WEIGHT * away_actual + SET_SCORE_WEIGHT * (1 - set_factor)
        elif away_actual > home_actual:  # Away player won
            home_actual_adjusted = MATCH_OUTCOME_WEIGHT * home_actual + SET_SCORE_WEIGHT * (1 - set_factor)
            away_actual_adjusted = MATCH_OUTCOME_WEIGHT * away_actual + SET_SCORE_WEIGHT * set_factor
        else:  # Draw (rare in table tennis)
            home_actual_adjusted = home_actual
            away_actual_adjusted = away_actual

        # Calculate ELO changes
        home_elo_change = int(round(home_k * (home_actual_adjusted - home_expected)))
        away_elo_change = int(round(away_k * (away_actual_adjusted - away_expected)))

        # Calculate new ratings
        new_home_elo = home_elo + home_elo_change
        new_away_elo = away_elo + away_elo_change

        return {
            'match_id': match_data.get('id'),
            'home_player_id': home_player_id,
            'away_player_id': away_player_id,
            'home_old_elo': home_elo,
            'away_old_elo': away_elo,
            'home_new_elo': new_home_elo,
            'away_new_elo': new_away_elo,
            'home_elo_change': home_elo_change,
            'away_elo_change': away_elo_change,
            'match_date': match_data.get('match_time')
        }

    def get_player_elo(self, player_id: int, league_id: int, db: TTDatabase = None) -> int:
        """
        Get a player's current ELO rating. If the player doesn't have a rating yet,
        initialize them with the default ELO.
        """
        if db is None:
            db = self.db
            
        db.cursor.execute('''
        SELECT elo FROM current_elo
        WHERE player_id = ? AND league_id = ?
        ''', (player_id, league_id))
        
        result = db.cursor.fetchone()
        if result:
            return result['elo']
        
        # Player doesn't have an ELO yet, initialize with default
        try:
            db.cursor.execute('''
            INSERT INTO current_elo (player_id, league_id, elo, matches_played)
            VALUES (?, ?, ?, 0)
            ''', (player_id, league_id, DEFAULT_ELO))
            db.conn.commit()
            return DEFAULT_ELO
        except sqlite3.IntegrityError:
            # Another thread created this player's ELO entry, fetch it
            db.cursor.execute('''
            SELECT elo FROM current_elo
            WHERE player_id = ? AND league_id = ?
            ''', (player_id, league_id))
            
            result = db.cursor.fetchone()
            return result['elo'] if result else DEFAULT_ELO

    def update_elo_for_match(self, match_id: str, db: TTDatabase = None) -> Dict[str, Any]:
        """
        Calculate and update ELO ratings for a specific match.
        """
        if db is None:
            db = self.db
            
        # Get match data from database
        db.cursor.execute('''
        SELECT * FROM matches WHERE id = ?
        ''', (match_id,))

        match_row = db.cursor.fetchone()
        if not match_row:
            return {'error': f'Match {match_id} not found'}

        match_data = dict(match_row)

        # Get the set scores
        db.cursor.execute('''
        SELECT set_number, home_score, away_score
        FROM sets
        WHERE match_id = ?
        ORDER BY set_number
        ''', (match_id,))

        sets = {}
        for set_row in db.cursor.fetchall():
            sets[set_row['set_number']] = {
                'home': set_row['home_score'],
                'away': set_row['away_score']
            }

        match_data['sets'] = sets

        # Calculate ELO changes
        elo_changes = self.calculate_elo_change(match_data, db)

        if 'error' in elo_changes:
            return elo_changes

        # Update ELO for both players
        self.update_player_elo(
            elo_changes['home_player_id'],
            match_data['league_id'],
            elo_changes['home_new_elo'],
            match_id,
            elo_changes['home_old_elo'],
            db
        )

        self.update_player_elo(
            elo_changes['away_player_id'],
            match_data['league_id'],
            elo_changes['away_new_elo'],
            match_id,
            elo_changes['away_old_elo'],
            db
        )
        
        # Mark the match as ELO processed
        db.mark_match_elo_processed(match_id)

        return elo_changes


    def update_player_elo(self, player_id: int, league_id: int, new_elo: int, match_id: str, old_elo: int, db: TTDatabase = None) -> bool:
        """
        Update a player's ELO rating in the database.
        Also records the ELO change in the history table.
        """
        if db is None:
            db = self.db
            
        try:
            with self._db_lock:
                # Update current ELO
                db.cursor.execute('''
                UPDATE current_elo
                SET elo = ?, last_match_id = ?, last_updated = CURRENT_TIMESTAMP,
                    matches_played = matches_played + 1
                WHERE player_id = ? AND league_id = ?
                ''', (new_elo, match_id, player_id, league_id))
                
                # If no rows were updated, the player doesn't have an entry yet
                if db.cursor.rowcount == 0:
                    try:
                        db.cursor.execute('''
                        INSERT INTO current_elo
                        (player_id, league_id, elo, matches_played, last_match_id, last_updated)
                        VALUES (?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
                        ''', (player_id, league_id, new_elo, match_id))
                    except sqlite3.IntegrityError:
                        # Another thread created this entry, try update again
                        db.cursor.execute('''
                        UPDATE current_elo
                        SET elo = ?, last_match_id = ?, last_updated = CURRENT_TIMESTAMP,
                            matches_played = matches_played + 1
                        WHERE player_id = ? AND league_id = ?
                        ''', (new_elo, match_id, player_id, league_id))
                
                # Record in ELO history
                db.cursor.execute('''
                INSERT INTO elo_history
                (player_id, league_id, match_id, old_elo, new_elo, match_date)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (player_id, league_id, match_id, old_elo, new_elo))
                
                db.conn.commit()
                return True
        except Exception as e:
            print(f"Error updating ELO for player {player_id}: {e}")
            db.conn.rollback()
            return False

    def process_match_chunk(self, match_ids: List[str]) -> int:
        """
        Process a chunk of matches in chronological order using a thread-safe database connection.
        Returns the number of matches successfully processed.
        """
        if not match_ids:
            return 0
            
        # Create a new database connection for this thread
        db = TTDatabase()
        matches_processed = 0
        
        try:
            for match_id in match_ids:
                # Retry on database lock errors
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        result = self.update_elo_for_match(match_id, db)
                        if 'error' not in result:
                            matches_processed += 1
                        break  # Success, exit retry loop
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e) and attempt < max_retries - 1:
                            # Wait a bit and retry
                            
                            time.sleep(0.1 * (attempt + 1))
                            continue
                        else:
                            print(f"Database error processing match {match_id}: {e}")
                            break
        finally:
            db.close()
            
        return matches_processed

    def process_all_matches(self, player_id: Optional[int] = None) -> int:
        """
        Process all matches in chronological order to calculate ELO ratings using multithreading.
        If player_id is provided, only process matches involving that player.

        Args:
            player_id: Optional player ID to filter matches

        Returns the number of matches processed.
        """
        
        
        # Use fewer workers for SQLite to reduce lock contention
        max_workers = min(4, os.cpu_count() or 4)
            
        # Get all matches in chronological order
        if player_id:
            self.db.cursor.execute('''
            SELECT id FROM matches
            WHERE home_player_id = ? OR away_player_id = ?
            ORDER BY match_time ASC
            ''', (player_id, player_id))
        else:
            self.db.cursor.execute('''
            SELECT id FROM matches
            ORDER BY match_time ASC
            ''')

        match_ids = [row['id'] for row in self.db.cursor.fetchall()]
        
        if not match_ids:
            return 0
            
        # Use larger chunks for SQLite to reduce lock contention
        chunk_size = max(100, len(match_ids) // (max_workers * 2))
        if chunk_size > 2000:  # Larger chunks for fewer database connections
            chunk_size = 2000
            
        # Split matches into chunks for parallel processing
        chunks = []
        for i in range(0, len(match_ids), chunk_size):
            chunks.append(match_ids[i:i + chunk_size])
        
        matches_processed = 0
        
        print(f"Processing {len(match_ids)} matches in {len(chunks)} chunks using {max_workers} threads...")
        
        # Process chunks in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_chunk = {
                executor.submit(self.process_match_chunk, chunk): i 
                for i, chunk in enumerate(chunks)
            }
            
            for future in as_completed(future_to_chunk):
                chunk_index = future_to_chunk[future]
                try:
                    chunk_matches_processed = future.result()
                    matches_processed += chunk_matches_processed
                    print(f"Completed chunk {chunk_index + 1}/{len(chunks)}: {chunk_matches_processed} matches processed")
                except Exception as e:
                    print(f"Error processing chunk {chunk_index + 1}: {e}")

        return matches_processed

    def process_unprocessed_matches_single_threaded(self, league_id: Optional[int] = None, limit: Optional[int] = None) -> int:
        """
        Process unprocessed matches in single-threaded mode for SQLite reliability.
        """
        # Get unprocessed matches in chronological order
        unprocessed_matches = self.db.get_unprocessed_elo_matches(league_id, limit)
        
        if not unprocessed_matches:
            print("No unprocessed matches found.")
            return 0
            
        print(f"Found {len(unprocessed_matches)} unprocessed matches for ELO calculation")
        print("Processing in single-threaded mode for SQLite reliability...")
        
        matches_processed = 0
        total_matches = len(unprocessed_matches)
        
        for i, match_id in enumerate(unprocessed_matches):
            try:
                result = self.update_elo_for_match(match_id)
                if 'error' not in result:
                    matches_processed += 1
                    
                # Progress update every 1000 matches
                if (i + 1) % 1000 == 0:
                    percentage = ((i + 1) / total_matches) * 100
                    print(f"Progress: {i + 1}/{total_matches} ({percentage:.1f}%) - {matches_processed} successful")
                    
            except Exception as e:
                print(f"Error processing match {match_id}: {e}")
                continue
        
        return matches_processed

    def process_unprocessed_matches(self, league_id: Optional[int] = None, batch_size: int = 2000, limit: Optional[int] = None) -> int:
        """
        Process only matches that haven't been processed for ELO calculation yet.
        This is the main method for incremental ELO updates.
        
        Args:
            league_id: Optional league ID to filter matches
            batch_size: Number of matches to process in each batch
            limit: Optional limit on total matches to process
            
        Returns the number of matches processed.
        """
        import os
        
        # Use fewer workers for SQLite to reduce lock contention
        max_workers = min(4, os.cpu_count() or 4)
            
        # Get unprocessed matches in chronological order
        unprocessed_matches = self.db.get_unprocessed_elo_matches(league_id, limit)
        
        if not unprocessed_matches:
            print("No unprocessed matches found.")
            return 0
            
        print(f"Found {len(unprocessed_matches)} unprocessed matches for ELO calculation")
        
        # Process in batches to maintain chronological order while using threading
        matches_processed = 0
        
        for i in range(0, len(unprocessed_matches), batch_size):
            batch = unprocessed_matches[i:i + batch_size]
            print(f"Processing batch {i//batch_size + 1}: {len(batch)} matches...")
            
            # Use larger chunks for SQLite to reduce lock contention
            chunk_size = max(50, len(batch) // (max_workers * 2))
            if chunk_size > 1000:  # Larger chunks for fewer database connections
                chunk_size = 1000
                
            # Split batch into chunks for parallel processing
            chunks = []
            for j in range(0, len(batch), chunk_size):
                chunks.append(batch[j:j + chunk_size])
            
            # Process chunks in parallel within this batch
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_chunk = {
                    executor.submit(self.process_match_chunk, chunk): k 
                    for k, chunk in enumerate(chunks)
                }
                
                for future in as_completed(future_to_chunk):
                    chunk_index = future_to_chunk[future]
                    try:
                        chunk_matches_processed = future.result()
                        matches_processed += chunk_matches_processed
                        print(f"  Completed chunk {chunk_index + 1}/{len(chunks)}: {chunk_matches_processed} matches processed")
                    except Exception as e:
                        print(f"  Error processing chunk {chunk_index + 1}: {e}")
            
            print(f"Batch {i//batch_size + 1} completed. Total processed so far: {matches_processed}")
        
        return matches_processed

    def get_elo_processing_status(self) -> Dict[str, Any]:
        """Get detailed status information about ELO processing."""
        stats = self.db.get_elo_processing_stats()
        
        # Add percentage information
        if stats.get('total_matches', 0) > 0:
            stats['processed_percentage'] = (stats['processed_matches'] / stats['total_matches']) * 100
        else:
            stats['processed_percentage'] = 0
            
        return stats

    def sync_existing_elo_data(self) -> int:
        """
        Sync existing ELO history with the new tracking system.
        This should be run once to mark already-processed matches.
        """
        return self.db.sync_elo_processed_status()

    def get_league_rankings(self, league_id: int, min_matches: int = 5) -> List[Dict]:
        """
        Get ELO rankings for a specific league, filtering for players with at least
        the minimum number of matches played.
        """
        self.db.cursor.execute('''
        SELECT p.id, p.name, ce.elo, ce.matches_played
        FROM players p
        JOIN current_elo ce ON p.id = ce.player_id AND p.league_id = ce.league_id
        WHERE p.league_id = ? AND ce.matches_played >= ?
        ORDER BY ce.elo DESC
        ''', (league_id, min_matches))

        return [dict(row) for row in self.db.cursor.fetchall()]

# Main function for processing a player's ELO
async def process_player_elo(player_id: int, player_name: str, league_id: int, limit: int = 100) -> None:
    """
    Process a player's match history and update their ELO rating.

    Steps:
    1. Fetch and store player's match history
    2. Calculate ELO for all matches involving the player
    3. Print player's current ELO rating and match history
    """
    # Initialize database and ELO calculator
    db = TTDatabase()
    elo_calculator = ELOCalculator(db)

    try:
        # First, check if the player exists
        if not db.does_player_exist(player_id, league_id):
            print(f"Player {player_name} (ID: {player_id}) not found in database.")
            print(f"Fetching match history for {player_name}...")

            # Create a session for API calls
            async with aiohttp.ClientSession() as session:
                # Collect player history
                match_count = await db.fetch_player_history(player_id, player_name, league_id, session, limit)

                if match_count == 0:
                    print(f"No matches found for player {player_name}.")
                    return

        # Process ELO for the player's matches
        print(f"\nCalculating ELO for {player_name}...")
        matches_processed = elo_calculator.process_all_matches(player_id)

        print(f"Processed {matches_processed} matches for ELO calculation.")

        # Get the player's current ELO and match history
        elo_calculator.db.cursor.execute('''
        SELECT p.id, p.name, ce.elo, ce.matches_played
        FROM players p
        JOIN current_elo ce ON p.id = ce.player_id
        WHERE p.id = ? AND ce.league_id = ?
        ''', (player_id, league_id))
        
        player_info = elo_calculator.db.cursor.fetchone()
        if not player_info:
            print(f"Could not find player {player_name} after processing.")
            return
            
        player_info = dict(player_info)
        
        # Get ELO history
        elo_calculator.db.cursor.execute('''
        SELECT eh.match_id, eh.old_elo, eh.new_elo, m.match_time,
               m.home_player_id, m.away_player_id, 
               m.home_player_name, m.away_player_name,
               m.home_score, m.away_score
        FROM elo_history eh
        JOIN matches m ON eh.match_id = m.id
        WHERE eh.player_id = ? AND eh.league_id = ?
        ORDER BY m.match_time ASC
        ''', (player_id, league_id))
        
        elo_history = [dict(row) for row in elo_calculator.db.cursor.fetchall()]

        print(f"\nCurrent ELO for {player_name}: {player_info.get('elo', DEFAULT_ELO)}")
        print(f"Matches played: {player_info.get('matches_played', 0)}")

        # Display ELO history
        if elo_history:
            print(f"\nELO History:")
            for i, entry in enumerate(elo_history):
                opponent_id = entry['home_player_id'] if entry['player_id'] == entry['away_player_id'] else entry['away_player_id']
                opponent_name = entry['home_player_name'] if entry['player_id'] == entry['away_player_id'] else entry['away_player_name']

                result = "Won" if ((player_id == entry['home_player_id'] and entry['home_score'] > entry['away_score']) or
                                 (player_id == entry['away_player_id'] and entry['away_score'] > entry['home_score'])) else "Lost"

                elo_change = entry['new_elo'] - entry['old_elo']
                sign = "+" if elo_change > 0 else ""

                print(f"{i+1}. {entry['match_time']}: {result} vs {opponent_name} - ELO: {entry['old_elo']} → {entry['new_elo']} ({sign}{elo_change})")

        # Display league rankings
        rankings = elo_calculator.get_league_rankings(league_id)
        if rankings:
            # Find the player's rank
            player_rank = next((i+1 for i, p in enumerate(rankings) if p['id'] == player_id), None)

            print(f"\nCurrent rank in league: {player_rank if player_rank else 'Not ranked'} of {len(rankings)}")

            # Show top 10 players in the league
            print(f"\nTop 10 players in league {league_id}:")
            for i, player in enumerate(rankings[:10]):
                print(f"{i+1}. {player['name']}: {player['elo']} (matches: {player['matches_played']})")

    finally:
        # Close the database connection
        db.close()

# Command-line interface
async def main():
    parser = argparse.ArgumentParser(description='Calculate ELO ratings for table tennis players')

    parser.add_argument('--player-id', type=int, help='Player ID to process')
    parser.add_argument('--player-name', type=str, help='Player name (for fetching history)')
    parser.add_argument('--league-id', type=int, default=22307,
                        help='League ID (default: 22307 for Setka Cup)')
    parser.add_argument('--limit', type=int, default=100,
                        help='Maximum number of matches to fetch (default: 100)')
    parser.add_argument('--process-all', action='store_true',
                        help='Process all matches in the database')
    parser.add_argument('--process-unprocessed', action='store_true',
                        help='Process only unprocessed matches (incremental update)')
    parser.add_argument('--status', action='store_true',
                        help='Show ELO processing status')
    parser.add_argument('--sync-existing', action='store_true',
                        help='Sync existing ELO data with new tracking system (run once)')
    parser.add_argument('--single-threaded', action='store_true',
                        help='Use single-threaded processing (more reliable for SQLite)')
    parser.add_argument('--test-limit', type=int, default=None,
                        help='Limit number of matches to process (for testing)')

    args = parser.parse_args()

    if args.process_all:
        db = TTDatabase()
        elo_calculator = ELOCalculator(db)

        try:
            print("Processing all matches in database...")
            matches_processed = elo_calculator.process_all_matches()
            print(f"Processed {matches_processed} matches for ELO calculation.")

            # Show rankings for each league
            for league_id in [22307, 22742, 22534, 24536]:  # All supported leagues
                rankings = elo_calculator.get_league_rankings(league_id)
                if rankings:
                    league_name = {
                        22307: "Setka Cup",
                        22742: "Czech Republic Liga Pro",
                        22534: "TT CUP",
                        24536: "Poland TT Elite Series"
                    }.get(league_id, f"League {league_id}")

                    print(f"\nTop 10 players in {league_name}:")
                    for i, player in enumerate(rankings[:10]):
                        print(f"{i+1}. {player['name']}: {player['elo']} (matches: {player['matches_played']})")
        finally:
            db.close()
    elif args.process_unprocessed:
        db = TTDatabase()
        elo_calculator = ELOCalculator(db)

        try:
            print("Processing unprocessed matches...")
            status = elo_calculator.get_elo_processing_status()
            print(f"Current status: {status['processed_matches']}/{status['total_matches']} matches processed ({status['processed_percentage']:.1f}%)")
            print(f"Unprocessed matches: {status['unprocessed_matches']}")
            
            if status['unprocessed_matches'] > 0:
                if args.single_threaded:
                    matches_processed = elo_calculator.process_unprocessed_matches_single_threaded(limit=args.test_limit)
                else:
                    matches_processed = elo_calculator.process_unprocessed_matches(limit=args.test_limit)
                print(f"Successfully processed {matches_processed} additional matches.")
                
                # Show updated status
                new_status = elo_calculator.get_elo_processing_status()
                print(f"New status: {new_status['processed_matches']}/{new_status['total_matches']} matches processed ({new_status['processed_percentage']:.1f}%)")
            else:
                print("All matches are already processed!")
        finally:
            db.close()
    elif args.status:
        db = TTDatabase()
        elo_calculator = ELOCalculator(db)

        try:
            status = elo_calculator.get_elo_processing_status()
            print(f"ELO Processing Status:")
            print(f"  Total matches: {status['total_matches']}")
            print(f"  Processed matches: {status['processed_matches']}")
            print(f"  Unprocessed matches: {status['unprocessed_matches']}")
            print(f"  Completion: {status['processed_percentage']:.1f}%")
        finally:
            db.close()
    elif args.sync_existing:
        db = TTDatabase()
        elo_calculator = ELOCalculator(db)

        try:
            print("Syncing existing ELO data with new tracking system...")
            synced_count = elo_calculator.sync_existing_elo_data()
            print(f"Successfully synced {synced_count} matches.")
            
            # Show updated status
            status = elo_calculator.get_elo_processing_status()
            print(f"Updated status: {status['processed_matches']}/{status['total_matches']} matches processed ({status['processed_percentage']:.1f}%)")
            print(f"Remaining unprocessed matches: {status['unprocessed_matches']}")
        finally:
            db.close()
    elif args.player_id and args.player_name:
        await process_player_elo(args.player_id, args.player_name, args.league_id, args.limit)
    else:
        parser.print_help()

if __name__ == "__main__":
    import aiohttp
    asyncio.run(main())

