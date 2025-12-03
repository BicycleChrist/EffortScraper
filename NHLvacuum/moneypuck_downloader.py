#!/usr/bin/env python3
"""
Download MoneyPuck game-by-game data for teams and players
"""

import requests
import sqlite3
import time
import csv
import os
from pathlib import Path
from typing import List, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# MoneyPuck URL templates
MP_TEAM_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/regular/teams/{team}.csv"
MP_SKATER_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/regular/skaters/{player_id}.csv"
MP_GOALIE_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/regular/goalies/{player_id}.csv"

# Output directory
OUTPUT_DIR = Path("moneypuck_data")


def download_file(url: str, output_path: Path, description: str = "", max_retries: int = 3, skip_existing: bool = False) -> bool:
    """Download a file from URL to output path with retry logic and caching

    Args:
        url: URL to download from
        output_path: Path to save the file
        description: Description for logging
        max_retries: Maximum number of retry attempts (default: 3)
        skip_existing: Skip download if file already exists (default: False - always refresh)

    Returns:
        True if download succeeded or file was cached, False otherwise
    """

    # Check if file already exists (caching)
    if skip_existing and output_path.exists() and output_path.stat().st_size > 100:
        print(f"⊙ Cached: {description or output_path.name}")
        return True

    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=30)

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

            response.raise_for_status()

            # Check if we got valid CSV data
            if response.status_code == 200 and len(response.content) > 100:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(response.content)
                print(f"✓ Downloaded: {description or output_path.name}")
                return True
            else:
                print(f"✗ Empty/invalid response for: {description or output_path.name}")
                return False

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
                return False

    print(f"✗ Failed after {max_retries} attempts: {description or output_path.name}")
    return False


def get_teams_from_db(db_path: str = "nhl_analytics.db") -> List[str]:
    """Get all team abbreviations from database"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT team_abbr FROM teams ORDER BY team_abbr")
    teams = [row[0] for row in cursor.fetchall()]

    conn.close()
    return teams


def get_players_from_db(db_path: str = "nhl_analytics.db") -> dict:
    """Get all players from database, separated by position"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all players except unknowns
    cursor.execute("""
        SELECT DISTINCT player_id, player_name, position
        FROM players
        WHERE player_id NOT LIKE 'UNKNOWN%'
        ORDER BY player_name
    """)

    players = cursor.fetchall()
    conn.close()

    # Separate goalies from skaters
    goalies = [(pid, name) for pid, name, pos in players if pos == 'G']
    skaters = [(pid, name) for pid, name, pos in players if pos != 'G']

    return {
        'goalies': goalies,
        'skaters': skaters
    }


def download_team_data(teams: List[str], output_dir: Path, max_workers: int = None, skip_existing: bool = False):
    """Download MoneyPuck team data for all teams using threading"""

    print(f"\n{'='*60}")
    print(f"Downloading Team Data ({len(teams)} teams, {max_workers} threads)")
    print(f"{'='*60}\n")

    team_dir = output_dir / "teams"
    team_dir.mkdir(parents=True, exist_ok=True)

    # MoneyPuck uses periods for some teams
    team_abbr_map = {
        'LA': 'L.A',
        'NJ': 'N.J',
        'SJ': 'S.J',
        'TB': 'T.B'
    }

    success_count = 0
    failed_count = 0
    lock = threading.Lock()

    def download_team(team: str) -> Tuple[bool, str]:
        """Download a single team's data"""
        mp_team = team_abbr_map.get(team, team)
        url = MP_TEAM_URL.format(team=mp_team)
        output_path = team_dir / f"{team}.csv"

        result = download_file(url, output_path, f"{team} team data", skip_existing=skip_existing)
        time.sleep(0.1)  # Small delay to avoid overwhelming server
        return result, team

    # Use ThreadPoolExecutor for parallel downloads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_team, team): team for team in teams}

        for future in as_completed(futures):
            success, team = future.result()
            with lock:
                if success:
                    success_count += 1
                else:
                    failed_count += 1

    print(f"\nTeam Data: {success_count} succeeded, {failed_count} failed")


def download_player_data(players: dict, output_dir: Path, limit: int = None, max_workers: int = None, skip_existing: bool = False):
    """Download MoneyPuck player data using threading"""

    skaters = players['skaters'][:limit] if limit else players['skaters']
    goalies = players['goalies'][:limit] if limit else players['goalies']

    # Download skaters
    print(f"\n{'='*60}")
    print(f"Downloading Skater Data ({len(skaters)} players, {max_workers} threads)")
    print(f"{'='*60}\n")

    skater_dir = output_dir / "skaters"
    skater_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0
    lock = threading.Lock()

    def download_skater(player_data: Tuple[str, str]) -> Tuple[bool, str]:
        """Download a single skater's data"""
        player_id, name = player_data
        url = MP_SKATER_URL.format(player_id=player_id)
        output_path = skater_dir / f"{player_id}.csv"

        result = download_file(url, output_path, f"{name} ({player_id})", skip_existing=skip_existing)
        time.sleep(0.05)  # Small delay to avoid overwhelming server
        return result, name

    # Use ThreadPoolExecutor for parallel downloads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_skater, player): player for player in skaters}

        completed = 0
        for future in as_completed(futures):
            success, name = future.result()
            with lock:
                completed += 1
                if success:
                    success_count += 1
                else:
                    failed_count += 1

                # Show progress every 50 downloads
                if completed % 50 == 0:
                    print(f"Progress: {completed}/{len(skaters)} skaters processed...")

    print(f"\nSkater Data: {success_count} succeeded, {failed_count} failed")

    # Download goalies
    print(f"\n{'='*60}")
    print(f"Downloading Goalie Data ({len(goalies)} goalies, {max_workers} threads)")
    print(f"{'='*60}\n")

    goalie_dir = output_dir / "goalies"
    goalie_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0

    def download_goalie(player_data: Tuple[str, str]) -> Tuple[bool, str]:
        """Download a single goalie's data"""
        player_id, name = player_data
        url = MP_GOALIE_URL.format(player_id=player_id)
        output_path = goalie_dir / f"{player_id}.csv"

        result = download_file(url, output_path, f"{name} ({player_id})", skip_existing=skip_existing)
        time.sleep(0.05)  # Small delay to avoid overwhelming server
        return result, name

    # Use ThreadPoolExecutor for parallel downloads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_goalie, player): player for player in goalies}

        completed = 0
        for future in as_completed(futures):
            success, name = future.result()
            with lock:
                completed += 1
                if success:
                    success_count += 1
                else:
                    failed_count += 1

                # Show progress every 10 downloads (fewer goalies typically)
                if completed % 10 == 0:
                    print(f"Progress: {completed}/{len(goalies)} goalies processed...")

    print(f"\nGoalie Data: {success_count} succeeded, {failed_count} failed")


def main():
    """Main entry point"""
    import argparse

    # Get CPU count for default thread count (cpu_count - 1)
    cpu_count = os.cpu_count() or 1
    default_threads = max(1, cpu_count - 1)

    parser = argparse.ArgumentParser(description='Download MoneyPuck game-by-game data')
    parser.add_argument('--teams-only', action='store_true', help='Download only team data')
    parser.add_argument('--players-only', action='store_true', help='Download only player data')
    parser.add_argument('--limit', type=int, help='Limit number of players to download (for testing)')
    parser.add_argument('--output', type=str, default='moneypuck_data', help='Output directory')
    parser.add_argument('--db', type=str, default='nhl_analytics.db', help='Database path')
    parser.add_argument('--threads', type=int, default=default_threads, help=f'Number of concurrent download threads (default: {default_threads})')
    parser.add_argument('--team-threads', type=int, default=default_threads, help=f'Number of threads for team downloads (default: {default_threads})')
    parser.add_argument('--use-cache', action='store_true', help='Skip downloading files that already exist (use for testing only)')

    args = parser.parse_args()

    # By default, always refresh files. Only skip if --use-cache is explicitly set
    skip_existing = args.use_cache

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nConfiguration:")
    print(f"  Threads (players): {args.threads}")
    print(f"  Threads (teams): {args.team_threads}")
    print(f"  Refresh mode: {'CACHE (skip existing)' if skip_existing else 'UPDATE (redownload all)'}")
    print(f"  Database: {args.db}")
    print(f"  Output: {output_dir}")

    # Download teams
    if not args.players_only:
        teams = get_teams_from_db(args.db)
        download_team_data(teams, output_dir, max_workers=args.team_threads, skip_existing=skip_existing)

    # Download players
    if not args.teams_only:
        players = get_players_from_db(args.db)
        download_player_data(players, output_dir, limit=args.limit, max_workers=args.threads, skip_existing=skip_existing)

    print(f"\n{'='*60}")
    print(f"Download complete! Data saved to: {output_dir}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
