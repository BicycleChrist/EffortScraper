#!/usr/bin/env python3
"""
NST Data Sanitizer

Comprehensive tool for detecting, fixing, and re-scraping corrupted NST game files.
Handles edge cases where NST's HTML is missing team names in section labels.

Problem: Some NST games have section labels like "- Individual" instead of "Bruins - Individual"
Solution: Detect these games, delete corrupted files, and re-scrape with enhanced parsing

Usage:
    # Scan for corrupted files
    python nst_sanitize.py --scan

    # Delete corrupted files and re-scrape
    python nst_sanitize.py --fix

    # Just delete without re-scraping
    python nst_sanitize.py --delete-only
"""

import os
import re
import csv
import time
import random
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


# Import from nstparse for re-scraping
import nstparse


class NSTSanitizer:
    """Sanitize NST data by detecting and fixing corrupted game files"""

    def __init__(self, base_dir='nhlteamreports'):
        self.base_dir = base_dir
        self.corrupted_files = []
        self.games_by_id = defaultdict(lambda: {
            'teams': set(),
            'files': [],
            'game_info': {}
        })
        self.session = None

    def create_session(self):
        """Create requests session with retry logic"""
        if not self.session:
            self.session = nstparse.create_session()
        return self.session

    def find_corrupted_files(self, season='2023-24'):
        """
        Find all files with '-' in team position or only headers (no data)

        Returns:
            List of corrupted file paths
        """
        print(f"\n{'='*80}")
        print(f"SCANNING FOR CORRUPTED FILES IN {season} SEASON")
        print(f"{'='*80}\n")

        pattern = re.compile(r'^([A-Z]+)vs([A-Z]+)_(\d+)_(.+)\.csv$')
        season_path = Path(self.base_dir)

        # Scan all team directories
        for team_dir in season_path.iterdir():
            if not team_dir.is_dir():
                continue

            games_dir = team_dir / 'games' / season
            if not games_dir.exists():
                continue

            team_abbr = team_dir.name

            # Find files
            for csv_file in games_dir.glob('*.csv'):
                # Skip games list files
                if '_games_list.csv' in csv_file.name:
                    continue

                match = pattern.match(csv_file.name)
                if match:
                    team1, team2, game_id, section_info = match.groups()

                    # Check ONLY if section_info starts with "-_" (missing team name)
                    # This is the specific NST bug where team names are missing from HTML
                    is_corrupted = section_info.startswith('-_')

                    if is_corrupted:
                        self.corrupted_files.append(csv_file)

                        # Track game information
                        self.games_by_id[game_id]['teams'].add(team1)
                        self.games_by_id[game_id]['teams'].add(team2)
                        self.games_by_id[game_id]['files'].append(str(csv_file))

        print(f"✓ Found {len(self.corrupted_files)} corrupted files")
        print(f"✓ Affecting {len(self.games_by_id)} unique games")
        print()

        return self.corrupted_files

    def _verify_file_is_corrupted(self, file_path):
        """Check if file has only headers (1-2 lines) or is empty"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Corrupted files have only header line (1 line) or header + empty line (2 lines)
                return len(lines) <= 2
        except Exception as e:
            print(f"  ⚠ Error checking {file_path}: {e}")
            return False

    def load_game_metadata(self):
        """Load metadata for all corrupted games from games_list CSVs"""
        print(f"\n{'='*80}")
        print("LOADING GAME METADATA FROM GAMES LISTS")
        print(f"{'='*80}\n")

        for game_id, game_data in self.games_by_id.items():
            # Try each team that has this game
            for team_abbr in game_data['teams']:
                game_info = self._get_game_info_from_games_list(team_abbr, game_id)
                if game_info:
                    game_data['game_info'] = game_info
                    print(f"  ✓ Game {game_id}: {game_info.get('title', 'Unknown')}")
                    break

            if not game_data['game_info']:
                print(f"  ⚠ Game {game_id}: Could not find metadata")

        print()

    def _get_game_info_from_games_list(self, team_abbr, game_id, season='2023-24'):
        """Load game information from team's games list CSV"""
        games_list_path = Path(self.base_dir) / team_abbr / 'games' / season / f"{team_abbr}_games_list.csv"

        if not games_list_path.exists():
            return None

        try:
            df = pd.read_csv(games_list_path)
            # Convert game_id column to string for comparison
            df['game_id'] = df['game_id'].astype(str)
            game_row = df[df['game_id'] == str(game_id)]

            if not game_row.empty:
                return game_row.iloc[0].to_dict()
        except Exception as e:
            print(f"  ⚠ Error reading games list for {team_abbr}: {e}")

        return None

    def verify_games_missing_team_names(self):
        """
        Verify that corrupted games actually have missing team names in NST HTML

        Returns:
            dict: {game_id: has_missing_team_names (bool)}
        """
        print(f"\n{'='*80}")
        print("VERIFYING NST HTML FOR CORRUPTED GAMES")
        print(f"{'='*80}\n")

        session = self.create_session()
        verification_results = {}

        for game_id, game_data in sorted(self.games_by_id.items()):
            game_info = game_data.get('game_info', {})
            full_report_url = game_info.get('full_report_url', '')

            if not full_report_url:
                print(f"  ⚠ Game {game_id}: No URL found")
                verification_results[game_id] = None
                continue

            try:
                print(f"  🔍 Checking game {game_id}...", end=' ')
                response = session.get(full_report_url, timeout=15)

                if response.status_code != 200:
                    print(f"✗ HTTP {response.status_code}")
                    verification_results[game_id] = None
                    continue

                soup = BeautifulSoup(response.content, 'html.parser')
                labels = soup.find_all('label', class_='section')

                # Check if section labels have team names
                # Team name labels look like: "Bruins - Individual"
                # Missing team name labels look like: "- Individual"
                has_missing_team_names = any(
                    label.get_text(strip=True).startswith('- ')
                    for label in labels
                )

                if has_missing_team_names:
                    print("✓ Confirmed missing team names")
                else:
                    print("✗ Team names present (should not be corrupted)")

                verification_results[game_id] = has_missing_team_names

                # Be respectful
                time.sleep(random.uniform(0.5, 1.5))

            except Exception as e:
                print(f"✗ Error: {e}")
                verification_results[game_id] = None

        print()
        return verification_results

    def delete_corrupted_files(self, dry_run=True):
        """
        Delete all corrupted files

        Args:
            dry_run: If True, only show what would be deleted
        """
        print(f"\n{'='*80}")
        if dry_run:
            print("DRY RUN - FILES THAT WOULD BE DELETED")
        else:
            print("DELETING CORRUPTED FILES")
        print(f"{'='*80}\n")

        deleted_count = 0
        failed_count = 0

        for file_path in self.corrupted_files:
            if dry_run:
                deleted_count += 1
            else:
                try:
                    os.remove(file_path)
                    print(f"  ✓ Deleted: {file_path}")
                    deleted_count += 1
                except Exception as e:
                    print(f"  ✗ Failed to delete {file_path}: {e}")
                    failed_count += 1

        print()
        if dry_run:
            print(f"📊 DRY RUN Summary: Would delete {deleted_count} files")
        else:
            print(f"📊 Deletion Summary: ✓ {deleted_count} deleted, ✗ {failed_count} failed")
        print()

        return deleted_count, failed_count

    def rescrape_corrupted_games(self, delay_min=2, delay_max=5, use_enhanced_parser=True):
        """
        Re-scrape corrupted games with enhanced parsing for missing team names

        Args:
            delay_min: Minimum delay between games (seconds)
            delay_max: Maximum delay between games (seconds)
            use_enhanced_parser: Use enhanced parser that handles missing team names
        """
        print(f"\n{'='*80}")
        print("RE-SCRAPING CORRUPTED GAMES")
        print(f"{'='*80}\n")

        session = self.create_session()
        game_cache = {}

        success_count = 0
        failed_count = 0

        total_games = len(self.games_by_id)

        try:
            for i, (game_id, game_data) in enumerate(sorted(self.games_by_id.items()), 1):
                game_info = game_data.get('game_info', {})
                title = game_info.get('title', 'Unknown')
                full_report_url = game_info.get('full_report_url', '')
                teams = list(game_data['teams'])
                season = game_info.get('season', '20232024')

                # Convert season from YYYYYYYY to YY-YY format
                if len(str(season)) == 8:
                    season_str = str(season)
                    start_year = season_str[:4]
                    end_year = season_str[6:8]
                    season_folder = f"{start_year}-{end_year}"
                else:
                    season_folder = str(season)

                print(f"[{i}/{total_games}] Game {game_id}: {title}")

                if not full_report_url:
                    print(f"  ⚠ Skipping - no URL found")
                    failed_count += 1
                    continue

                # Prepare game data
                game_scrape_info = {
                    'game_id': game_id,
                    'title': title,
                    'full_report_url': full_report_url,
                    'season': season,
                    'date': game_info.get('date', ''),
                    'team1_full': game_info.get('team1_full', ''),
                    'team2_full': game_info.get('team2_full', '')
                }

                # Parse teams from title if missing
                if title and ' - ' in title and not game_scrape_info['team1_full']:
                    _, score_part = title.split(' - ', 1)
                    if ',' in score_part:
                        teams_scores = score_part.split(',')
                        if len(teams_scores) == 2:
                            team1_parts = teams_scores[0].strip().rsplit(' ', 1)
                            team2_parts = teams_scores[1].strip().rsplit(' ', 1)
                            if len(team1_parts) > 1:
                                game_scrape_info['team1_full'] = team1_parts[0]
                            if len(team2_parts) > 1:
                                game_scrape_info['team2_full'] = team2_parts[0]

                # Re-scrape for each team involved
                game_success = True
                for team_abbr in teams:
                    team_abbr = team_abbr.strip()
                    if not team_abbr:
                        continue

                    print(f"  📥 Re-scraping for {team_abbr}...")

                    try:
                        if use_enhanced_parser:
                            # Use enhanced scraper with missing team name handling
                            self._scrape_game_with_enhanced_parser(
                                game_url=full_report_url,
                                game_info=game_scrape_info,
                                team_abbr=team_abbr,
                                season_folder=season_folder,
                                session=session,
                                game_cache=game_cache
                            )
                        else:
                            # Use standard nstparse scraper
                            nstparse.scrape_game_report(
                                game_url=full_report_url,
                                game_info=game_scrape_info,
                                team_abbr=team_abbr,
                                season_folder=season_folder,
                                session=session,
                                game_cache=game_cache,
                                use_threading=False,
                                max_workers=1
                            )

                        print(f"  ✓ Successfully re-scraped for {team_abbr}")

                    except Exception as e:
                        print(f"  ✗ Error re-scraping for {team_abbr}: {e}")
                        game_success = False

                if game_success:
                    success_count += 1
                else:
                    failed_count += 1

                # Respectful delay between games
                if i < total_games:
                    delay = random.uniform(delay_min, delay_max)
                    print(f"  ⏳ Waiting {delay:.1f}s before next game...")
                    time.sleep(delay)
                print()

        except KeyboardInterrupt:
            print("\n\n⚠ Re-scraping interrupted by user")
        except Exception as e:
            print(f"\n\n✗ Unexpected error: {e}")
            import traceback
            traceback.print_exc()

        # Summary
        print(f"\n{'='*80}")
        print("RE-SCRAPING SUMMARY")
        print(f"{'='*80}")
        print(f"✓ Successfully re-scraped: {success_count} games")
        print(f"✗ Failed: {failed_count} games")
        print(f"{'='*80}\n")

    def _scrape_game_with_enhanced_parser(self, game_url, game_info, team_abbr,
                                         season_folder, session, game_cache):
        """
        Enhanced game scraper that handles missing team names in section labels

        Strategy:
        1. Download the game HTML
        2. Parse sections normally
        3. For sections with missing team names (starts with "- "), infer team from context:
           - Use the team_abbr parameter (current team being processed)
           - Check table data for player names and match to team rosters
           - Use game_info to determine both teams
        """
        game_id = game_info.get('game_id', 'unknown')

        # Determine opponent
        team1_full = game_info.get('team1_full', '')
        team2_full = game_info.get('team2_full', '')

        opponent_abbr = None
        if team1_full and team2_full:
            team1_abbr = nstparse.extract_team_abbr_from_name(team1_full, team_abbr)
            team2_abbr = nstparse.extract_team_abbr_from_name(team2_full, team_abbr)

            if team1_abbr == team_abbr:
                opponent_abbr = team2_abbr
            elif team2_abbr == team_abbr:
                opponent_abbr = team1_abbr

        if not opponent_abbr:
            opponent_abbr = 'OPP'

        # Create game file prefix
        teams_sorted = sorted([team_abbr, opponent_abbr])
        game_file_prefix = f"{teams_sorted[0]}vs{teams_sorted[1]}_{game_id}"

        base_folder_path = "nhlteamreports"
        games_folder = "games"
        team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)

        # Download game HTML
        print(f"      📥 Downloading {game_file_prefix}")
        response = session.get(game_url, timeout=30)

        if response.status_code == 403:
            raise Exception("IP banned (403)")
        elif response.status_code == 429:
            raise Exception("Rate limited (429)")

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract tables grouped by section
        sections = nstparse.extract_table_sections(soup)

        if sections:
            os.makedirs(team_games_path, exist_ok=True)
            total_tables = 0

            # Track which team index we're on for alternating sections
            team_index = 0
            both_teams = [team_abbr, opponent_abbr]

            for section_name, section_data in sections.items():
                # Process each situation
                for situation, table_list in section_data['situations'].items():
                    for i, table in enumerate(table_list):
                        # Parse table
                        df = nstparse.parse_table_with_multiheader(table)

                        if df is not None:
                            # Check if this is an Overview section with multiple teams
                            if isinstance(df, dict):
                                # Overview section - save each team separately
                                for team_name, team_df in df.items():
                                    team_name_abbr = nstparse.extract_team_abbr_from_name(team_name, team_abbr)

                                    if not team_name_abbr:
                                        continue

                                    team_specific_games_path = os.path.join(
                                        base_folder_path, team_name_abbr, games_folder, season_folder
                                    )
                                    os.makedirs(team_specific_games_path, exist_ok=True)

                                    file_name = f"{game_file_prefix}_{team_name_abbr}_{section_name.lower()}_{situation}.csv"
                                    file_path = os.path.join(team_specific_games_path, file_name)

                                    team_df.to_csv(file_path, index=False)
                                    total_tables += 1
                            else:
                                # Regular table - determine team
                                section_label = section_data['label']

                                # Try to extract team from section label
                                section_team_abbr = nstparse.extract_team_from_section_label(section_label)

                                # ENHANCED: Handle missing team names
                                if not section_team_abbr:
                                    # Section label doesn't have team name (e.g., "- Individual")
                                    # Infer from context:
                                    # Sections alternate between teams in order
                                    # Use team_index to determine which team this section belongs to
                                    section_team_abbr = both_teams[team_index % 2]
                                    team_index += 1

                                    print(f"      ⚠ Missing team name in '{section_label}', inferred: {section_team_abbr}")

                                # Save to team-specific directory
                                if section_team_abbr:
                                    save_team_games_path = os.path.join(
                                        base_folder_path, section_team_abbr, games_folder, season_folder
                                    )
                                    os.makedirs(save_team_games_path, exist_ok=True)
                                else:
                                    save_team_games_path = team_games_path

                                # Build filename with team abbr to avoid "-" issue
                                file_name = f"{game_file_prefix}_{section_team_abbr}_{section_name}_{situation}.csv"
                                file_path = os.path.join(save_team_games_path, file_name)

                                df.to_csv(file_path, index=False)
                                total_tables += 1

            print(f"      ✓ Downloaded {total_tables} tables for {game_file_prefix}")

        time.sleep(random.uniform(2, 5))

    def print_summary(self):
        """Print detailed summary of corrupted files"""
        print(f"\n{'='*80}")
        print("CORRUPTION SUMMARY")
        print(f"{'='*80}\n")

        print(f"Total corrupted files: {len(self.corrupted_files)}")
        print(f"Total affected games: {len(self.games_by_id)}")
        print()

        if self.games_by_id:
            print("Games affected (by ID):")
            print("-" * 80)
            for game_id, game_data in sorted(self.games_by_id.items()):
                game_info = game_data.get('game_info', {})
                title = game_info.get('title', 'Unknown')
                file_count = len(game_data['files'])
                teams = ' vs '.join(sorted(game_data['teams']))

                print(f"  Game {game_id}: {teams}")
                print(f"    Title: {title}")
                print(f"    Corrupted files: {file_count}")
                print()


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(
        description='NST Data Sanitizer - Fix corrupted game files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan for corrupted files
  python nst_sanitize.py --scan

  # Verify games have missing team names in NST HTML
  python nst_sanitize.py --verify

  # Delete corrupted files only (no re-scraping)
  python nst_sanitize.py --delete-only

  # Full fix: delete and re-scrape
  python nst_sanitize.py --fix

  # Fix with custom delays
  python nst_sanitize.py --fix --delay-min 3 --delay-max 7
        """
    )

    parser.add_argument('--scan', action='store_true',
                        help='Scan for corrupted files and show summary (dry run)')
    parser.add_argument('--verify', action='store_true',
                        help='Verify that corrupted games have missing team names in NST HTML')
    parser.add_argument('--delete-only', action='store_true',
                        help='Delete corrupted files without re-scraping')
    parser.add_argument('--fix', action='store_true',
                        help='Delete corrupted files and re-scrape (full fix)')
    parser.add_argument('--season', type=str, default='2023-24',
                        help='Season to process (default: 2023-24)')
    parser.add_argument('--delay-min', type=float, default=2.0,
                        help='Minimum delay between games (seconds, default: 2)')
    parser.add_argument('--delay-max', type=float, default=5.0,
                        help='Maximum delay between games (seconds, default: 5)')
    parser.add_argument('--use-standard-parser', action='store_true',
                        help='Use standard parser instead of enhanced parser (not recommended for these games)')

    args = parser.parse_args()

    # If no action specified, default to scan
    if not (args.scan or args.verify or args.delete_only or args.fix):
        args.scan = True

    # Initialize sanitizer
    sanitizer = NSTSanitizer()

    # Step 1: Find corrupted files
    sanitizer.find_corrupted_files(season=args.season)

    if len(sanitizer.corrupted_files) == 0:
        print("✓ No corrupted files found!")
        return

    # Step 2: Load game metadata
    sanitizer.load_game_metadata()

    # Step 3: Show summary
    if args.scan or args.verify or args.delete_only or args.fix:
        sanitizer.print_summary()

    # Step 4: Verify games (optional)
    if args.verify:
        verification_results = sanitizer.verify_games_missing_team_names()

        confirmed_count = sum(1 for v in verification_results.values() if v is True)
        print(f"\n✓ Verified {confirmed_count}/{len(verification_results)} games have missing team names in NST HTML\n")

    # Step 5: Delete files
    if args.delete_only or args.fix:
        confirm = input("\n⚠️  Are you sure you want to DELETE these files? (yes/no): ")
        if confirm.lower() == 'yes':
            sanitizer.delete_corrupted_files(dry_run=False)
        else:
            print("Deletion cancelled.")
            return

    # Step 6: Re-scrape (if --fix)
    if args.fix:
        print("\n" + "="*80)
        print("STARTING RE-SCRAPING")
        print("="*80)

        sanitizer.rescrape_corrupted_games(
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            use_enhanced_parser=not args.use_standard_parser
        )

    print("\n" + "="*80)
    print("✓ DONE")
    print("="*80)


if __name__ == '__main__':
    main()
