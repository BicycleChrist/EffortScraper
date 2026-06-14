#!/usr/bin/env python3
"""
Download MoneyPuck game-by-game data for teams and players
"""

import requests
import sqlite3
import time
import csv
import os
import shutil
import pandas as pd
import zipfile
from pathlib import Path
from typing import List, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from collections import deque

# MoneyPuck URL templates
# {season_type} is either "regular" or "playoffs" (MoneyPuck hosts both under gameByGame/)
MP_TEAM_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/{season_type}/teams/{team}.csv"
MP_SKATER_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/{season_type}/skaters/{player_id}.csv"
MP_GOALIE_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/{season_type}/goalies/{player_id}.csv"


def season_subdir(output_dir: Path, season_type: str, leaf: str) -> Path:
    """Resolve the output directory for a given data leaf and season type.

    Regular-season data keeps its historical location (output_dir/<leaf>) so the
    importer is unaffected; playoff data goes to a parallel output_dir/playoffs/<leaf>.
    """
    base = output_dir if season_type == "regular" else output_dir / season_type
    return base / leaf

# Headers to mimic a browser request (required to avoid 403 Forbidden)
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
}

# Output directory
OUTPUT_DIR = Path("moneypuck_data")


class RateLimiter:
    """Global rate limiter to control request frequency across all threads"""

    def __init__(self, requests_per_second: float = 5.0):
        """
        Initialize rate limiter

        Args:
            requests_per_second: Maximum number of requests per second (default: 5.0)
        """
        self.min_interval = 1.0 / requests_per_second  # Minimum time between requests
        self.last_request_time = 0
        self.lock = threading.Lock()

    def wait(self):
        """Wait if necessary to respect rate limit"""
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time

            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)

            self.last_request_time = time.time()


# Global rate limiter instance
# Conservative 5 req/sec default to avoid overwhelming MoneyPuck servers
# If you still experience rate limiting, reduce to 3.0 or lower
# If no issues occur, you can cautiously increase to 8.0-10.0
_rate_limiter = RateLimiter(requests_per_second=5.0)


def download_file(url: str, output_path: Path, description: str = "", max_retries: int = 3, skip_existing: bool = False) -> tuple[bool, int]:
    """Download a file from URL to output path with retry logic and caching

    Args:
        url: URL to download from
        output_path: Path to save the file
        description: Description for logging
        max_retries: Maximum number of retry attempts (default: 3)
        skip_existing: Skip download if file already exists (default: False - always refresh)

    Returns:
        Tuple of (success: bool, status_code: int)
    """

    # Check if file already exists (caching)
    if skip_existing and output_path.exists() and output_path.stat().st_size > 100:
        print(f"⊙ Cached: {description or output_path.name}")
        return True, 200

    for attempt in range(max_retries):
        try:
            # Apply global rate limiting before making request
            _rate_limiter.wait()

            response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)

            # Handle rate limiting (429) or server errors (5xx)
            if response.status_code == 429:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                print(f"⚠ Rate limited for {description or output_path.name}, waiting {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue

            if response.status_code >= 500:
                wait_time = 2 ** attempt
                print(f"⚠ Server error for {description or output_path.name}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue

            # Return 404 immediately without retry
            if response.status_code == 404:
                return False, 404

            response.raise_for_status()

            # Check if we got valid CSV data
            if response.status_code == 200 and len(response.content) > 100:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(response.content)
                print(f"✓ Downloaded: {description or output_path.name}")
                return True, 200
            else:
                print(f"✗ Empty/invalid response for: {description or output_path.name}")
                return False, response.status_code

        except requests.exceptions.Timeout:
            wait_time = 2 ** attempt
            print(f"⚠ Timeout for {description or output_path.name}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(wait_time)

        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"⚠ Error downloading {description or output_path.name}: {e}, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"✗ Failed after {max_retries} attempts: {description or output_path.name}: {e}")
                return False, 0

    print(f"✗ Failed after {max_retries} attempts: {description or output_path.name}")
    return False, 0


def concatenate_split_team_files(team_dir: Path, team: str, mp_team_old: str, mp_team_new: str):
    """Concatenate old and new team CSV files into one unified file

    Args:
        team_dir: Directory containing team CSV files
        team: Standard team abbreviation (e.g., 'SJ')
        mp_team_old: Old MoneyPuck abbreviation without period (e.g., 'SJS')
        mp_team_new: New MoneyPuck abbreviation with period (e.g., 'S.J')
    """
    old_file = team_dir / f"{mp_team_old}_temp.csv"
    new_file = team_dir / f"{mp_team_new}_temp.csv"
    output_file = team_dir / f"{mp_team_new}.csv"

    # Check if both files exist
    if not old_file.exists() or not new_file.exists():
        print(f"⚠ Cannot concatenate {team}: Missing old or new file")
        return False

    try:
        # Read both CSV files
        df_old = pd.read_csv(old_file)  # Larger file: 2008-2020
        df_new = pd.read_csv(new_file)  # Smaller file: 2020-current

        # Append new data to old data (new data goes at the bottom)
        df_combined = pd.concat([df_old, df_new], ignore_index=True)

        # Sort by season and game date if those columns exist
        if 'season' in df_combined.columns:
            df_combined = df_combined.sort_values('season')

        # Save combined file with the NEW abbreviation (with period)
        df_combined.to_csv(output_file, index=False)

        # Remove temporary files
        old_file.unlink()
        new_file.unlink()

        print(f"✓ Concatenated {team}: {len(df_old)} + {len(df_new)} = {len(df_combined)} rows → {mp_team_new}.csv")
        return True

    except Exception as e:
        print(f"✗ Error concatenating {team} files: {e}")
        return False


def get_teams_from_db(db_path: str = "nhl_analytics.db") -> List[str]:
    """Get all team abbreviations from database"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT team_abbr FROM teams ORDER BY team_abbr")
    teams = [row[0] for row in cursor.fetchall()]

    conn.close()
    return teams


def get_players_from_file(file_path: str = "player_ids.txt") -> dict:
    """Get all players from player_ids.txt file

    File format: "Player Name: player_id [G]"
    - [G] suffix indicates a goalie (optional)
    - Players without [G] are treated as skaters

    Returns:
        dict with 'goalies' and 'skaters' keys, each containing list of (player_id, name) tuples
    """

    goalies = []
    skaters = []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or ':' not in line:
                    continue

                # Parse "Player Name: player_id [G]" format
                parts = line.split(':', 1)
                if len(parts) == 2:
                    name = parts[0].strip()
                    rest = parts[1].strip()

                    # Check for position indicator
                    is_goalie = rest.endswith('[G]')

                    # Extract player_id (remove position indicator if present)
                    if is_goalie:
                        player_id = rest.replace('[G]', '').strip()
                        goalies.append((player_id, name))
                    else:
                        player_id = rest
                        skaters.append((player_id, name))

    except FileNotFoundError:
        print(f"Warning: {file_path} not found. Please run playeridextract.py first.")
        return {'goalies': [], 'skaters': []}

    return {
        'goalies': goalies,
        'skaters': skaters
    }


def download_team_data(teams: List[str], output_dir: Path, max_workers: int = None, skip_existing: bool = False, season_type: str = "regular"):
    """Download MoneyPuck team data for all teams using threading"""

    print(f"\n{'='*60}")
    print(f"Downloading Team Data [{season_type}] ({len(teams)} teams, {max_workers} threads)")
    print(f"{'='*60}\n")

    team_dir = season_subdir(output_dir, season_type, "teams")
    team_dir.mkdir(parents=True, exist_ok=True)

    # MoneyPuck changed abbreviations mid-way for these teams
    # Map: team -> (old_abbr_for_historical_data, new_abbr_with_period)
    team_abbr_map = {
        'LA': ('LAK', 'L.A'),    # Old: LAK (2008-2020), New: L.A (2020-current)
        'NJ': ('NJD', 'N.J'),    # Old: NJD (2008-2020), New: N.J (2020-current)
        'SJ': ('SJS', 'S.J'),    # Old: SJS (2008-2020), New: S.J (2020-current)
        'TB': ('TBL', 'T.B')     # Old: TBL (2008-2020), New: T.B (2020-current)
    }

    success_count = 0
    failed_count = 0
    concat_teams = []  # Teams that need concatenation
    lock = threading.Lock()

    def download_team(team: str) -> Tuple[bool, str]:
        """Download a single team's data (handles split files for LA, SJ, TB, NJ)"""

        # Check if this team has split files
        if team in team_abbr_map:
            old_abbr, new_abbr = team_abbr_map[team]

            # Download OLD abbreviation file (larger file: 2008-2020)
            url_old = MP_TEAM_URL.format(season_type=season_type, team=old_abbr)
            output_path_old = team_dir / f"{old_abbr}_temp.csv"
            result_old, _ = download_file(url_old, output_path_old, f"{team} (historical: {old_abbr})", skip_existing=skip_existing)

            # Download NEW abbreviation file (smaller file: 2020-current)
            url_new = MP_TEAM_URL.format(season_type=season_type, team=new_abbr)
            output_path_new = team_dir / f"{new_abbr}_temp.csv"
            result_new, _ = download_file(url_new, output_path_new, f"{team} (recent: {new_abbr})", skip_existing=skip_existing)

            # Both files needed for concatenation
            if result_old and result_new:
                return True, team  # Will concatenate later
            else:
                return False, team
        else:
            # Standard download for teams without split files
            url = MP_TEAM_URL.format(season_type=season_type, team=team)
            output_path = team_dir / f"{team}.csv"
            result, _ = download_file(url, output_path, f"{team} team data", skip_existing=skip_existing)
            return result, team

    # Use ThreadPoolExecutor for parallel downloads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_team, team): team for team in teams}

        for future in as_completed(futures):
            success, team = future.result()
            with lock:
                if success:
                    success_count += 1
                    # Mark teams that need concatenation
                    if team in team_abbr_map:
                        concat_teams.append(team)
                else:
                    failed_count += 1

    print(f"\nTeam Data: {success_count} succeeded, {failed_count} failed")

    # Concatenate split files
    if concat_teams:
        print(f"\n{'='*60}")
        print(f"Concatenating Split Team Files ({len(concat_teams)} teams)")
        print(f"{'='*60}\n")

        concat_success = 0
        for team in concat_teams:
            old_abbr, new_abbr = team_abbr_map[team]
            if concatenate_split_team_files(team_dir, team, old_abbr, new_abbr):
                concat_success += 1

        print(f"\nConcatenation: {concat_success}/{len(concat_teams)} successful")


def download_player_data(players: dict, output_dir: Path, limit: int = None, max_workers: int = None, skip_existing: bool = False, season_type: str = "regular") -> Set[str]:
    """Download MoneyPuck player data using threading

    Returns:
        Set of player IDs that 404'd on skater endpoint (assumed to be goalies)
    """

    skaters = players['skaters'][:limit] if limit else players['skaters']
    goalies = players['goalies'][:limit] if limit else players['goalies']

    # Download skaters
    print(f"\n{'='*60}")
    print(f"Downloading Skater Data [{season_type}] ({len(skaters)} players, {max_workers} threads)")
    print(f"{'='*60}\n")

    skater_dir = season_subdir(output_dir, season_type, "skaters")
    skater_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0
    not_found_players = set()  # Track 404'd player IDs
    lock = threading.Lock()

    def download_skater(player_data: Tuple[str, str]) -> Tuple[bool, str, int]:
        """Download a single skater's data"""
        player_id, name = player_data
        url = MP_SKATER_URL.format(season_type=season_type, player_id=player_id)
        output_path = skater_dir / f"{player_id}.csv"

        result, status_code = download_file(url, output_path, f"{name} ({player_id})", skip_existing=skip_existing)
        return result, player_id, status_code

    # First pass: Download with normal concurrency
    failed_skaters = []  # Track failed downloads for retry
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_skater, player): player for player in skaters}

        completed = 0
        for future in as_completed(futures):
            success, player_id, status_code = future.result()
            with lock:
                completed += 1
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                    # Track 404s - these are likely goalies
                    if status_code == 404:
                        not_found_players.add(player_id)
                    else:
                        # Non-404 failures go to retry queue (rate limiting, timeouts, etc.)
                        failed_skaters.append(futures[future])

                # Show progress every 50 downloads
                if completed % 50 == 0:
                    print(f"Progress: {completed}/{len(skaters)} skaters processed...")

    # Retry failed downloads with reduced concurrency (single-threaded)
    if failed_skaters:
        print(f"\n⟳ Retrying {len(failed_skaters)} failed downloads with reduced concurrency...")
        retry_success = 0
        retry_failed = 0

        for player_data in failed_skaters:
            success, player_id, status_code = download_skater(player_data)
            if success:
                retry_success += 1
                success_count += 1
                failed_count -= 1
            else:
                retry_failed += 1
                if status_code == 404:
                    not_found_players.add(player_id)

        print(f"⟳ Retry results: {retry_success} succeeded, {retry_failed} still failed")

    print(f"\nSkater Data: {success_count} succeeded, {failed_count} failed")
    if not_found_players:
        print(f"  → {len(not_found_players)} players not found (likely goalies)")

    # Download goalies
    print(f"\n{'='*60}")
    print(f"Downloading Goalie Data [{season_type}] ({len(goalies)} goalies, {max_workers} threads)")
    print(f"{'='*60}\n")

    goalie_dir = season_subdir(output_dir, season_type, "goalies")
    goalie_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0

    def download_goalie(player_data: Tuple[str, str]) -> Tuple[bool, str, str]:
        """Download a single goalie's data"""
        player_id, name = player_data
        url = MP_GOALIE_URL.format(season_type=season_type, player_id=player_id)
        output_path = goalie_dir / f"{player_id}.csv"

        result, status_code = download_file(url, output_path, f"{name} ({player_id})", skip_existing=skip_existing)
        return result, player_id, name

    # First pass: Download with normal concurrency
    failed_goalies = []  # Track failed downloads for retry
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_goalie, player): player for player in goalies}

        completed = 0
        for future in as_completed(futures):
            success, player_id, name = future.result()
            with lock:
                completed += 1
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                    # Add to retry queue (non-404 failures)
                    failed_goalies.append(futures[future])

                # Show progress every 10 downloads (fewer goalies typically)
                if completed % 10 == 0:
                    print(f"Progress: {completed}/{len(goalies)} goalies processed...")

    # Retry failed downloads with reduced concurrency (single-threaded)
    if failed_goalies:
        print(f"\n⟳ Retrying {len(failed_goalies)} failed goalie downloads with reduced concurrency...")
        retry_success = 0
        retry_failed = 0

        for player_data in failed_goalies:
            success, player_id, name = download_goalie(player_data)
            if success:
                retry_success += 1
                success_count += 1
                failed_count -= 1
            else:
                retry_failed += 1

        print(f"⟳ Retry results: {retry_success} succeeded, {retry_failed} still failed")

    print(f"\nGoalie Data: {success_count} succeeded, {failed_count} failed")

    return not_found_players


def download_shots_data(season: int, output_dir: Path) -> bool:
    """Download and extract MoneyPuck shots data for a specific season

    Args:
        season: Season year (e.g., 2025 for 2025-26 season)
        output_dir: Directory to save the extracted CSV

    Returns:
        bool: True if successful, False otherwise
    """
    print(f"\n{'='*60}")
    print(f"Downloading Shots Data for {season}-{season+1} season")
    print(f"{'='*60}\n")

    output_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://peter-tanner.com/moneypuck/downloads/shots_{season}.zip"
    zip_path = output_dir / f"shots_{season}.zip"

    try:
        # Download the ZIP file
        success, _ = download_file(url, zip_path, f"shots_{season}.zip")
        if not success:
            print(f"✗ Failed to download shots data for {season}")
            return False

        # Extract the CSV directly to output_dir (overwrites existing file)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(output_dir)

        # Remove the ZIP file after extraction
        zip_path.unlink()

        print(f"✓ Extracted shots data to {output_dir}")
        return True

    except Exception as e:
        print(f"✗ Error processing shots data: {e}")
        return False


def update_player_ids_with_goalies(player_ids_file: str, goalie_ids: Set[str]) -> int:
    """Update player_ids.txt to mark goalies with [G] indicator

    Args:
        player_ids_file: Path to player_ids.txt
        goalie_ids: Set of player IDs that are goalies

    Returns:
        Number of players marked as goalies
    """
    if not goalie_ids:
        return 0

    # Create backup
    backup_file = f"{player_ids_file}.backup"
    shutil.copy(player_ids_file, backup_file)
    print(f"\n{'='*60}")
    print(f"Updating {player_ids_file} with goalie markers")
    print(f"Backup created: {backup_file}")
    print(f"{'='*60}\n")

    # Read current file
    updated_lines = []
    goalies_marked = 0

    with open(player_ids_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or ':' not in line:
                continue

            # Parse "Player Name: player_id" or "Player Name: player_id [G]"
            parts = line.split(':', 1)
            if len(parts) != 2:
                continue

            name = parts[0].strip()
            rest = parts[1].strip()

            # Skip if already marked as goalie
            if rest.endswith('[G]'):
                updated_lines.append(line)
                continue

            player_id = rest.strip()

            # Mark as goalie if in the 404 list
            if player_id in goalie_ids:
                updated_lines.append(f"{name}: {player_id} [G]")
                goalies_marked += 1
            else:
                updated_lines.append(line)

    # Write updated file
    with open(player_ids_file, 'w', encoding='utf-8') as f:
        for line in updated_lines:
            f.write(line + '\n')

    print(f"✓ Marked {goalies_marked} new players as goalies")
    return goalies_marked


def main():
    """Main entry point"""
    import argparse

    # Get CPU count for default thread count
    # Use conservative defaults to avoid rate limiting: 3 for players, 4 for teams
    cpu_count = os.cpu_count() or 1
    default_player_threads = min(3, cpu_count)  # Conservative: max 3 threads for players
    default_team_threads = min(4, cpu_count)    # Teams are less restrictive

    parser = argparse.ArgumentParser(description='Download MoneyPuck game-by-game data')
    parser.add_argument('--teams-only', action='store_true', help='Download only team data')
    parser.add_argument('--players-only', action='store_true', help='Download only player data')
    parser.add_argument('--limit', type=int, help='Limit number of players to download (for testing)')
    parser.add_argument('--output', type=str, default='moneypuck_data', help='Output directory')
    parser.add_argument('--db', type=str, default='nhl_analytics.db', help='Database path')
    parser.add_argument('--threads', type=int, default=default_player_threads, help=f'Number of concurrent download threads for players (default: {default_player_threads}, reduced to avoid rate limiting)')
    parser.add_argument('--team-threads', type=int, default=default_team_threads, help=f'Number of threads for team downloads (default: {default_team_threads})')
    parser.add_argument('--use-cache', action='store_true', help='Skip downloading files that already exist (use for testing only)')
    parser.add_argument('--season-type', choices=['regular', 'playoffs', 'both'], default='regular',
                        help="Which season type(s) to download. Playoff data goes to <output>/playoffs/ (default: regular)")

    args = parser.parse_args()

    # By default, always refresh files. Only skip if --use-cache is explicitly set
    skip_existing = args.use_cache

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    season_types = ['regular', 'playoffs'] if args.season_type == 'both' else [args.season_type]

    print(f"\nConfiguration:")
    print(f"  Threads (players): {args.threads}")
    print(f"  Threads (teams): {args.team_threads}")
    print(f"  Refresh mode: {'CACHE (skip existing)' if skip_existing else 'UPDATE (redownload all)'}")
    print(f"  Season type(s): {', '.join(season_types)}")
    print(f"  Database: {args.db}")
    print(f"  Output: {output_dir}")

    for season_type in season_types:
        # Download teams
        if not args.players_only:
            teams = get_teams_from_db(args.db)
            download_team_data(teams, output_dir, max_workers=args.team_threads, skip_existing=skip_existing, season_type=season_type)

        # Download players
        if not args.teams_only:
            player_ids_file = "player_ids.txt"
            players = get_players_from_file(player_ids_file)
            goalie_404s = download_player_data(players, output_dir, limit=args.limit, max_workers=args.threads, skip_existing=skip_existing, season_type=season_type)

            # Update player_ids.txt with newly identified goalies (regular season is the canonical roster source)
            if goalie_404s and season_type == 'regular':
                update_player_ids_with_goalies(player_ids_file, goalie_404s)

    print(f"\n{'='*60}")
    print(f"Download complete! Data saved to: {output_dir}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
