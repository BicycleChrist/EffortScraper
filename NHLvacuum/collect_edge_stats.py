#!/usr/bin/env python3
"""
NHL EDGE Stats & Play-by-Play Bulk Collection Script
=====================================================
Collects EDGE statistics and play-by-play data, saving to JSON files.

Data collected:
  - Player EDGE stats (skating speed, shot speed, zone time, etc.)
  - Team EDGE stats (team-level skating, shooting, zone metrics)
  - Play-by-play events for all games in database
  - Shift charts (per-player shift start/end times -> stint reconstruction / RAPM)

Saves data to EdgeStats/ directory:
  - EdgeStats/skaters/{player_id}.json (all historical EDGE data)
  - EdgeStats/goalies/{player_id}.json (all historical EDGE data)
  - EdgeStats/teams/{team_abbr}.json (team EDGE data)
  - EdgeStats/pbp/pbp_{game_id}_{away}_{at}_{home}_{date}.json
  - EdgeStats/shifts/shifts_{game_id}_{away}_{at}_{home}_{date}.json

Usage:
    # Player EDGE stats
    python collect_edge_stats.py --all                              # All players
    python collect_edge_stats.py --player 8478402                   # Single player

    # Team EDGE stats
    python collect_edge_stats.py --teams                            # All teams
    python collect_edge_stats.py --team TOR                         # Single team

    # Play-by-play
    python collect_edge_stats.py --pbp                              # All games
    python collect_edge_stats.py --pbp --limit 10                   # Test with 10 games
    python collect_edge_stats.py --game 2024030416                  # Single game

    # Shift charts (for stint reconstruction / RAPM)
    python collect_edge_stats.py --shifts                           # All games
    python collect_edge_stats.py --shifts --limit 10                # Test with 10 games
    python collect_edge_stats.py --shifts --game 2024030416         # Single game

    # Combined collection
    python collect_edge_stats.py --all --teams --pbp                # Everything

Author: NHL Analytics Database Project
Date: 2025-12-03
"""

import argparse
import sys
import json
import re
import html as ihtml
import sqlite3
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from typing import Dict, Set, Optional, List, Tuple
from nhl_api_client import NHLAPIClient
from nhlpy import NHLClient


class EdgeStatsCollector:
    """Collects and saves NHL EDGE statistics and play-by-play data to JSON files"""

    # NHL API Team ID mapping (team_abbr -> NHL API team_id)
    NHL_TEAM_IDS = {
        'ANA': '24', 'ARI': '53', 'BOS': '6', 'BUF': '7', 'CAR': '12',
        'CBJ': '29', 'CGY': '20', 'CHI': '16', 'COL': '21', 'DAL': '25',
        'DET': '17', 'EDM': '22', 'FLA': '13', 'LA': '26', 'MIN': '30',
        'MTL': '8', 'NJ': '1', 'NSH': '18', 'NYI': '2', 'NYR': '3',
        'OTT': '9', 'PHI': '4', 'PIT': '5', 'SJ': '28', 'SEA': '55',
        'STL': '19', 'TB': '14', 'TOR': '10', 'UTA': '59', 'VAN': '23',
        'VGK': '54', 'WPG': '52', 'WSH': '15'
    }

    def __init__(self, output_dir: str = "EdgeStats", player_ids_file: str = "player_ids.txt",
                 db_path: str = "nhl_analytics.db", verbose: bool = True):
        """
        Initialize collector.

        Args:
            output_dir: Directory to save JSON files
            player_ids_file: Path to player_ids.txt
            db_path: Path to NHL analytics database
            verbose: Print progress messages
        """
        self.output_dir = Path(output_dir)
        self.player_ids_file = player_ids_file
        self.db_path = db_path
        self.verbose = verbose
        self.api_client = NHLAPIClient(verbose=verbose)
        self.nhl_client = NHLClient()
        self._goalie_ids: Optional[Set[str]] = None
        self._print_lock = threading.Lock()

        # Create directory structure
        self.skaters_dir = self.output_dir / "skaters"
        self.goalies_dir = self.output_dir / "goalies"
        self.teams_dir = self.output_dir / "teams"
        self.pbp_dir = self.output_dir / "pbp"
        self.shifts_dir = self.output_dir / "shifts"

        for directory in [self.skaters_dir, self.goalies_dir, self.teams_dir,
                          self.pbp_dir, self.shifts_dir]:
            directory.mkdir(parents=True, exist_ok=True)

    def _load_player_ids(self) -> tuple[list[str], Set[str]]:
        """
        Load all player IDs from player_ids.txt file.

        Returns:
            Tuple of (all_player_ids, goalie_ids)
        """
        all_player_ids = []
        goalie_ids = set()
        player_ids_path = Path(self.player_ids_file)

        if not player_ids_path.exists():
            raise FileNotFoundError(f"Could not find {self.player_ids_file}")

        try:
            with open(player_ids_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or ':' not in line:
                        continue

                    parts = line.split(':')
                    if len(parts) == 2:
                        player_id = parts[1].replace('[G]', '').strip()
                        all_player_ids.append(player_id)

                        if '[G]' in line:
                            goalie_ids.add(player_id)

            self._goalie_ids = goalie_ids
            if self.verbose:
                print(f"✓ Loaded {len(all_player_ids)} player IDs from {self.player_ids_file}")
                print(f"  - {len(goalie_ids)} goalies")
                print(f"  - {len(all_player_ids) - len(goalie_ids)} skaters")

        except Exception as e:
            raise Exception(f"Error loading player IDs: {e}")

        return all_player_ids, goalie_ids

    def is_goalie(self, player_id: str) -> bool:
        """Check if a player ID is a goalie"""
        if self._goalie_ids is None:
            # Load if not already loaded
            _, self._goalie_ids = self._load_player_ids()
        return player_id in self._goalie_ids

    def _thread_safe_print(self, message: str):
        """Thread-safe print function"""
        if self.verbose:
            with self._print_lock:
                print(message)

    def collect_player_edge_stats(self, player_id: str, season: str = None,
                                  game_type: int = 2, collect_all_seasons: bool = True) -> bool:
        """
        Collect EDGE stats for a single player and save to JSON.
        When season=None and collect_all_seasons=True, collects ALL historical seasons.

        Args:
            player_id: NHL player ID
            season: Season in API format (YYYYYYYY), or None for all historical data
            game_type: 2 = Regular, 3 = Playoffs
            collect_all_seasons: If True (default), collect all available seasons

        Returns:
            True if successful (even with partial data)
        """
        # Buffer messages for this player to print atomically
        message_buffer = []

        try:
            is_goalie = self.is_goalie(player_id)
            output_dir = self.goalies_dir if is_goalie else self.skaters_dir

            # Create a temporary API client with message buffering
            buffered_client = NHLAPIClient(
                db_path=self.api_client.db_path,
                verbose=self.verbose,
                message_buffer=message_buffer
            )

            # Multi-season collection (like teams)
            if season is None and collect_all_seasons:
                # First, get current season to find available seasons
                initial_data = buffered_client.fetch_goalie_edge_details(
                    player_id=player_id,
                    season="20252026",
                    game_type=game_type
                ) if is_goalie else buffered_client.fetch_player_edge_details(
                    player_id=player_id,
                    season="20252026",
                    game_type=game_type
                )

                if 'error' in initial_data:
                    message_buffer.append(f"  ✗ Error fetching player {player_id}: {initial_data['error']}")
                    self._thread_safe_print('\n'.join(message_buffer))
                    return False

                # Get list of available seasons
                available_seasons = initial_data.get('detail', {}).get('seasonsWithEdgeStats', [])

                if not available_seasons:
                    message_buffer.append(f"  ⚠ No season data available for player {player_id}")
                    self._thread_safe_print('\n'.join(message_buffer))
                    return False

                # Collect data for each available season
                all_seasons_data = {}
                season_ids = [s['id'] for s in available_seasons if 'id' in s]

                message_buffer.append(f"  Collecting {len(season_ids)} seasons for player {player_id}...")

                for season_id in season_ids:
                    season_data = buffered_client.fetch_goalie_edge_details(
                        player_id=player_id,
                        season=str(season_id),
                        game_type=game_type
                    ) if is_goalie else buffered_client.fetch_player_edge_details(
                        player_id=player_id,
                        season=str(season_id),
                        game_type=game_type
                    )

                    if 'error' not in season_data:
                        # Check if there's actual data
                        data_keys = [k for k in season_data.keys() if not k.startswith('_')]
                        if data_keys:
                            all_seasons_data[str(season_id)] = season_data

                if not all_seasons_data:
                    message_buffer.append(f"  ✗ No data collected for any season")
                    self._thread_safe_print('\n'.join(message_buffer))
                    return False

                # Add metadata
                combined_data = {
                    'seasons': all_seasons_data,
                    '_metadata': {
                        'player_id': player_id,
                        'is_goalie': is_goalie,
                        'seasons_collected': list(all_seasons_data.keys()),
                        'total_seasons': len(all_seasons_data),
                        'game_type': game_type,
                        'collected_at': datetime.now().isoformat()
                    }
                }

                # Save to JSON
                output_file = output_dir / f"{player_id}.json"
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(combined_data, f, indent=2)

                message_buffer.append(f"✓ Saved {len(all_seasons_data)} seasons for player {player_id}")
                self._thread_safe_print('\n'.join(message_buffer))
                return True

            else:
                # Single season collection (original behavior)
                edge_data = buffered_client.fetch_goalie_edge_details(
                    player_id=player_id,
                    season=season if season else "20252026",
                    game_type=game_type
                ) if is_goalie else buffered_client.fetch_player_edge_details(
                    player_id=player_id,
                    season=season if season else "20252026",
                    game_type=game_type
                )

                # Check if we got any data at all
                data_keys = [k for k in edge_data.keys() if not k.startswith('_')]
                if not data_keys:
                    message_buffer.append(f"  ✗ No data available for player {player_id}")
                    self._thread_safe_print('\n'.join(message_buffer))
                    return False

                # Add metadata
                edge_data['_metadata'] = {
                    'player_id': player_id,
                    'season': season if season else 'current',
                    'game_type': game_type,
                    'is_goalie': is_goalie,
                    'collected_at': datetime.now().isoformat(),
                    'endpoints_collected': data_keys,
                    'endpoints_failed': len(edge_data.get('_failed_endpoints', []))
                }

                # Save to JSON file
                if season:
                    output_file = output_dir / f"{player_id}_{season}_{game_type}.json"
                else:
                    output_file = output_dir / f"{player_id}.json"

                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(edge_data, f, indent=2)

                # Report success with warnings if some endpoints failed
                if '_failed_endpoints' in edge_data:
                    message_buffer.append(f"⚠ Saved EDGE stats for player {player_id} (some endpoints failed)")
                else:
                    message_buffer.append(f"✓ Saved EDGE stats for player {player_id}")

                self._thread_safe_print('\n'.join(message_buffer))
                return True

        except Exception as e:
            message_buffer.append(f"✗ Error collecting stats for player {player_id}: {e}")
            self._thread_safe_print('\n'.join(message_buffer))
            return False

    def collect_all_players(self, season: str = None, game_type: int = 2,
                           player_limit: Optional[int] = None,
                           max_workers: int = 1) -> Dict[str, int]:
        """
        Collect EDGE stats for all players.
        When season=None, collects ALL historical data for each player.

        Args:
            season: Season in API format (YYYYYYYY), or None for all historical data
            game_type: 2 = Regular, 3 = Playoffs
            player_limit: Limit number of players (for testing)
            max_workers: Number of concurrent threads (1 = sequential)

        Returns:
            Dictionary with collection statistics
        """
        if self.verbose:
            print(f"\n{'='*60}")
            if season:
                print(f"Collecting EDGE stats for season {season}")
            else:
                print(f"Collecting ALL historical EDGE stats")
            if max_workers > 1:
                print(f"Using {max_workers} concurrent threads")
            print(f"{'='*60}\n")

        # Get all player IDs from player_ids.txt
        player_ids, _ = self._load_player_ids()

        if player_limit:
            player_ids = player_ids[:player_limit]
            if self.verbose:
                print(f"\n⚠ Limiting to {player_limit} players for testing\n")

        stats = {
            'total': len(player_ids),
            'success': 0,
            'failed': 0
        }
        stats_lock = threading.Lock()

        def process_player(player_id: str, index: int) -> bool:
            """Process a single player"""
            self._thread_safe_print(f"[{index}/{stats['total']}] Processing player {player_id}")
            return self.collect_player_edge_stats(player_id, season, game_type)

        # Process players
        if max_workers > 1:
            # Threaded processing
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_player = {
                    executor.submit(process_player, player_id, i): player_id
                    for i, player_id in enumerate(player_ids, 1)
                }

                for future in as_completed(future_to_player):
                    try:
                        success = future.result()
                        with stats_lock:
                            if success:
                                stats['success'] += 1
                            else:
                                stats['failed'] += 1
                    except Exception as e:
                        self._thread_safe_print(f"✗ Exception: {e}")
                        with stats_lock:
                            stats['failed'] += 1
        else:
            # Sequential processing
            for i, player_id in enumerate(player_ids, 1):
                success = process_player(player_id, i)
                if success:
                    stats['success'] += 1
                else:
                    stats['failed'] += 1

        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Collection Complete!")
            print(f"  Total: {stats['total']}")
            print(f"  Success: {stats['success']}")
            print(f"  Failed: {stats['failed']}")
            print(f"  Files saved to: {self.output_dir}")
            print(f"{'='*60}\n")

        return stats

    # =========================================================================
    # TEAM EDGE STATS COLLECTION
    # =========================================================================

    def _get_teams_from_db(self) -> List[Tuple[str, int]]:
        """
        Get all active teams from database.

        Returns:
            List of tuples: (team_abbr, team_id)
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get only active NHL teams (exclude historical franchises like ARI)
        cursor.execute("""
            SELECT team_abbr, team_id
            FROM teams
            WHERE team_abbr IN ('ANA', 'BOS', 'BUF', 'CAR', 'CBJ', 'CGY', 'CHI',
                                'COL', 'DAL', 'DET', 'EDM', 'FLA', 'LA', 'MIN',
                                'MTL', 'NJ', 'NSH', 'NYI', 'NYR', 'OTT', 'PHI',
                                'PIT', 'SJ', 'SEA', 'STL', 'TB', 'TOR', 'UTA',
                                'VAN', 'VGK', 'WPG', 'WSH')
            ORDER BY team_abbr
        """)

        teams = cursor.fetchall()
        conn.close()

        return teams

    def collect_team_edge_stats(self, team_abbr: str, season: str = None,
                                 game_type: int = 2, all_seasons: bool = True) -> bool:
        """
        Collect EDGE stats for a single team and save to JSON.

        Args:
            team_abbr: Team abbreviation (e.g., 'TOR', 'EDM')
            season: Season in API format (YYYYYYYY), or None to collect all available seasons
            game_type: 2 = Regular, 3 = Playoffs
            all_seasons: If True, collect all available seasons in one file (default)

        Returns:
            True if successful
        """
        try:
            # Get NHL API team ID
            nhl_team_id = self.NHL_TEAM_IDS.get(team_abbr)
            if not nhl_team_id:
                self._thread_safe_print(f"  ✗ Unknown team abbreviation: {team_abbr}")
                return False

            # Generate filename
            if season:
                output_file = self.teams_dir / f"{team_abbr}_{season}_{game_type}.json"
            else:
                output_file = self.teams_dir / f"{team_abbr}.json"

            # Skip if already exists
            if output_file.exists():
                self._thread_safe_print(f"  ⊙ Team EDGE already exists: {output_file.name}")
                return True

            # If all_seasons is True and no specific season requested, collect all available
            if all_seasons and not season:
                # First, get current season data to find available seasons
                initial_data = self.api_client.fetch_team_edge_details(
                    team_id=nhl_team_id,
                    season="20242025",
                    game_type=game_type
                )

                if 'error' in initial_data:
                    self._thread_safe_print(f"  ✗ Error fetching team {team_abbr}: {initial_data['error']}")
                    return False

                # Get list of available seasons
                available_seasons = initial_data.get('detail', {}).get('seasonsWithEdgeStats', [])

                if not available_seasons:
                    self._thread_safe_print(f"  ⚠ No season data available for {team_abbr}")
                    return False

                # Collect data for each available season
                all_seasons_data = {}
                season_ids = [s['id'] for s in available_seasons if 'id' in s]

                self._thread_safe_print(f"  Collecting {len(season_ids)} seasons for {team_abbr}...")

                for season_id in season_ids:
                    season_data = self.api_client.fetch_team_edge_details(
                        team_id=nhl_team_id,
                        season=str(season_id),
                        game_type=game_type
                    )

                    if 'error' not in season_data:
                        all_seasons_data[str(season_id)] = season_data

                # Add metadata
                combined_data = {
                    'seasons': all_seasons_data,
                    '_metadata': {
                        'team_abbr': team_abbr,
                        'nhl_team_id': nhl_team_id,
                        'seasons_collected': list(all_seasons_data.keys()),
                        'total_seasons': len(all_seasons_data),
                        'game_type': game_type,
                        'collected_at': datetime.now().isoformat()
                    }
                }

                # Save to JSON
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(combined_data, f, indent=2)

                self._thread_safe_print(f"  ✓ Saved {len(all_seasons_data)} seasons: {output_file.name}")
                return True

            else:
                # Single season collection (original behavior)
                edge_data = self.api_client.fetch_team_edge_details(
                    team_id=nhl_team_id,
                    season=season if season else "20242025",
                    game_type=game_type
                )

                if 'error' in edge_data:
                    self._thread_safe_print(f"  ✗ Error fetching team {team_abbr}: {edge_data['error']}")
                    return False

                # Add metadata
                edge_data['_metadata'] = {
                    'team_abbr': team_abbr,
                    'nhl_team_id': nhl_team_id,
                    'season': season if season else 'current',
                    'game_type': game_type,
                    'collected_at': datetime.now().isoformat()
                }

                # Save to JSON
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(edge_data, f, indent=2)

                self._thread_safe_print(f"  ✓ Saved team EDGE: {output_file.name}")
                return True

        except Exception as e:
            self._thread_safe_print(f"  ✗ Error collecting team {team_abbr}: {e}")
            return False

    def collect_all_teams_edge_stats(self, season: str = None, game_type: int = 2,
                                      max_workers: int = 1) -> Dict[str, int]:
        """
        Collect EDGE stats for all teams.

        Args:
            season: Season in API format (YYYYYYYY), or None for current season
            game_type: 2 = Regular, 3 = Playoffs
            max_workers: Number of concurrent threads

        Returns:
            Dictionary with collection statistics
        """
        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Collecting Team EDGE Stats")
            if season:
                print(f"Season: {season}")
            else:
                print(f"Season: Current (20242025)")
            if max_workers > 1:
                print(f"Using {max_workers} concurrent threads")
            print(f"{'='*60}\n")

        # Get all teams from database
        teams = self._get_teams_from_db()

        if self.verbose:
            print(f"Found {len(teams)} active teams\n")

        stats = {
            'total': len(teams),
            'success': 0,
            'failed': 0
        }
        stats_lock = threading.Lock()

        def process_team(team_data: Tuple, index: int) -> bool:
            """Process a single team"""
            team_abbr, team_id = team_data
            self._thread_safe_print(f"[{index}/{stats['total']}] Team {team_abbr}")
            return self.collect_team_edge_stats(team_abbr, season, game_type)

        # Process teams
        if max_workers > 1:
            # Threaded processing
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_team = {
                    executor.submit(process_team, team, i): team
                    for i, team in enumerate(teams, 1)
                }

                for future in as_completed(future_to_team):
                    try:
                        success = future.result()
                        with stats_lock:
                            if success:
                                stats['success'] += 1
                            else:
                                stats['failed'] += 1
                    except Exception as e:
                        self._thread_safe_print(f"✗ Exception: {e}")
                        with stats_lock:
                            stats['failed'] += 1
        else:
            # Sequential processing
            for i, team in enumerate(teams, 1):
                success = process_team(team, i)
                if success:
                    stats['success'] += 1
                else:
                    stats['failed'] += 1

        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Team EDGE Collection Complete!")
            print(f"  Total Teams: {stats['total']}")
            print(f"  Successful: {stats['success']}")
            print(f"  Failed: {stats['failed']}")
            print(f"  Files saved to: {self.teams_dir}")
            print(f"{'='*60}\n")

        return stats

    # =========================================================================
    # PLAY-BY-PLAY COLLECTION
    # =========================================================================

    def _get_games_from_db(self) -> List[Tuple[str, str, str, str]]:
        """
        Get all games from database.

        Returns:
            List of tuples: (game_id, away_team_abbr, home_team_abbr, game_date)
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                g.game_id,
                away.team_abbr as away_team,
                home.team_abbr as home_team,
                g.game_date
            FROM games g
            JOIN teams away ON g.away_team_id = away.team_id
            JOIN teams home ON g.home_team_id = home.team_id
            ORDER BY g.game_date DESC, g.game_id
        """)

        games = cursor.fetchall()
        conn.close()

        return games

    def collect_game_pbp(self, game_id: str, away_team: str, home_team: str,
                         game_date: str) -> bool:
        """
        Collect play-by-play data for a single game and save to JSON.

        Args:
            game_id: NHL game ID (e.g., "2024030416")
            away_team: Away team abbreviation
            home_team: Home team abbreviation
            game_date: Game date (YYYY-MM-DD)

        Returns:
            True if successful
        """
        try:
            # Generate filename: pbp_{game_id}_{away}_at_{home}_{date}.json
            output_file = self.pbp_dir / f"pbp_{game_id}_{away_team}_at_{home_team}_{game_date}.json"

            # Skip if already exists
            if output_file.exists():
                self._thread_safe_print(f"  ⊙ PBP already exists: {output_file.name}")
                return True

            # Fetch play-by-play data from API
            pbp_data = self.nhl_client.game_center.play_by_play(game_id)

            if not pbp_data or 'plays' not in pbp_data:
                self._thread_safe_print(f"  ✗ No PBP data for game {game_id}")
                return False

            # Add metadata
            pbp_data['_metadata'] = {
                'game_id': game_id,
                'away_team': away_team,
                'home_team': home_team,
                'game_date': game_date,
                'collected_at': datetime.now().isoformat(),
                'total_plays': len(pbp_data.get('plays', []))
            }

            # Save to JSON
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(pbp_data, f, indent=2)

            self._thread_safe_print(f"  ✓ Saved PBP: {output_file.name} ({len(pbp_data['plays'])} events)")
            return True

        except Exception as e:
            self._thread_safe_print(f"  ✗ Error collecting PBP for game {game_id}: {e}")
            return False

    def collect_all_game_pbp(self, game_limit: Optional[int] = None,
                             max_workers: int = 1) -> Dict[str, int]:
        """
        Collect play-by-play data for all games in database.

        Args:
            game_limit: Limit number of games (for testing)
            max_workers: Number of concurrent threads

        Returns:
            Dictionary with collection statistics
        """
        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Collecting Play-by-Play Data")
            if max_workers > 1:
                print(f"Using {max_workers} concurrent threads")
            print(f"{'='*60}\n")

        # Get all games from database
        games = self._get_games_from_db()

        if game_limit:
            games = games[:game_limit]
            if self.verbose:
                print(f"\n⚠ Limiting to {game_limit} games for testing\n")

        if self.verbose:
            print(f"Found {len(games)} games in database\n")

        stats = {
            'total': len(games),
            'success': 0,
            'failed': 0,
            'skipped': 0
        }
        stats_lock = threading.Lock()

        def process_game(game_data: Tuple, index: int) -> bool:
            """Process a single game"""
            game_id, away_team, home_team, game_date = game_data
            self._thread_safe_print(f"[{index}/{stats['total']}] Game {game_id}: {away_team} @ {home_team} ({game_date})")
            return self.collect_game_pbp(game_id, away_team, home_team, game_date)

        # Process games
        if max_workers > 1:
            # Threaded processing
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_game = {
                    executor.submit(process_game, game, i): game
                    for i, game in enumerate(games, 1)
                }

                for future in as_completed(future_to_game):
                    try:
                        success = future.result()
                        with stats_lock:
                            if success:
                                stats['success'] += 1
                            else:
                                stats['failed'] += 1
                    except Exception as e:
                        self._thread_safe_print(f"✗ Exception: {e}")
                        with stats_lock:
                            stats['failed'] += 1
        else:
            # Sequential processing
            for i, game in enumerate(games, 1):
                success = process_game(game, i)
                if success:
                    stats['success'] += 1
                else:
                    stats['failed'] += 1

        if self.verbose:
            print(f"\n{'='*60}")
            print(f"PBP Collection Complete!")
            print(f"  Total Games: {stats['total']}")
            print(f"  Successful: {stats['success']}")
            print(f"  Failed: {stats['failed']}")
            print(f"  Files saved to: {self.pbp_dir}")
            print(f"{'='*60}\n")

        return stats

    # =========================================================================
    # SHIFT CHART COLLECTION
    # =========================================================================
    # NHL shift charts give per-player shift start/end times, which let us
    # reconstruct on-ice stints (constant personnel between shift boundaries).
    # Joined with mp_shots xG by time, this is the basis for stint-level RAPM.
    # Endpoint: https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId=ID
    # (Not available via nhlpy, so we hit it directly with requests.)

    SHIFT_CHART_URL = "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"
    # Legacy HTML shift reports (TV = visitor/away, TH = home). The newer stats JSON
    # API has gaps (mainly the current season); these HTML reports remain complete.
    SHIFT_HTML_URL = "https://www.nhl.com/scores/htmlreports/{folder}/{side}{num}.HTM"

    def collect_game_shifts(self, game_id: str, away_team: str, home_team: str,
                            game_date: str) -> bool:
        """
        Collect shift-chart data for a single game and save to JSON.

        Tries the stats JSON API first; if it returns no shifts (an NHL-side gap,
        not an error), falls back to parsing the legacy HTML shift reports. Both
        paths produce the same record schema (playerId/teamId/period/startTime/
        endTime) so downstream stint reconstruction is source-agnostic.
        """
        try:
            # Filename mirrors PBP: shifts_{game_id}_{away}_at_{home}_{date}.json
            output_file = self.shifts_dir / f"shifts_{game_id}_{away_team}_at_{home_team}_{game_date}.json"

            # Skip if already exists (idempotent / resumable)
            if output_file.exists():
                self._thread_safe_print(f"  ⊙ Shifts already exist: {output_file.name}")
                return True

            source = 'json'
            shift_data = None
            try:
                resp = requests.get(self.SHIFT_CHART_URL.format(game_id=game_id),
                                    timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                resp.raise_for_status()
                jd = resp.json()
                if jd.get('data'):
                    shift_data = jd
            except Exception as e:
                self._thread_safe_print(f"  ⚠ JSON shift fetch failed for {game_id} ({e}); trying HTML fallback")

            # Fallback: legacy HTML shift reports
            if shift_data is None:
                shift_data = self._collect_game_shifts_html(game_id)
                source = 'html'

            records = shift_data.get('data', []) if shift_data else []
            if not records:
                self._thread_safe_print(f"  ✗ No shift data (json+html) for game {game_id}")
                return False

            # Add metadata (mirrors PBP collection); 'source' marks json vs html
            shift_data['_metadata'] = {
                'game_id': game_id,
                'away_team': away_team,
                'home_team': home_team,
                'game_date': game_date,
                'collected_at': datetime.now().isoformat(),
                'source': source,
                'total_shifts': len(records)
            }

            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(shift_data, f, indent=2)

            self._thread_safe_print(f"  ✓ Saved shifts [{source}]: {output_file.name} ({len(records)} shifts)")
            return True

        except Exception as e:
            self._thread_safe_print(f"  ✗ Error collecting shifts for game {game_id}: {e}")
            return False

    def _load_roster_from_pbp(self, game_id: str) -> Optional[Dict]:
        """Build {sweaterNumber: playerId} maps per team from the saved PBP JSON's
        rosterSpots. Used to resolve jersey numbers in HTML shift reports to IDs."""
        matches = list(self.pbp_dir.glob(f"pbp_{game_id}_*.json"))
        if not matches:
            return None
        try:
            d = json.loads(matches[0].read_text(encoding='utf-8'))
        except Exception:
            return None
        home_id = (d.get('homeTeam') or {}).get('id')
        away_id = (d.get('awayTeam') or {}).get('id')
        home_map, away_map = {}, {}
        for s in d.get('rosterSpots', []):
            tid, num, pid = s.get('teamId'), s.get('sweaterNumber'), s.get('playerId')
            if num is None or pid is None:
                continue
            if tid == home_id:
                home_map[int(num)] = pid
            elif tid == away_id:
                away_map[int(num)] = pid
        if not home_map and not away_map:
            return None
        return {'home_id': home_id, 'away_id': away_id,
                'home_map': home_map, 'away_map': away_map}

    @staticmethod
    def _parse_shift_report_html(html_text: str, team_id, num_to_pid: Dict[int, int]) -> List[Dict]:
        """Parse one NHL HTML shift report (TV or TH) into shift records.

        Each player block starts with a 'playerHeading' td of '{number} LAST, FIRST',
        followed by shift rows: [Shift#, Period, 'Start / Remain', 'End / Remain',
        Duration, Event]. Start/End are time elapsed in the period (MM:SS), matching
        the stats-API startTime/endTime fields.
        """
        def pad(t):
            return ':'.join(p.zfill(2) for p in t.split(':'))

        records = []
        heads = [m.start() for m in re.finditer(r'class="playerHeading', html_text)]
        heads.append(len(html_text))
        for i in range(len(heads) - 1):
            block = html_text[heads[i]:heads[i + 1]]
            hm = re.search(r'playerHeading[^>]*>(.*?)</td>', block, re.S)
            if not hm:
                continue
            head = re.sub(r'<[^>]+>', '', ihtml.unescape(hm.group(1))).strip()
            nm = re.match(r'(\d+)\s+(.*)', head)
            if not nm:
                continue
            pid = num_to_pid.get(int(nm.group(1)))
            if pid is None:
                continue
            for row in re.findall(r'<tr[^>]*>(.*?)</tr>', block, re.S):
                cells = [re.sub(r'<[^>]+>', '', ihtml.unescape(c)).strip()
                         for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
                # A real shift row: shift# digit + start/end carry the 'elapsed / remaining' slash
                if len(cells) >= 5 and cells[0].isdigit() and '/' in cells[2] and '/' in cells[3]:
                    # Period cell is '1'/'2'/'3', or 'OT'/'OT2'... (overtime), or 'SO'.
                    per = cells[1].strip().upper()
                    if per.isdigit():
                        period = int(per)
                    elif per == 'OT':
                        period = 4
                    elif per.startswith('OT') and per[2:].isdigit():
                        period = 3 + int(per[2:])   # OT2 -> 5, OT3 -> 6 (playoff multi-OT)
                    elif per == 'SO':
                        continue  # shootout has no time-based shifts
                    else:
                        continue  # unknown period label -> skip just this row
                    records.append({
                        'playerId': pid,
                        'teamId': team_id,
                        'period': period,
                        'startTime': pad(cells[2].split('/')[0].strip()),
                        'endTime': pad(cells[3].split('/')[0].strip()),
                        'duration': cells[4],
                    })
        return records

    def _collect_game_shifts_html(self, game_id: str) -> Optional[Dict]:
        """Fallback: build shift records from the legacy HTML reports (TV away + TH home)."""
        roster = self._load_roster_from_pbp(game_id)
        if not roster:
            self._thread_safe_print(f"  ⚠ No PBP roster for {game_id}; cannot resolve HTML shift IDs")
            return None
        folder = f"{game_id[:4]}{int(game_id[:4]) + 1}"
        num = game_id[4:]
        out = []
        for side, team_id, nmap in [('TV', roster['away_id'], roster['away_map']),
                                    ('TH', roster['home_id'], roster['home_map'])]:
            try:
                r = requests.get(self.SHIFT_HTML_URL.format(folder=folder, side=side, num=num),
                                 timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code != 200:
                    continue
                out.extend(self._parse_shift_report_html(r.text, team_id, nmap))
            except Exception as e:
                self._thread_safe_print(f"  ⚠ HTML {side} fetch failed for {game_id}: {e}")
        return {'data': out, 'total': len(out)} if out else None

    def collect_all_game_shifts(self, game_limit: Optional[int] = None,
                                max_workers: int = 1) -> Dict[str, int]:
        """
        Collect shift-chart data for all games in database.

        Args:
            game_limit: Limit number of games (for testing)
            max_workers: Number of concurrent threads (keep low to avoid HTTP 429)

        Returns:
            Dictionary with collection statistics
        """
        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Collecting Shift Chart Data")
            if max_workers > 1:
                print(f"Using {max_workers} concurrent threads")
            print(f"{'='*60}\n")

        games = self._get_games_from_db()

        if game_limit:
            games = games[:game_limit]
            if self.verbose:
                print(f"\n⚠ Limiting to {game_limit} games for testing\n")

        if self.verbose:
            print(f"Found {len(games)} games in database\n")

        stats = {'total': len(games), 'success': 0, 'failed': 0, 'skipped': 0}
        stats_lock = threading.Lock()

        def process_game(game_data: Tuple, index: int) -> bool:
            game_id, away_team, home_team, game_date = game_data
            self._thread_safe_print(f"[{index}/{stats['total']}] Game {game_id}: {away_team} @ {home_team} ({game_date})")
            return self.collect_game_shifts(game_id, away_team, home_team, game_date)

        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_game = {
                    executor.submit(process_game, game, i): game
                    for i, game in enumerate(games, 1)
                }
                for future in as_completed(future_to_game):
                    try:
                        success = future.result()
                        with stats_lock:
                            stats['success' if success else 'failed'] += 1
                    except Exception as e:
                        self._thread_safe_print(f"✗ Exception: {e}")
                        with stats_lock:
                            stats['failed'] += 1
        else:
            for i, game in enumerate(games, 1):
                success = process_game(game, i)
                stats['success' if success else 'failed'] += 1

        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Shift Chart Collection Complete!")
            print(f"  Total Games: {stats['total']}")
            print(f"  Successful: {stats['success']}")
            print(f"  Failed: {stats['failed']}")
            print(f"  Files saved to: {self.shifts_dir}")
            print(f"{'='*60}\n")

        return stats


def main():
    parser = argparse.ArgumentParser(
        description='Collect NHL EDGE statistics and play-by-play data, saving to JSON files.'
    )

    # Season selection (for EDGE stats only)
    parser.add_argument(
        '--season',
        type=str,
        default=None,
        help='Season in format YYYYYYYY (e.g., 20242025 for 2024-25 season). '
             'If not specified, collects ALL historical EDGE data for each player.'
    )

    # Game type (for EDGE stats only)
    parser.add_argument(
        '--game-type',
        type=int,
        default=2,
        choices=[2, 3],
        help='Game type: 2=Regular Season, 3=Playoffs (default: 2)'
    )

    # Collection mode - EDGE stats
    parser.add_argument(
        '--all',
        action='store_true',
        help='Collect EDGE stats for all players'
    )

    parser.add_argument(
        '--player',
        type=str,
        help='Collect EDGE stats for a single player ID'
    )

    parser.add_argument(
        '--teams',
        action='store_true',
        help='Collect EDGE stats for all teams'
    )

    parser.add_argument(
        '--team',
        type=str,
        help='Collect EDGE stats for a single team abbreviation (e.g., TOR, EDM)'
    )

    # Collection mode - Play-by-play
    parser.add_argument(
        '--pbp',
        action='store_true',
        help='Collect play-by-play data for all games in database'
    )

    parser.add_argument(
        '--game',
        type=str,
        help='Collect a single game ID (play-by-play, or shifts if --shifts is set)'
    )

    # Collection mode - Shift charts (for stint reconstruction / RAPM)
    parser.add_argument(
        '--shifts',
        action='store_true',
        help='Collect shift-chart data for all games (or a single game with --game). '
             'Keep --threads low (default 1) to avoid HTTP 429 from api.nhle.com.'
    )

    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of players (for testing)'
    )

    # Output directory
    parser.add_argument(
        '--output-dir',
        type=str,
        default='EdgeStats',
        help='Directory to save JSON files (default: EdgeStats)'
    )

    # Verbosity
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress progress messages'
    )

    # Threading
    parser.add_argument(
        '--threads',
        type=int,
        default=1,
        help='Number of concurrent threads to use (default: 1 for sequential)'
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.all and not args.player and not args.teams and not args.team and not args.pbp and not args.game and not args.shifts:
        parser.error("Must specify at least one of: --all, --player, --teams, --team, --pbp, --shifts, or --game")

    # Initialize collector
    verbose = not args.quiet
    collector = EdgeStatsCollector(
        output_dir=args.output_dir,
        verbose=verbose
    )

    try:
        start_time = datetime.now()

        if verbose:
            print(f"\n{'='*70}")
            print(f"NHL Data Collection")
            print(f"{'='*70}")
            print(f"Output: {args.output_dir}")
            print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*70}\n")

        # =====================================================================
        # EDGE STATS COLLECTION
        # =====================================================================

        # Single player mode
        if args.player:
            if verbose:
                print(f"\n{'='*70}")
                print(f"EDGE Stats - Single Player")
                print(f"{'='*70}")
                if args.season:
                    print(f"Season: {args.season}")
                else:
                    print(f"Season: ALL (complete historical data)")
                print(f"Game Type: {'Regular Season' if args.game_type == 2 else 'Playoffs'}")
                print(f"{'='*70}\n")

            success = collector.collect_player_edge_stats(
                player_id=args.player,
                season=args.season,
                game_type=args.game_type
            )

            if success:
                print(f"\n✓ Successfully collected EDGE stats for player {args.player}")
            else:
                print(f"\n✗ Failed to collect EDGE stats for player {args.player}")
                sys.exit(1)

        # Bulk player mode
        elif args.all:
            if verbose:
                print(f"\n{'='*70}")
                print(f"EDGE Stats - All Players")
                print(f"{'='*70}")
                if args.season:
                    print(f"Season: {args.season}")
                else:
                    print(f"Season: ALL (complete historical data)")
                print(f"Game Type: {'Regular Season' if args.game_type == 2 else 'Playoffs'}")
                print(f"{'='*70}\n")

            stats = collector.collect_all_players(
                season=args.season,
                game_type=args.game_type,
                player_limit=args.limit,
                max_workers=args.threads
            )

            # Show summary
            end_time = datetime.now()
            duration = end_time - start_time

            if verbose:
                print(f"\n{'='*70}")
                print(f"EDGE Stats Collection Summary")
                print(f"{'='*70}")
                print(f"  Total Players: {stats['total']}")
                print(f"  Successful: {stats['success']}")
                print(f"  Failed: {stats['failed']}")
                print(f"  Duration: {duration}")
                print(f"  Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*70}\n")

        # =====================================================================
        # TEAM EDGE STATS COLLECTION
        # =====================================================================

        # Single team mode
        if args.team:
            if verbose:
                print(f"\n{'='*70}")
                print(f"Team EDGE Stats - Single Team")
                print(f"{'='*70}")
                if args.season:
                    print(f"Season: {args.season}")
                else:
                    print(f"Season: Current (20242025)")
                print(f"Game Type: {'Regular Season' if args.game_type == 2 else 'Playoffs'}")
                print(f"{'='*70}\n")

            success = collector.collect_team_edge_stats(
                team_abbr=args.team.upper(),
                season=args.season,
                game_type=args.game_type
            )

            if success:
                print(f"\n✓ Successfully collected team EDGE stats for {args.team.upper()}")
            else:
                print(f"\n✗ Failed to collect team EDGE stats for {args.team.upper()}")
                sys.exit(1)

        # Bulk team mode
        elif args.teams:
            stats = collector.collect_all_teams_edge_stats(
                season=args.season,
                game_type=args.game_type,
                max_workers=args.threads
            )

            # Show summary
            end_time = datetime.now()
            duration = end_time - start_time

            if verbose:
                print(f"\n{'='*70}")
                print(f"Team EDGE Stats Collection Summary")
                print(f"{'='*70}")
                print(f"  Total Teams: {stats['total']}")
                print(f"  Successful: {stats['success']}")
                print(f"  Failed: {stats['failed']}")
                print(f"  Duration: {duration}")
                print(f"  Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*70}\n")

        # =====================================================================
        # PLAY-BY-PLAY COLLECTION
        # =====================================================================

        # Single game mode (PBP). Skipped when --shifts is set (handled below).
        if args.game and not args.shifts:
            if verbose:
                print(f"\n{'='*70}")
                print(f"Play-by-Play - Single Game")
                print(f"{'='*70}\n")

            # Get game info from database
            conn = sqlite3.connect(collector.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT g.game_id, away.team_abbr, home.team_abbr, g.game_date
                FROM games g
                JOIN teams away ON g.away_team_id = away.team_id
                JOIN teams home ON g.home_team_id = home.team_id
                WHERE g.game_id = ?
            """, (args.game,))
            result = cursor.fetchone()
            conn.close()

            if not result:
                print(f"✗ Game {args.game} not found in database")
                sys.exit(1)

            game_id, away_team, home_team, game_date = result
            success = collector.collect_game_pbp(game_id, away_team, home_team, game_date)

            if success:
                print(f"\n✓ Successfully collected PBP for game {args.game}")
            else:
                print(f"\n✗ Failed to collect PBP for game {args.game}")
                sys.exit(1)

        # Bulk game mode
        elif args.pbp:
            stats = collector.collect_all_game_pbp(
                game_limit=args.limit,
                max_workers=args.threads
            )

            # Show summary
            end_time = datetime.now()
            duration = end_time - start_time

            if verbose:
                print(f"\n{'='*70}")
                print(f"Play-by-Play Collection Summary")
                print(f"{'='*70}")
                print(f"  Total Games: {stats['total']}")
                print(f"  Successful: {stats['success']}")
                print(f"  Failed: {stats['failed']}")
                print(f"  Duration: {duration}")
                print(f"  Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*70}\n")

        # =====================================================================
        # SHIFT CHART COLLECTION
        # =====================================================================
        if args.shifts:
            if args.game:
                # Single game shifts
                if verbose:
                    print(f"\n{'='*70}")
                    print(f"Shift Charts - Single Game")
                    print(f"{'='*70}\n")

                conn = sqlite3.connect(collector.db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT g.game_id, away.team_abbr, home.team_abbr, g.game_date
                    FROM games g
                    JOIN teams away ON g.away_team_id = away.team_id
                    JOIN teams home ON g.home_team_id = home.team_id
                    WHERE g.game_id = ?
                """, (args.game,))
                result = cursor.fetchone()
                conn.close()

                if not result:
                    print(f"✗ Game {args.game} not found in database")
                    sys.exit(1)

                game_id, away_team, home_team, game_date = result
                success = collector.collect_game_shifts(game_id, away_team, home_team, game_date)
                if success:
                    print(f"\n✓ Successfully collected shifts for game {args.game}")
                else:
                    print(f"\n✗ Failed to collect shifts for game {args.game}")
                    sys.exit(1)
            else:
                # Bulk shift collection
                stats = collector.collect_all_game_shifts(
                    game_limit=args.limit,
                    max_workers=args.threads
                )

                end_time = datetime.now()
                duration = end_time - start_time

                if verbose:
                    print(f"\n{'='*70}")
                    print(f"Shift Chart Collection Summary")
                    print(f"{'='*70}")
                    print(f"  Total Games: {stats['total']}")
                    print(f"  Successful: {stats['success']}")
                    print(f"  Failed: {stats['failed']}")
                    print(f"  Duration: {duration}")
                    print(f"  Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"{'='*70}\n")

    except KeyboardInterrupt:
        print("\n\n⚠ Collection interrupted by user")
        sys.exit(1)

    except Exception as e:
        print(f"\n✗ Error during collection: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
