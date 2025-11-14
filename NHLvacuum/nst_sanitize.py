#!/usr/bin/env python3
"""
NST Data Sanitizer

Comprehensive tool for detecting, fixing, and re-scraping corrupted NST game files.
Handles edge cases where NST's HTML is missing team names in section labels.
Also includes player ID fixing functionality for UNKNOWN player IDs.

Problems addressed:
1. Corrupted game files: Some NST games have section labels like "- Individual" instead of "Bruins - Individual"
2. Unknown player IDs: Players imported without NHL IDs (labeled as UNKNOWN_*)

Solutions:
1. Detect corrupted games, delete corrupted files, and re-scrape with enhanced parsing
2. Scrape NST player lists to fix UNKNOWN player IDs in database

Usage:
    # Scan for corrupted files
    python nst_sanitize.py --scan

    # Delete corrupted files and re-scrape
    python nst_sanitize.py --fix

    # Fix UNKNOWN player IDs in database
    python nst_sanitize.py --fix-players

    # Fix players without fetching new data (use existing player_ids.txt)
    python nst_sanitize.py --fix-players --no-fetch
"""

import os
import re
import csv
import time
import random
import argparse
import sqlite3
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
        self.null_games_by_season = defaultdict(set)  # Track null games per season

    def create_session(self):
        """Create requests session with retry logic"""
        if not self.session:
            self.session = nstparse.create_session()
        return self.session

    def load_null_games(self, season):
        """
        Load previously identified null games from CSV file

        Args:
            season: Season string (e.g., '2024-25')

        Returns:
            set: Set of game IDs to skip
        """
        null_games_file = f"null_games_{season}.csv"

        if not os.path.exists(null_games_file):
            return set()

        try:
            df = pd.read_csv(null_games_file)
            null_game_ids = set(df['game_id'].astype(str))
            print(f"  Loaded {len(null_game_ids)} null games from {null_games_file}")
            return null_game_ids
        except Exception as e:
            print(f"  ⚠ Error loading {null_games_file}: {e}")
            return set()

    def save_null_games(self, season):
        """
        Save corrupted/null games to CSV file for future reference

        Args:
            season: Season string (e.g., '2024-25')
        """
        if season not in self.null_games_by_season or not self.null_games_by_season[season]:
            print(f"  No null games to save for {season}")
            return

        null_games_file = f"null_games_{season}.csv"

        # Prepare data for CSV
        data = []
        for game_id in sorted(self.null_games_by_season[season]):
            if game_id in self.games_by_id:
                game_data = self.games_by_id[game_id]
                game_info = game_data.get('game_info', {})

                data.append({
                    'game_id': game_id,
                    'title': game_info.get('title', 'Unknown'),
                    'date': game_info.get('date', ''),
                    'teams': ' vs '.join(sorted(game_data['teams'])),
                    'corrupted_file_count': len(game_data['files']),
                    'full_report_url': game_info.get('full_report_url', '')
                })

        # Save to CSV with UTF-8 encoding
        df = pd.DataFrame(data)
        df.to_csv(null_games_file, index=False, encoding='utf-8')
        print(f"  ✓ Saved {len(data)} null games to {null_games_file}")

    def get_available_seasons(self):
        """
        Get list of all available seasons in the nhlteamreports directory

        Returns:
            list: List of season strings (e.g., ['2022-23', '2023-24', '2024-25'])
        """
        seasons = set()
        season_path = Path(self.base_dir)

        for team_dir in season_path.iterdir():
            if not team_dir.is_dir():
                continue

            games_dir = team_dir / 'games'
            if not games_dir.exists():
                continue

            # Get all season directories
            for season_dir in games_dir.iterdir():
                if season_dir.is_dir() and re.match(r'\d{4}-\d{2}', season_dir.name):
                    seasons.add(season_dir.name)

        return sorted(list(seasons))

    def find_corrupted_files(self, season='2024-25', skip_known_null_games=True):
        """
        Find all files with '-' in team position or only headers (no data)

        Args:
            season: Season to scan (e.g., '2024-25')
            skip_known_null_games: If True, skip games already in null_games_{season}.csv

        Returns:
            List of corrupted file paths
        """
        print(f"\n{'='*80}")
        print(f"SCANNING FOR CORRUPTED FILES IN {season} SEASON")
        print(f"{'='*80}\n")

        # Load known null games to skip
        known_null_games = set()
        if skip_known_null_games:
            known_null_games = self.load_null_games(season)
            if known_null_games:
                print(f"  Skipping {len(known_null_games)} known null games\n")

        # Pattern updated to handle team abbreviations with periods (e.g., S.J, L.A, T.B, N.J)
        pattern = re.compile(r'^([A-Z][A-Z\.]+)vs([A-Z][A-Z\.]+)_(\d+)_(.+)\.csv$')
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

                    # Skip if this game is already in known null games
                    if game_id in known_null_games:
                        continue

                    # Check ONLY if section_info starts with "-_" (missing team name)
                    # This is the specific NST bug where team names are missing from HTML
                    is_corrupted = section_info.startswith('-_')

                    if is_corrupted:
                        self.corrupted_files.append(csv_file)

                        # Track game information
                        self.games_by_id[game_id]['teams'].add(team1)
                        self.games_by_id[game_id]['teams'].add(team2)
                        self.games_by_id[game_id]['files'].append(str(csv_file))
                        self.games_by_id[game_id]['season'] = season  # Track season

                        # Add to null games for this season
                        self.null_games_by_season[season].add(game_id)

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

    def _get_game_info_from_games_list(self, team_abbr, game_id, season='2024-25'):
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

    # ============================================================================
    # PLAYER ID FIXING METHODS
    # ============================================================================

    @staticmethod
    def get_nst_player_ids_for_range(from_season, thru_season):
        """
        Scrape player IDs from Natural Stat Trick for a specific season range

        Args:
            from_season: Starting season in format YYYYYYYY (e.g., 20232024)
            thru_season: Ending season in format YYYYYYYY (e.g., 20252026)

        Returns:
            dict: {player_name: player_id}
        """
        # Use 'all' situation to get all players, not just 5v5
        url = f"https://www.naturalstattrick.com/playerlist.php?fromseason={from_season}&thruseason={thru_season}&stype=2&sit=all&stdoi=oi&rate=n"

        print(f"  Fetching players from {from_season} to {thru_season}...")

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
        except Exception as e:
            print(f"  ✗ Error fetching: {e}")
            return {}

        soup = BeautifulSoup(response.content, 'html.parser')

        # Find all table rows
        rows = soup.find_all('tr')
        if not rows:
            print("  ⚠ Could not find any table rows!")
            return {}

        player_map = {}

        # Parse each row
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 4:
                continue

            # First cell has player name
            player_name = cells[0].text.strip()
            if not player_name:
                continue

            # Find any link in the row with playerid parameter
            links = row.find_all('a', href=True)
            for link in links:
                href = link.get('href', '')

                # Extract player ID from URL like: playerreport.php?...&playerid=8474604&...
                match = re.search(r'playerid=(\d+)', href)
                if match:
                    player_id = match.group(1)
                    player_map[player_name] = player_id
                    break  # Found ID for this player, move to next row

        print(f"  ✓ Found {len(player_map)} players")
        return player_map

    @staticmethod
    def get_nst_player_ids(start_year=2023, end_year=2025, max_iterations=10):
        """
        Scrape player IDs from Natural Stat Trick, going back in time if needed

        Args:
            start_year: Most recent year to start with (e.g., 2023 for 2023-24 season)
            end_year: End year for initial range (e.g., 2026 for 2025-26 season)
            max_iterations: Maximum number of 3-year spans to try

        Returns:
            dict: {player_name: player_id}
        """
        print("\n" + "="*80)
        print("FETCHING PLAYER IDs FROM NATURAL STAT TRICK")
        print("="*80 + "\n")

        all_players = {}

        # Start with the most recent seasons
        current_start = start_year
        current_end = end_year

        for iteration in range(max_iterations):
            # Convert years to season format (YYYYYYYY)
            # e.g., 2023 -> 20232024 (2023-24 season)
            from_season = current_start * 10000 + (current_start + 1)
            thru_season = current_end * 10000 + (current_end + 1)

            print(f"Iteration {iteration + 1}: Seasons {current_start}-{current_start+1} to {current_end}-{current_end+1}")

            # Fetch players for this range
            players = NSTSanitizer.get_nst_player_ids_for_range(from_season, thru_season)

            # Merge with existing (don't overwrite existing mappings)
            new_players = 0
            for name, player_id in players.items():
                if name not in all_players:
                    all_players[name] = player_id
                    new_players += 1

            print(f"  Added {new_players} new players (total: {len(all_players)})")

            # Move back 3 years for next iteration
            current_start -= 3
            current_end -= 3

            # Stop if we go too far back (before NHL started tracking this data ~2007)
            if current_end < 2007:
                print(f"\nReached earliest available data (2007-08 season)")
                break

            # Small delay to be respectful to NST
            time.sleep(1)
            print()

        print("="*80)
        print(f"TOTAL: Found {len(all_players)} unique players across all seasons")
        print("="*80 + "\n")

        # Save to player_ids.txt
        NSTSanitizer.save_player_ids_to_file(all_players)

        return all_players

    @staticmethod
    def load_player_ids_from_file(filename='player_ids.txt'):
        """
        Load player IDs from file

        Args:
            filename: Input filename (default: player_ids.txt)

        Returns:
            dict: {player_name: player_id}
        """
        if not os.path.exists(filename):
            print(f"⚠ Warning: {filename} not found. No player IDs loaded.")
            return {}

        player_map = {}

        try:
            with open(filename, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line:
                        parts = line.rsplit(':', 1)
                        if len(parts) == 2:
                            name = parts[0].strip()
                            player_id = parts[1].strip()
                            player_map[name] = player_id
        except Exception as e:
            print(f"⚠ Error reading {filename}: {e}")
            return {}

        return player_map

    @staticmethod
    def save_player_ids_to_file(player_map, filename='player_ids.txt'):
        """
        Save player IDs to file with UTF-8 encoding

        Args:
            player_map: Dictionary of {player_name: player_id}
            filename: Output filename (default: player_ids.txt)
        """
        print(f"Saving {len(player_map)} player IDs to {filename}...")

        # Sort by player name for consistency
        sorted_players = sorted(player_map.items(), key=lambda x: x[0])

        # Write with UTF-8 encoding to handle accent marks correctly
        with open(filename, 'w', encoding='utf-8') as f:
            for player_name, player_id in sorted_players:
                f.write(f"{player_name}: {player_id}\n")

        print(f"✓ Saved to {filename}")

    @staticmethod
    def fix_unknown_players(db_path='nhl_analytics.db', fetch_new_data=True):
        """
        Update unknown player IDs in database

        Args:
            db_path: Path to database
            fetch_new_data: If True, fetch fresh player data from NST. If False, use existing player_ids.txt
        """
        print("\n" + "="*80)
        print("FIXING UNKNOWN PLAYER IDs")
        print("="*80 + "\n")

        # Get player IDs from NST or load from file
        if fetch_new_data:
            print("→ Fetching fresh player data from Natural Stat Trick...")
            nst_players = NSTSanitizer.get_nst_player_ids()
        else:
            print("→ Loading player data from player_ids.txt...")
            nst_players = NSTSanitizer.load_player_ids_from_file()
            print(f"✓ Loaded {len(nst_players)} players from file\n")

        # Connect to database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get all unknown players
        cursor.execute("""
            SELECT player_id, player_name, position
            FROM players
            WHERE player_id LIKE 'UNKNOWN%'
            ORDER BY player_name
        """)

        unknown_players = cursor.fetchall()
        print(f"Found {len(unknown_players)} unknown players in database\n")

        if len(unknown_players) == 0:
            print("✓ No unknown players to fix!")
            conn.close()
            return

        fixed_count = 0
        not_found = []

        for old_id, name, position in unknown_players:
            # Try exact match first
            nst_id = nst_players.get(name)

            if nst_id:
                # Check if this player_id already exists
                cursor.execute("SELECT COUNT(*) FROM players WHERE player_id = ?", (nst_id,))
                if cursor.fetchone()[0] > 0:
                    print(f"⚠ Merging: {name} ({position}) -> existing {nst_id}")
                else:
                    # Update player_id in players table
                    cursor.execute("""
                        UPDATE players
                        SET player_id = ?
                        WHERE player_id = ?
                    """, (nst_id, old_id))

                # Update all references in stats tables
                for table in ['player_game_stats', 'goalie_game_stats',
                             'player_onice_stats', 'player_shift_stats',
                             'line_combinations']:

                    if table == 'line_combinations':
                        # Handle player1_id, player2_id, player3_id
                        cursor.execute(f"""
                            SELECT DISTINCT game_id, team_id, situation_id, player1_id, player2_id, player3_id
                            FROM {table}
                            WHERE player1_id = ? OR player2_id = ? OR player3_id = ?
                        """, (old_id, old_id, old_id))

                        old_combos = cursor.fetchall()
                        deleted_total = 0

                        for game_id, team_id, situation_id, p1, p2, p3 in old_combos:
                            # Create what the new combo would be after update
                            new_p1 = nst_id if p1 == old_id else p1
                            new_p2 = nst_id if p2 == old_id else p2
                            new_p3 = nst_id if p3 == old_id else p3

                            # Check if this new combo already exists
                            cursor.execute(f"""
                                SELECT COUNT(*) FROM {table}
                                WHERE game_id = ? AND team_id = ? AND situation_id = ?
                                AND player1_id = ? AND player2_id = ? AND player3_id = ?
                            """, (game_id, team_id, situation_id, new_p1, new_p2, new_p3))

                            if cursor.fetchone()[0] > 0:
                                # Duplicate would exist, delete the old one
                                cursor.execute(f"""
                                    DELETE FROM {table}
                                    WHERE game_id = ? AND team_id = ? AND situation_id = ?
                                    AND player1_id = ? AND player2_id = ? AND player3_id = ?
                                """, (game_id, team_id, situation_id, p1, p2, p3))
                                deleted_total += cursor.rowcount

                        if deleted_total > 0:
                            print(f"    Deleted {deleted_total} duplicate line combos from {table}")

                        # Now safely update all remaining records
                        for col in ['player1_id', 'player2_id', 'player3_id']:
                            cursor.execute(f"""
                                UPDATE {table}
                                SET {col} = ?
                                WHERE {col} = ?
                            """, (nst_id, old_id))
                    else:
                        # For stats tables, delete UNKNOWN records where real record already exists
                        try:
                            cursor.execute(f"""
                                DELETE FROM {table}
                                WHERE player_id = ?
                                AND EXISTS (
                                    SELECT 1 FROM {table} t2
                                    WHERE t2.player_id = ?
                                    AND t2.game_id = {table}.game_id
                                    AND t2.team_id = {table}.team_id
                                    AND t2.situation_id = {table}.situation_id
                                )
                            """, (old_id, nst_id))

                            deleted = cursor.rowcount
                            if deleted > 0:
                                print(f"    Deleted {deleted} duplicate rows from {table}")

                            # Now update remaining UNKNOWN records
                            cursor.execute(f"""
                                UPDATE {table}
                                SET player_id = ?
                                WHERE player_id = ?
                            """, (nst_id, old_id))

                        except sqlite3.IntegrityError as e:
                            print(f"    ⚠ Warning: Could not update {table} for {name}: {e}")
                            continue

                # Delete the old unknown player entry if it still exists
                cursor.execute("DELETE FROM players WHERE player_id = ?", (old_id,))

                print(f"✓ Fixed: {name} ({position}) -> {nst_id}")
                fixed_count += 1
            else:
                not_found.append((name, position))

        conn.commit()
        conn.close()

        print(f"\n{'='*80}")
        print(f"✓ Fixed {fixed_count} players")
        if not_found:
            print(f"⚠ Could not find {len(not_found)} players:")
            for name, pos in not_found[:10]:
                print(f"    - {name} ({pos})")
            if len(not_found) > 10:
                print(f"    ... and {len(not_found) - 10} more")
        print(f"{'='*80}\n")

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
    parser.add_argument('--season', type=str, default='all',
                        help='Season to process (default: all) or specific season like 2024-25')
    parser.add_argument('--delay-min', type=float, default=2.0,
                        help='Minimum delay between games (seconds, default: 2)')
    parser.add_argument('--delay-max', type=float, default=5.0,
                        help='Maximum delay between games (seconds, default: 5)')
    parser.add_argument('--use-standard-parser', action='store_true',
                        help='Use standard parser instead of enhanced parser (not recommended for these games)')
    parser.add_argument('--force-rescan', action='store_true',
                        help='Force re-scan of all games, including those in null_games_*.csv files')
    parser.add_argument('--fix-players', action='store_true',
                        help='Fix UNKNOWN player IDs in database by scraping NST player lists')
    parser.add_argument('--no-fetch', action='store_true',
                        help='Use existing player_ids.txt instead of fetching fresh data (use with --fix-players)')

    args = parser.parse_args()

    # Handle player fixing separately (doesn't require file scanning)
    if args.fix_players:
        NSTSanitizer.fix_unknown_players(
            db_path='nhl_analytics.db',
            fetch_new_data=not args.no_fetch
        )
        return

    # If no action specified, default to scan
    if not (args.scan or args.verify or args.delete_only or args.fix):
        args.scan = True

    # Initialize sanitizer
    sanitizer = NSTSanitizer()

    # Determine which seasons to process
    if args.season == 'all':
        seasons = sanitizer.get_available_seasons()
        if not seasons:
            print("✗ No seasons found in nhlteamreports/")
            return
        print(f"\nProcessing all seasons: {', '.join(seasons)}\n")
    else:
        seasons = [args.season]

    # Process each season
    for season in seasons:
        print(f"\n{'='*80}")
        print(f"PROCESSING SEASON: {season}")
        print(f"{'='*80}\n")

        # Reset for each season
        sanitizer.corrupted_files = []
        sanitizer.games_by_id = defaultdict(lambda: {
            'teams': set(),
            'files': [],
            'game_info': {}
        })

        # Step 1: Find corrupted files
        sanitizer.find_corrupted_files(season=season, skip_known_null_games=not args.force_rescan)

        if len(sanitizer.corrupted_files) == 0:
            print(f"✓ No corrupted files found for {season}!")
            continue

        # Step 2: Load game metadata
        sanitizer.load_game_metadata()

        # Step 3: Show summary
        if args.scan or args.verify or args.delete_only or args.fix:
            sanitizer.print_summary()

        # Step 4: Save null games to CSV
        print(f"\n{'='*80}")
        print(f"SAVING NULL GAMES FOR {season}")
        print(f"{'='*80}\n")
        sanitizer.save_null_games(season)

    # After processing all seasons, return if scan only
    if args.scan:
        print(f"\n{'='*80}")
        print("SCAN COMPLETE - Check null_games_*.csv files")
        print(f"{'='*80}")
        return

    # The rest of the logic only applies if processing a single season
    if len(seasons) > 1:
        print(f"\n⚠ Additional operations (--verify, --delete-only, --fix) only work with a single season")
        print(f"   Please specify --season <season> to perform these operations")
        return

    # Continue with single season operations
    season = seasons[0]

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
