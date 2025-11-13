#!/usr/bin/env python3
"""
Fix unknown player IDs by scraping Natural Stat Trick player list
"""

import sqlite3
import requests
import time
import os
from bs4 import BeautifulSoup
import re

# ============================================================================
# CONFIGURATION
# ============================================================================

# Set to True to fetch fresh player data from Natural Stat Trick
# Set to False to use existing player_ids.txt file only
FETCH_NEW_PLAYER_DATA = True

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

    # Save to player_ids.txt with UTF-8 encoding
    save_player_ids_to_file(all_players)

    return all_players


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


def fix_unknown_players(db_path='nhl_analytics.db'):
    """Update unknown player IDs in database"""

    # Get player IDs from NST or load from file
    if FETCH_NEW_PLAYER_DATA:
        print("\n→ Fetching fresh player data from Natural Stat Trick...")
        nst_players = get_nst_player_ids()
    else:
        print("\n→ Loading player data from player_ids.txt...")
        nst_players = load_player_ids_from_file()
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
                    # Strategy: Find and delete line combos with UNKNOWN IDs where a matching combo with real IDs exists

                    # First, find all unique line combos with the old_id
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
