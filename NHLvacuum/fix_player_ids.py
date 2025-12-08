import sqlite3
import unicodedata

DB_PATH = 'nhl_analytics.db'

def normalize_name(name):
    if not name:
        return ""
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('utf-8')
    return "".join(c for c in n.lower() if c.isalnum())

def fix_player_ids():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("--- Scanning for Player ID Issues (Optimized) ---")
    
    # 1. Fetch all UNKNOWN players from players table
    cursor.execute("SELECT player_id, player_name FROM players WHERE player_id LIKE 'UNKNOWN%'")
    unknown_players_db = cursor.fetchall() # [(id, name), ...]
    unknown_ids_set = set(u[0] for u in unknown_players_db)
    
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
        valid_map = {}
        for pid, name in valid_players_db:
            norm = normalize_name(name)
            valid_map[norm] = {'id': pid, 'name': name}
            
        # We also need the names of the used_unknowns to match them
        # We can get them from our initial fetch `unknown_players_db`
        unknown_name_map = {uid: name for uid, name in unknown_players_db}
        
        migrations = []
        for uid in used_unknowns:
            uname = unknown_name_map.get(uid)
            if not uname: continue
            
            unorm = normalize_name(uname)
            if unorm in valid_map:
                valid = valid_map[unorm]
                migrations.append((uid, valid['id'], uname, valid['name']))
            else:
                print(f"Warning: Could not find match for active unknown: {uname} ({uid})")

        print(f"Found matches for {len(migrations)} active players.")
        
        # Execute Migrations
        # Columns to update: same as check_map
        # Logic: Try Update. If fail (duplicate), Delete.
        
        for uid, vid, uname, vname in migrations:
            print(f"Migrating: {uname} -> {vname}")
            
            for table, cols in check_map:
                for col in cols:
                    try:
                        # 1. Try to update rows to valid ID
                        # This will fail if (game_id, player_id) constraint exists and valid ID is already there
                        cursor.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (vid, uid))
                    except sqlite3.IntegrityError:
                        # 2. If update failed, it means valid data exists. Delete the duplicate UNKNOWN data.
                        # print(f"  Conflict in {table}.{col} - deleting duplicate rows.")
                        cursor.execute(f"DELETE FROM {table} WHERE {col} = ?", (uid,))
                    except Exception as e:
                        print(f"  Error updating {table}.{col}: {e}")

            # Finally delete the player record itself
            cursor.execute("DELETE FROM players WHERE player_id = ?", (uid,))
            
        conn.commit()
        print("Migration complete.")

    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    fix_player_ids()
