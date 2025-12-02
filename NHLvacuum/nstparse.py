"""
Natural Stat Trick (NST) Game Data Scraper
===========================================
Scrapes detailed NHL game data from naturalstattrick.com
Saves ~400-450 CSV files per game split between both teams (~200-225 per team)
"""

import requests
import time
from datetime import datetime
from bs4 import BeautifulSoup
import pandas as pd
import os
from urllib.parse import urljoin
import random
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import sqlite3
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ============================================================================
# CONFIGURATION AND CONSTANTS
# ============================================================================

# NHL Teams - Include 'ARI' for Coyotes historical data
nhl_teams = [
    'ANA', 'ARI', 'BOS', 'BUF', 'CGY', 'CAR', 'CHI', 'COL', 'CBJ', 'DAL', 'DET', 'EDM',
    'FLA', 'L.A', 'MIN', 'MTL', 'NSH', 'N.J', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'S.J',
    'SEA', 'STL', 'T.B', 'TOR', 'UTA', 'VGK', 'WSH', 'WPG', 'VAN'
]

# Mapping of full team names to abbreviations
# Used for converting team names from game reports to team codes
TEAM_NAME_MAP = {
    'Ducks': 'ANA', 'Anaheim': 'ANA',
    'Coyotes': 'ARI', 'Arizona': 'ARI',  # Arizona Coyotes (moved to Utah in 2024)
    'Bruins': 'BOS', 'Boston': 'BOS',
    'Sabres': 'BUF', 'Buffalo': 'BUF',
    'Flames': 'CGY', 'Calgary': 'CGY',
    'Hurricanes': 'CAR', 'Carolina': 'CAR',
    'Blackhawks': 'CHI', 'Chicago': 'CHI',
    'Avalanche': 'COL', 'Colorado': 'COL',
    'Blue Jackets': 'CBJ', 'Columbus': 'CBJ',
    'Stars': 'DAL', 'Dallas': 'DAL',
    'Red Wings': 'DET', 'Detroit': 'DET',
    'Oilers': 'EDM', 'Edmonton': 'EDM',
    'Panthers': 'FLA', 'Florida': 'FLA',
    'Kings': 'L.A', 'Los Angeles': 'L.A',
    'Wild': 'MIN', 'Minnesota': 'MIN',
    'Canadiens': 'MTL', 'Montreal': 'MTL', 'Montréal': 'MTL',
    'Predators': 'NSH', 'Nashville': 'NSH',
    'Devils': 'N.J', 'New Jersey': 'N.J',
    'Islanders': 'NYI', 'NY Islanders': 'NYI',
    'Rangers': 'NYR', 'NY Rangers': 'NYR',
    'Senators': 'OTT', 'Ottawa': 'OTT',
    'Flyers': 'PHI', 'Philadelphia': 'PHI',
    'Penguins': 'PIT', 'Pittsburgh': 'PIT',
    'Sharks': 'S.J', 'San Jose': 'S.J',
    'Kraken': 'SEA', 'Seattle': 'SEA',
    'Blues': 'STL', 'St. Louis': 'STL', 'St Louis': 'STL',
    'Lightning': 'T.B', 'Tampa Bay': 'T.B',
    'Maple Leafs': 'TOR', 'Toronto': 'TOR',
    # Utah team names - all variations map to UTA
    'Mammoth': 'UTA', 'Utah': 'UTA', 'Utah HC': 'UTA', 'HC': 'UTA',
    'Golden Knights': 'VGK', 'Vegas': 'VGK',
    'Capitals': 'WSH', 'Washington': 'WSH',
    'Jets': 'WPG', 'Winnipeg': 'WPG',
    'Canucks': 'VAN', 'Vancouver': 'VAN'
}


# Site paths to exclude from scraping
site_directory_paths = [
    "/playerreport.php",
    "/gameflow.php",
    "/dl.php",
    "/hm.php",
    "/game.php",
    "/linestats.php?",
    "/graphs/",
    "/heatmaps/",
    "/images/",
    "/DataTables-1.10.3/"
]

# Sections to scrape (each generates multiple files per game)
ESSENTIAL_SECTIONS = {
    'overview': True,           # Game summary stats
    'power_plays': True,        # Power play stats
    'individual': True,         # Base player stats
    'on_ice': True,            # On ice stats
    'shift_report': True,      # Shift data
    'forward_lines': True,     # Forward line combinations
    'individual_event_maps': False,  # Event maps (usually images, not tables)
    'linemates': True,         # Player linemates stats (many files per player)
    'opposition': True,        # Player opposition stats (many files per player)
}

# ============================================================================
# HTTP SESSION AND RATE LIMITING
# ============================================================================

def should_include_section(section_name):
    """Determine if a section should be scraped based on configuration"""
    section_lower = section_name.lower()

    # Skip these first (most specific patterns)
    if 'linemate' in section_lower:
        return ESSENTIAL_SECTIONS['linemates']
    elif 'opposition' in section_lower:
        return ESSENTIAL_SECTIONS['opposition']
    elif 'event map' in section_lower:
        return ESSENTIAL_SECTIONS['individual_event_maps']

    # Include these (broader patterns)
    elif 'overview' in section_lower or 'gameflow' in section_lower or 'heatmap' in section_lower or 'shift chart' in section_lower:
        return ESSENTIAL_SECTIONS['overview']
    elif 'power play' in section_lower:
        return ESSENTIAL_SECTIONS['power_plays']
    elif 'individual' in section_lower:
        return ESSENTIAL_SECTIONS['individual']
    elif 'on ice' in section_lower:
        return ESSENTIAL_SECTIONS['on_ice']
    elif 'shift report' in section_lower:
        return ESSENTIAL_SECTIONS['shift_report']
    elif 'forward line' in section_lower:
        return ESSENTIAL_SECTIONS['forward_lines']

    # Default: include if we don't recognize it (safer)
    return True

# Pool of realistic User-Agents (recent browsers, common OS versions)
USER_AGENTS = [
    # Chrome on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    # Chrome on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    # Firefox on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0',
    # Firefox on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0',
    # Safari on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
    # Edge on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
]

# Create a session with retry strategy and proper headers
def create_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.5,  # Increased from 0.3 to be more respectful
        status_forcelist=(500, 502, 504, 429)  # Added 429 (rate limit)
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    # Randomize User-Agent from pool of realistic browsers
    user_agent = random.choice(USER_AGENTS)

    # Use comprehensive browser-like headers
    session.headers.update({
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"'
    })

    return session

# Thread-safe file writing
_write_lock = threading.Lock()
_write_executor = None
_cache_lock = threading.Lock()  # Global lock for game cache access

# Thread-local storage for sessions (each thread gets its own session)
_thread_local = threading.local()

def get_thread_session():
    """Get or create a session for the current thread"""
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = create_session()
    return _thread_local.session

def get_write_executor(max_workers=8):
    """Get or create the global write executor"""
    global _write_executor
    if _write_executor is None:
        _write_executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='csv_writer')
    return _write_executor

def write_csv_threadsafe(df, file_path):
    """Thread-safe CSV writing function"""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    df.to_csv(file_path, index=False)

def save_table_data(table_data, game_file_prefix, section_name, situation, table_index,
                    table_list_length, section_label, team_abbr, season_folder,
                    use_threading, executor, write_futures):
    """
    Helper function to save table data (either dict or DataFrame) to appropriate directories.
    Consolidates duplicate save logic from cache and download paths.

    Returns: number of tables saved
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"
    tables_saved = 0

    if isinstance(table_data, dict):
        # Overview section with multiple teams - save each team's data to their directory
        for team_name, team_df in table_data.items():
            team_name_abbr = extract_team_abbr_from_name(team_name, team_abbr)
            if not team_name_abbr:
                continue

            team_specific_games_path = os.path.join(base_folder_path, team_name_abbr, games_folder, season_folder)
            os.makedirs(team_specific_games_path, exist_ok=True)

            # Sanitize situation name (remove spaces from player names)
            safe_situation = situation.replace(' ', '')

            # Extract section type for filename (e.g., "linemates" from "Utah_HC_Linemates")
            section_for_filename = section_name.lower()
            if 'linemates' in section_for_filename:
                section_for_filename = 'linemates'
            elif 'opposition' in section_for_filename:
                section_for_filename = 'opposition'

            # Build filename
            if table_list_length > 1:
                file_name = f"{game_file_prefix}_{team_name_abbr}_{section_for_filename}_{safe_situation}_{table_index+1}.csv"
            else:
                file_name = f"{game_file_prefix}_{team_name_abbr}_{section_for_filename}_{safe_situation}.csv"
            file_path = os.path.join(team_specific_games_path, file_name)

            # Write CSV
            if use_threading and executor:
                write_futures.append(executor.submit(write_csv_threadsafe, team_df, file_path))
            else:
                team_df.to_csv(file_path, index=False)
            tables_saved += 1
    else:
        # Regular table (single DataFrame) - extract team from section label
        section_team_abbr = extract_team_from_section_label(section_label)

        # Determine save directory
        if section_team_abbr:
            save_team_games_path = os.path.join(base_folder_path, section_team_abbr, games_folder, season_folder)
        else:
            save_team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)
        os.makedirs(save_team_games_path, exist_ok=True)

        # Sanitize situation name (remove spaces from player names)
        safe_situation = situation.replace(' ', '')

        # Extract section type for filename (e.g., "linemates" from "Utah_HC_Linemates")
        section_for_filename = section_name.lower()
        if 'linemates' in section_for_filename:
            section_for_filename = 'linemates'
        elif 'opposition' in section_for_filename:
            section_for_filename = 'opposition'

        # Build filename
        if table_list_length > 1:
            file_name = f"{game_file_prefix}_{section_for_filename}_{safe_situation}_{table_index+1}.csv"
        else:
            file_name = f"{game_file_prefix}_{section_for_filename}_{safe_situation}.csv"
        file_path = os.path.join(save_team_games_path, file_name)

        # Write CSV
        if use_threading and executor:
            write_futures.append(executor.submit(write_csv_threadsafe, table_data, file_path))
        else:
            table_data.to_csv(file_path, index=False)
        tables_saved += 1

    return tables_saved

# Request rate limiting with adaptive backoff
_last_request_time = 0
_request_lock = threading.Lock()
_consecutive_429s = 0  # Track rate limit responses
_backoff_multiplier = 1.0  # Dynamic backoff multiplier

def respectful_delay(is_429=False):
    """
    Implements a delay between requests to be respectful to the server.
    Uses a global rate limiter with adaptive backoff for rate limit responses.
    With parallel scraping, only actual HTTP requests need delays.

    Args:
        is_429: True if the last request got a 429 response (rate limited)
    """
    global _last_request_time, _consecutive_429s, _backoff_multiplier

    # Base delays: configurable based on DELAY_SECONDS global
    try:
        from __main__ import DELAY_SECONDS
        min_delay = float(DELAY_SECONDS)
        # Scale jitter proportionally (3-10% of base delay)
        max_jitter = max(0.3, min(min_delay * 0.1, 10.0))
    except (ImportError, AttributeError):
        # Fallback if called outside main (conservative default)
        min_delay = 15.0
        max_jitter = 1.0

    with _request_lock:
        # Adaptive backoff for 429 responses
        if is_429:
            _consecutive_429s += 1
            # Exponential backoff: 2x, 4x, 8x, etc. (capped at 16x)
            _backoff_multiplier = min(2.0 ** _consecutive_429s, 16.0)
            print(f"    ⚠ Rate limited! Backing off {_backoff_multiplier}x (consecutive 429s: {_consecutive_429s})")
        else:
            # Gradually reduce backoff when successful
            if _consecutive_429s > 0:
                _consecutive_429s = max(0, _consecutive_429s - 1)
                _backoff_multiplier = max(1.0, _backoff_multiplier * 0.5)

        # Calculate delay with backoff and jitter
        jitter = random.uniform(0, max_jitter)
        effective_delay = (min_delay * _backoff_multiplier) + jitter

        current_time = time.time()
        elapsed = current_time - _last_request_time

        if elapsed < effective_delay:
            sleep_time = effective_delay - elapsed
            time.sleep(sleep_time)

        _last_request_time = time.time()

# ============================================================================
# LEGACY/OPTIONAL SCRAPERS (Controlled by config flags)
# ============================================================================

def get_static_tables(team_abbr, date_folder, session):
    url = f"https://www.naturalstattrick.com/teamreport.php?team={team_abbr}"
    base_folder_path = "nhlteamreports"
    general_data_folder = "generalTRdata"
    team_folder_path = os.path.join(base_folder_path, team_abbr, general_data_folder, date_folder)

    try:
        # Add Referer header to appear like normal browsing
        headers = {'Referer': 'https://www.naturalstattrick.com/'}

        print(f"Fetching data for {team_abbr}...")
        response = session.get(url, headers=headers, timeout=30)

        # Check for IP ban or rate limiting
        if response.status_code == 403:
            print(f"✗ IP BANNED or ACCESS FORBIDDEN - Status 403")
            print(f"  Your IP may be temporarily blocked. Wait 24 hours or use a different IP.")
            respectful_delay(is_429=True)  # Trigger backoff even for 403
            raise Exception("IP banned (403)")
        elif response.status_code == 429:
            print(f"✗ RATE LIMITED - Status 429")
            print(f"  Too many requests. Backing off...")
            respectful_delay(is_429=True)  # Trigger adaptive backoff
            raise Exception("Rate limited (429)")

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        tables = soup.find_all("table")

        os.makedirs(team_folder_path, exist_ok=True)

        for k, table in enumerate(tables):
            df = parse_table_fast(table)
            file_name = f"{team_abbr}_static_table_{k+1}.csv"
            file_path = os.path.join(team_folder_path, file_name)
            df.to_csv(file_path, index=False)

        # Be respectful - add delay after successful request
        respectful_delay(is_429=False)

    except requests.exceptions.RequestException as e:
        print(f"✗ Error accessing data for {team_abbr}: {e}")
    except Exception as e:
        print(f"✗ Error processing data for {team_abbr}: {e}")

def allofit(date_folder, session):
    print(f"\n=== Starting data collection for all {len(nhl_teams)} teams ===\n")
    for i, team in enumerate(nhl_teams, 1):
        print(f"[{i}/{len(nhl_teams)}] Processing {team}")
        get_static_tables(team, date_folder, session)
    print("\n=== Team data collection complete ===\n")

def download_file(team_abbr, link, base_url, date_folder, session):
    directory_path = os.path.join('nhlteamreports', team_abbr, 'generalTRdata', date_folder, 'rollingavggraphs')

    if not os.path.exists(directory_path):
        os.makedirs(directory_path)

    full_url = urljoin(base_url, link['href'])

    try:
        with session.get(full_url, stream=True, timeout=30) as file_response:
            file_response.raise_for_status()
            file_path = os.path.join(directory_path, link['href'])

            with open(file_path, 'wb') as f:
                for chunk in file_response.iter_content(chunk_size=8192):
                    f.write(chunk)

    except Exception as e:
        print(f"  ✗ Failed to download {link['href']} for {team_abbr}: {e}")

def download_charts(base_url, date_folder, session):
    print("\n=== Starting chart downloads ===\n")

    try:
        response = session.get(base_url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')
        links = [link for link in soup.find_all('a', href=True) if link['href'].endswith('.png')]

        print(f"Found {len(links)} PNG files to download")

        for i, link in enumerate(links, 1):
            parts = link['href'].split('-')
            if len(parts) < 2 or not parts[1].isupper():
                continue

            team_abbr = parts[1]
            print(f"[{i}/{len(links)}] Downloading chart for {team_abbr}")
            download_file(team_abbr, link, base_url, date_folder, session)

        print("\n=== Chart downloads complete ===\n")

    except Exception as e:
        print(f"✗ Error downloading charts: {e}")

# ============================================================================
# GAME LIST MANAGEMENT
# ============================================================================

def get_games_list(team_abbr, season_folder, session, fromseason=None, thruseason=None, stype=2):
    """
    Scrape the list of games for a team and save the game IDs for later processing
    Games are automatically sorted into correct season folders based on their 'season' field

    Args:
        team_abbr: Team abbreviation (e.g., 'BOS')
        season_folder: Default folder name (unused now - games are sorted by actual season)
        session: HTTP session
        fromseason: Starting season in format YYYYYYYY (e.g., 20242025 for 2024-25 season)
        thruseason: Ending season in format YYYYYYYY (e.g., 20252026 for 2025-26 season)
        stype: Season type (2=regular season, 3=playoffs)
    """
    # Build URL with season parameters if provided
    if fromseason and thruseason:
        url = f"https://www.naturalstattrick.com/games.php?fromseason={fromseason}&thruseason={thruseason}&stype={stype}&sit=all&loc=B&team={team_abbr}&rate=n"
    else:
        url = f"https://www.naturalstattrick.com/games.php?team={team_abbr}"

    base_folder_path = "nhlteamreports"
    games_folder = "games"

    try:
        # Add Referer to look like normal navigation
        headers = {'Referer': 'https://www.naturalstattrick.com/'}

        print(f"  Fetching games list for {team_abbr}...")
        response = session.get(url, headers=headers, timeout=30)

        # Handle rate limiting
        if response.status_code == 429:
            print(f"  ⚠ Rate limited on games list. Backing off...")
            respectful_delay(is_429=True)
            return []
        elif response.status_code == 403:
            print(f"  ⚠ Access forbidden (403). IP may be blocked.")
            respectful_delay(is_429=True)
            return []

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')

        # Find the games table
        tables = soup.find_all("table")
        if not tables:
            return []

        # Parse the table
        games_data = []
        table = tables[0]  # Assuming first table contains games
        rows = table.find_all('tr')[1:]  # Skip header row

        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 3:
                continue

            # Extract game info
            game_info = {}

            # First cell contains date and score (e.g., "2025-10-07 - Blackhawks 2, Panthers 3")
            game_title = cells[0].get_text(strip=True)
            game_info['title'] = game_title

            # Parse the title to extract teams and create vs naming
            if game_title and ' - ' in game_title:
                date_part, score_part = game_title.split(' - ', 1)
                game_info['date'] = date_part

                # Parse score part to get both teams
                # Format is typically "Team1 Score1, Team2 Score2"
                if ',' in score_part:
                    teams_scores = score_part.split(',')
                    if len(teams_scores) == 2:
                        # Extract team names (remove scores)
                        team1_parts = teams_scores[0].strip().rsplit(' ', 1)
                        team2_parts = teams_scores[1].strip().rsplit(' ', 1)

                        if len(team1_parts) > 1 and len(team2_parts) > 1:
                            team1_name = team1_parts[0]
                            team2_name = team2_parts[0]

                            # Convert full team names to abbreviations if possible
                            game_info['team1_full'] = team1_name
                            game_info['team2_full'] = team2_name

            # Team name from second cell
            game_info['team'] = cells[1].get_text(strip=True) if len(cells) > 1 else ''

            # Extract report links (only Full Report)
            report_links = cells[2].find_all('a') if len(cells) > 2 else []
            for link in report_links:
                href = link.get('href', '')
                # Only process Full Report links, skip Limited
                if 'Full' in link.get_text():
                    game_info['full_report_url'] = f"https://www.naturalstattrick.com/{href}"

                    # Extract game ID and season from URL parameters
                    if 'game.php' in href:
                        params = href.split('?')[1] if '?' in href else ''
                        for param in params.split('&'):
                            if '=' in param:
                                key, value = param.split('=')
                                if key == 'game':
                                    game_info['game_id'] = value
                                elif key == 'season':
                                    game_info['season'] = value

            # Store remaining stats from other cells
            if len(cells) > 3:
                game_info['stats'] = [cell.get_text(strip=True) for cell in cells[3:]]

            # Initialize missing_data flag (0 = has data, 1 = missing/incomplete data)
            game_info['missing_data'] = 0
            # Initialize connection_timeout flag (0 = no timeout, 1 = connection timeout/403/404)
            game_info['connection_timeout'] = 0

            games_data.append(game_info)

        # Group games by their actual season and save to appropriate folders
        if games_data:
            # Group games by season
            games_by_season = {}
            for game in games_data:
                game_season = game.get('season', '')
                if not game_season:
                    print(f"    ⚠ Warning: Game {game.get('game_id', 'unknown')} has no season field, skipping")
                    continue

                # Convert to string if it's an int (happens when loading from CSV)
                if isinstance(game_season, int):
                    game_season = str(game_season)

                # Convert season format from YYYYYYYY to YYYY-YY
                # e.g., 20242025 -> 2024-25
                if len(str(game_season)) == 8:
                    game_season_str = str(game_season)
                    start_year = game_season_str[:4]
                    end_year = game_season_str[6:8]
                    folder_name = f"{start_year}-{end_year}"
                else:
                    print(f"    ⚠ Warning: Invalid season format '{game_season}' for game {game.get('game_id', 'unknown')}")
                    continue

                if folder_name not in games_by_season:
                    games_by_season[folder_name] = []
                games_by_season[folder_name].append(game)

            # Save games to their respective season folders
            total_new_games = 0
            for folder_name, season_games in games_by_season.items():
                team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, folder_name)
                os.makedirs(team_games_path, exist_ok=True)
                games_list_file = os.path.join(team_games_path, f"{team_abbr}_games_list.csv")

                # Load existing games if file exists
                if os.path.exists(games_list_file):
                    try:
                        existing_df = pd.read_csv(games_list_file)
                        existing_game_ids = set(existing_df['game_id'].astype(str))

                        # Filter out games that already exist
                        new_games = [g for g in season_games if str(g.get('game_id', '')) not in existing_game_ids]

                        if new_games:
                            # Append new games to existing CSV
                            new_games_df = pd.DataFrame(new_games)
                            combined_df = pd.concat([existing_df, new_games_df], ignore_index=True)
                            combined_df.to_csv(games_list_file, index=False)
                            print(f"    ✓ Added {len(new_games)} new games to {team_abbr} {folder_name}")
                            total_new_games += len(new_games)
                    except Exception as e:
                        print(f"    ⚠ Error reading existing games list for {folder_name}, overwriting: {e}")
                        games_df = pd.DataFrame(season_games)
                        games_df.to_csv(games_list_file, index=False)
                        total_new_games += len(season_games)
                else:
                    # No existing file, create new one
                    games_df = pd.DataFrame(season_games)
                    games_df.to_csv(games_list_file, index=False)
                    print(f"    ✓ Created {folder_name} games list with {len(season_games)} games for {team_abbr}")
                    total_new_games += len(season_games)

            if total_new_games == 0:
                print(f"    ℹ No new games to add for {team_abbr}")

        respectful_delay(is_429=False)
        return games_data

    except Exception as e:
        print(f"  ✗ Error fetching games for {team_abbr}: {e}")
        return []


def extract_team_abbr_from_name(team_full_name, current_team_abbr):
    """Extract team abbreviation from full team name using TEAM_NAME_MAP"""
    # Check each part of the team name
    for part in team_full_name.split():
        if part in TEAM_NAME_MAP:
            return TEAM_NAME_MAP[part]

    # Check full name
    if team_full_name in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[team_full_name]

    # If we can't determine, return None
    return None


# ============================================================================
# HTML PARSING (Core parsing logic - DO NOT MODIFY)
# ============================================================================

def parse_table_fast(table):
    """
    Fast table parsing using direct lxml - 100-2000x faster than pd.read_html()

    Uses recursive=False to avoid nested tables and only get direct child elements.
    Returns a pandas DataFrame.
    """
    rows = []

    # Get headers - use recursive=False to avoid nested tables
    thead = table.find('thead')
    if thead:
        header_row = thead.find('tr', recursive=False) or thead.find('tr')
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(['th', 'td'], recursive=False)]
        else:
            headers = None
    else:
        # No thead, check if first row is header
        first_row = table.find('tr', recursive=False) or table.find('tr')
        if first_row and first_row.find('th', recursive=False):
            headers = [th.get_text(strip=True) for th in first_row.find_all(['th', 'td'], recursive=False)]
        else:
            headers = None

    # Get data rows - CRITICAL: use recursive=False to avoid nested tables
    tbody = table.find('tbody')
    if tbody:
        data_rows = tbody.find_all('tr', recursive=False)
    else:
        # No tbody, get all rows except first if it was header
        all_rows = table.find_all('tr', recursive=False)
        if headers and all_rows:
            data_rows = all_rows[1:]
        else:
            data_rows = all_rows

    # Parse data - use recursive=False to only get direct child cells
    for row in data_rows:
        cells = row.find_all(['td', 'th'], recursive=False)
        row_data = [cell.get_text(strip=True) for cell in cells]
        if row_data:  # Skip empty rows
            rows.append(row_data)

    # Create DataFrame
    if rows:
        if headers and len(headers) == len(rows[0]):
            return pd.DataFrame(rows, columns=headers)
        else:
            return pd.DataFrame(rows)
    else:
        return pd.DataFrame()

def sanitize_filename(name):
    """Convert section label to valid filename"""
    # Remove special characters and replace spaces with underscores
    name = name.replace(' - ', '_')
    name = name.replace(' ', '_')
    name = name.replace('/', '_')
    name = name.replace('\\', '_')
    return name

def extract_team_from_section_label(section_label):
    """
    Extract team name from section label (e.g., 'Penguins - On Ice' -> 'Penguins')
    Returns the team abbreviation if found, None otherwise.
    """
    # Section labels are typically formatted as "Team Name - Section Type"
    # e.g., "Penguins - On Ice", "Kraken - Individual", "Bruins - Forward Lines"

    if ' - ' in section_label:
        team_name = section_label.split(' - ')[0].strip()
        # Map team name to abbreviation using existing function
        team_abbr = extract_team_abbr_from_name(team_name, None)
        return team_abbr

    return None

def parse_table_with_multiheader(table):
    """
    Parse HTML table that may have multi-level headers or period-by-period data.
    Returns a pandas DataFrame with flattened column names.

    Handles special case where cells contain <div class="hall"> with <br/> tags
    representing multiple periods - these are expanded into separate rows.
    """
    try:
        # Check if this is a period-by-period table (Overview section)
        # These tables have cells with <div class="hall"> containing period data
        tbody = table.find('tbody')
        if tbody:
            rows = tbody.find_all('tr')
            if rows:
                # Check first data row for period-by-period structure
                first_row = rows[0]
                cells = first_row.find_all('td')

                # Look for the characteristic <div class="hall"> structure
                has_period_divs = False
                for cell in cells:
                    hall_div = cell.find('div', class_='hall')
                    if hall_div:
                        has_period_divs = True
                        break

                if has_period_divs:
                    # Custom parsing for period-by-period tables
                    return parse_period_by_period_table(table)

        # Standard lxml parsing for regular tables (100x faster than pd.read_html)
        df = parse_table_fast(table)
        if df is None or df.empty:
            return None

        # Check if we have multi-level columns (tuples)
        if isinstance(df.columns[0], tuple):
            # Flatten multi-level column names
            df.columns = ['_'.join(str(i) for i in col if str(i) != 'Unnamed: 0_level_0').strip('_')
                         for col in df.columns]

        return df
    except Exception as e:
        print(f"        ⚠ Warning: Failed to parse table: {e}")
        return None

def extract_cell_values(cell):
    """
    Extract multiple values from a cell separated by <br> and <hr> tags.

    Example:
    <td>20:00<br>20:00<br>20:00<br><hr>60:00</td>
    Returns: ['20:00', '20:00', '20:00', '60:00']
    """
    # Get HTML string
    cell_html = str(cell)

    # Replace <br> tags with newlines
    import re
    cell_html = re.sub(r'<br\s*/?>', '\n', cell_html, flags=re.IGNORECASE)

    # Replace <hr> with newline
    cell_html = re.sub(r'<hr\s*/?>', '\n', cell_html, flags=re.IGNORECASE)

    # Parse to extract text
    from bs4 import BeautifulSoup
    temp_soup = BeautifulSoup(cell_html, 'lxml')
    cell_text = temp_soup.get_text()

    # Split by newlines and filter
    values = [v.strip() for v in cell_text.split('\n') if v.strip()]

    return values

def parse_period_by_period_table(table):
    """
    Parse NST overview tables where cells contain period-by-period data split by <br/> tags.
    Expands these into separate rows for each period and splits by team.

    Returns a dictionary: {team_name: DataFrame} for Overview tables with multiple teams.

    Structure:
    - Header row with column names
    - Data rows: one per team
    - Each cell (except first) contains period-by-period data:
      Value1<br>Value2<br>Value3<br><hr>FinalValue

    Example HTML structure (Overview section):
    Row 1:
      <td>Kings</td>
      <td><div class="hall">1<br>2<br>3<br><hr></div>Final</td>
      <td>20:00<br>20:00<br>20:00<br><hr>60:00</td>
      ... (more cells with same pattern)

    This becomes separate DataFrames for each team with rows:
      Period 1, Period 2, Period 3, Final (with corresponding values from each cell)
    """
    # Get headers
    thead = table.find('thead')
    if not thead:
        return None

    header_row = thead.find('tr')
    if not header_row:
        return None

    headers = [th.get_text(strip=True) for th in header_row.find_all('th')]

    # Get data rows
    tbody = table.find('tbody')
    if not tbody:
        return None

    data_rows = tbody.find_all('tr', recursive=False)

    teams_data = {}

    for row in data_rows:
        cells = row.find_all('td', recursive=False)
        if not cells:
            continue

        # First cell is team name
        team_name = cells[0].get_text(strip=True)
        if not team_name:
            continue

        # Extract period labels from the Period column (second cell)
        period_cell = cells[1]
        period_labels = []

        # Check for hall div (contains period numbers)
        hall_div = period_cell.find('div', class_='hall')
        if hall_div:
            # Get text content and split
            hall_text = hall_div.get_text(separator='\n', strip=True)
            period_labels = [p.strip() for p in hall_text.split('\n') if p.strip() and p.strip() != '']

            # Get "Final" text after hall div
            final_text = ''
            for sibling in hall_div.next_siblings:
                if sibling.name is None:  # Text node
                    text = str(sibling).strip()
                    if text:
                        final_text = text
                        break

            if final_text:
                period_labels.append(final_text)
        else:
            # No hall div - extract from cell directly
            cell_text = period_cell.get_text(separator='\n', strip=True)
            period_labels = [p.strip() for p in cell_text.split('\n') if p.strip() and p.strip() != '']

        num_periods = len(period_labels)
        if num_periods == 0:
            continue

        # Initialize list to hold rows for this team
        team_rows = []

        # For each period, extract values from all columns
        for period_idx in range(num_periods):
            row_data = {}

            # Add Period column
            row_data['Period'] = period_labels[period_idx]

            # Process each data column
            for col_idx in range(2, len(cells)):  # Skip team name and Period
                if col_idx - 1 >= len(headers):  # Safety check
                    break

                header = headers[col_idx]
                cell = cells[col_idx]

                # Extract values from cell using helper function
                cell_values = extract_cell_values(cell)

                # Get value for this period
                if period_idx < len(cell_values):
                    row_data[header] = cell_values[period_idx]
                else:
                    row_data[header] = ''

            team_rows.append(row_data)

        # Convert to DataFrame
        df = pd.DataFrame(team_rows)
        teams_data[team_name] = df

    return teams_data if teams_data else None

# Mapping of situation classes to readable names
SITUATION_NAMES = {
    'tall': 'All',
    'tev': 'EV',
    't5v5': '5v5',
    'tsva': 'SVA',  # Score and Venue Adjusted 5v5
    't5v4': '5v4',
    't4v5': '4v5',
    'tpp': 'PP',
    'tpk': 'PK',
    't5v3': '5v3',
    't3v5': '3v5',
    't4v4': '4v4',
    't3v3': '3v3',
}

def extract_table_sections(soup):
    """
    Extract tables grouped by their section labels and situations.
    Returns a dict mapping section names to situation-grouped tables.
    Only includes sections based on ESSENTIAL_SECTIONS configuration.

    Handles nested tables properly - extracts the detailed period-by-period tables.
    """
    sections = {}

    # Find all section labels
    labels = soup.find_all('label', class_='section')

    for label in labels:
        label_text = label.get_text(strip=True)

        # Check if we should include this section
        if not should_include_section(label_text):
            continue

        sanitized_name = sanitize_filename(label_text)

        # Find the parent content div
        parent = label.find_parent('div', class_='content')

        if parent:
            # Check if this is a player-specific section (linemates/opposition)
            is_player_section = 'linemate' in label_text.lower() or 'opposition' in label_text.lower()
            player_name_map = {}

            if is_player_section:
                # Extract player names from label elements
                # Labels have for="slUTA9" (linemates) or for="soUTA9" (opposition)
                player_labels = parent.find_all('label', attrs={'for': lambda x: x and (x.startswith('sl') or x.startswith('so'))})

                for player_label in player_labels:
                    label_for = player_label.get('for', '')
                    if len(label_for) > 2:
                        # Remove 'sl' or 'so' prefix to get team+jersey (e.g., 'UTA9')
                        player_key = label_for[2:]
                        player_name = player_label.get_text(strip=True).replace('\xa0', ' ')
                        if player_name:
                            player_name_map[player_key] = player_name

            # Find all datadiv containers (each represents a different situation)
            datadivs = parent.find_all('div', class_='datadiv')

            # Group tables by situation
            situation_tables = {}

            for datadiv in datadivs:
                # Extract the situation class (e.g., 'tall', 'tev', 't5v5')
                classes = datadiv.get('class', [])
                situation_class = [c for c in classes if c != 'datadiv']

                if situation_class:
                    situation = situation_class[0]
                    readable_situation = SITUATION_NAMES.get(situation, situation)

                    # Get all tables within this situation div
                    all_tables = datadiv.find_all('table')

                    # For player sections, create compound keys: PlayerName_Situation
                    if is_player_section and situation in player_name_map:
                        player_name = player_name_map[situation]

                        # Extract actual situation from table ID
                        table_situation = None
                        for table in all_tables:
                            table_id = table.get('id', '')
                            if table_id:
                                # Extract suffix from table ID (e.g., 'tlUTA92a' -> 'a')
                                import re
                                match = re.search(r'[a-z]+$', table_id)
                                if match:
                                    suffix = match.group()
                                    # Map suffix to situation name
                                    suffix_map = {
                                        'a': 'All',
                                        'e': 'EV',
                                        '5': '5v5',
                                        's': '5v5',
                                        'pp': 'PP',
                                        'pk': 'PK'
                                    }
                                    table_situation = suffix_map.get(suffix, suffix)
                                    break

                        if table_situation:
                            readable_situation = f"{player_name}_{table_situation}"

                    # Filter to get only the actual data tables (not wrapper/empty tables)
                    # Real data tables have an 'id' attribute (e.g., id="tbtsall", id="tbstall")
                    # Wrapper tables and empty tables don't have IDs
                    data_tables = []

                    for table in all_tables:
                        table_id = table.get('id', '')
                        # Only include tables with IDs (these are the real data tables)
                        if table_id:
                            # Special filtering for Overview section to avoid getting 484 mixed tables
                            if label_text == 'Overview':
                                # Only include period-by-period tables (starting with 'tbts')
                                # AND matching the current situation
                                if table_id.startswith('tbts'):
                                    # Match table ID to situation: tbtsall -> tall, tbts5v5 -> t5v5
                                    table_situation = table_id[4:]  # Remove 'tbts' prefix
                                    if table_situation == situation or table_situation == situation.lstrip('t'):
                                        data_tables.append(table)
                            else:
                                data_tables.append(table)

                    if data_tables:
                        situation_tables[readable_situation] = data_tables

            if situation_tables:
                sections[sanitized_name] = {
                    'label': label_text,
                    'situations': situation_tables
                }

    return sections

# OLD GAME-SKIPPING LOGIC REMOVED
# Functions is_corrupted_game() and game_already_exists() removed
# These will be replaced with new robust logic

# ============================================================================
# GAME SCRAPING
# ============================================================================

def scrape_game_report(full_report_url, game, team_abbr, season_folder, session, game_cache, use_threading=False, max_workers=8):
    """
    FINAL FIXED VERSION – drop-in replacement
    - Full Overview (all periods + Final) for BOTH teams
    - Linemates/Opposition saved with player name
    - File writes are fully parallel (no blocking .result() per game)
    """
    game_id = game.get('game_id')
    title = game.get('title', 'Unknown Game')

    # Build game prefix and detect opponent FIRST
    team1_full = game.get('team1_full', '')
    team2_full = game.get('team2_full', '')
    team1_abbr = extract_team_abbr_from_name(team1_full, team_abbr)
    team2_abbr = extract_team_abbr_from_name(team2_full, team_abbr)
    opponent_abbr = team2_abbr if team1_abbr == team_abbr else team1_abbr
    teams_sorted = sorted([team_abbr, opponent_abbr]) if opponent_abbr else [team_abbr, 'OPP']
    game_file_prefix = f"{teams_sorted[0]}vs{teams_sorted[1]}_{game_id}"


    print(f"      Scraping full report: {title} | {game_id}")

    # Thread-safe cache check/add
    # Check if already cached first (read-only, no lock needed)
    if game_id in game_cache:
        soup = game_cache[game_id]
    else:
        # Not in cache - need to download
        # Delay BEFORE acquiring lock so threads don't wait for each other's delays
        respectful_delay(is_429=False)

        # Now acquire lock to check again and potentially download
        with _cache_lock:
            # Double-check in case another thread downloaded while we waited
            if game_id in game_cache:
                soup = game_cache[game_id]
            else:
                # Download with proper headers
                try:
                    # Add Referer to mimic clicking from game list
                    headers = {'Referer': 'https://www.naturalstattrick.com/games.php'}
                    response = session.get(full_report_url, headers=headers, timeout=30)

                    if response.status_code == 403:
                        print("      ✗ IP blocked (403). Stopping.")
                        respectful_delay(is_429=True)
                        # Mark as connection timeout and return special code
                        mark_game_connection_timeout(team_abbr, season_folder, game_id)
                        return -1  # Return -1 to indicate connection timeout
                    elif response.status_code == 404:
                        print("      ✗ Game not found (404).")
                        mark_game_connection_timeout(team_abbr, season_folder, game_id)
                        return -1  # Return -1 to indicate connection timeout
                    elif response.status_code == 429:
                        print("      ✗ Rate limited (429). Backing off.")
                        respectful_delay(is_429=True)
                        mark_game_connection_timeout(team_abbr, season_folder, game_id)
                        return -1  # Return -1 to indicate connection timeout

                    response.raise_for_status()
                    soup = BeautifulSoup(response.content, 'lxml')
                    game_cache[game_id] = soup
                except Exception as e:
                    print(f"      ✗ Failed to fetch {full_report_url}: {e}")
                    # Check if it's a connection-related error
                    if '403' in str(e) or '404' in str(e) or '429' in str(e) or 'timeout' in str(e).lower():
                        mark_game_connection_timeout(team_abbr, season_folder, game_id)
                        return -1  # Return -1 to indicate connection timeout
                    return 0

    tables_saved = 0
    executor = get_write_executor(max_workers) if use_threading else None
    write_futures = []

    # ==============================================================
    # 1. OVERVIEW – Full Game + All Periods (both teams)
    # ==============================================================
    if should_include_section('overview'):
        # Find Overview section by label (not h2)
        overview_label = soup.find('label', {'id': 'overviewlb'})
        if overview_label:
            # Find parent content div
            content_div = overview_label.find_parent('div', class_='content')
            if content_div:
                # Find all datadiv containers (one per situation: All, EV, 5v5, PP, PK)
                # They are direct children of content_div, not nested in tb_div
                # Filter to only the Overview datadivs (first 6: tall, tev, t5v5, tsva, tpp, tpk)
                all_datadivs = content_div.find_all('div', class_='datadiv')

                # The first 6 datadivs are the Overview tables (one per situation)
                # The rest are nested tables we don't want
                datadivs = all_datadivs[:6] if len(all_datadivs) >= 6 else all_datadivs

                for datadiv in datadivs:
                    # Determine situation from div classes
                    classes = datadiv.get('class', [])
                    situation_class = [c for c in classes if c != 'datadiv']
                    if situation_class:
                        situation = SITUATION_NAMES.get(situation_class[0], situation_class[0])
                    else:
                        situation = "All"

                    # Find the correct Overview table by ID (tbts prefix)
                    # These tables have IDs like: tbtsall, tbts5v5, tbtsev, tbtspp, tbtspk
                    overview_table = None
                    for table in datadiv.find_all('table'):
                        table_id = table.get('id', '')
                        if table_id.startswith('tbts'):
                            overview_table = table
                            break

                    if overview_table:
                        # Parse the period-by-period table
                        parsed_data = parse_period_by_period_table(overview_table)

                        if parsed_data is None:
                            print(f"      ⚠ WARNING: parse_period_by_period_table returned None for game {game_id} situation {situation}")

                        if parsed_data:
                            # parse_period_by_period_table returns a dict {team_name: DataFrame}
                            if isinstance(parsed_data, dict):
                                # Multiple teams - save each team's data separately
                                for team_name, team_df in parsed_data.items():
                                    save_abbr = extract_team_abbr_from_name(team_name, team_abbr) or opponent_abbr or team_abbr

                                    # Add GameID column
                                    team_df.insert(0, 'GameID', game_id)

                                    file_name = f"{game_file_prefix}_{save_abbr}_overview_{situation}.csv"
                                    file_path = os.path.join("nhlteamreports", save_abbr, "games", season_folder, file_name)

                                    if use_threading and executor:
                                        write_futures.append(executor.submit(write_csv_threadsafe, team_df, file_path))
                                    else:
                                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                                        team_df.to_csv(file_path, index=False)
                                    tables_saved += 1
                            else:
                                # Single DataFrame (shouldn't happen for Overview, but handle it)
                                parsed_data.insert(0, 'GameID', game_id)

                                file_name = f"{game_file_prefix}_{team_abbr}_overview_{situation}.csv"
                                file_path = os.path.join("nhlteamreports", team_abbr, "games", season_folder, file_name)

                                if use_threading and executor:
                                    write_futures.append(executor.submit(write_csv_threadsafe, parsed_data, file_path))
                                else:
                                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                                    parsed_data.to_csv(file_path, index=False)
                                tables_saved += 1

    # ==============================================================
    # 2. ALL OTHER SECTIONS - using extract_table_sections()
    # ==============================================================
    # Extract all sections EXCEPT Overview (Individual, On Ice, Shift Report, Forward Lines, Linemates, Opposition, etc.)
    # Overview is already handled in Section 1 above
    sections = extract_table_sections(soup)

    for section_name, section_data in sections.items():
        # Skip Overview section - it's already processed in Section 1
        if 'overview' in section_name.lower():
            continue

        # Process each situation (All, EV, 5v5, PP, PK, etc.)
        for situation, table_list in section_data['situations'].items():
            for i, table in enumerate(table_list):
                # Parse table with fast lxml parser
                df = parse_table_fast(table)

                if df is not None and not df.empty:
                    # Add GameID column
                    df.insert(0, 'GameID', game_id)

                    # Save using helper function
                    tables_saved += save_table_data(
                        df, game_file_prefix, section_name, situation, i,
                        len(table_list), section_data['label'], team_abbr, season_folder,
                        use_threading, executor, write_futures
                    )

    # NO BLOCKING WAIT HERE — global shutdown in process_all_games() handles it
    print(f"      ✓ Saved {tables_saved} tables for {game_id}")
    return tables_saved


def load_existing_games_list(team_abbr, season_folder):
    """
    Load existing games list from CSV if it exists
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"
    team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)
    games_list_file = os.path.join(team_games_path, f"{team_abbr}_games_list.csv")

    if os.path.exists(games_list_file):
        try:
            df = pd.read_csv(games_list_file)
            # Convert DataFrame to list of dicts
            games_data = df.to_dict('records')
            print(f"  ✓ Loaded {len(games_data)} existing games for {team_abbr} {season_folder}")
            return games_data
        except Exception as e:
            print(f"  ✗ Error loading existing games list for {team_abbr} {season_folder}: {e}")
            return []
    else:
        return []

def load_all_existing_games(team_abbr):
    """
    Load existing games lists from ALL season folders for a team
    Returns a combined list of all games

    NOTE: This function is not currently used in the main scraping flow.
    Use load_existing_games_list(team_abbr, season_folder) instead to load
    games from a specific season only.
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"
    team_games_base = os.path.join(base_folder_path, team_abbr, games_folder)

    all_games = []

    if not os.path.exists(team_games_base):
        return []

    # Find all season folders
    season_folders = [d for d in os.listdir(team_games_base)
                     if os.path.isdir(os.path.join(team_games_base, d))]

    for season_folder in season_folders:
        games_data = load_existing_games_list(team_abbr, season_folder)
        all_games.extend(games_data)

    if all_games:
        print(f"  ✓ Loaded total of {len(all_games)} games across {len(season_folders)} seasons for {team_abbr}")

    return all_games

# ============================================================================
# GAME FILTERING AND DETECTION
# ============================================================================

def mark_game_missing_data(team_abbr, season_folder, game_id):
    """
    Mark a game as having missing/incomplete data in the team's games_list CSV.
    This prevents the scraper from repeatedly attempting to scrape games with no data.

    Args:
        team_abbr: Team abbreviation (e.g., 'ANA')
        season_folder: Season folder (e.g., '2023-24')
        game_id: Game ID to mark (e.g., '20100')
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"
    team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)
    games_list_file = os.path.join(team_games_path, f"{team_abbr}_games_list.csv")

    if not os.path.exists(games_list_file):
        print(f"      ⚠ Cannot mark game {game_id} as missing: games_list file not found for {team_abbr} {season_folder}")
        return

    try:
        df = pd.read_csv(games_list_file)

        # Add missing_data column if it doesn't exist
        if 'missing_data' not in df.columns:
            df.insert(df.columns.get_loc('stats') if 'stats' in df.columns else len(df.columns),
                     'missing_data', 0)

        # Mark the game as missing data
        game_id_str = str(game_id)
        mask = df['game_id'].astype(str) == game_id_str

        if mask.any():
            df.loc[mask, 'missing_data'] = 1
            df.to_csv(games_list_file, index=False)
            print(f"      ℹ Marked game {game_id} as missing data for {team_abbr}")
        else:
            print(f"      ⚠ Game {game_id} not found in {team_abbr} games_list")

    except Exception as e:
        print(f"      ✗ Error marking game {game_id} as missing data: {e}")


def mark_game_connection_timeout(team_abbr, season_folder, game_id):
    """
    Mark a game as having connection timeout (403/404) in the team's games_list CSV.
    This prevents the scraper from repeatedly attempting to scrape games that fail due to connection issues.

    Args:
        team_abbr: Team abbreviation (e.g., 'ANA')
        season_folder: Season folder (e.g., '2023-24')
        game_id: Game ID to mark (e.g., '20100')
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"
    team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)
    games_list_file = os.path.join(team_games_path, f"{team_abbr}_games_list.csv")

    if not os.path.exists(games_list_file):
        print(f"      ⚠ Cannot mark game {game_id} as connection timeout: games_list file not found for {team_abbr} {season_folder}")
        return

    try:
        df = pd.read_csv(games_list_file)

        # Add connection_timeout column if it doesn't exist
        if 'connection_timeout' not in df.columns:
            df.insert(df.columns.get_loc('stats') if 'stats' in df.columns else len(df.columns),
                     'connection_timeout', 0)

        # Mark the game as connection timeout
        game_id_str = str(game_id)
        mask = df['game_id'].astype(str) == game_id_str

        if mask.any():
            df.loc[mask, 'connection_timeout'] = 1
            df.to_csv(games_list_file, index=False)
            print(f"      ℹ Marked game {game_id} as connection timeout for {team_abbr}")
        else:
            print(f"      ⚠ Game {game_id} not found in {team_abbr} games_list")

    except Exception as e:
        print(f"      ✗ Error marking game {game_id} as connection timeout: {e}")


def build_scraped_games_set(season_folder):
    """
    Build a set of game IDs to skip (already scraped OR marked as missing data).
    Single filesystem scan with O(1) lookup time.

    Strategy:
    1. Any game with 150+ files (across all teams) is considered "scraped"
    2. Any game marked with missing_data=1 in games_list CSV is skipped

    Args:
        season_folder: The season folder to scan (e.g., '2023-24')

    Returns:
        Set of game IDs that should be skipped (e.g., {'20001', '20002', ...})
    """
    from collections import Counter

    base_folder_path = "nhlteamreports"
    games_folder = "games"
    game_file_counts = Counter()
    missing_data_games = set()

    print(f"  🔍 Scanning for already-scraped and missing-data games in {season_folder}...")

    # Single pass through all team folders
    for team in nhl_teams:
        team_games_path = os.path.join(base_folder_path, team, games_folder, season_folder)

        if not os.path.exists(team_games_path):
            continue

        # Check games_list for missing_data flags
        games_list_file = os.path.join(team_games_path, f"{team}_games_list.csv")
        if os.path.exists(games_list_file):
            try:
                df = pd.read_csv(games_list_file)
                if 'missing_data' in df.columns:
                    # Add games with missing_data=1 to the skip set
                    missing_games = df[df['missing_data'] == 1]['game_id'].astype(str).tolist()
                    missing_data_games.update(missing_games)
            except Exception as e:
                print(f"    ⚠ Error reading {team} games_list: {e}")

        # Count files per game_id
        for filename in os.listdir(team_games_path):
            if filename.endswith('.csv') and '_games_list' not in filename:
                parts = filename.split('_')
                if len(parts) >= 2 and parts[1].isdigit():
                    game_id = parts[1]
                    game_file_counts[game_id] += 1

    # Games with 150+ files are considered scraped (conservative threshold)
    # Typical complete games have 193-239 files per team, 400-450 total
    THRESHOLD = 150
    scraped_games = {
        game_id for game_id, count in game_file_counts.items()
        if count >= THRESHOLD
    }

    # Combine both sets: scraped games + missing data games
    games_to_skip = scraped_games | missing_data_games

    print(f"  ✓ Found {len(scraped_games)} already-scraped games")
    if missing_data_games:
        print(f"  ✓ Found {len(missing_data_games)} games marked as missing data")
    print(f"  📊 Total games to skip: {len(games_to_skip)}")
    if game_file_counts:
        print(f"  📊 File count summary: {len(game_file_counts)} unique games, "
              f"{sum(game_file_counts.values())} total files")

    return games_to_skip


def _process_single_game(team, game, game_idx, total_games, season_folder, game_cache,
                         use_threading, max_workers, stats_dict, stats_lock):
    """
    Process a single game (thread-safe helper for process_all_games)

    Args:
        team: Team abbreviation
        game: Game dict with game_id, title, full_report_url
        game_idx: Current game index (for display)
        total_games: Total number of games (for display)
        season_folder: Season folder name (e.g., '2023-24')
        game_cache: Shared game cache dict
        use_threading: Enable threaded CSV writing
        max_workers: Number of CSV write threads
        stats_dict: Shared stats dictionary with keys: unique_downloads, total_saves, skipped_games
        stats_lock: Threading lock for stats updates
    """
    game_id = game.get('game_id', 'unknown')
    print(f"    [{game_idx}/{total_games}] Processing game {game_id}: {game.get('title', 'Unknown')}")

    # Get thread-local session
    thread_session = get_thread_session()

    with _cache_lock:
        cache_size_before = len(game_cache)

    # Only process full report
    if 'full_report_url' in game:
        tables_saved = scrape_game_report(
            game['full_report_url'],
            game,
            team,
            season_folder,
            thread_session,
            game_cache,
            use_threading=use_threading,
            max_workers=max_workers
        )

        with _cache_lock:
            cache_size_after = len(game_cache)
            was_new_download = cache_size_after > cache_size_before

        # Check if this game has no data (corrupted/incomplete)
        # If we got 0 tables AND it's not a connection timeout (return code -1), mark it as missing data
        # Connection timeouts return -1 and are already marked by scrape_game_report
        if tables_saved == 0:
            mark_game_missing_data(team, season_folder, game_id)
        elif tables_saved == -1:
            # Connection timeout - already marked by scrape_game_report, don't mark as missing_data
            tables_saved = 0  # Reset to 0 for stats tracking

        # Update stats (separate lock to minimize contention)
        with stats_lock:
            if was_new_download:
                stats_dict['unique_downloads'] += 1
            if tables_saved > 0:
                stats_dict['total_saves'] += 1


# ============================================================================
# MAIN ORCHESTRATION
# ============================================================================


def process_all_games(season_folder, session, max_games=None, fetch_new_lists=True, fromseason=None, thruseason=None, stype=2, use_threading=False, max_workers=8, parallel_scraping=False, scraping_workers=2):
    """
    Process all games for all teams, using cache to avoid duplicate downloads
    but saving to each team's directory - ONLY FULL REPORTS

    Memory-optimized version with batching to prevent unbounded cache growth.

    Args:
        season_folder: Folder name based on season (e.g., '2024-25' for 2024-25 season)
        session: HTTP session
        max_games: Optional limit on games per team
        fetch_new_lists: If True, fetch new game lists. If False, use existing CSV files.
        fromseason: Starting season in format YYYYYYYY (e.g., 20242025 for 2024-25 season)
        thruseason: Ending season in format YYYYYYYY (e.g., 20252026 for 2025-26 season)
        stype: Season type (2=regular season, 3=playoffs)
        use_threading: Enable parallel CSV writing
        max_workers: Number of threads for CSV writing
        parallel_scraping: Enable parallel game downloads/parsing
        scraping_workers: Number of concurrent game scrapes (be conservative!)
    """
    print("\n=== Starting game-by-game data collection (Full Reports Only) ===\n")

    if fromseason and thruseason:
        print(f"Season range: {fromseason} to {thruseason}")
        print(f"Season type: {'Regular Season' if stype == 2 else 'Playoffs'}\n")

    # Memory optimization: Process in batches to prevent cache from growing too large
    BATCH_SIZE = 100  # Process 100 games at a time before clearing cache

    # First, collect all games from all teams
    all_games_by_team = {}

    if fetch_new_lists:
        print("Fetching new game lists from website...\n")
        for team in nhl_teams:
            games_data = get_games_list(team, season_folder, session, fromseason=fromseason, thruseason=thruseason, stype=stype)
            all_games_by_team[team] = games_data
    else:
        print(f"Using existing game lists from {season_folder} CSV files...\n")
        for team in nhl_teams:
            # Load only the specified season, not all seasons
            games_data = load_existing_games_list(team, season_folder)
            all_games_by_team[team] = games_data

    # Build set of already-scraped games ONCE before processing
    print("\n" + "="*60)
    print("Building scraped games cache...")
    print("="*60)
    scraped_games_set = build_scraped_games_set(season_folder)
    print("="*60 + "\n")

    # Track total unique games and total saves
    stats_dict = {
        'unique_downloads': 0,
        'total_saves': 0,
        'skipped_games': 0
    }
    stats_lock = threading.Lock()  # Lock for updating statistics only

    # Now process games for each team with batching
    for i, team in enumerate(nhl_teams, 1):
        print(f"\n[{i}/{len(nhl_teams)}] Processing games for {team}")

        games_data = all_games_by_team.get(team, [])
        if not games_data:
            continue

        # Filter out already-scraped games using the set
        games_to_process = [
            game for game in games_data
            if str(game.get('game_id', '')) not in scraped_games_set
        ]

        skipped_count = len(games_data) - len(games_to_process)
        if skipped_count > 0:
            print(f"  ⭐ Skipped {skipped_count} already-scraped games")

        if not games_to_process:
            print(f"  ✓ All games already scraped for {team}")
            continue

        # Limit number of games if specified
        if max_games:
            games_to_process = games_to_process[:max_games]

        total_team_games = len(games_to_process)
        print(f"  📊 {total_team_games} games to scrape")

        # Process games in batches to manage memory
        for batch_start in range(0, total_team_games, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_team_games)
            batch_games = games_to_process[batch_start:batch_end]

            print(f"  📦 Processing batch {batch_start//BATCH_SIZE + 1}/{(total_team_games + BATCH_SIZE - 1)//BATCH_SIZE} (games {batch_start+1}-{batch_end}/{total_team_games})")

            # Create a fresh cache for this batch
            game_cache = {}

            if parallel_scraping:
                # Parallel processing with ThreadPoolExecutor
                print(f"  Using {scraping_workers} parallel workers")
                with ThreadPoolExecutor(max_workers=scraping_workers, thread_name_prefix=f'scraper_{team}') as executor:
                    futures = []
                    for j, game in enumerate(batch_games, 1):
                        # Calculate global game index for display
                        global_idx = batch_start + j
                        future = executor.submit(_process_single_game, team, game, global_idx, total_team_games,
                                                season_folder, game_cache, use_threading, max_workers,
                                                stats_dict, stats_lock)
                        futures.append(future)

                    # Wait for all to complete
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            print(f"      ✗ Error processing game: {e}")
            else:
                # Sequential processing
                for j, game in enumerate(batch_games, 1):
                    # Calculate global game index for display
                    global_idx = batch_start + j
                    _process_single_game(team, game, global_idx, total_team_games,
                                        season_folder, game_cache, use_threading, max_workers,
                                        stats_dict, stats_lock)

            # Flush write operations for this batch
            if use_threading:
                write_executor = get_write_executor(max_workers)
                print(f"  ⏳ Flushing writes for batch {batch_start//BATCH_SIZE + 1}...")
                write_executor.shutdown(wait=True)
                # Recreate executor for next batch
                global _write_executor
                _write_executor = None

            # Clear the cache after batch completion to free memory
            cache_size = len(game_cache)
            game_cache.clear()
            print(f"  🧹 Cleared cache ({cache_size} games freed from memory)")

    # Final cleanup - ensure all writes are complete
    if use_threading:
        # Executor should already be shut down from last batch, but check
        if _write_executor is not None:
            print("\n⏳ Waiting for final file writes to complete...")
            _write_executor.shutdown(wait=True)
            print("✓ All file writes completed")

    print(f"\n=== Game data collection complete ===")
    print(f"    📊 Downloaded {stats_dict['unique_downloads']} unique game reports")
    print(f"    💾 Processed {stats_dict['total_saves']} games")
    print(f"    ⭐ Skipped {stats_dict['skipped_games']} already-scraped games")
    print(f"    🔄 Cache reuses: {stats_dict['total_saves'] - stats_dict['unique_downloads']}")
    print("===========================================\n")


def fetch_season_data(start_year, end_year, session):
    print("\n=== Fetching historical season data ===\n")

    base_url = "https://www.naturalstattrick.com/teamtable.php"
    eng_data_folder = "ENGdataYbY"

    eng_data_path = os.path.join('nhlteamreports', eng_data_folder)
    if not os.path.exists(eng_data_path):
        os.makedirs(eng_data_path)

    for year in range(start_year, end_year, 3):
        from_season = f"{year}{year + 1:02d}"
        thru_season = f"{min(year + 2, end_year - 1)}{min(year + 3, end_year):02d}"
        url = f"{base_url}?fromseason={from_season}&thruseason={thru_season}&stype=2&sit=ena&score=all&rate=n&team=all&loc=B&gpf=410&fd=&td="

        try:
            print(f"Fetching seasons {from_season}-{thru_season}...")
            response = session.get(url, timeout=30)

            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'lxml')
                tables = soup.find_all("table")

                if tables:
                    df = parse_table_fast(tables[0])
                    csv_filename = f"{from_season}-{thru_season}_ENGdata.csv"
                    df.to_csv(os.path.join(eng_data_path, csv_filename), index=False)
            else:
                print(f"  ✗ Failed to retrieve data for seasons {from_season}-{thru_season} (Status: {response.status_code})")

            # Respectful delay between season requests
            respectful_delay()

        except Exception as e:
            print(f"  ✗ Error fetching seasons {from_season}-{thru_season}: {e}")

    print("\n=== Historical data collection complete ===\n")

# Main execution logic
if __name__ == "__main__":

    # ========================================================================
    # ROBOTS.TXT COMPLIANCE SETTINGS
    # ========================================================================
    # Site robots.txt: Crawl-delay: 240s, Disallows: /game.php (for all user agents)
    # Note: /game.php is allowed for Googlebot, suggesting some access is OK
    #
    # SCRAPING PATTERN:
    # - games.php: Called once per team (32 times total) - ALLOWED for Googlebot
    # - game.php: Called once per game (~7,872 for 3 seasons) - DISALLOWED for bots
    #
    # REALISTIC OPTIONS (balancing speed vs ban risk):
    # 1. STRICT (240s):     ~15 games/hour  → 22 days for 3 seasons (no ban risk)
    # 2. MODERATE (30s):    ~120 games/hour → 2.75 days (low ban risk)
    # 3. RELAXED (10s):     ~360 games/hour → 22 hours (medium ban risk)
    # 4. AGGRESSIVE (3s):   ~1200 games/hour → 6.5 hours (high ban risk)
    # 5. VERY AGGRESSIVE (1s): ~3600 games/hour → 2.2 hours (very high ban risk)
    #
    # RECOMMENDATION: Start with MODERATE (30s) or RELAXED (10s)
    # ========================================================================

    # Delay configuration (in seconds)
    DELAY_SECONDS = 7  # Start with 10s - adjust based on whether you get banned
    # Options: 240 (strict), 30 (moderate), 10 (relaxed), 3 (aggressive), 1 (very aggressive)

    # Configuration
    SCRAPE_TEAM_REPORTS = False # Team lvl overview for a season
    SCRAPE_GAMES = True # Gather game level data
    FETCH_NEW_GAME_LISTS = False # Set to True to add new games to existing games lists (SCARPE_GAMES must also be true)
    SCRAPE_CHARTS = False  # Set to False since charts might be blocked
    SCRAPE_HISTORICAL = False
    MAX_GAMES_PER_TEAM = None  # Set to a number like 5 for testing, None for all games

    # Multithreading Configuration
    USE_MULTITHREADING = True  # Enable parallel file writing
    MAX_WORKERS = 8  # Number of concurrent file writes (4-8 recommended)

    # Parallel Game Processing
    # NOTE: Parallel scraping with delays is OK since each thread has its own delay
    # The global rate limiter ensures minimum spacing between ANY requests
    # However, more workers = more simultaneous connections = higher detection risk
    PARALLEL_GAME_SCRAPING = False  # Set to True for faster scraping (higher ban risk)
    SCRAPING_WORKERS = 1 # Start with 1; can try 2 if 10s+ delay and no bans

    # Season Configuration
    # Format: YYYYYYYY (e.g., 20242025 = 2024-25 season)
    # Set to None to use current season (default behavior)
    # Examples:
    #   2024-25 season: FROM_SEASON = 20242025, THRU_SEASON = 20252026
    #   2023-24 season: FROM_SEASON = 20232024, THRU_SEASON = 20242025
    #   2022-23 season: FROM_SEASON = 20222023, THRU_SEASON = 20232024
    FROM_SEASON = 20242025  # 2024-25 season
    THRU_SEASON = 20242025  # Through 2025-26 (for current season, this is the "thru" year)
    SEASON_TYPE = 2  # 2 = Regular Season, 3 = Playoffs

    # Create a session to reuse connections
    session = create_session()

    # Convert season format from YYYYYYYY to YY-YY for folder naming
    # Example: 20242025 -> 2024-25
    if FROM_SEASON and THRU_SEASON:
        # Extract start year from FROM_SEASON (first 4 digits)
        start_year = str(FROM_SEASON)[:4]
        # Extract end year from THRU_SEASON (last 2 digits)
        end_year = str(THRU_SEASON)[-2:]
        season_folder = f"{start_year}-{end_year}"
    else:
        # Fallback to current season if not specified
        current_year = datetime.now().year
        current_month = datetime.now().month
        # NHL season starts in October, so if we're before October, use previous year
        if current_month < 10:
            season_start = current_year - 1
        else:
            season_start = current_year
        season_folder = f"{season_start}-{str(season_start + 1)[-2:]}"

    print(f"\n📁 Using season folder: {season_folder}")
    print(f"   (based on FROM_SEASON={FROM_SEASON}, THRU_SEASON={THRU_SEASON})")

    # Display rate limiting info
    print(f"\n⏱️  Rate Limiting Configuration:")
    print(f"   Delay per request: {DELAY_SECONDS}s")
    print(f"   Parallel workers: {SCRAPING_WORKERS}")
    games_per_hour = int(3600 / DELAY_SECONDS * SCRAPING_WORKERS)
    print(f"   Estimated rate: ~{games_per_hour} games/hour")

    # Estimate total time if scraping all games
    if SCRAPE_GAMES and not FETCH_NEW_GAME_LISTS:
        approx_games = 246 * len(nhl_teams)  # Rough estimate
        hours_needed = approx_games / games_per_hour
        if hours_needed < 24:
            print(f"   Estimated time for {approx_games} games: ~{hours_needed:.1f} hours")
        else:
            print(f"   Estimated time for {approx_games} games: ~{hours_needed/24:.1f} days")
    print()

    try:
        if SCRAPE_TEAM_REPORTS:
            # Collect current team data (still uses date for general reports)
            date_folder = datetime.now().strftime('%Y-%m-%d')
            allofit(date_folder, session)

        if SCRAPE_GAMES:
            # Process all games with global duplicate tracking
            process_all_games(
                season_folder,
                session,
                max_games=MAX_GAMES_PER_TEAM,
                fetch_new_lists=FETCH_NEW_GAME_LISTS,
                fromseason=FROM_SEASON,
                thruseason=THRU_SEASON,
                stype=SEASON_TYPE,
                use_threading=USE_MULTITHREADING,
                max_workers=MAX_WORKERS,
                parallel_scraping=PARALLEL_GAME_SCRAPING,
                scraping_workers=SCRAPING_WORKERS
            )

        if SCRAPE_CHARTS:
            # Download charts (might be blocked)
            date_folder = datetime.now().strftime('%Y-%m-%d')
            download_charts("https://www.naturalstattrick.com/teams/20232024/charts/pos_rolling/", date_folder, session)

        if SCRAPE_HISTORICAL:
            # Fetch historical data from 2007-2008 to 2023-2024
            fetch_season_data(2007, 2024, session)



    except KeyboardInterrupt:
        print("\n\n⚠ Script interrupted by user")
    except Exception as e:
        print(f"\n\n✗ Unexpected error: {e}")
    finally:
        session.close()
