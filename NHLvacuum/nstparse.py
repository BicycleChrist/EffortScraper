import requests
import time
from datetime import datetime
from bs4 import BeautifulSoup
import pandas as pd
import os
from urllib.parse import urljoin
import random
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import sqlite3
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Note that Overview section is split into two tr classes "odd" and "even"
# No more Coyotes ):
#TODO: CANT DO IT; certain games on NST have very limited data due to exotic venue presumably
# These games are denoted by a "-" in the dataset and should be deleted

# Add in 'ARI' for Coyotes historical data
nhl_teams = [
    'ANA', 'BOS', 'BUF', 'CGY', 'CAR', 'CHI', 'COL', 'CBJ', 'DAL', 'DET', 'EDM',
    'FLA', 'L.A', 'MIN', 'MTL', 'NSH', 'N.J', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'S.J',
    'SEA', 'STL', 'T.B', 'TOR', 'UTA', 'VGK', 'WSH', 'WPG', 'VAN'
]


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

# Scraping configuration - sections to include
ESSENTIAL_SECTIONS = {
    'overview': True,           # Game summary stats
    'power_plays': True,        # Power play stats
    'individual': True,         # Base player stats
    'on_ice': True,            # On ice stats
    'shift_report': True,      # Shift data
    'forward_lines': True,     # Forward line combinations
    'individual_event_maps': False,  # Event maps (usually images, not tables)
    'linemates': False,        # Skip - too many permutations
    'opposition': False,       # Skip - too many permutations
}

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

# Create a session with retry strategy and proper headers
def create_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.3,
        status_forcelist=(500, 502, 504)
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    # Use a browser-like User-Agent to appear as regular traffic
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })

    return session

# Thread-safe file writing
_write_lock = threading.Lock()
_write_executor = None

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

# Add polite delays between requests
def respectful_delay():
    """
    Implements a delay between requests to be respectful to the server.
    2-5 second delays to avoid IP bans.
    """
    delay = random.uniform(1.3, 5)
    time.sleep(delay)

def get_static_tables(team_abbr, date_folder, session):
    url = f"https://www.naturalstattrick.com/teamreport.php?team={team_abbr}"
    base_folder_path = "nhlteamreports"
    general_data_folder = "generalTRdata"
    team_folder_path = os.path.join(base_folder_path, team_abbr, general_data_folder, date_folder)

    try:
        print(f"Fetching data for {team_abbr}...")
        response = session.get(url, timeout=30)

        # Check for IP ban or rate limiting
        if response.status_code == 403:
            print(f"✗ IP BANNED or ACCESS FORBIDDEN - Status 403")
            print(f"  Your IP may be temporarily blocked. Wait 24 hours or use a different IP.")
            raise Exception("IP banned (403)")
        elif response.status_code == 429:
            print(f"✗ RATE LIMITED - Status 429")
            print(f"  Too many requests. Increase delay or wait before retrying.")
            raise Exception("Rate limited (429)")

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        tables = soup.find_all("table")

        os.makedirs(team_folder_path, exist_ok=True)

        for k, table in enumerate(tables):
            df = pd.read_html(str(table))[0]
            file_name = f"{team_abbr}_static_table_{k+1}.csv"
            file_path = os.path.join(team_folder_path, file_name)
            df.to_csv(file_path, index=False)

        # Be respectful - add delay after successful request
        respectful_delay()

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

        soup = BeautifulSoup(response.text, 'html.parser')
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
        print(f"  Fetching games list for {team_abbr}...")
        response = session.get(url, timeout=30)


        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

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

        respectful_delay()
        return games_data

    except Exception as e:
        print(f"  ✗ Error fetching games for {team_abbr}: {e}")
        return []


def extract_team_abbr_from_name(team_full_name, current_team_abbr):
    """
    Try to extract team abbreviation from full team name
    """
    # Mapping of common team names to abbreviations
    team_name_map = {
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

    # Check each part of the team name
    for part in team_full_name.split():
        if part in team_name_map:
            return team_name_map[part]

    # Check full name
    if team_full_name in team_name_map:
        return team_name_map[team_full_name]

    # If we can't determine, return None
    return None


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

        # Standard pandas parsing for regular tables
        df_list = pd.read_html(str(table), header=0)
        if not df_list:
            return None
        df = df_list[0]

        # Check if we have multi-level columns (tuples)
        if isinstance(df.columns[0], tuple):
            # Flatten multi-level column names
            df.columns = ['_'.join(str(i) for i in col if str(i) != 'Unnamed: 0_level_0').strip('_')
                         for col in df.columns]

        return df
    except Exception as e:
        print(f"        ⚠ Warning: Failed to parse table: {e}")
        return None

def parse_period_by_period_table(table):
    """
    Parse tables where cells contain period-by-period data split by <br/> tags.
    Expands these into separate rows for each period and splits by team.

    Returns a dictionary: {team_name: DataFrame} for Overview tables with multiple teams.
    For other period-by-period tables, returns a single DataFrame.

    Example HTML structure (Overview section):
    Row 1:
      <td>Ducks</td>
      <td><div class="hall">1<br>2<br>3<br><hr></div>Final</td>
      <td>17:10<br>16:00<br>16:42<br><hr>49:52</td>
      ... (more cells with same pattern)

    This becomes separate DataFrames for each team with rows:
      Period 1, Period 2, Period 3, Final (with corresponding values from each cell)
    """
    # Extract headers
    thead = table.find('thead')
    if not thead:
        return None

    header_row = thead.find('tr')
    if not header_row:
        return None

    headers = [th.get_text(strip=True) for th in header_row.find_all('th')]

    # Extract data rows
    tbody = table.find('tbody')
    if not tbody:
        return None

    data_rows = tbody.find_all('tr')

    # Process each row and expand period data
    # Group by team name
    teams_data = {}

    for row in data_rows:
        cells = row.find_all('td')
        if not cells:
            continue

        # Extract team name (first cell)
        team_name = cells[0].get_text(strip=True)

        if not team_name:  # Skip empty rows
            continue

        # Determine number of periods by examining the second cell (Period column)
        # which has <div class="hall"> containing period labels
        num_periods = 0
        if len(cells) > 1:
            period_cell = cells[1]
            hall_div = period_cell.find('div', class_='hall')
            if hall_div:
                # Count <br> tags to determine number of periods
                # HTML shows: 1<br>2<br>3<br><hr> = 3 periods + Final
                br_tags = hall_div.find_all('br')
                # Number of periods = number of <br> tags before <hr>
                hr_tag = hall_div.find('hr')
                if hr_tag:
                    # Count br tags before hr
                    num_periods_before_hr = 0
                    for elem in hall_div.children:
                        if elem.name == 'hr':
                            break
                        if elem.name == 'br':
                            num_periods_before_hr += 1
                    # Number of values = br_count + 1 (for first value) + 1 (for Final)
                    num_periods = num_periods_before_hr + 2
                else:
                    num_periods = len(br_tags) + 1
            else:
                # No hall div found - might be malformed, try to extract from cell text
                cell_text = period_cell.get_text(strip=True)
                # Count how many values are separated by newlines
                num_periods = len([x for x in cell_text.split('\n') if x.strip()])

        if num_periods == 0:
            # Can't determine structure, skip this row
            continue

        # Initialize team data list if not exists
        if team_name not in teams_data:
            teams_data[team_name] = []

        # Extract values for each period from all cells
        for period_idx in range(num_periods):
            row_data = [team_name]  # Start with team name

            for cell_idx, cell in enumerate(cells[1:], 1):  # Skip first cell (team)
                # Split cell content by <br> tags and <hr>
                cell_values = []

                # Check if cell has <div class="hall">
                hall_div = cell.find('div', class_='hall')

                if hall_div:
                    # Extract values from hall div (period labels column)
                    for content in hall_div.children:
                        if content.name == 'br':
                            continue
                        elif content.name == 'hr':
                            break
                        else:
                            text = str(content).strip()
                            if text:
                                cell_values.append(text)

                    # Get the "Final" text (after </div>)
                    final_text = ''
                    for sibling in hall_div.next_siblings:
                        if sibling.name is None:  # Text node
                            final_text += str(sibling).strip()
                    if final_text:
                        cell_values.append(final_text)
                else:
                    # Regular cell - split by <br> tags
                    # Replace <br> with newlines for easy splitting
                    for br in cell.find_all('br'):
                        br.replace_with('\n')

                    # Get text and split by newlines
                    cell_text = cell.get_text()

                    # Split by <hr> first (if present)
                    if '<hr>' in str(cell) or cell.find('hr'):
                        # Split before and after <hr>
                        parts = cell_text.split('\n')
                        final_val = None
                        period_vals = []

                        # Find where the hr would be (after the last period value)
                        for i, part in enumerate(parts):
                            part = part.strip()
                            if part:
                                # Check if this is after the hr
                                # The hr appears after the period values
                                period_vals.append(part)

                        # Last value is the Final
                        if period_vals:
                            final_val = period_vals[-1]
                            period_vals = period_vals[:-1]
                            cell_values = period_vals + [final_val]
                        else:
                            cell_values = period_vals
                    else:
                        # Just split by newlines
                        cell_values = [part.strip() for part in cell_text.split('\n') if part.strip()]

                # Get value for this period
                if period_idx < len(cell_values):
                    row_data.append(cell_values[period_idx])
                else:
                    row_data.append('')

            teams_data[team_name].append(row_data)

    # Create DataFrames for each team
    if teams_data:
        if len(teams_data) == 1:
            # Single team - return just the DataFrame
            team_name = list(teams_data.keys())[0]
            df = pd.DataFrame(teams_data[team_name], columns=headers)
            # Remove the team name column (first column)
            if len(df.columns) > 0 and df.columns[0] == '':
                df = df.iloc[:, 1:]
            return df
        else:
            # Multiple teams - return dict of DataFrames
            result = {}
            for team_name, rows in teams_data.items():
                df = pd.DataFrame(rows, columns=headers)
                # Remove the team name column (first column)
                if len(df.columns) > 0:
                    df = df.iloc[:, 1:]
                # Remove trailing empty rows
                df = df.dropna(how='all')
                # Remove rows where all values (except Period) are empty or NaN
                df = df[df.iloc[:, 1:].notna().any(axis=1)]
                result[team_name] = df
            return result

    return None

# Mapping of situation classes to readable names
SITUATION_NAMES = {
    'tall': 'All',
    'tev': 'EV',
    't5v5': '5v5',
    'tsva': '5v5',  # Sometimes 5v5 is labeled differently
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

                    # Filter to get only the actual data tables (not wrapper/empty tables)
                    # Real data tables have an 'id' attribute (e.g., id="tbtsall", id="tbstall")
                    # Wrapper tables and empty tables don't have IDs
                    data_tables = []

                    for table in all_tables:
                        table_id = table.get('id', '')
                        # Only include tables with IDs (these are the real data tables)
                        if table_id:
                            data_tables.append(table)

                    if data_tables:
                        situation_tables[readable_situation] = data_tables

            if situation_tables:
                sections[sanitized_name] = {
                    'label': label_text,
                    'situations': situation_tables
                }

    return sections

def is_corrupted_game(game_file_prefix, season_folder, team_abbr=None, opponent_abbr=None):
    """
    Check if a game has corrupted files (identified by '_-_' pattern in filename).
    Corrupted games have incomplete data on NST (exotic venues, etc.) and should be skipped.

    Args:
        game_file_prefix: The game prefix (e.g., "BOSvsVGK_20069")
        season_folder: The season folder to check (e.g., '2024-25')
        team_abbr: Current team abbreviation (optional)
        opponent_abbr: Opponent team abbreviation (optional)

    Returns:
        True if corrupted game files are found, False otherwise
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"

    # List of teams to check
    teams_to_check = []
    if team_abbr:
        teams_to_check.append(team_abbr)
    if opponent_abbr and opponent_abbr != 'OPP':
        teams_to_check.append(opponent_abbr)

    if not teams_to_check and team_abbr:
        teams_to_check = [team_abbr]

    # Check for corrupted files (files with '_-_' pattern)
    for team in teams_to_check:
        team_games_path = os.path.join(base_folder_path, team, games_folder, season_folder)
        if os.path.exists(team_games_path):
            for file in os.listdir(team_games_path):
                # Extract game_id from filename to match
                if file.startswith(game_file_prefix) and '_-_' in file:
                    return True

    return False

def game_already_exists(game_file_prefix, season_folder, team_abbr=None, opponent_abbr=None):
    """
    Check if a game has already been scraped by looking for any files with the game ID.
    Checks both teams' directories if opponent is known.
    Also returns True if the game is corrupted (to skip re-scraping).

    Args:
        game_file_prefix: The game prefix (e.g., "BOSvsVGK_20069")
        season_folder: The season folder to check (e.g., '2024-25')
        team_abbr: Current team abbreviation (optional)
        opponent_abbr: Opponent team abbreviation (optional)

    Returns:
        True if game already exists or is corrupted, False otherwise
    """
    base_folder_path = "nhlteamreports"
    games_folder = "games"

    # First check if this is a corrupted game - if so, skip it
    if is_corrupted_game(game_file_prefix, season_folder, team_abbr, opponent_abbr):
        return True

    # Extract game_id from prefix (e.g., "BOSvsVGK_20069" -> "20069")
    game_id = game_file_prefix.split('_')[-1]

    # List of teams to check
    teams_to_check = []
    if team_abbr:
        teams_to_check.append(team_abbr)
    if opponent_abbr and opponent_abbr != 'OPP':
        teams_to_check.append(opponent_abbr)

    # If we don't have specific teams, just check the current team
    if not teams_to_check and team_abbr:
        teams_to_check = [team_abbr]

    # Check if ANY files exist for this game_id
    # This handles cases where opponent was previously unknown (saved as "OPP")
    # or team names have changed (e.g., "Utah HC" -> "Mammoth")
    for team in teams_to_check:
        team_games_path = os.path.join(base_folder_path, team, games_folder, season_folder)
        if os.path.exists(team_games_path):
            # Look for any file containing this game_id
            for file in os.listdir(team_games_path):
                # Skip the games_list CSV
                if '_games_list.csv' in file:
                    continue
                # Check if this file is for our game_id
                # Format: TEAMvsOPP_GAMEID_... or TEAMvsTEAM_GAMEID_...
                if f'_{game_id}_' in file or file.startswith(f'{game_file_prefix}_'):
                    return True

    return False

def scrape_game_report(game_url, game_info, team_abbr, season_folder, session, game_cache, use_threading=True, max_workers=4):
    """
    Scrape individual game report and save with teamAvsTeamB_gameID naming
    Downloads once but saves to current team's directory (cache allows saving to multiple teams)
    Only processes full reports.
    Tables are now named by their section labels for clarity.
    Skips games that have already been scraped.

    Args:
        use_threading: If True, CSV writes are submitted to thread pool for parallel execution
        max_workers: Number of worker threads for CSV writing (default: 8)
    """
    game_id = game_info.get('game_id', 'unknown')

    # Determine the correct season folder from the game's season field
    game_season = game_info.get('season', '')
    # Convert to string if it's an int (happens when loading from CSV)
    if isinstance(game_season, int):
        game_season = str(game_season)

    if game_season and len(str(game_season)) == 8:
        game_season_str = str(game_season)
        start_year = game_season_str[:4]
        end_year = game_season_str[6:8]
        season_folder = f"{start_year}-{end_year}"
    # else: use the season_folder passed in as parameter (fallback)

    # Determine opponent team abbreviation
    team1_full = game_info.get('team1_full', '')
    team2_full = game_info.get('team2_full', '')

    # Figure out which team is the opponent
    opponent_abbr = None
    if team1_full and team2_full:
        team1_abbr = extract_team_abbr_from_name(team1_full, team_abbr)
        team2_abbr = extract_team_abbr_from_name(team2_full, team_abbr)

        # Determine which is the opponent
        if team1_abbr == team_abbr:
            opponent_abbr = team2_abbr
        elif team2_abbr == team_abbr:
            opponent_abbr = team1_abbr
        else:
            # Try to match based on current team
            if team_abbr in team1_full.upper():
                opponent_abbr = team2_abbr
            elif team_abbr in team2_full.upper():
                opponent_abbr = team1_abbr

    if not opponent_abbr:
        opponent_abbr = 'OPP'  # Generic opponent if we can't determine

    # Create consistent game filename (alphabetical order for teams)
    teams_sorted = sorted([team_abbr, opponent_abbr])
    game_file_prefix = f"{teams_sorted[0]}vs{teams_sorted[1]}_{game_id}"

    # Check if this game has already been scraped FOR THIS TEAM ONLY
    # We don't check opponent because we want to save to both teams' directories
    # If the game is in cache, it will be saved to both teams below
    if game_already_exists(game_file_prefix, season_folder, team_abbr, None):
        return

    # Cache key for this specific game
    cache_key = game_file_prefix

    base_folder_path = "nhlteamreports"
    games_folder = "games"
    team_games_path = os.path.join(base_folder_path, team_abbr, games_folder, season_folder)

    # Get thread pool executor if threading is enabled
    write_futures = []
    executor = get_write_executor(max_workers) if use_threading else None

    # Check if we have this game in cache
    if cache_key in game_cache:
        # We already downloaded this game, just save the cached data to this team's directory

        os.makedirs(team_games_path, exist_ok=True)

        # Save cached sections with situations
        for section_name, section_data in game_cache[cache_key]['sections'].items():
            for situation, table_list in section_data['situations'].items():
                for i, table_data in enumerate(table_list):
                    if table_data is not None:
                        # Check if this is an Overview section with multiple teams (dict)
                        if isinstance(table_data, dict):
                            # Overview section - save ALL teams' data to their respective directories
                            for team_name, team_df in table_data.items():
                                # Map full team name to abbreviation
                                team_name_abbr = extract_team_abbr_from_name(team_name, team_abbr)

                                if not team_name_abbr:
                                    # If we can't map it, skip this team
                                    continue

                                # Determine the directory for this team
                                team_specific_games_path = os.path.join(base_folder_path, team_name_abbr, games_folder, season_folder)
                                os.makedirs(team_specific_games_path, exist_ok=True)

                                # Build file path with team abbreviation
                                if len(table_list) > 1:
                                    file_name = f"{game_file_prefix}_{team_name_abbr}_{section_name.lower()}_{situation}_{i+1}.csv"
                                else:
                                    file_name = f"{game_file_prefix}_{team_name_abbr}_{section_name.lower()}_{situation}.csv"
                                file_path = os.path.join(team_specific_games_path, file_name)

                                if use_threading:
                                    write_futures.append(executor.submit(write_csv_threadsafe, team_df, file_path))
                                else:
                                    team_df.to_csv(file_path, index=False)
                        else:
                            # Regular table (single DataFrame)
                            # Extract team from section label to determine which directory to save to
                            section_team_abbr = extract_team_from_section_label(section_data['label'])

                            # Determine which directory to save to
                            if section_team_abbr:
                                # Save to the team-specific directory
                                save_team_games_path = os.path.join(base_folder_path, section_team_abbr, games_folder, season_folder)
                                os.makedirs(save_team_games_path, exist_ok=True)
                            else:
                                # Fallback to current team's directory if we can't determine the team
                                save_team_games_path = team_games_path

                            # Name: GameID_Section_Situation[_TableNum if multiple].csv
                            if len(table_list) > 1:
                                file_name = f"{game_file_prefix}_{section_name}_{situation}_{i+1}.csv"
                            else:
                                file_name = f"{game_file_prefix}_{section_name}_{situation}.csv"
                            file_path = os.path.join(save_team_games_path, file_name)

                            if use_threading:
                                write_futures.append(executor.submit(write_csv_threadsafe, table_data, file_path))
                            else:
                                table_data.to_csv(file_path, index=False)

        return

    # Game not in cache, need to download it
    try:
        print(f"      📥 Downloading {game_file_prefix}")
        response = session.get(game_url, timeout=30)

        # Check for IP ban or rate limiting
        if response.status_code == 403:
            print(f"      ✗ IP BANNED - Status 403. Stopping scraper!")
            raise Exception("IP banned (403)")
        elif response.status_code == 429:
            print(f"      ✗ RATE LIMITED - Status 429. Stopping scraper!")
            raise Exception("Rate limited (429)")

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract tables grouped by section
        sections = extract_table_sections(soup)

        # Skip image link extraction - too intensive and not needed
        img_links = []

        if sections:
            os.makedirs(team_games_path, exist_ok=True)

            # Parse and cache sections with their situations
            cached_sections = {}
            total_tables = 0

            for section_name, section_data in sections.items():
                cached_situations = {}

                # Process each situation (All, EV, 5v5, PP, PK, etc.)
                for situation, table_list in section_data['situations'].items():
                    cached_tables = []

                    for i, table in enumerate(table_list):
                        # Parse table to DataFrame with proper header handling
                        df = parse_table_with_multiheader(table)

                        if df is not None:
                            # Check if this is an Overview section with multiple teams (returns dict)
                            if isinstance(df, dict):
                                # Overview section - save separate file for EACH team to their respective directories
                                cached_tables.append(df)

                                # Iterate through all teams in the overview data
                                for team_name, team_df in df.items():
                                    # Map full team name to abbreviation
                                    team_name_abbr = extract_team_abbr_from_name(team_name, team_abbr)

                                    if not team_name_abbr:
                                        # If we can't map it, skip this team
                                        continue

                                    # Determine the directory for this team
                                    team_specific_games_path = os.path.join(base_folder_path, team_name_abbr, games_folder, season_folder)
                                    os.makedirs(team_specific_games_path, exist_ok=True)

                                    # Build file path with team abbreviation
                                    if len(table_list) > 1:
                                        file_name = f"{game_file_prefix}_{team_name_abbr}_{section_name.lower()}_{situation}_{i+1}.csv"
                                    else:
                                        file_name = f"{game_file_prefix}_{team_name_abbr}_{section_name.lower()}_{situation}.csv"
                                    file_path = os.path.join(team_specific_games_path, file_name)

                                    if use_threading:
                                        write_futures.append(executor.submit(write_csv_threadsafe, team_df, file_path))
                                    else:
                                        team_df.to_csv(file_path, index=False)
                                    total_tables += 1
                            else:
                                # Regular table (single DataFrame)
                                cached_tables.append(df)

                                # Extract team from section label to determine which directory to save to
                                section_team_abbr = extract_team_from_section_label(section_data['label'])

                                # Determine which directory to save to
                                if section_team_abbr:
                                    # Save to the team-specific directory
                                    save_team_games_path = os.path.join(base_folder_path, section_team_abbr, games_folder, season_folder)
                                    os.makedirs(save_team_games_path, exist_ok=True)
                                else:
                                    # Fallback to current team's directory if we can't determine the team
                                    save_team_games_path = team_games_path

                                # Build file path
                                if len(table_list) > 1:
                                    file_name = f"{game_file_prefix}_{section_name}_{situation}_{i+1}.csv"
                                else:
                                    file_name = f"{game_file_prefix}_{section_name}_{situation}.csv"
                                file_path = os.path.join(save_team_games_path, file_name)

                                # Write CSV (thread-safe if enabled)
                                if use_threading:
                                    write_futures.append(executor.submit(write_csv_threadsafe, df, file_path))
                                else:
                                    df.to_csv(file_path, index=False)
                                total_tables += 1

                    cached_situations[situation] = cached_tables

                cached_sections[section_name] = {
                    'label': section_data['label'],
                    'situations': cached_situations
                }

            # Cache this game's data for future use
            game_cache[cache_key] = {
                'sections': cached_sections
            }

            # Check if this is a corrupted game (21 tables or less indicates incomplete data)
            if total_tables <= 21:
                print(f"      ⚠ WARNING: Only {total_tables} tables downloaded for {game_file_prefix}")
                print(f"      ⚠ This indicates incomplete data (exotic venue/corrupted)")
                print(f"      ⚠ Marking as corrupted and will skip in future scrapes")
                # Remove from cache so it doesn't get saved to other teams
                if cache_key in game_cache:
                    del game_cache[cache_key]
            else:
                print(f"      ✓ Downloaded {total_tables} tables for {game_file_prefix}")

        # Wait for all writes to complete if threading is enabled
        if use_threading and write_futures:
            for future in as_completed(write_futures):
                try:
                    future.result()  # Raise any exceptions that occurred
                except Exception as e:
                    print(f"        ⚠ Write error: {e}")

        time.sleep(random.uniform(2, 5))  # Delay between game reports

    except Exception as e:
        print(f"      ✗ Error scraping full report for {game_file_prefix}: {e}")


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

def process_all_games(season_folder, session, max_games=None, fetch_new_lists=True, fromseason=None, thruseason=None, stype=2, use_threading=False, max_workers=8, parallel_scraping=False, scraping_workers=2):
    """
    Process all games for all teams, using cache to avoid duplicate downloads
    but saving to each team's directory - ONLY FULL REPORTS

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

    # Cache to store downloaded game data
    game_cache = {}

    # First, collect all games from all teams
    all_games_by_team = {}

    if fetch_new_lists:
        print("Fetching new game lists from website...\n")
        for team in nhl_teams:
            games_data = get_games_list(team, season_folder, session, fromseason=fromseason, thruseason=thruseason, stype=stype)
            all_games_by_team[team] = games_data
    else:
        print("Using existing game lists from ALL season CSV files...\n")
        for team in nhl_teams:
            games_data = load_all_existing_games(team)
            all_games_by_team[team] = games_data

    # Track total unique games and total saves
    unique_downloads = 0
    total_saves = 0
    skipped_games = 0

    # Thread-safe cache access lock
    cache_lock = threading.Lock()

    def process_single_game(team, game, game_idx, total_games):
        """Process a single game (thread-safe)"""
        nonlocal unique_downloads, total_saves, skipped_games

        game_id = game.get('game_id', 'unknown')
        print(f"    [{game_idx}/{total_games}] Processing game {game_id}: {game.get('title', 'Unknown')}")

        with cache_lock:
            cache_size_before = len(game_cache)

        # Only process full report
        if 'full_report_url' in game:
            scrape_game_report(
                game['full_report_url'],
                game,
                team,
                season_folder,
                session,
                game_cache,
                use_threading=use_threading,
                max_workers=max_workers
            )

            with cache_lock:
                # Check if game was downloaded or skipped
                if len(game_cache) == cache_size_before:
                    # Check if game exists to confirm it was skipped
                    team1_full = game.get('team1_full', '')
                    team2_full = game.get('team2_full', '')
                    team1_abbr = extract_team_abbr_from_name(team1_full, team)
                    team2_abbr = extract_team_abbr_from_name(team2_full, team)
                    opponent_abbr = team1_abbr if team1_abbr != team else team2_abbr
                    teams_sorted = sorted([team, opponent_abbr]) if opponent_abbr else [team, 'OPP']
                    game_file_prefix = f"{teams_sorted[0]}vs{teams_sorted[1]}_{game_id}"

                    if game_already_exists(game_file_prefix, season_folder, team, opponent_abbr):
                        skipped_games += 1
                else:
                    total_saves += 1

                # Track if this was a new download
                if len(game_cache) > cache_size_before:
                    unique_downloads += 1

    # Now process games for each team
    for i, team in enumerate(nhl_teams, 1):
        print(f"\n[{i}/{len(nhl_teams)}] Processing games for {team}")

        games_data = all_games_by_team.get(team, [])
        if not games_data:
            continue

        # Limit number of games if specified
        games_to_process = games_data[:max_games] if max_games else games_data

        if parallel_scraping:
            # Parallel processing with ThreadPoolExecutor
            print(f"  Using {scraping_workers} parallel workers")
            with ThreadPoolExecutor(max_workers=scraping_workers, thread_name_prefix=f'scraper_{team}') as executor:
                futures = []
                for j, game in enumerate(games_to_process, 1):
                    future = executor.submit(process_single_game, team, game, j, len(games_to_process))
                    futures.append(future)

                # Wait for all to complete
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        print(f"      ✗ Error processing game: {e}")
        else:
            # Sequential processing (original)
            for j, game in enumerate(games_to_process, 1):
                process_single_game(team, game, j, len(games_to_process))

    print(f"\n=== Game data collection complete ===")
    print(f"    📊 Downloaded {unique_downloads} unique game reports")
    print(f"    💾 Processed {total_saves} games")
    print(f"    ⏭ Skipped {skipped_games} already-scraped games")
    print(f"    {total_saves - unique_downloads} ")
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
                soup = BeautifulSoup(response.content, 'html.parser')
                tables = soup.find_all("table")

                if tables:
                    df = pd.read_html(str(tables[0]))[0]
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

    # Configuration
    SCRAPE_TEAM_REPORTS = False # Team lvl overview for a season
    SCRAPE_GAMES = True # Gather game level data
    FETCH_NEW_GAME_LISTS = True # Set to True to add new games to existing games lists (SCARPE_GAMES must also be true)
    SCRAPE_CHARTS = False  # Set to False since charts might be blocked
    SCRAPE_HISTORICAL = False
    MAX_GAMES_PER_TEAM = None  # Set to a number like 5 for testing, None for all games

    # Multithreading Configuration
    USE_MULTITHREADING = True  # Enable parallel file writing
    MAX_WORKERS = 8  # Number of concurrent file writes (4-8 recommended)

    # Parallel Game Processing
    PARALLEL_GAME_SCRAPING = True  # Enable parallel game scraping (downloads + parsing)
    SCRAPING_WORKERS = 6 # Number of concurrent game downloads (START WITH 2, increase carefully to avoid rate limits)

    # Season Configuration
    # Format: YYYYYYYY (e.g., 20242025 = 2024-25 season)
    # Set to None to use current season (default behavior)
    # Examples:
    #   2024-25 season: FROM_SEASON = 20242025, THRU_SEASON = 20252026
    #   2023-24 season: FROM_SEASON = 20232024, THRU_SEASON = 20242025
    #   2022-23 season: FROM_SEASON = 20222023, THRU_SEASON = 20232024
    FROM_SEASON = 20252026  # 2024-25 season
    THRU_SEASON = 20252026  # Through 2025-26 (for current season, this is the "thru" year)
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
    print(f"   (based on FROM_SEASON={FROM_SEASON}, THRU_SEASON={THRU_SEASON})\n")

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
