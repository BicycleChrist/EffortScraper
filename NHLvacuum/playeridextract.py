#!/usr/bin/env python3
"""
Extract player IDs from Natural Stat Trick across all season spans.
NST allows a maximum of 3 seasons per page, so we iterate through
season ranges starting from 2007-08.
"""

import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime

# NST URL template
NST_URL_TEMPLATE = "https://www.naturalstattrick.com/playerlist.php?fromseason={from_season}&thruseason={thru_season}&stype=2&sit=5v5&stdoi=oi&rate=n"

# First season of data
START_YEAR = 2025
# Maximum seasons per request
MAX_SEASONS_PER_REQUEST = 3


def generate_season_string(year):
    """Convert year to season string format (e.g., 2007 -> '20072008')"""
    return f"{year}{year + 1}"


def generate_season_spans(start_year, end_year=None):
    """Generate season span tuples for iteration

    Args:
        start_year: First year to start from (e.g., 2007 for 2007-08 season)
        end_year: Last year to end at (defaults to current year)

    Returns:
        List of tuples: [(from_season, thru_season), ...]
    """
    if end_year is None:
        # Use current year
        current_month = datetime.now().month
        current_year = datetime.now().year
        # If we're before August, use previous year as the latest complete season
        end_year = current_year if current_month >= 8 else current_year - 1

    spans = []
    current_start = start_year

    while current_start <= end_year:
        # Calculate end of this span (max 3 seasons)
        span_end = min(current_start + MAX_SEASONS_PER_REQUEST - 1, end_year)

        from_season = generate_season_string(current_start)
        thru_season = generate_season_string(span_end)

        spans.append((from_season, thru_season))

        # Move to next span
        current_start = span_end + 1

    return spans


def _get_nst_session():
    """Build an authenticated NST session (carries the nst-key header + UA).

    NST now blocks unauthenticated playerlist.php requests with HTTP 403, so we
    reuse nstparse's session machinery which loads the key from nst_api_key.txt.
    Falls back to a plain session if nstparse is unavailable.
    """
    try:
        import nstparse
        return nstparse.create_session()
    except Exception as e:
        print(f"Warning: could not build authenticated NST session ({e}); using unauthenticated request.")
        return requests.Session()


def download_html(url, file_name, session=None):
    """Download HTML content from URL using an authenticated NST session.

    NST requires BOTH the nst-key header (from the session) and a Referer header;
    nst-key alone still returns HTTP 403 on playerlist.php.
    """
    sess = session or _get_nst_session()
    headers = {'Referer': 'https://www.naturalstattrick.com/'}
    response = sess.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    with open(file_name, 'w', encoding='utf-8') as file:
        file.write(response.text)


def parse_player_names_ids(file_path):
    """Parse player names and IDs from HTML file"""
    with open(file_path, 'r', encoding='utf-8') as file:
        html_content = file.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    players = {}

    # Find all 'a' tags with 'href' attributes containing 'playerreport.php'
    player_links = soup.find_all('a', href=lambda href: href and 'playerreport.php' in href)

    for link in player_links:
        if 'Summary' in link.text:
            try:
                # Extract the player ID from the 'href' attribute
                player_id = link['href'].split('&')[2].split('=')[1]
                # Get player name from previous 'tr' element
                player_name = link.find_previous('tr').find_all('td')[0].text.strip()
                # Add to dictionary (will overwrite duplicates, which is fine)
                players[player_name] = player_id
            except (IndexError, AttributeError) as e:
                print(f"Warning: Could not parse player link: {link.get('href', 'unknown')}")
                continue

    return players


def write_players_to_file(players_dict, output_file):
    """Write player data to file in sorted order"""
    with open(output_file, 'w', encoding='utf-8') as file:
        for name in sorted(players_dict.keys()):
            player_id = players_dict[name]
            file.write(f"{name}: {player_id}\n")


def extract_players(start_year, end_year=None):
    """
    Extract player IDs from NST for a specific season range.
    
    Args:
        start_year: First year to start from
        end_year: Last year to end at (defaults to current year)
        
    Returns:
        Dictionary of {player_name: player_id}
    """
    print(f"Extraction range: {start_year} to {end_year if end_year else 'Current'}")
    
    # Generate season spans
    season_spans = generate_season_spans(start_year, end_year)
    print(f"Extracting player IDs from {len(season_spans)} season spans...")
    
    if not season_spans:
        print("No seasons to process.")
        return {}

    print(f"Starting: {season_spans[0][0][:4]}-{season_spans[0][0][4:]}")
    print(f"Ending:   {season_spans[-1][1][:4]}-{season_spans[-1][1][4:]}\n")

    all_players = {}
    session = _get_nst_session()

    for i, (from_season, thru_season) in enumerate(season_spans, 1):
        url = NST_URL_TEMPLATE.format(from_season=from_season, thru_season=thru_season)
        temp_file = "playerlist_temp.html"

        print(f"[{i}/{len(season_spans)}] Processing {from_season[:4]}-{from_season[4:]} to {thru_season[:4]}-{thru_season[4:]}...", end=' ')

        try:
            download_html(url, temp_file, session=session)
            players = parse_player_names_ids(temp_file)

            # Merge into master dictionary
            all_players.update(players)

            print(f"✓ Found {len(players)} players (Total unique: {len(all_players)})")

            # Be nice to the server
            if i < len(season_spans):
                time.sleep(0.5)

        except Exception as e:
            print(f"✗ Error: {e}")
            continue
            
    # Clean up temp file
    import os
    if os.path.exists("playerlist_temp.html"):
        os.remove("playerlist_temp.html")
        
    return all_players


def main():
    """Main extraction logic"""
    print(f"{'='*60}")
    print("Natural Stat Trick Player ID Extractor")
    print(f"{'='*60}\n")
    
    # Use global START_YEAR
    all_players = extract_players(START_YEAR)

    # Write final output
    output_file = 'player_ids_new.txt'
    write_players_to_file(all_players, output_file)

    print(f"\n{'='*60}")
    print(f"Extraction complete!")
    print(f"Total unique players: {len(all_players)}")
    print(f"Output file: {output_file}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()

