#!/usr/bin/env python3
"""
NHL Database Update Orchestrator
=================================
Unified update system for keeping the NHL analytics database current.
Coordinates updates across all data sources using game_id as the universal key.

Data Sources:
1. Natural Stat Trick (NST) - Detailed game stats
2. MoneyPuck - Advanced analytics (team/player/shots)
3. MoneyPuck Odds - Betting lines from multiple sportsbooks
4. NHL EDGE - Official tracking data (future integration)

Author: NHL Analytics Database Project
Date: 2025-12-02
"""

import sqlite3
import subprocess
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Set, Tuple, Optional
from dataclasses import dataclass
from enum import Enum
import sys
import os
import glob

# Import data collectors and importers
import nstparse
import nhl_db_manager
import moneypuck_downloader
import mp_import
import collect_edge_stats
import nhl_edge_importer
import playeridextract


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('nhl_update_orchestrator.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class UpdateStatus(Enum):
    """Status of update operations"""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class GameToUpdate:
    """Represents a game that needs updating"""
    game_id: str
    game_date: str
    home_team: str
    away_team: str
    home_team_full: str
    away_team_full: str
    season: str
    has_nst_data: bool = False
    has_mp_team_data: bool = False
    has_mp_player_data: bool = False
    has_odds_data: bool = False
    has_edge_data: bool = False


@dataclass
class UpdateResult:
    """Result of an update operation"""
    source: str
    status: UpdateStatus
    games_processed: int
    games_added: int
    games_updated: int
    errors: List[str]
    duration_seconds: float


class NHLUpdateOrchestrator:
    """
    Orchestrates updates across all NHL data sources.
    Uses game_id as the universal key to track what needs updating.
    """

    def __init__(self, db_path: str = "nhl_analytics.db", lookback_days: int = 7):
        """
        Initialize the update orchestrator.

        Args:
            db_path: Path to the NHL analytics database
            lookback_days: How many days back to check for new/updated games
        """
        self.db_path = db_path
        self.lookback_days = lookback_days
        self.conn: Optional[sqlite3.Connection] = None

        # Track results
        self.results: List[UpdateResult] = []

    def connect(self):
        """Connect to the database"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        logger.info(f"Connected to database: {self.db_path}")

    def disconnect(self):
        """Disconnect from the database"""
        if self.conn:
            self.conn.close()
            logger.info("Disconnected from database")

    # =========================================================================
    # Game Discovery - Find what needs updating
    # =========================================================================

    def get_recent_games_from_nhl_api(self) -> List[Dict]:
        """
        Query the NHL API for recent games.
        This is the source of truth for what games exist.

        Returns:
            List of game dictionaries with game_id, date, teams, etc.
        """
        try:
            from nhlpy import NHLClient
            client = NHLClient()

            # Get games from the last N days
            games = []
            end_date = datetime.now()
            start_date = end_date - timedelta(days=self.lookback_days)

            logger.info(f"Fetching games from NHL API between {start_date.date()} and {end_date.date()}")

            # Query schedule for each day
            current_date = start_date
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                try:
                    # Use the correct nhlpy API method
                    schedule = client.schedule.daily_schedule(date=date_str)

                    # Parse schedule response - daily_schedule returns a dict with 'games' key
                    if 'games' in schedule and schedule['games']:
                        for game in schedule['games']:
                            # Filter for completed games only
                            if game.get('gameState') not in ['FINAL', 'OFF']:
                                continue

                            # Convert season to season string format (20242025 -> "2024-25")
                            season_int = str(game['season'])
                            season_str = f"{season_int[:4]}-{season_int[6:]}"

                            game_data = {
                                'game_id': str(game['id']),
                                'game_date': schedule['date'],  # YYYY-MM-DD from schedule response
                                'season': season_str,
                                'game_type': game['gameType'],
                                'away_team': game['awayTeam']['abbrev'],
                                'home_team': game['homeTeam']['abbrev'],
                                'game_state': game['gameState']
                            }
                            games.append(game_data)

                except Exception as e:
                    logger.debug(f"No games found for {date_str}: {e}")

                current_date += timedelta(days=1)

            logger.info(f"Found {len(games)} games from NHL API")
            return games

        except ImportError:
            logger.error("nhlpy library not installed. Run: pip install nhlpy")
            return []
        except Exception as e:
            logger.error(f"Error fetching games from NHL API: {e}")
            return []

    def get_downloaded_nst_games(self) -> Set[str]:
        """
        Scan the nhlteamreports directory to find games that have already been downloaded.
        Returns a set of game_ids (e.g., '2024020484').
        """
        logger.info("Scanning nhlteamreports for downloaded games...")
        downloaded_games = set()
        
        # Pattern: nhlteamreports/{TEAM}/games/{SEASON}/*.csv
        # We look for files that contain a game ID in their name
        # Typically format: {AWAY}vs{HOME}_{GAMEID}_...
        
        # We can use a glob to find all CSVs in the games subdirectories
        # Using glob.iglob for iterator to be memory efficient
        search_pattern = "nhlteamreports/*/games/**/*.csv"
        
        count = 0
        for file_path in glob.iglob(search_pattern, recursive=True):
            filename = os.path.basename(file_path)
            
            # Skip games_list files
            if "_games_list.csv" in filename:
                continue
                
            # Try to extract game ID
            # Format usually: BOSvsOTT_20149_... or similar
            # The game ID is usually the second part after splitting by underscore
            # But sometimes it might be different.
            # Let's try a regex or split.
            
            parts = filename.split('_')
            if len(parts) >= 2:
                # Check if the second part is a digit (game id suffix)
                # or if it's a full game id
                potential_id = parts[1]
                
                if potential_id.isdigit():
                    # If it's a short ID (e.g. 20149), we might need context to know the full ID
                    # But wait, our DB uses full IDs (2024020149).
                    # The filenames usually have the short ID (20149).
                    # We need to map short ID to full ID if possible, OR just return the suffix
                    # and let the checker handle the matching.
                    
                    # Actually, nstparse.py logic uses season code to reconstruct full ID.
                    # We can try to extract season from the path.
                    # Path: nhlteamreports/OTT/games/2024-25/...
                    path_parts = file_path.split(os.sep)
                    try:
                        season_idx = path_parts.index("games") + 1
                        season_str = path_parts[season_idx] # "2024-25"
                        
                        if len(season_str) == 7 and '-' in season_str:
                            # "2024-25" -> "20242025"
                            start_year = season_str[:4]
                            end_year = "20" + season_str[5:]
                            season_code = f"{start_year}{end_year}"
                            
                            # Construct full ID
                            # Game ID in filename is usually 5 digits (20484)
                            # Full ID: 2024020484
                            # logic: {start_year}0{suffix} if suffix starts with 2?
                            # Actually, suffix 20484 -> 020484. 30111 -> 030111.
                            # So full ID = start_year + "0" + suffix (if suffix is 5 digits)
                            
                            if len(potential_id) == 5:
                                full_id = f"{start_year}0{potential_id}"
                                downloaded_games.add(full_id)
                                count += 1
                    except (ValueError, IndexError):
                        pass

        logger.info(f"Found {len(downloaded_games)} unique downloaded games on disk")
        return downloaded_games

    def get_games_in_db(self) -> Set[str]:
        """
        Get all game_ids currently in the database.

        Returns:
            Set of game_ids
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT game_id FROM games")
        game_ids = {row[0] for row in cursor.fetchall()}
        logger.info(f"Found {len(game_ids)} games in database")
        return game_ids

    def check_data_coverage(self, game_ids: List[str], known_games_map: Dict = None) -> List[GameToUpdate]:
        """
        Check what data exists for each game_id.

        Args:
            game_ids: List of game_ids to check
            known_games_map: Optional dict of game data from API {game_id: game_data}

        Returns:
            List of GameToUpdate objects with coverage flags
        """
        cursor = self.conn.cursor()
        games_to_update = []
        known_games_map = known_games_map or {}

        for game_id in game_ids:
            # Get game metadata from DB
            cursor.execute("""
                SELECT game_id, game_date, home_team_id, away_team_id, season
                FROM games
                WHERE game_id = ?
            """, (game_id,))

            game_row = cursor.fetchone()
            if not game_row:
                # Game not in database yet - try to get info from known_games_map
                game_info = known_games_map.get(game_id, {})
                
                games_to_update.append(GameToUpdate(
                    game_id=game_id,
                    game_date=game_info.get('game_date', ""),
                    home_team=game_info.get('home_team', ""),
                    away_team=game_info.get('away_team', ""),
                    home_team_full="", # Full names are not in api_games, will be fetched from DB later
                    away_team_full="",
                    season=game_info.get('season', "")
                ))
                continue

            # Check NST data - Use team_game_overview as the indicator
            cursor.execute("""
                SELECT COUNT(*) FROM team_game_overview WHERE game_id = ?
            """, (game_id,))
            has_nst = cursor.fetchone()[0] > 0

            # Check MoneyPuck team data
            cursor.execute("""
                SELECT COUNT(*) FROM mp_team_game_stats WHERE game_id = ?
            """, (game_id,))
            has_mp_team = cursor.fetchone()[0] > 0

            # Check MoneyPuck player data
            cursor.execute("""
                SELECT COUNT(*) FROM mp_skater_game_stats WHERE game_id = ?
            """, (game_id,))
            has_mp_player = cursor.fetchone()[0] > 0

            # Check odds data
            cursor.execute("""
                SELECT COUNT(*) FROM game_odds WHERE game_id = ?
            """, (game_id,))
            has_odds = cursor.fetchone()[0] > 0

            # Check EDGE data (placeholder - not implemented yet)
            has_edge = False

            # Get team abbreviations AND full names
            cursor.execute("""
                SELECT ht.team_abbr, at.team_abbr, ht.team_name, at.team_name
                FROM games g
                JOIN teams ht ON g.home_team_id = ht.team_id
                JOIN teams at ON g.away_team_id = at.team_id
                WHERE g.game_id = ?
            """, (game_id,))
            team_row = cursor.fetchone()
            home_team = team_row[0] if team_row else ""
            away_team = team_row[1] if team_row else ""
            home_team_full = team_row[2] if team_row else ""
            away_team_full = team_row[3] if team_row else ""

            games_to_update.append(GameToUpdate(
                game_id=game_id,
                game_date=game_row['game_date'],
                home_team=home_team,
                away_team=away_team,
                home_team_full=home_team_full,
                away_team_full=away_team_full,
                season=game_row['season'],
                has_nst_data=has_nst,
                has_mp_team_data=has_mp_team,
                has_mp_player_data=has_mp_player,
                has_odds_data=has_odds,
                has_edge_data=has_edge
            ))

        return games_to_update

    def identify_games_to_update(self) -> List[GameToUpdate]:
        """
        Identify which games need updating based on:
        1. New games from NHL API not in DB
        2. Recent games in DB with incomplete data
        3. Games within lookback window

        Returns:
            List of GameToUpdate objects
        """
        logger.info("=" * 70)
        logger.info("IDENTIFYING GAMES TO UPDATE")
        logger.info("=" * 70)

        # Get recent games from NHL API
        api_games = self.get_recent_games_from_nhl_api()
        api_game_ids = {g['game_id'] for g in api_games}
        api_games_map = {g['game_id']: g for g in api_games} # Create map here

        # Get games already in DB
        db_game_ids = self.get_games_in_db()
        
        # Get games already downloaded (on disk)
        downloaded_game_ids = self.get_downloaded_nst_games()

        # Find new games (in API but not in DB)
        new_game_ids = api_game_ids - db_game_ids
        logger.info(f"Found {len(new_game_ids)} new games not in database")

        # Get recent games from DB to check for updates
        cursor = self.conn.cursor()
        cutoff_date = (datetime.now() - timedelta(days=self.lookback_days)).strftime('%Y-%m-%d')
        cursor.execute("""
            SELECT game_id FROM games
            WHERE game_date >= ?
            ORDER BY game_date DESC
        """, (cutoff_date,))
        recent_db_game_ids = {row[0] for row in cursor.fetchall()}
        logger.info(f"Found {len(recent_db_game_ids)} recent games in database")

        # Combine: new games + recent games
        all_game_ids_to_check = list(new_game_ids | recent_db_game_ids)

        # Check data coverage for all games, passing the API games map for new games
        games_to_update = self.check_data_coverage(all_game_ids_to_check, known_games_map=api_games_map)

        # Filter to games that actually need updates
        games_needing_updates = []
        for g in games_to_update:
            # Check NST: missing if (not in DB) AND (not on disk)
            # If it is on disk but not in DB, we still need to 'update' it (by importing),
            # but we shouldn't re-scrape it.
            # The update_nst_data function handles the scraping part.
            # We will mark has_nst_data as True if it is on disk, effectively skipping scraping
            # but check_data_coverage uses DB.
            # Let's override has_nst_data if it's on disk so we don't re-scrape.
            
            if g.game_id in downloaded_game_ids:
                # It's downloaded. If it's not in DB (g.has_nst_data is False), 
                # we treat it as "has data" for the purpose of *scraping*, 
                # but we still want to process it to *import* it.
                # However, the current logic in update_nst_data filters by `not g.has_nst_data`.
                # If we set True, it skips scraping AND importing.
                # If we set False, it scrapes AND imports.
                # We want: Skip Scraping, Do Import.
                
                # We'll leave has_nst_data as False (from DB check), but in update_nst_data
                # we will check if it's in downloaded_game_ids before scraping.
                pass

            if not (g.has_nst_data and g.has_mp_team_data and
                   g.has_mp_player_data and g.has_odds_data):
                   games_needing_updates.append(g)

        logger.info(f"Found {len(games_needing_updates)} games needing updates:")
        logger.info(f"  - Missing NST data (team_game_overview): {sum(1 for g in games_needing_updates if not g.has_nst_data)}")
        logger.info(f"  - Missing MP team data: {sum(1 for g in games_needing_updates if not g.has_mp_team_data)}")
        logger.info(f"  - Missing MP player data: {sum(1 for g in games_needing_updates if not g.has_mp_player_data)}")
        logger.info(f"  - Missing odds data: {sum(1 for g in games_needing_updates if not g.has_odds_data)}")

        return games_needing_updates

    # =========================================================================
    # Update Execution - Run specific data source updates
    # =========================================================================

    def _get_current_season_code(self) -> str:
        """
        Determine the current NHL season code based on the current date.
        NHL season starts in October.
        
        Logic:
        - If month >= 10 (Oct, Nov, Dec): Season is CurrentYear + (CurrentYear + 1)
        - If month < 10 (Jan - Sep): Season is (CurrentYear - 1) + CurrentYear
        
        Example:
        - Date: 2025-12-07 -> Season: 20252026
        - Date: 2026-02-15 -> Season: 20252026
        - Date: 2024-11-01 -> Season: 20242025
        """
        now = datetime.now()
        current_year = now.year
        current_month = now.month
        
        if current_month >= 10:
            start_year = current_year
            end_year = current_year + 1
        else:
            start_year = current_year - 1
            end_year = current_year
            
        return f"{start_year}{end_year}"

    def update_nst_data(self, games: List[GameToUpdate], force_reimport: bool = False) -> UpdateResult:
        """
        Update Natural Stat Trick data for specified games.
        Selectively scrapes missing games and imports them into the database.
        
        Args:
            games: List of games to update
            force_reimport: Whether to force re-scraping of existing data
            
        Returns:
            UpdateResult object
        """
        start_time = datetime.now()
        logger.info("=" * 70)
        logger.info("UPDATING NST DATA (Selective Scraping + Importing)")
        logger.info("=" * 70)

        # Filter to games needing NST data
        games_to_scrape = [g for g in games if not g.has_nst_data or force_reimport]
        
        if not games_to_scrape:
            logger.info("No games need NST data - skipping scraping phase")
        else:
            logger.info(f"Will selectively scrape NST data for {len(games_to_scrape)} games")

        # 1. Scrape Data - Selective Game Mode
        session = nstparse.create_session()
        game_cache = {}
        scraped_games_count = 0

        # Get list of already downloaded games to avoid re-scraping
        downloaded_game_ids = self.get_downloaded_nst_games()

        # Determine current season code dynamically for fallback
        current_season_code = self._get_current_season_code()

        # Update games_list for all affected teams before scraping
        if games_to_scrape:
            logger.info("Updating games_list files for affected teams...")
            teams_to_update = set()
            for game in games_to_scrape:
                if game.home_team:
                    teams_to_update.add(game.home_team)
                if game.away_team:
                    teams_to_update.add(game.away_team)

            for team in teams_to_update:
                try:
                    # Fetch and update games list from NST for this team
                    # Use current season code for the date range
                    nstparse.get_games_list(
                        team_abbr=team,
                        season_folder=f"{current_season_code[:4]}-{current_season_code[6:]}",
                        session=session,
                        fromseason=current_season_code,
                        thruseason=current_season_code,
                        stype=2  # Regular season
                    )
                except Exception as e:
                    logger.warning(f"Could not update games_list for {team}: {e}")

        games_to_actually_scrape = []
        for game in games_to_scrape:
            if game.game_id not in downloaded_game_ids or force_reimport:
                games_to_actually_scrape.append(game)

        if not games_to_actually_scrape:
            logger.info("No games need active scraping.")
        else:
            logger.info(f"Initiating scrape for {len(games_to_actually_scrape)} games.")

        for game in games_to_actually_scrape:
            # Skip scraping if already downloaded (unless forced)
            if game.game_id in downloaded_game_ids and not force_reimport:
                logger.info(f"Skipping scraping for {game.game_id} - already downloaded")
                continue

            # Prepare season strings
            if game.season:
                season_folder = game.season  # "2025-26"
                # Convert "2025-26" -> "20252026" for URL
                if len(game.season) == 7 and '-' in game.season:
                    start_year = game.season[:4]
                    end_year = "20" + game.season[5:]
                    season_code_url = f"{start_year}{end_year}"
                else:
                    season_code_url = game.season.replace('-', '')
            else:
                season_folder = f"{current_season_code[:4]}-{current_season_code[6:]}" # "2025-26"
                season_code_url = current_season_code # "20252026"

            # Construct NST URL
            # NST URL format: https://www.naturalstattrick.com/game.php?season={season}&game={game_id_suffix}
            # DB Game ID: 2024020484 -> NST Suffix: 20484 (Game Type + Number)
            if len(game.game_id) == 10:
                nst_game_suffix = game.game_id[5:]
            else:
                logger.warning(f"Invalid Game ID format for NST scraping: {game.game_id}")
                continue
            
            full_report_url = f"https://www.naturalstattrick.com/game.php?season={season_code_url}&game={nst_game_suffix}"
            
            # Construct game dict for scraper
            game_dict = {
                'game_id': nst_game_suffix,
                'title': f"{game.away_team} @ {game.home_team}, {game.game_date}",
                'full_report_url': full_report_url,
                'team1_full': game.home_team_full,
                'team2_full': game.away_team_full,
                'date': game.game_date
            }
            
            logger.info(f"Scraping game: {game_dict['title']} ({full_report_url})")
            
            # Call scraper directly
            try:
                tables_saved = nstparse.scrape_game_report(
                    full_report_url=full_report_url,
                    game=game_dict,
                    team_abbr=game.home_team, # Pass home team as primary, but it scrapes both
                    season_folder=season_folder, # Use "2025-26" format
                    session=session,
                    game_cache=game_cache,
                    use_threading=False # Disable internal threading for better control here
                )
                if tables_saved > 0:
                    scraped_games_count += 1
            except Exception as e:
                logger.error(f"Error scraping game {game.game_id}: {e}")

        # 2. Import Data
        # We run import if we scraped new games OR if there were games missing from DB (even if already on disk)
        if len(games_to_scrape) > 0 or force_reimport:
            logger.info("Importing NST data into database (Targeted Import)...")
            
            # Prepare metadata for targeted import
            games_metadata = []
            for g in games_to_scrape:
                games_metadata.append({
                    'game_id': g.game_id,
                    'home_team': g.home_team,
                    'away_team': g.away_team,
                    'season': g.season,
                    'game_date': g.game_date
                })
            
            db_manager = nhl_db_manager.NHLDatabaseManager(self.db_path)
            with db_manager:
                # Use the new targeted import method
                db_manager.import_specific_games(games_metadata, use_multithreading=True)
        else:
            logger.info("No NST data to import")

        duration = (datetime.now() - start_time).total_seconds()

        return UpdateResult(
            source="NST",
            status=UpdateStatus.COMPLETED,
            games_processed=len(games_to_scrape),
            games_added=0, 
            games_updated=scraped_games_count,
            errors=[],
            duration_seconds=duration
        )

    def update_moneypuck_data(self, games: List[GameToUpdate]) -> UpdateResult:
        """
        Update MoneyPuck data (team, player, shots).
        Downloads latest CSVs and imports them.

        Args:
            games: List of games to update

        Returns:
            UpdateResult object
        """
        start_time = datetime.now()
        logger.info("=" * 70)
        logger.info("UPDATING MONEYPUCK DATA")
        logger.info("=" * 70)

        games_needing_mp = [
            g for g in games
            if not (g.has_mp_team_data and g.has_mp_player_data)
        ]

        if not games_needing_mp:
            logger.info("No games need MoneyPuck data - skipping")
            # We still might want to run it if lookback is small, but let's respect the check
        else:
            logger.info(f"Will update MoneyPuck data for {len(games_needing_mp)} games")

        # 1. Download Data
        output_dir = Path("moneypuck_data")
        output_dir.mkdir(exist_ok=True)
        
        logger.info("Downloading MoneyPuck data...")
        # Get teams list
        teams = moneypuck_downloader.get_teams_from_db(self.db_path)
        
        # --- PLAYER FILTERING LOGIC ---
        # 1. Identify current season start year
        now = datetime.now()
        current_start_year = now.year if now.month >= 8 else now.year - 1
        logger.info(f"Extracting active players for season starting {current_start_year}...")
        
        # 2. Extract current players from NST
        try:
            current_players = playeridextract.extract_players(current_start_year)
            logger.info(f"Found {len(current_players)} active players.")
            
            # 3. Update master player_ids.txt with new players
            master_file = "player_ids.txt"
            if os.path.exists(master_file):
                # Load existing master list
                existing_players = {}
                with open(master_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if ':' in line:
                            parts = line.split(':', 1)
                            name = parts[0].strip()
                            pid = parts[1].strip()
                            existing_players[name] = pid
                
                # Identify new players
                new_players_count = 0
                with open(master_file, 'a', encoding='utf-8') as f:
                    for name, pid in current_players.items():
                        if name not in existing_players:
                            f.write(f"{name}: {pid}\n")
                            new_players_count += 1
                
                if new_players_count > 0:
                    logger.info(f"Added {new_players_count} new players to {master_file}")
            else:
                # Create if doesn't exist
                playeridextract.write_players_to_file(current_players, master_file)
                logger.info(f"Created {master_file} with {len(current_players)} players")

            # 4. Create temp file for current players (to filter download)
            temp_players_file = "current_player_ids_temp.txt"
            # We need to preserve goalie markers from master list if possible, or just download checks
            # But the user asked to save temp file of current players and their ids
            # Let's write the current_players dict to temp file. 
            # Note: extract_players returns {name: id}, no [G] markers. 
            # moneypuck_downloader handles 404s to detect goalies, but it's better if we know.
            # We can cross-reference with master file to get [G] markers for current players.
            
            # Re-read master to get updated goalie statuses
            master_players_map = {} # name -> id string (with [G])
            with open(master_file, 'r', encoding='utf-8') as f:
                 for line in f:
                    if ':' in line:
                        parts = line.split(':', 1)
                        master_players_map[parts[0].strip()] = parts[1].strip()
            
            with open(temp_players_file, 'w', encoding='utf-8') as f:
                for name in current_players:
                    if name in master_players_map:
                        f.write(f"{name}: {master_players_map[name]}\n")
                    else:
                        f.write(f"{name}: {current_players[name]}\n")
            
            # 5. Load players from TEMP file for downloading
            players_dict = moneypuck_downloader.get_players_from_file(temp_players_file)
            
        except Exception as e:
            logger.error(f"Error filtering players: {e}. Falling back to full player list.")
            players_dict = moneypuck_downloader.get_players_from_file("player_ids.txt")
            temp_players_file = None

        # Download (this refreshes the files)
        moneypuck_downloader.download_team_data(teams, output_dir, max_workers=4, skip_existing=False)
        goalie_404s = moneypuck_downloader.download_player_data(players_dict, output_dir, max_workers=8, skip_existing=False)
        
        # Update master file with any newly discovered goalies
        if goalie_404s:
            moneypuck_downloader.update_player_ids_with_goalies("player_ids.txt", goalie_404s)

        # Cleanup temp file
        if temp_players_file and os.path.exists(temp_players_file):
            os.remove(temp_players_file)
            logger.info("Removed temporary player list file.")

        # Download shots data for current season (updated daily)
        # Shots file uses start year of season (e.g., 20252026 season -> shots_2025.zip)
        current_season_code = self._get_current_season_code()
        shots_season_year = int(current_season_code[:4])  # Extract start year (e.g., 20252026 -> 2025)
        logger.info(f"Downloading shots data for {shots_season_year}-{shots_season_year+1} season...")
        moneypuck_downloader.download_shots_data(shots_season_year, output_dir)

        # 2. Import Data
        logger.info("Importing MoneyPuck data...")

        # Set DB path for importers
        mp_import.MoneyPuckTeamImporter.DB_PATH = self.db_path
        mp_import.MoneyPuckPlayerImporter.DB_PATH = self.db_path
        mp_import.MoneyPuckShotsImporter.DB_PATH = self.db_path
        mp_import.MoneyPuckOddsImporter.DB_PATH = self.db_path

        # Prepare game_ids filter
        game_ids_to_import = {g.game_id for g in games_needing_mp}
        logger.info(f"Filtering import for {len(game_ids_to_import)} games")

        # Run imports with filter
        mp_import.MoneyPuckTeamImporter.import_all(game_ids=game_ids_to_import)
        mp_import.MoneyPuckPlayerImporter.import_all(game_ids=game_ids_to_import)

        # Import current season shots data (updates existing data)
        logger.info(f"Importing shots data for {shots_season_year-1}-{shots_season_year} season...")
        mp_import.MoneyPuckShotsImporter.import_all(season=shots_season_year)

        duration = (datetime.now() - start_time).total_seconds()

        return UpdateResult(
            source="MoneyPuck",
            status=UpdateStatus.COMPLETED,
            games_processed=len(games_needing_mp),
            games_added=0,
            games_updated=0,
            errors=[],
            duration_seconds=duration
        )

    def update_odds_data(self, games: List[GameToUpdate]) -> UpdateResult:
        """
        Update betting odds data for specified games.

        Args:
            games: List of games to update

        Returns:
            UpdateResult object
        """
        start_time = datetime.now()
        logger.info("=" * 70)
        logger.info("UPDATING ODDS DATA")
        logger.info("=" * 70)

        games_needing_odds = [g for g in games if not g.has_odds_data]

        if not games_needing_odds:
            logger.info("No games need odds data - skipping")
            return UpdateResult(
                source="Odds",
                status=UpdateStatus.SKIPPED,
                games_processed=0,
                games_added=0,
                games_updated=0,
                errors=[],
                duration_seconds=0
            )

        logger.info(f"Will scrape odds for {len(games_needing_odds)} games")

        # The moneypuck_odds_scraper.py can scrape specific games
        # We would create a list of game_ids and pass to the scraper

        logger.info("Odds scraping would be executed here via moneypuck_odds_scraper.py")
        # Since we don't have the odds scraper hooked up directly yet (it's a separate script usually),
        # we will re-use the MP importer for odds which reads from CSVs if they exist.
        # Assuming moneypuck_downloader or a separate process has fetched odds CSVs.
        
        # Prepare game_ids filter
        game_ids_to_import = {g.game_id for g in games_needing_odds}
        
        mp_import.MoneyPuckOddsImporter.DB_PATH = self.db_path
        mp_import.MoneyPuckOddsImporter.import_all(game_ids=game_ids_to_import)

        duration = (datetime.now() - start_time).total_seconds()

        return UpdateResult(
            source="Odds",
            status=UpdateStatus.COMPLETED,
            games_processed=len(games_needing_odds),
            games_added=0,
            games_updated=0,
            errors=[],
            duration_seconds=duration
        )

    def update_edge_data(self, games: List[GameToUpdate]) -> UpdateResult:
        """
        Update NHL EDGE tracking data for specified games.
        Collects season stats and PBP, then imports.

        Args:
            games: List of games to update

        Returns:
            UpdateResult object
        """
        start_time = datetime.now()
        logger.info("=" * 70)
        logger.info("UPDATING EDGE DATA")
        logger.info("=" * 70)
        
        # 1. Collect Data
        collector = collect_edge_stats.EdgeStatsCollector(
            output_dir="EdgeStats",
            db_path=self.db_path
        )
        
        # Determine current season code dynamically
        current_season_code = self._get_current_season_code()
        
        # Collect Team Stats (Current Season)
        # logger.info(f"Collecting Team EDGE Stats for season {current_season_code}...")
        # collector.collect_all_teams_edge_stats(season=current_season_code, max_workers=4)
        
        # Collect Player Stats (Current Season)
        # logger.info(f"Collecting Player EDGE Stats for season {current_season_code}...")
        # collector.collect_all_players(season=current_season_code, max_workers=8)
        
        # Collect PBP for specific games
        if games:
            logger.info(f"Collecting PBP for {len(games)} games...")
            for game in games:
                # Need valid team abbreviations for filename
                # (The DB might have empty strings if game data incomplete, but GameToUpdate usually populated)
                if game.home_team and game.away_team:
                    collector.collect_game_pbp(game.game_id, game.away_team, game.home_team, game.game_date)
        
        # 2. Import Data
        logger.info("Importing EDGE data (PBP only for selected games)...")
        importer = nhl_edge_importer.NHLEdgeImporter(db_path=self.db_path)
        importer.connect()
        try:
            edge_dir = Path("EdgeStats")
            
            # Import only the PBP files for the games we just processed
            # PBP Filename format: pbp_{game_id}_{away}_at_{home}_{date}.json
            # We can glob for pbp_{game_id}_*.json
            
            pbp_files_to_import = []
            for game in games:
                pbp_matches = list((edge_dir / "pbp").glob(f"pbp_{game.game_id}_*.json"))
                pbp_files_to_import.extend(pbp_matches)
            
            if pbp_files_to_import:
                logger.info(f"Found {len(pbp_files_to_import)} PBP files to import")
                importer.import_pbp_files(pbp_files_to_import, max_workers=4)
                
            # Skaters/Goalies - skip bulk import for now as we didn't collect them
            # skater_files = list((edge_dir / "skaters").glob("*.json"))
            # if skater_files:
            #    importer._import_player_json_files(skater_files, is_goalie=False, max_workers=4)
                
            # Goalies
            # goalie_files = list((edge_dir / "goalies").glob("*.json"))
            # if goalie_files:
            #    importer._import_player_json_files(goalie_files, is_goalie=True, max_workers=4)
                
        finally:
            importer.close()

        duration = (datetime.now() - start_time).total_seconds()

        return UpdateResult(
            source="EDGE",
            status=UpdateStatus.COMPLETED,
            games_processed=len(games),
            games_added=0,
            games_updated=0,
            errors=[],
            duration_seconds=duration
        )

    # =========================================================================
    # Main Orchestration
    # =========================================================================

    def run_update(self,
                   update_nst: bool = True,
                   update_moneypuck: bool = True,
                   update_odds: bool = True,
                   update_edge: bool = False,
                   force_reimport_nst: bool = False) -> Dict:
        """
        Run the complete update process.

        Args:
            update_nst: Whether to update NST data
            update_moneypuck: Whether to update MoneyPuck data
            update_odds: Whether to update odds data
            update_edge: Whether to update EDGE data
            force_reimport_nst: Force re-scraping of NST data

        Returns:
            Dictionary with update summary
        """
        start_time = datetime.now()
        logger.info("╔" + "═" * 68 + "╗")
        logger.info("║" + " NHL DATABASE UPDATE ORCHESTRATOR ".center(68) + "║")
        logger.info("╠" + "═" * 68 + "╣")
        logger.info(f"║ Start time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}".ljust(69) + "║")
        logger.info(f"║ Lookback: {self.lookback_days} days".ljust(69) + "║")
        logger.info("╚" + "═" * 68 + "╝")

        try:
            # Connect to database
            self.connect()

            # Step 1: Identify games needing updates
            games_to_update = self.identify_games_to_update()

            if not games_to_update:
                logger.info("\n✓ Database is up to date - no games need updating")
                self.disconnect()
                return {
                    'status': 'success',
                    'games_checked': 0,
                    'games_updated': 0,
                    'duration_seconds': (datetime.now() - start_time).total_seconds()
                }

            # Step 2: Update each data source
            if update_nst:
                result = self.update_nst_data(games_to_update, force_reimport_nst)
                self.results.append(result)

            if update_moneypuck:
                result = self.update_moneypuck_data(games_to_update)
                self.results.append(result)

            if update_odds:
                result = self.update_odds_data(games_to_update)
                self.results.append(result)

            if update_edge:
                result = self.update_edge_data(games_to_update)
                self.results.append(result)

            # Step 3: Generate summary
            duration = (datetime.now() - start_time).total_seconds()
            self._print_summary(games_to_update, duration)

            self.disconnect()

            return {
                'status': 'success',
                'games_checked': len(games_to_update),
                'games_updated': len(games_to_update),
                'results': self.results,
                'duration_seconds': duration
            }

        except Exception as e:
            logger.error(f"Update failed: {e}", exc_info=True)
            self.disconnect()
            return {
                'status': 'failed',
                'error': str(e),
                'duration_seconds': (datetime.now() - start_time).total_seconds()
            }

    def _print_summary(self, games: List[GameToUpdate], duration: float):
        """Print a summary of the update operation"""
        logger.info("\n" + "=" * 70)
        logger.info("UPDATE SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Total games checked: {len(games)}")
        logger.info(f"Total duration: {duration:.1f} seconds")
        logger.info("")

        for result in self.results:
            logger.info(f"{result.source}:")
            logger.info(f"  Status: {result.status.value}")
            logger.info(f"  Games processed: {result.games_processed}")
            logger.info(f"  Duration: {result.duration_seconds:.1f}s")
            if result.errors:
                logger.info(f"  Errors: {len(result.errors)}")
                for error in result.errors[:3]:  # Show first 3 errors
                    logger.info(f"    - {error}")
            logger.info("")

        logger.info("=" * 70)


def main():
    """Main entry point for command-line usage"""
    import argparse

    parser = argparse.ArgumentParser(
        description='NHL Database Update Orchestrator - Keep your database current'
    )
    parser.add_argument(
        '--db',
        default='nhl_analytics.db',
        help='Path to database (default: nhl_analytics.db)'
    )
    parser.add_argument(
        '--lookback',
        type=int,
        default=7,
        help='Days to look back for updates (default: 7)'
    )
    parser.add_argument(
        '--skip-nst',
        action='store_true',
        help='Skip NST data updates'
    )
    parser.add_argument(
        '--skip-moneypuck',
        action='store_true',
        help='Skip MoneyPuck data updates'
    )
    parser.add_argument(
        '--skip-odds',
        action='store_true',
        help='Skip odds data updates'
    )
    parser.add_argument(
        '--force-nst',
        action='store_true',
        help='Force re-import of NST data (even if already exists)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be updated without actually updating'
    )

    args = parser.parse_args()

    # Create orchestrator
    orchestrator = NHLUpdateOrchestrator(
        db_path=args.db,
        lookback_days=args.lookback
    )

    # Run update
    if args.dry_run:
        logger.info("DRY RUN MODE - No actual updates will be performed")
        orchestrator.connect()
        games = orchestrator.identify_games_to_update()
        orchestrator.disconnect()
        logger.info(f"\nWould update {len(games)} games")
    else:
        result = orchestrator.run_update(
            update_nst=not args.skip_nst,
            update_moneypuck=not args.skip_moneypuck,
            update_odds=not args.skip_odds,
            update_edge=True,  # Enabled now
            force_reimport_nst=args.force_nst
        )

        if result['status'] == 'success':
            logger.info("\n✓ Update completed successfully")
            sys.exit(0)
        else:
            logger.error(f"\n✗ Update failed: {result.get('error', 'Unknown error')}")
            sys.exit(1)


if __name__ == '__main__':
    main()
