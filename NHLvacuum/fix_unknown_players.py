#!/usr/bin/env python3
"""
Fix unknown player IDs by scraping Natural Stat Trick player list
"""

import sqlite3
import requests
import time
from bs4 import BeautifulSoup
import re

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
    print("\n" + "="*60)
    print("FETCHING PLAYER IDs FROM NATURAL STAT TRICK")
    print("="*60 + "\n")

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
        players = get_nst_player_ids_for_range(from_season, thru_season)

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

    print("="*60)
    print(f"TOTAL: Found {len(all_players)} unique players across all seasons")
    print("="*60 + "\n")

    return all_players

def fix_unknown_players(db_path='nhl_analytics.db'):
    """Update unknown player IDs in database"""

    # Get player IDs from NST
    nst_players = get_nst_player_ids()

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
    print(f"\nFound {len(unknown_players)} unknown players in database")

    fixed_count = 0
    not_found = []

    for old_id, name, position in unknown_players:
        # Try exact match first
        nst_id = nst_players.get(name)

        if nst_id:
            # Check if this player_id already exists
            cursor.execute("SELECT COUNT(*) FROM players WHERE player_id = ?", (nst_id,))
            if cursor.fetchone()[0] > 0:
                # Player ID already exists, just update stats references and delete unknown
                print(f"⚠ Merging: {name} ({position}) -> existing {nst_id}")
            else:
                # Update player_id in players table
                cursor.execute("""
                    UPDATE players
                    SET player_id = ?
                    WHERE player_id = ?
                """, (nst_id, old_id))

            # Update all references in stats tables
            # Strategy: Delete duplicates first, then update remaining records
            for table in ['player_game_stats', 'goalie_game_stats',
                         'player_onice_stats', 'player_shift_stats',
                         'line_combinations']:

                if table == 'line_combinations':
                    # Handle player1_id, player2_id, player3_id
                    for col in ['player1_id', 'player2_id', 'player3_id']:
                        # Just update - line_combinations doesn't have unique constraint issues
                        cursor.execute(f"""
                            UPDATE {table}
                            SET {col} = ?
                            WHERE {col} = ?
                        """, (nst_id, old_id))
                else:
                    # For stats tables, delete UNKNOWN records where real record already exists
                    # This handles the case where a player appears with both IDs in same game
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

    print(f"\n{'='*60}")
    print(f"Fixed {fixed_count} players")
    print(f"Could not find {len(not_found)} players:")
    for name, pos in not_found[:10]:
        print(f"  - {name} ({pos})")
    if len(not_found) > 10:
        print(f"  ... and {len(not_found) - 10} more")
    print(f"{'='*60}")

if __name__ == '__main__':
    fix_unknown_players()
