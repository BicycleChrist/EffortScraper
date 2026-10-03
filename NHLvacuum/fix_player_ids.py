import sqlite3
import unicodedata

DB_PATH = 'nhl_analytics.db'
PLAYER_IDS_FILE = 'player_ids.txt'

def normalize_name(name):
    if not name:
        return ""
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('utf-8')
    return "".join(c for c in n.lower() if c.isalnum())


def load_player_ids_file(path=PLAYER_IDS_FILE):
    """Load player_ids.txt into {normalized_name: (real_id, is_goalie)}.

    File format: "Player Name: 8482475" or "Player Name: 8482475 [G]" for goalies.
    Used as a fallback to seed valid player records for UNKNOWN players (mostly
    rookies/prospects) that have no numeric-ID twin in the players table yet.
    """
    mapping = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if ':' not in line:
                    continue
                name, rest = line.split(':', 1)
                rest = rest.strip()
                is_goalie = rest.endswith('[G]')
                real_id = rest.replace('[G]', '').strip()
                if real_id:
                    mapping[normalize_name(name)] = (real_id, is_goalie)
    except FileNotFoundError:
        print(f"Warning: {path} not found; cannot seed missing player records.")
    return mapping

def fix_player_ids(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- Scanning for Player ID Issues (Optimized) ---")
    
    # 1. Fetch all UNKNOWN players from players table
    cursor.execute("SELECT player_id, player_name, position FROM players WHERE player_id LIKE 'UNKNOWN%'")
    unknown_rows = cursor.fetchall() # [(id, name, position), ...]
    unknown_players_db = [(u[0], u[1]) for u in unknown_rows]
    unknown_ids_set = set(u[0] for u in unknown_players_db)

    # Per normalized name, pick the best display name (prefer one with a space, i.e.
    # the NST variant over the no-space MoneyPuck variant) and most specific position
    # (anything over the generic 'F') for seeding new records.
    seed_info = {}  # norm_name -> {'name': str, 'position': str}
    for _uid, _name, _pos in unknown_rows:
        norm = normalize_name(_name)
        info = seed_info.setdefault(norm, {'name': _name, 'position': _pos or 'F'})
        if ' ' in _name and ' ' not in info['name']:
            info['name'] = _name  # prefer spaced name
        if info['position'] in (None, 'F') and _pos not in (None, 'F'):
            info['position'] = _pos  # prefer specific position
    
    print(f"Total 'UNKNOWN' entries in players table: {len(unknown_players_db)}")

    # 2. Identify which UNKNOWN IDs are actually used in stats (Active vs Orphan)
    # We scan the columns where player_id references exist.
    used_unknowns = set()
    
    # Table : Columns to check
    check_map = [
        ('player_game_stats', ['player_id']),
        ('goalie_game_stats', ['player_id']),
        ('player_onice_stats', ['player_id']),
        ('player_shift_stats', ['player_id']),
        ('mp_skater_game_stats', ['player_id']),
        ('mp_goalie_game_stats', ['player_id']),
        ('line_combinations', ['player1_id', 'player2_id', 'player3_id']),
        ('player_linemate_stats', ['player_id', 'linemate_id']),
        ('player_opposition_stats', ['player_id', 'opponent_id']),
        ('mp_shots', ['shooter_player_id', 'goalie_player_id']),
        ('edge_player_season_stats', ['player_id']),
        ('edge_shot_events', ['player_id']),
        ('edge_skating_speed_events', ['player_id'])
    ]
    
    print("Scanning statistics tables for usage...")
    for table, cols in check_map:
        for col in cols:
            try:
                # Optimized: Only fetch DISTINCT IDs that look like 'UNKNOWN%'
                query = f"SELECT DISTINCT {col} FROM {table} WHERE {col} LIKE 'UNKNOWN%'"
                cursor.execute(query)
                rows = cursor.fetchall()
                for r in rows:
                    if r[0]: # check not None
                        used_unknowns.add(r[0])
            except sqlite3.OperationalError:
                # Table might not exist or column might be wrong (though schema says otherwise)
                print(f"Warning: Could not check {table}.{col}")
                pass

    print(f"Active 'UNKNOWN' players (with stats): {len(used_unknowns)}")
    
    # 3. Identify Orphans (In players table, but NOT in any stats table)
    orphans = unknown_ids_set - used_unknowns
    print(f"Orphan 'UNKNOWN' players (to be deleted): {len(orphans)}")

    # 4. Delete Orphans
    if orphans:
        print("Deleting orphans...")
        orphan_list = list(orphans)
        batch_size = 900 # Safe limit for SQLite variables
        deleted_count = 0
        
        for i in range(0, len(orphan_list), batch_size):
            batch = orphan_list[i:i+batch_size]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(f"DELETE FROM players WHERE player_id IN ({placeholders})", batch)
            deleted_count += cursor.rowcount
            
        conn.commit()
        print(f"Deleted {deleted_count} orphan records.")

    # 5. Migrate Active Unknowns
    # We need to map these `used_unknowns` to valid IDs.
    if used_unknowns:
        print("\n--- Migrating Active Unknowns ---")
        
        # Fetch ALL players again to build validation map (only need valid ones now)
        cursor.execute("SELECT player_id, player_name FROM players WHERE player_id NOT LIKE 'UNKNOWN%'" )
        valid_players_db = cursor.fetchall()
        
        # Build normalization map: valid_norm_name -> {id, name}
        valid_map, ambiguous = {}, set()
        for pid, name in valid_players_db:
            norm = normalize_name(name)
            if norm in valid_map and str(valid_map[norm]['id']) != str(pid):
                ambiguous.add(norm)   # two real players share this name (e.g. Elias Pettersson, Sebastian Aho)
            valid_map[norm] = {'id': pid, 'name': name}
        for norm in ambiguous:
            valid_map.pop(norm, None)   # never guess between same-name players

        # Fallback source: real NHL ids from player_ids.txt to seed missing records
        pid_file_map = load_player_ids_file()
        seeded_count = 0

        # We also need the names of the used_unknowns to match them
        # We can get them from our initial fetch `unknown_players_db`
        unknown_name_map = {uid: name for uid, name in unknown_players_db}

        migrations = []
        for uid in used_unknowns:
            uname = unknown_name_map.get(uid)
            if not uname: continue

            unorm = normalize_name(uname)

            # If no valid record exists yet, try to seed one from player_ids.txt
            # (covers rookies/prospects that were never registered with a real id).
            if unorm not in valid_map and unorm in pid_file_map:
                real_id, is_goalie = pid_file_map[unorm]
                info = seed_info.get(unorm, {'name': uname, 'position': 'F'})
                position = 'G' if is_goalie else info['position']
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO players (player_id, player_name, position) VALUES (?, ?, ?)",
                        (real_id, info['name'], position)
                    )
                    valid_map[unorm] = {'id': real_id, 'name': info['name']}
                    seeded_count += 1
                    print(f"Seeded missing player: {info['name']} ({real_id}, {position})")
                except sqlite3.Error as e:
                    print(f"  Could not seed {info['name']} ({real_id}): {e}")

            if unorm in ambiguous:
                print(f"Warning: '{uname}' ({uid}) matches more than one real player — NOT migrated, resolve by hand")
                continue
            if unorm in valid_map:
                valid = valid_map[unorm]
                migrations.append((uid, valid['id'], uname, valid['name']))
            else:
                print(f"Warning: Could not find match for active unknown: {uname} ({uid})")

        if seeded_count:
            print(f"Seeded {seeded_count} new player records from {PLAYER_IDS_FILE}.")

        print(f"Found matches for {len(migrations)} active players.")
        
        # Execute Migrations
        # Columns to update: same as check_map
        # Logic (hardened 2026-10-02): UPDATE OR IGNORE moves every row that can move; rows still left under the
        # UNKNOWN id collided with an identical key already filed under the real id -> those (and only those) are
        # duplicates and get deleted. (The old version deleted ALL of the id's rows in a table on any conflict.)

        for uid, vid, uname, vname in migrations:
            moved = dup = 0
            for table, cols in check_map:
                for col in cols:
                    try:
                        before = conn.total_changes
                        cursor.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?", (vid, uid))
                        moved += conn.total_changes - before
                        before = conn.total_changes
                        cursor.execute(f"DELETE FROM {table} WHERE {col} = ?", (uid,))
                        dup += conn.total_changes - before
                    except sqlite3.OperationalError:
                        pass   # table/column not present in this DB
                    except Exception as e:
                        print(f"  Error updating {table}.{col}: {e}")
            print(f"Migrated: {uname} -> {vname} ({vid}) | rows moved {moved}, duplicate rows removed {dup}")

            # Finally delete the player record itself
            cursor.execute("DELETE FROM players WHERE player_id = ?", (uid,))
            
        conn.commit()
        print("Migration complete.")

    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    fix_player_ids()
