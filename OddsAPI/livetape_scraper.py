import requests
import json
from bs4 import BeautifulSoup
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def _fetch_boxscore_soup(game_id, sport_name, timeout=10):
    """
    Helper function to fetch and parse boxscore HTML.

    Args:
        game_id: ESPN game ID
        sport_name: Sport name in lowercase
        timeout: Request timeout in seconds

    Returns:
        BeautifulSoup object or None on error
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        boxscore_url = f"https://www.espn.com/{sport_name.lower()}/boxscore/_/gameId/{game_id}"
        response = requests.get(boxscore_url, headers=headers, timeout=timeout)
        return BeautifulSoup(response.content, 'html.parser')
    except Exception as e:
        print(f"Error fetching boxscore for game {game_id}: {e}")
        return None

def parse_boxscore_stats(soup, sport_name):
    """
    Parse box score statistics from ESPN box score page.

    Args:
        soup: BeautifulSoup object of the box score page
        sport_name: Sport name in lowercase (e.g., "mlb", "nba", "nfl", "nhl")

    Returns:
        Dictionary containing parsed stats for both teams
    """
    stats_data = {
        'away_team_stats': {},
        'home_team_stats': {}
    }

    try:
        # NBA and NHL use different HTML structure than other sports
        if sport_name.lower() == 'nba':
            return parse_nba_boxscore_stats(soup)
        elif sport_name.lower() == 'nhl':
            return parse_nhl_boxscore_stats(soup)

        # Find all stat categories (for NFL, MLB)
        categories = soup.find_all('div', class_='Boxscore__Category')

        for category in categories:
            teams = category.find_all('div', class_='Boxscore__Team')

            for team_idx, team in enumerate(teams):
                team_key = 'away_team_stats' if team_idx == 0 else 'home_team_stats'

                # Get category name (e.g., "Detroit Passing", "Kansas City Rushing")
                team_title = team.find('div', class_='TeamTitle__Name')
                if not team_title:
                    continue

                category_name = team_title.text.strip()

                # Extract the stat type (e.g., "Passing", "Rushing", "Batting")
                stat_type = category_name.split()[-1] if category_name else "Unknown"

                # Parse the table
                tables = team.find_all('table', class_='Table')
                if len(tables) < 2:
                    continue

                # First table has player names, second has stats
                name_table = tables[0]
                stats_table = tables[1]

                # Get headers from stats table
                headers = []
                header_row = stats_table.find('thead')
                if header_row:
                    header_cells = header_row.find_all('th')
                    headers = [cell.text.strip() for cell in header_cells]

                # Get player names
                player_rows = name_table.find('tbody').find_all('tr', class_=lambda x: x and 'Boxscore__Totals' not in x)
                players = []
                for row in player_rows:
                    player_link = row.find('a', class_='Boxscore__Athlete_Name')
                    jersey = row.find('span', class_='Boxscore__Athlete_Jersey')
                    if player_link:
                        players.append({
                            'name': player_link.text.strip(),
                            'jersey': jersey.text.strip() if jersey else '',
                            'url': player_link.get('href', '')
                        })

                # Get stats
                stat_rows = stats_table.find('tbody').find_all('tr', class_=lambda x: x and 'Boxscore__Totals' not in x)

                player_stats = []
                for idx, (player, stat_row) in enumerate(zip(players, stat_rows)):
                    stat_cells = stat_row.find_all('td')
                    stat_values = [cell.text.strip() for cell in stat_cells]

                    player_data = player.copy()
                    player_data['stats'] = {}
                    for header, value in zip(headers, stat_values):
                        if header:  # Skip empty headers
                            player_data['stats'][header] = value

                    player_stats.append(player_data)

                # Store by stat type
                if stat_type not in stats_data[team_key]:
                    stats_data[team_key][stat_type] = []
                stats_data[team_key][stat_type].extend(player_stats)

        return stats_data

    except Exception as e:
        print(f"Error parsing box score stats: {e}")
        import traceback
        traceback.print_exc()
        return stats_data


def parse_nba_boxscore_stats(soup):
    """
    Parse NBA box score statistics (different HTML structure than other sports).
    NBA uses ResponsiveTable with starters/bench sections instead of Boxscore__Category.
    """
    stats_data = {
        'away_team_stats': {'Starters': [], 'Bench': []},
        'home_team_stats': {'Starters': [], 'Bench': []}
    }

    try:
        # Find all ResponsiveTable containers (one for away, one for home)
        tables = soup.find_all('div', class_='ResponsiveTable')

        for team_idx, table_container in enumerate(tables[:2]):  # First 2 are away/home teams
            team_key = 'away_team_stats' if team_idx == 0 else 'home_team_stats'

            # Find the two internal tables (name table and stats table)
            internal_tables = table_container.find_all('table', class_='Table')
            if len(internal_tables) < 2:
                continue

            name_table = internal_tables[0]
            stats_table = internal_tables[1]

            # Get stat headers from the stats table
            headers = []
            header_cells = stats_table.find_all('td', class_='Table__customHeader')
            for cell in header_cells:
                header_div = cell.find('div', class_='Table__customHeader')
                if header_div and header_div.text.strip():
                    headers.append(header_div.text.strip())

            # Get player names and organize by starters/bench
            name_rows = name_table.find('tbody').find_all('tr')
            stat_rows = stats_table.find('tbody').find_all('tr')

            current_section = None  # Will be 'Starters' or 'Bench'
            player_idx = 0

            for row_idx, (name_row, stat_row) in enumerate(zip(name_rows, stat_rows)):
                # Check if this is a section header (starters/bench/team)
                section_header = name_row.find('div', class_='Table__customHeader')
                if section_header:
                    section_text = section_header.text.strip().lower()
                    if 'starters' in section_text:
                        current_section = 'Starters'
                    elif 'bench' in section_text:
                        current_section = 'Bench'
                    elif 'team' in section_text:
                        current_section = None  # Skip team totals
                    continue

                if not current_section:
                    continue

                # Extract player name
                player_link = name_row.find('a', class_='AnchorLink')
                if not player_link:
                    continue

                # Get full name from Boxscore__AthleteName--long
                name_span = player_link.find('span', class_='Boxscore__AthleteName--long')
                if not name_span:
                    continue

                player_name = name_span.text.strip()
                jersey_span = name_row.find('span', class_='playerJersey')
                jersey = jersey_span.text.strip() if jersey_span else ''

                # Extract stats (skip the section header row)
                stat_cells = stat_row.find_all('td', class_='Table__TD')
                stat_values = []
                for cell in stat_cells:
                    # Skip cells with customHeader class
                    if 'Table__customHeader' in cell.get('class', []):
                        continue
                    stat_values.append(cell.text.strip())

                # Create player data
                player_data = {
                    'name': player_name,
                    'jersey': jersey,
                    'stats': {}
                }

                # Map stats to headers
                for header, value in zip(headers, stat_values):
                    if header and value:
                        player_data['stats'][header] = value

                # Add to appropriate section
                stats_data[team_key][current_section].append(player_data)
                player_idx += 1

        return stats_data

    except Exception as e:
        print(f"Error parsing NBA box score stats: {e}")
        import traceback
        traceback.print_exc()
        return stats_data


def parse_nhl_boxscore_stats(soup):
    """
    Parse NHL box score statistics (similar structure to NBA).
    NHL uses ResponsiveTable with forwards/defensemen/goaltending sections.
    """
    stats_data = {
        'away_team_stats': {'Skaters': [], 'Goaltending': []},
        'home_team_stats': {'Skaters': [], 'Goaltending': []}
    }

    try:
        # Find all ResponsiveTable containers (one for away, one for home)
        tables = soup.find_all('div', class_='ResponsiveTable')

        for team_idx, table_container in enumerate(tables[:2]):  # First 2 are away/home teams
            team_key = 'away_team_stats' if team_idx == 0 else 'home_team_stats'

            # Find the two internal tables (name table and stats table)
            internal_tables = table_container.find_all('table', class_='Table')
            if len(internal_tables) < 2:
                continue

            name_table = internal_tables[0]
            stats_table = internal_tables[1]

            # Get stat headers from the stats table
            headers = []
            header_cells = stats_table.find_all('td', class_='Table__customHeader')
            if not header_cells:
                # Try th elements if td doesn't work
                header_row = stats_table.find('thead')
                if header_row:
                    header_cells = header_row.find_all('th')
                    headers = [cell.text.strip() for cell in header_cells if cell.text.strip()]
            else:
                for cell in header_cells:
                    header_div = cell.find('div', class_='Table__customHeader')
                    if header_div and header_div.text.strip():
                        headers.append(header_div.text.strip())

            # Get player names and organize by forwards/defensemen/goaltending
            name_rows = name_table.find('tbody').find_all('tr')
            stat_rows = stats_table.find('tbody').find_all('tr')

            current_section = None  # Will be 'forwards', 'defensemen', or 'goaltending'

            for row_idx, (name_row, stat_row) in enumerate(zip(name_rows, stat_rows)):
                # Check if this is a section header
                section_header = name_row.find('div', class_='Table__customHeader')
                if section_header:
                    section_text = section_header.text.strip().lower()
                    if 'forward' in section_text or 'defensemen' in section_text:
                        current_section = 'Skaters'  # Combine forwards and defensemen
                    elif 'goaltending' in section_text or 'goalie' in section_text:
                        current_section = 'Goaltending'
                    elif 'team' in section_text:
                        current_section = None  # Skip team totals
                    continue

                if not current_section:
                    continue

                # Extract player name
                player_link = name_row.find('a', class_='AnchorLink')
                if not player_link:
                    continue

                # Get full name from Boxscore__AthleteName--long
                name_span = player_link.find('span', class_='Boxscore__AthleteName--long')
                if not name_span:
                    continue

                player_name = name_span.text.strip()
                jersey_span = name_row.find('span', class_='playerJersey')
                jersey = jersey_span.text.strip() if jersey_span else ''

                # Extract stats
                stat_cells = stat_row.find_all('td', class_='Table__TD')
                stat_values = []
                for cell in stat_cells:
                    # Skip cells with customHeader class
                    if 'Table__customHeader' in cell.get('class', []):
                        continue
                    stat_values.append(cell.text.strip())

                # Create player data
                player_data = {
                    'name': player_name,
                    'jersey': jersey,
                    'stats': {}
                }

                # Map stats to headers
                for header, value in zip(headers, stat_values):
                    if header and value:
                        player_data['stats'][header] = value

                # Add to appropriate section
                stats_data[team_key][current_section].append(player_data)

        return stats_data

    except Exception as e:
        print(f"Error parsing NHL box score stats: {e}")
        import traceback
        traceback.print_exc()
        return stats_data


def _safe_int(value, default=0):
    """Parse an int from an ESPN stat value, tolerating '--', '', None, etc."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return default


def get_top_players_mlb(stats_data):
    """
    Extract top players for MLB games.
    Returns: Starting Pitchers, top hitters based on hits/RBI/HR, and relief pitchers with saves
    """
    top_players = {
        'away_team': {'pitcher': None, 'hitters': [], 'closer': None},
        'home_team': {'pitcher': None, 'hitters': [], 'closer': None}
    }

    for team_key, display_key in [('away_team_stats', 'away_team'), ('home_team_stats', 'home_team')]:
        team_stats = stats_data.get(team_key, {})

        # Get starting pitcher (first pitcher listed)
        pitchers = team_stats.get('Pitching', [])
        if pitchers:
            sp = pitchers[0]
            top_players[display_key]['pitcher'] = {
                'name': sp['name'],
                'stats': f"{sp['stats'].get('IP', '0')} IP, {sp['stats'].get('H', '0')} H, {sp['stats'].get('R', '0')} R, {sp['stats'].get('ER', '0')} ER, {sp['stats'].get('K', '0')} K"
            }

            # Check for relief pitchers with saves (usually last pitcher in list for winning team)
            for pitcher in pitchers:
                sv = pitcher['stats'].get('SV', '0')
                if sv and sv != '0':
                    top_players[display_key]['closer'] = {
                        'name': pitcher['name'],
                        'stats': f"SV ({pitcher['stats'].get('IP', '0')} IP)"
                    }
                    break

        # Get top hitters (sort by hits, then RBI, then HR)
        batters = team_stats.get('Batting', [])
        scored_batters = []
        for batter in batters:
            hits = _safe_int(batter['stats'].get('H', '0'))
            rbi = _safe_int(batter['stats'].get('RBI', '0'))
            hr = _safe_int(batter['stats'].get('HR', '0'))
            score = hits * 10 + rbi * 5 + hr * 15  # Weighted scoring

            if hits > 0 or rbi > 0 or hr > 0:
                scored_batters.append((batter, score))

        scored_batters.sort(key=lambda x: x[1], reverse=True)
        top_players[display_key]['hitters'] = [
            {
                'name': b[0]['name'],
                'stats': f"{b[0]['stats'].get('H', '0')}-{b[0]['stats'].get('AB', '0')}, {b[0]['stats'].get('RBI', '0')} RBI, {b[0]['stats'].get('HR', '0')} HR"
            }
            for b in scored_batters[:3]
        ]

    return top_players


def get_top_players_nba(stats_data):
    """
    Extract top players for NBA games.
    Returns: Top scorers and key contributors
    """
    top_players = {
        'away_team': {'players': []},
        'home_team': {'players': []}
    }

    def safe_int(value, default=0):
        """Safely convert string to int, handling non-numeric values"""
        if not value:
            return default
        try:
            # Handle empty strings, "Has not entered game", etc.
            cleaned_value = value.strip()
            if not cleaned_value or not cleaned_value.replace('-', '').isdigit():
                return default
            return int(cleaned_value)
        except (ValueError, TypeError):
            return default

    for team_key, display_key in [('away_team_stats', 'away_team'), ('home_team_stats', 'home_team')]:
        team_stats = stats_data.get(team_key, {})

        # Get all players and sort by points
        all_players = team_stats.get('Starters', []) + team_stats.get('Bench', [])
        scored_players = []

        for player in all_players:
            pts = safe_int(player['stats'].get('PTS', '0'))
            reb = safe_int(player['stats'].get('REB', '0'))
            ast = safe_int(player['stats'].get('AST', '0'))
            score = pts * 10 + reb * 3 + ast * 5  # Weighted scoring

            if pts > 0:  # Only include players who actually scored
                scored_players.append((player, score))

        scored_players.sort(key=lambda x: x[1], reverse=True)
        top_players[display_key]['players'] = [
            {
                'name': p[0]['name'],
                'stats': f"{p[0]['stats'].get('PTS', '0')} PTS, {p[0]['stats'].get('REB', '0')} REB, {p[0]['stats'].get('AST', '0')} AST"
            }
            for p in scored_players[:3]
        ]

    return top_players


def get_top_players_nfl(stats_data):
    """
    Extract top players for NFL games.
    Returns: QB, top rusher, top receiver
    """
    top_players = {
        'away_team': {'qb': None, 'rusher': None, 'receiver': None},
        'home_team': {'qb': None, 'rusher': None, 'receiver': None}
    }

    for team_key, display_key in [('away_team_stats', 'away_team'), ('home_team_stats', 'home_team')]:
        team_stats = stats_data.get(team_key, {})

        # Get QB (first passer)
        passers = team_stats.get('Passing', [])
        if passers:
            qb = passers[0]
            td = qb['stats'].get('TD', '0')
            td_str = f", {td} TD" if td != '0' else ""
            top_players[display_key]['qb'] = {
                'name': qb['name'],
                'stats': f"{qb['stats'].get('C/ATT', '0/0')}, {qb['stats'].get('YDS', '0')} YDS{td_str}, {qb['stats'].get('INT', '0')} INT"
            }

        # Get top rusher (by yards)
        rushers = team_stats.get('Rushing', [])
        if rushers:
            top_rusher = max(rushers, key=lambda r: _safe_int(r['stats'].get('YDS', '0')))
            td = top_rusher['stats'].get('TD', '0')
            td_str = f", {td} TD" if td != '0' else ""
            top_players[display_key]['rusher'] = {
                'name': top_rusher['name'],
                'stats': f"{top_rusher['stats'].get('CAR', '0')} CAR, {top_rusher['stats'].get('YDS', '0')} YDS{td_str}"
            }

        # Get top receiver (by yards)
        receivers = team_stats.get('Receiving', [])
        if receivers:
            top_receiver = max(receivers, key=lambda r: _safe_int(r['stats'].get('YDS', '0')))
            td = top_receiver['stats'].get('TD', '0')
            td_str = f", {td} TD" if td != '0' else ""
            top_players[display_key]['receiver'] = {
                'name': top_receiver['name'],
                'stats': f"{top_receiver['stats'].get('REC', '0')} REC, {top_receiver['stats'].get('YDS', '0')} YDS{td_str}"
            }

    return top_players


def get_top_players_nhl(stats_data):
    """
    Extract top players for NHL games.
    Returns: Goalies and goal scorers
    """
    top_players = {
        'away_team': {'goalie': None, 'scorers': []},
        'home_team': {'goalie': None, 'scorers': []}
    }

    for team_key, display_key in [('away_team_stats', 'away_team'), ('home_team_stats', 'home_team')]:
        team_stats = stats_data.get(team_key, {})

        # Get goalie
        goalies = team_stats.get('Goaltending', [])
        if goalies:
            goalie = goalies[0]  # Primary goalie
            top_players[display_key]['goalie'] = {
                'name': goalie['name'],
                'stats': f"{goalie['stats'].get('SA', '0')} SA, {goalie['stats'].get('SV', '0')} SV, {goalie['stats'].get('SV%', '.000')} SV%"
            }

        # Get point scorers (players with a goal or 2 assists)
        skaters = team_stats.get('Skaters', [])
        scorers = []
        for skater in skaters:
            goals = _safe_int(skater['stats'].get('G', '0'))
            assists = _safe_int(skater['stats'].get('A', '0'))
            if goals > 0:
                scorers.append((skater, goals * 10 + assists * 5))
            if assists > 1: # this needs to be adjusted
                scorers.append((skater, assists))
        scorers.sort(key=lambda x: x[1], reverse=True)
        top_players[display_key]['scorers'] = [
            {
                'name': s[0]['name'],
                'stats': f"{s[0]['stats'].get('G', '0')} G, {s[0]['stats'].get('A', '0')} A, {s[0]['stats'].get('PTS', '0')} PTS"
            }
            for s in scorers[:3]
        ]

    return top_players


# Maps an ESPN summary-API statistics group to the bucket key the get_top_players_*
# functions expect. The group identifier lives in `type` for some sports (MLB) and
# in `name` for others (NFL/NHL); NBA has neither. Returning None ignores the group.
def _normalize_group_name(sport_name, raw_name, raw_type=None):
    sport = (sport_name or '').upper()
    name = (raw_type or raw_name or '').lower()
    if sport == 'MLB':
        return {'batting': 'Batting', 'pitching': 'Pitching'}.get(name)
    if sport == 'NBA':
        # NBA returns a single unnamed group of all players; get_top_players_nba
        # merges 'Starters' + 'Bench', so dumping everything into 'Starters' works.
        return 'Starters'
    if sport == 'NFL':
        return {'passing': 'Passing', 'rushing': 'Rushing', 'receiving': 'Receiving'}.get(name)
    if sport == 'NHL':
        if name in ('forwards', 'defenses', 'skaters'):
            return 'Skaters'
        if name == 'goalies':
            return 'Goaltending'
    return None


def _parse_summary_players(summary, sport_name):
    """Convert an ESPN summary API payload into the stats_data dict shape that the
    get_top_players_* functions consume:
        {'away_team_stats': {GroupKey: [{'name', 'stats': {label: value}}]}, 'home_team_stats': {...}}
    """
    boxscore = summary.get('boxscore', {}) or {}
    # team id -> homeAway ('away'/'home') comes from boxscore.teams
    side_by_id = {
        (t.get('team') or {}).get('id'): t.get('homeAway')
        for t in (boxscore.get('teams') or [])
    }

    stats_data = {'away_team_stats': {}, 'home_team_stats': {}}
    for team_block in (boxscore.get('players') or []):
        tid = (team_block.get('team') or {}).get('id')
        side = side_by_id.get(tid)
        if side == 'away':
            dest = stats_data['away_team_stats']
        elif side == 'home':
            dest = stats_data['home_team_stats']
        else:
            continue

        for group in team_block.get('statistics', []):
            key = _normalize_group_name(sport_name, group.get('name'), group.get('type'))
            if not key:
                continue
            labels = group.get('labels', []) or []
            bucket = dest.setdefault(key, [])
            for ath in group.get('athletes', []):
                athlete = ath.get('athlete', {}) or {}
                name = athlete.get('shortName') or athlete.get('displayName') or ''
                sdict = dict(zip(labels, ath.get('stats', []) or []))
                # NHL skater feed has no PTS column; derive it from G + A.
                if (sport_name or '').upper() == 'NHL' and key == 'Skaters':
                    try:
                        sdict['PTS'] = str(int(sdict.get('G', '0') or 0) + int(sdict.get('A', '0') or 0))
                    except (ValueError, TypeError):
                        pass
                bucket.append({'name': name, 'stats': sdict})

    return stats_data


def _dispatch_top_players(stats_data, sport_name):
    sp = (sport_name or '').lower()
    if sp == 'mlb':
        return get_top_players_mlb(stats_data)
    if sp == 'nba':
        return get_top_players_nba(stats_data)
    if sp == 'nfl':
        return get_top_players_nfl(stats_data)
    if sp == 'nhl':
        return get_top_players_nhl(stats_data)
    return None


def get_boxscore_stats_api(game_id, sport_name, timeout=15):
    """Fetch detailed box score stats from ESPN's JSON summary API.

    This is the same backend ESPN's own site renders from, so the data is at the
    same (or lower) latency than the old HTML scrape, and far more robust.
    Returns a top_players dict or None on failure.
    """
    api_path = _api_path_for_sport(sport_name)
    if not api_path:
        return None
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    url = f"https://site.api.espn.com/apis/site/v2/sports/{api_path}/summary?event={game_id}"
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    stats_data = _parse_summary_players(response.json(), sport_name)
    return _dispatch_top_players(stats_data, sport_name)


def get_boxscore_stats(game_id, sport_name):
    """
    Fetch box score stats from ESPN and extract top players.
    This is called separately when we want detailed player stats.

    Primary source is ESPN's JSON summary API; falls back to the legacy HTML
    boxscore scrape if the API call fails.

    Args:
        game_id: ESPN game ID (e.g., "401772749")
        sport_name: Sport name in lowercase (e.g., "nfl", "nhl", "mlb", "nba")

    Returns:
        Dictionary with top players or None if error
    """
    # Primary: JSON summary API
    try:
        top_players = get_boxscore_stats_api(game_id, sport_name)
        if top_players:
            return top_players
    except Exception as e:
        print(f"Summary API failed for {sport_name} game {game_id}, falling back to HTML: {e}")

    # Fallback: legacy HTML boxscore scrape
    try:
        soup = _fetch_boxscore_soup(game_id, sport_name, timeout=10)
        if not soup:
            return None

        # Parse box score stats
        stats_data = parse_boxscore_stats(soup, sport_name)
        return _dispatch_top_players(stats_data, sport_name)

    except Exception as e:
        print(f"Error fetching boxscore stats for game {game_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_game_status_from_boxscore(game_id, sport_name):
    """
    Fetch game status from the boxscore page for games where JavaScript is needed.
    This is a fallback for NFL/NHL games where the scoreboard doesn't have the time in static HTML.

    Args:
        game_id: ESPN game ID (e.g., "401772749")
        sport_name: Sport name in lowercase (e.g., "nfl", "nhl")

    Returns:
        Status string like "2:00 - 4th" or None if not found
    """
    soup = _fetch_boxscore_soup(game_id, sport_name, timeout=5)
    if not soup:
        return None

    try:
        # Find spans with class containing "hsDdd" and "FuEs" (the time and quarter)
        time_spans = soup.find_all('span', class_=re.compile(r'hsDdd.*FuEs'))

        if len(time_spans) >= 2:
            # First span is time, second is quarter/period
            time_text = time_spans[0].text.strip()
            period_text = time_spans[1].text.strip()
            status = f"{time_text} - {period_text}"
            return status

        return None

    except Exception as e:
        print(f"Error parsing boxscore status for game {game_id}: {e}")
        return None

def get_game_status_from_scoreboard(section):
    """
    Extract game status directly from scoreboard section HTML.
    Works for all sports (MLB, NBA, NFL, NHL).
    Returns the status text from ScoreCell__Time element.

    Args:
        section: BeautifulSoup object of the entire <section class="Scoreboard"> element
    """
    try:
        # Find the status element - search from the section root
        # The ScoreCell__Time is inside ScoreboardScoreCell__Overview
        status_element = section.find('div', class_='ScoreCell__Time')

        if status_element and status_element.text.strip():
            status_text = status_element.text.strip()
            return status_text

        # Try to find game status in ScoreboardScoreCell__GameNote (for some game states)
        game_note = section.find('div', class_='ScoreboardScoreCell__GameNote')
        if game_note and game_note.text.strip():
            return game_note.text.strip()

        # Check for LastPlay which sometimes contains timing info (works for NBA)
        # Look in the parent container, not just this section
        parent_container = section.find_parent('div', class_='Scoreboard__RowContainer')
        if parent_container:
            last_play = parent_container.find('section', class_='LastPlay')
        else:
            last_play = section.find('section', class_='LastPlay')

        if last_play:
            play_header = last_play.find('h1')
            if play_header and play_header.text.strip():
                play_text = play_header.text.strip()
                # Extract just the time/quarter part (e.g., "Last Play 1:54 - 1st" -> "1:54 - 1st")
                if 'Last Play' in play_text:
                    time_part = play_text.replace('Last Play', '').strip()
                    if time_part:
                        return time_part

        # If no status text found, check the CSS classes for game state
        score_cell = section.find('div', class_=re.compile(r'ScoreboardScoreCell'))
        if score_cell:
            classes = ' '.join(score_cell.get('class', []))
            if 'ScoreboardScoreCell--post' in classes:
                return 'Final'
            elif 'ScoreboardScoreCell--in' in classes:
                return 'Live'
            elif 'ScoreboardScoreCell--pre' in classes:
                return 'Pre-Game'

        return 'Pre-Game'
    except Exception as e:
        print(f"Error extracting game status: {e}")
        return 'Unknown'

# ESPN's public JSON scoreboard API. Maps our sport names to the league paths.
# This replaces the old HTML/CSS-class scraping, which broke when ESPN renamed
# its scoreboard CSS classes (e.g. ScoreCell__Score no longer exists), causing
# every game to be silently dropped and the ticker to show nothing.
ESPN_API_PATHS = {
    "MLB": "baseball/mlb",
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "NHL": "hockey/nhl",
}


def _api_path_for_sport(sport_name, url=""):
    """Resolve the ESPN JSON API league path from a sport name (or fall back to URL)."""
    if sport_name and sport_name.upper() in ESPN_API_PATHS:
        return ESPN_API_PATHS[sport_name.upper()]
    # Fall back to inferring from a legacy scoreboard URL like .../mlb/scoreboard
    for name, path in ESPN_API_PATHS.items():
        if f"/{name.lower()}/" in (url or "").lower():
            return path
    return None


def _status_from_event(status_type):
    """Translate an ESPN status block into the strings the ticker expects.

    Returns 'Final' for completed games, 'Pre-Game' for scheduled games, and
    the human-readable live detail (e.g. 'Bottom 7th', '10:18 - 1st Quarter')
    for in-progress games.
    """
    state = (status_type or {}).get('state')
    if state == 'post':
        return 'Final'
    if state == 'pre':
        return 'Pre-Game'
    # In-progress: prefer the full detail, fall back to the short detail.
    return (status_type or {}).get('detail') or (status_type or {}).get('shortDetail') or 'Live'


def scrape_espn_scores(url="https://www.espn.com/mlb/scoreboard", max_workers=10, sport_name="MLB", include_stats=False):
    """
    Fetch scores from ESPN's public JSON scoreboard API.

    Args:
        url: Legacy ESPN scoreboard URL (only used to infer the league if sport_name is unknown)
        max_workers: Thread pool size for fetching per-game box score stats in parallel
        sport_name: Sport name (e.g., "MLB", "NBA", "NFL", "NHL")
        include_stats: If True, fetches detailed box score stats for each game (slower)

    Returns:
        List of game dictionaries with scores and optionally player stats
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    api_path = _api_path_for_sport(sport_name, url)
    if not api_path:
        print(f"scrape_espn_scores: unknown sport '{sport_name}' (url={url})")
        return []

    api_url = f"https://site.api.espn.com/apis/site/v2/sports/{api_path}/scoreboard"
    response = requests.get(api_url, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()

    games = []
    for event in data.get('events', []):
        competitions = event.get('competitions') or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get('competitors') or []
        if len(competitors) != 2:
            continue

        # ESPN lists competitors in arbitrary order; key by homeAway.
        by_side = {c.get('homeAway'): c for c in competitors}
        away = by_side.get('away')
        home = by_side.get('home')
        if not away or not home:
            continue

        game_id = event.get('id')
        game_status = _status_from_event(event.get('status', {}).get('type', {}))

        def _team_name(comp):
            team = comp.get('team') or {}
            return team.get('shortDisplayName') or team.get('displayName') or team.get('abbreviation') or ''

        def _record(comp):
            records = comp.get('records') or []
            return records[0].get('summary', '') if records else ''

        def _period_scores(comp):
            return [str(ls.get('value', '')) for ls in (comp.get('linescores') or [])]

        game_data = {
            'game_id': game_id,
            'away_team': _team_name(away),
            'home_team': _team_name(home),
            'away_record': _record(away),
            'home_record': _record(home),
            'away_score': {
                'runs': str(away.get('score', '')),
                'period_scores': _period_scores(away),
            },
            'home_score': {
                'runs': str(home.get('score', '')),
                'period_scores': _period_scores(home),
            },
            'status': game_status,
        }

        games.append(game_data)

    # Fetch detailed box score stats in parallel. Pre-game events have no stats
    # yet, so skip them entirely (saves a wasted request per scheduled game).
    if include_stats:
        stat_games = [g for g in games if g.get('game_id') and g['status'] != 'Pre-Game']
        if stat_games:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_game = {
                    executor.submit(get_boxscore_stats, g['game_id'], sport_name): g
                    for g in stat_games
                }
                for future in as_completed(future_to_game):
                    g = future_to_game[future]
                    try:
                        top_players = future.result()
                        if top_players:
                            g['top_players'] = top_players
                    except Exception as e:
                        print(f"Error fetching boxscore stats for {sport_name} game {g['game_id']}: {e}")

    return games

def scrape_all_sports(max_workers=10, include_stats=True):
    """
    Scrape live scores from all major sports leagues.

    Args:
        max_workers: Thread pool size for parallel per-game box score stat fetches
        include_stats: If True, fetches detailed box score stats for each game (default: True)

    Returns:
        Dictionary with scores and optionally player stats for all sports
    """
    sports_config = {
        "MLB": {"url": "https://www.espn.com/mlb/scoreboard", "icon": "⚾"},
        "NBA": {"url": "https://www.espn.com/nba/scoreboard", "icon": "🏀"},
        "NFL": {"url": "https://www.espn.com/nfl/scoreboard", "icon": "🏈"},
        "NHL": {"url": "https://www.espn.com/nhl/scoreboard", "icon": "🏒"}
    }

    all_scores = {}

    # Scrape all sports concurrently; each call also fetches its games' stats in
    # parallel internally, so the whole multi-sport pull overlaps end to end.
    with ThreadPoolExecutor(max_workers=len(sports_config)) as executor:
        future_to_sport = {
            executor.submit(
                scrape_espn_scores,
                url=config['url'],
                max_workers=max_workers,
                sport_name=sport_name,
                include_stats=include_stats,
            ): (sport_name, config)
            for sport_name, config in sports_config.items()
        }
        for future in as_completed(future_to_sport):
            sport_name, config = future_to_sport[future]
            try:
                scores = future.result()
                if scores:
                    all_scores[sport_name] = {
                        "games": scores,
                        "icon": config["icon"],
                    }
            except Exception as e:
                print(f"Error scraping {sport_name}: {e}")

    # Preserve the original MLB/NBA/NFL/NHL ordering (as_completed returns out of order)
    return {name: all_scores[name] for name in sports_config if name in all_scores}

def save_to_json(data, filename='espn_scores.json'):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved {len(data)} games to {filename}")

if __name__ == "__main__":
    
    print("Starting ESPN score scraper...")
    start_time = time.time()
    
    scores = scrape_espn_scores()
    
    end_time = time.time()
    print(f"Scraped {len(scores)} games in {end_time - start_time:.2f} seconds")
    
    save_to_json(scores)
