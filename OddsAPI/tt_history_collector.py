import asyncio
import aiohttp
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from TableTennisClient import TT_KEY, BASE_URL, get_event_view_async
from ttDB import TTDatabase


#TODO: Last stopped gathering set data 1/19/23

# ========== CONFIGURATION SETTINGS ==========
CONFIG = {
    # Common settings
    "sport_id": 92,  # Table tennis sport ID
    "days_back": 5,  # Number of days to look back for current data
    
    # Data sources
    "current_leagues": {
        22307: "Setka Cup",
        # 22742: "Czech Republic Liga Pro",
        # 22534: "TT CUP",
        # 24536: "Poland TT Elite Series"
    },
    "historical_leagues": {
        # 38397: {"name": "Setka Cup 2023", "year": 2023}
        # 38396: {"name": "Setka Cup 2022", "year": 2022},
        # 38395: {"name": "Setka Cup 2021", "year": 2021},
        # 38394: {"name": "Setka Cup 2020", "year": 2020}
    },
    "players": [
        #{"id": 359529, "name": "Hryhorii Kulishov"}
    ],
    
    # Operation modes (set only ONE to True)
    "mode": "populate_sets",  # One of: current_matches, historical_matches, player_data, populate_sets, historical_odds
    
    # API and processing settings
    "batch_size": 100,     # Number of API requests to make in parallel
    "commit_frequency": 100,  # How often to commit to DB (number of matches)
    "api_delay": 0.2,       # Delay between API calls in seconds
    "max_matches": None,    # Maximum number of matches to process (None for all)
    "bookmaker": "bet365",  # Bookmaker to use for odds data
    
    # Output path
    "save_dir": Path.cwd() / "TTT_savedata"
}

# ========== UTILITY FUNCTIONS ==========

async def get_paged_events(session, event_type, params, page_limit=50):
    """Get all pages of events from the API"""
    events = []
    page = 1
    total_pages = 1
    
    while page <= total_pages:
        params["page"] = page
        print(f"Fetching {event_type} events page {page}/{total_pages}")
        
        try:
            async with session.get(f"{BASE_URL}/v3/events/{event_type}", params=params) as response:
                if response.status != 200:
                    print(f"Error fetching {event_type} events: {response.status}")
                    break
                
                data = await response.json()
                if data.get("success") != 1:
                    print(f"API returned error for {event_type} events")
                    break
                
                # Update pagination info
                pager = data.get("pager", {})
                if pager:
                    total_pages = (pager.get("total", 0) + pager.get("per_page", 50) - 1) // pager.get("per_page", 50)
                    if total_pages == 0:
                        total_pages = 1
                
                events.extend(data.get("results", []))
                print(f"Found {len(data.get('results', []))} events on page {page}")
                page += 1
                if ((page_limit is not None) and (page >= page_limit)):
                    print(f"events hit page limit! {page_limit}")
                    break
                
                # Add delay to avoid rate limits
                await asyncio.sleep(CONFIG["api_delay"])
        except Exception as e:
            print(f"Exception fetching events: {str(e)}")
            break
    
    return events

async def get_matches_for_date(session, league_id, date_str):
    """Get all matches for a specific date and league"""
    params = {
        "token": TT_KEY,
        "sport_id": CONFIG["sport_id"],
        "league_id": league_id,
        "day": date_str
    }
    
    matches = await get_paged_events(session, "ended", params)
    return matches

def format_match_data(match):
    """Standardize data formats for database insertion"""
    # Fix time field
    if 'time' in match and isinstance(match['time'], str):
        try:
            # print(f"converting match time ({match['time']}) to int")
            match['time'] = int(match['time'])
        except ValueError as TIME_ERROR:
            print(f"{TIME_ERROR}; fallback to current time!")
            match['time'] = int(datetime.now().timestamp())
    
    # Fix IDs
    for key in ['league', 'home', 'away']:
        if key in match and isinstance(match[key], dict) and 'id' in match[key]:
            if isinstance(match[key]['id'], str):
                try:
                    # print(f"converting match id ({match[key]['id']}) to int" )
                    match[key]['id'] = int(match[key]['id'])
                except ValueError as MATCHID_ERROR:
                    print(f"{MATCHID_ERROR} - doing nothing")
    
    return match

def add_match_to_db(db, match):
    """Add only the match to the database, without set data"""
    try:
        # Format match data
        match = format_match_data(match)
        
        # Add match to database
        if db.add_match(match):
            # Add note to match that set data needs to be collected
            db.cursor.execute('''
                UPDATE matches 
                SET notes = 'Set data not yet collected'
                WHERE id = ? AND (notes IS NULL OR notes = '')
            ''', (match['id'],))
            
            return True
    except sqlite3.IntegrityError:
        # Duplicate match, silently skip
        return True
    except Exception as e:
        print(f"Error adding match {match.get('id', 'unknown')}: {e}")
        return False

# ========== CORE COLLECTION FUNCTIONS ==========

async def collect_current_matches(session, league_id, league_name, days_back=None):
    """Collect matches for a current league"""
    if days_back is None:
        days_back = CONFIG["days_back"]
    
    print(f"\n===== COLLECTING CURRENT MATCHES FOR {league_name} (Last {days_back} days) =====")
    
    db = TTDatabase()
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    current_date = start_date
    
    total_days = 0
    total_matches = 0
    committed_matches = 0
    commit_counter = 0
    
    while current_date <= end_date:
        day_str = current_date.strftime("%Y%m%d")
        print(f"Fetching matches for {day_str}...")
        
        # Get matches for this day
        matches = await get_matches_for_date(session, league_id, day_str)
        
        if matches:
            total_matches += len(matches)
            
            # Add matches to database directly without fetching detailed data
            day_committed = 0
            for match in matches:
                if add_match_to_db(db, match):
                    day_committed += 1
                    committed_matches += 1
                    commit_counter += 1
                
                # Commit periodically
                if commit_counter >= CONFIG["commit_frequency"]:
                    db.conn.commit()
                    print(f"Committed {commit_counter} matches to database")
                    commit_counter = 0
            
            print(f"Added {day_committed} matches for {day_str}")
        
        # Move to next day
        current_date += timedelta(days=1)
        total_days += 1
    
    # Final commit
    if commit_counter > 0:
        db.conn.commit()
    
    print(f"\n===== COLLECTION COMPLETE FOR {league_name} =====")
    print(f"Processed {total_days} days, found {total_matches} matches")
    print(f"Successfully committed {committed_matches} matches to database")
    print(f"Database now has {db.get_match_count()} matches and {db.get_player_count()} players")
    print(f"NOTE: Set data was not collected. Use 'populate_sets' mode to collect set data later.")
    
    db.close()
    return committed_matches

async def collect_historical_matches(session, league_id, league_name, year, day_limit=None):
    """Collect match data for a historical league"""
    print(f"\n===== COLLECTING HISTORICAL MATCHES FOR {league_name} (Year: {year}) =====")
    
    db = TTDatabase()
    
    # Set date range for the specific year
    start_date = datetime(year, 1, 1)
    end_date = datetime(year, 12, 31)
    
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    current_date = start_date
    total_days = 0
    total_matches = 0
    committed_matches = 0
    commit_counter = 0
    
    while current_date <= end_date:
        day_str = current_date.strftime("%Y%m%d")
        print(f"Fetching matches for {day_str}...")
        
        # Get matches for this day
        matches = await get_matches_for_date(session, league_id, day_str)
        
        if matches:
            print(f"Found {len(matches)} matches for {day_str}")
            total_matches += len(matches)
            
            # Add matches to database directly without fetching detailed data
            day_committed = 0
            for match in matches:
                if add_match_to_db(db, match):
                    day_committed += 1
                    committed_matches += 1
                    commit_counter += 1
                
                # Commit periodically
                if commit_counter >= CONFIG["commit_frequency"]:
                    db.conn.commit()
                    print(f"Committed {commit_counter} matches to database")
                    commit_counter = 0
            
            print(f"Added {day_committed} matches for {day_str}")
        
        # Move to next day
        current_date += timedelta(days=1)
        total_days += 1
        if ((day_limit is not None) and (total_days >= day_limit)):
            print(f"day limit reached for historical matches! ({day_limit})! returning.")
            break
    
    # Final commit
    if commit_counter > 0:
        db.conn.commit()
    
    print(f"\n===== HISTORICAL DATA COLLECTION COMPLETE FOR {league_name} =====")
    print(f"Processed {total_days} days, found {total_matches} matches")
    print(f"Successfully committed {committed_matches} matches to database")
    print(f"Database now has {db.get_match_count()} matches and {db.get_player_count()} players")
    print(f"NOTE: Set data was not collected. Use 'populate_sets' mode to collect set data later.")
    
    db.close()
    return committed_matches

async def collect_player_data(session, player_id, player_name, days_back=None, page_limit=None):
    """Collect match history for a specific player"""
    if days_back is None:
        days_back = CONFIG["days_back"]
        
    print(f"\n===== COLLECTING MATCH HISTORY FOR PLAYER {player_name} (ID: {player_id}) =====")
    
    db = TTDatabase()
    
    if page_limit is not None:
        print(f"page limit: {page_limit}")
    
    # Get player matches
    params = {"token": TT_KEY, "sport_id": CONFIG["sport_id"], "team_id": player_id}
    matches = await get_paged_events(session, "ended", params, page_limit)
    
    if not matches:
        print(f"No matches found for player {player_name}")
        db.close()
        return []
    
    print(f"Found {len(matches)} total matches for player {player_name}")
    
    # Filter by date if needed
    if days_back > 0:
        min_timestamp = int((datetime.now() - timedelta(days=days_back)).timestamp())
        matches = [m for m in matches if int(m.get("time", 0)) >= min_timestamp]
        print(f"Filtered to {len(matches)} matches within the {days_back} day window")
    
    # Add matches to database directly
    committed_matches = 0
    commit_counter = 0
    
    for match in matches:
        if add_match_to_db(db, match):
            committed_matches += 1
            commit_counter += 1
        
        # Commit periodically
        if commit_counter >= CONFIG["commit_frequency"]:
            db.conn.commit()
            print(f"Committed {commit_counter} matches to database")
            commit_counter = 0
    
    # Final commit
    if commit_counter > 0:
        db.conn.commit()
    
    print(f"\n===== PLAYER DATA COLLECTION COMPLETE =====")
    print(f"Successfully committed {committed_matches} matches to database")
    print(f"Database now has {db.get_match_count()} matches and {db.get_player_count()} players")
    print(f"NOTE: Set data was not collected. Use 'populate_sets' mode to collect set data later.")
    
    db.close()
    return matches



# This function is a monstrosity. Run Fucking Anything
async def populate_missing_sets(session, batch_size=None, max_matches=None):
    """Find matches without set data and populate from API"""
    if batch_size is None:
        batch_size = CONFIG["batch_size"]
        
    print(f"\n===== POPULATING SETS FOR MATCHES WITHOUT SET DATA =====")
    
    # Status code reference
    status_codes = {
        0: "Not Started", 1: "InPlay", 2: "TO BE FIXED", 3: "Ended",
        4: "Postponed", 5: "Cancelled", 6: "Walkover", 7: "Interrupted",
        8: "Abandoned", 9: "Retired", 10: "Suspended", 11: "Decided by FA",
        99: "Removed"
    }
    
    db = TTDatabase()
    
    # Find matches without set data
    db.cursor.execute("""
        SELECT m.id FROM matches m
        LEFT JOIN sets s ON m.id = s.match_id
        WHERE s.id IS NULL
        ORDER BY m.match_time DESC
    """)
    
    match_ids = [row['id'] for row in db.cursor.fetchall()]
    total_matches = len(match_ids)
    
    print(f"Found {total_matches} matches without set data")
    
    if max_matches and max_matches < total_matches:
        match_ids = match_ids[:max_matches]
        print(f"Limited to processing {max_matches} matches")
    
    if not match_ids:
        print("No matches found that need set data. Exiting.")
        db.close()
        return
    
    # Track status codes and results
    status_counts = {code: 0 for code in status_codes.keys()}
    successful = 0
    commit_counter = 0
    
    # Process in batches
    for i in range(0, len(match_ids), batch_size):
        batch = match_ids[i:i+batch_size]
        print(f"\nProcessing batch {i//batch_size + 1} of {(len(match_ids) + batch_size - 1) // batch_size} ({len(batch)} matches)")
        
        # Get match details from API
        tasks = [get_event_view_async(session, match_id) for match_id in batch]
        detailed_matches = await asyncio.gather(*tasks)
        
        # Filter out None results
        detailed_matches = [match for match in detailed_matches if match]
        
        # Process results
        batch_successful = 0
        for match in detailed_matches:
            match_id = match.get('id')
            if not match_id:
                continue
            
            # Track status code
            time_status = match.get('time_status')
            if time_status is not None:
                try:
                    time_status = int(time_status)
                    status_counts[time_status] += 1
                except (ValueError, KeyError):
                    pass
            
            # Get set data - looking for "scores" field as per the API docs
            set_data = None
            if 'scores' in match:
                set_data = match.get('scores')
                print(f"Found scores data for match {match_id}")
            elif 'detailed_scores' in match:
                set_data = match.get('detailed_scores')
                print(f"Found detailed_scores data for match {match_id}")
            
            # Info about match
            status_name = status_codes.get(time_status, "Unknown")
            print(f"Match {match_id} - Status: {time_status} ({status_name})")
            print(f"Has set data: {set_data is not None and len(set_data) > 0 if set_data else False}")
            
            # Add set data to database
            if set_data and len(set_data) > 0:
                # Normal set data
                # Use "scores" as the key in the match data to ensure compatibility with db.add_sets_from_match_data
                match_with_scores = match.copy()
                if 'detailed_scores' in match and 'scores' not in match:
                    match_with_scores['scores'] = set_data
                
                sets_added = db.add_sets_from_match_data(match_with_scores)
                print(f"Added {sets_added} sets for match {match_id}")
                if sets_added > 0:
                    batch_successful += 1
                    successful += 1
                    commit_counter += 1
            elif time_status == 2:
                # Special handling for status 2 (TO BE FIXED)
                print(f"Match {match_id} has status 2 (TO BE FIXED)")
                # Check if it's an older match that might need re-fetching in the future
                match_time = match.get('time', 0)
                try:
                    match_time = int(match_time)
                    match_date = datetime.fromtimestamp(match_time).strftime('%Y-%m-%d')
                    
                    # Skip creating placeholder for very recent matches - they might be fixed soon
                    current_time = int(datetime.now().timestamp())
                    if (current_time - match_time) < 86400:  # Less than 1 day old
                        print(f"Match {match_id} is recent ({match_date}), skipping placeholder creation")
                        continue
                except (ValueError, TypeError) as e:
                    print(f"Error parsing match time: {e}")
                
                # For older matches with status 2, create placeholder set
                print(f"Creating placeholder set data for match {match_id}")
                
                # Get match score
                score_str = match.get('ss', '0-0')
                home_score, away_score = 0, 0
                
                # Make sure score_str is not None before attempting to split
                if score_str and isinstance(score_str, str) and '-' in score_str:
                    try:
                        parts = score_str.split('-')
                        if len(parts) == 2:
                            home_score = int(parts[0])
                            away_score = int(parts[1])
                    except (ValueError, TypeError, IndexError):
                        # If any conversion fails, fall back to defaults
                        home_score, away_score = 0, 0
                
                # Add placeholder set
                db.cursor.execute('''
                    INSERT OR IGNORE INTO sets (match_id, set_number, home_score, away_score)
                    VALUES (?, ?, ?, ?)
                ''', (match_id, 1, home_score, away_score))
                
                # Add note to match
                db.cursor.execute('''
                    UPDATE matches 
                    SET notes = ?
                    WHERE id = ? AND (notes IS NULL OR notes = '')
                ''', (f'No set data available - API status code 2 (TO BE FIXED)', match_id))
                
                batch_successful += 1
                successful += 1
                commit_counter += 1
            elif time_status in [5, 6, 8, 9, 99]:
                # Other special status codes that won't have set data
                print(f"Match {match_id} has special status {time_status} ({status_codes.get(time_status)})")
                
                # Create placeholder for these as well
                print(f"Creating placeholder set data for match {match_id}")
                
                # Get match score
                score_str = match.get('ss', '0-0')
                home_score, away_score = 0, 0
                
                # Make sure score_str is not None before attempting to split
                if score_str and isinstance(score_str, str) and '-' in score_str:
                    try:
                        parts = score_str.split('-')
                        if len(parts) == 2:
                            home_score = int(parts[0])
                            away_score = int(parts[1])
                    except (ValueError, TypeError, IndexError):
                        # If any conversion fails, fall back to defaults
                        home_score, away_score = 0, 0
                
                # Add placeholder set
                db.cursor.execute('''
                    INSERT OR IGNORE INTO sets (match_id, set_number, home_score, away_score)
                    VALUES (?, ?, ?, ?)
                ''', (match_id, 1, home_score, away_score))
                
                # Add note to match
                status_desc = status_codes.get(time_status, "Unknown")
                db.cursor.execute('''
                    UPDATE matches 
                    SET notes = ?
                    WHERE id = ? AND (notes IS NULL OR notes = '')
                ''', (f'No set data available - API status code {time_status} ({status_desc})', match_id))
                
                batch_successful += 1
                successful += 1
                commit_counter += 1
            
            # Update match note after processing sets
            db.cursor.execute('''
                UPDATE matches 
                SET notes = ?
                WHERE id = ? AND notes = 'Set data not yet collected'
            ''', ('Set data collected', match_id))
            
            # Commit periodically
            if commit_counter >= CONFIG["commit_frequency"]:
                db.conn.commit()
                print(f"Committed {commit_counter} updates to database")
                commit_counter = 0
        
        # Commit after each batch
        if commit_counter > 0:
            db.conn.commit()
            commit_counter = 0
            
        print(f"Processed {len(batch)} matches, added set data for {batch_successful} matches")
        
        # Add delay between batches to avoid rate limits
        await asyncio.sleep(CONFIG["api_delay"] * 2)
    
    # Final report
    print(f"\n===== MATCH STATUS STATISTICS =====")
    for code, desc in status_codes.items():
        if status_counts[code] > 0:
            print(f"Status {code} ({desc}): {status_counts[code]} matches")
    
    print(f"\n===== SETS POPULATION COMPLETE =====")
    print(f"Successfully added set data for {successful} matches")
    print(f"Database now has {db.get_match_count()} matches")
    
    db.close()
    
    
    
# Need to rotate in historical league keys to properly gather historical odds
async def collect_historical_odds(session, batch_size=10, max_matches=None, bookmaker="bet365"):
    """Collect historical odds data for matches in the database"""
    if batch_size is None:
        batch_size = CONFIG["batch_size"]
        
    print(f"\n===== COLLECTING HISTORICAL ODDS DATA =====")
    print(f"Using bookmaker: {bookmaker}")
    
    db = TTDatabase()
    
    # Define league mappings by year
    historical_league_mapping = {
        "2020": 38394,  # Setka Cup 2020
        "2021": 38395,  # Setka Cup 2021
        "2022": 38396,  # Setka Cup 2022
        "2023": 38397   # Setka Cup 2023
    }
    
    # Get matches without odds data, grouped by year
    db.cursor.execute("""
        SELECT id, strftime('%Y', match_time) as match_year, league_id 
        FROM matches
        WHERE home_odds IS NULL OR away_odds IS NULL
        AND (notes IS NULL OR notes NOT LIKE '%API status code 2%')
        ORDER BY match_time DESC
    """)
    
    all_matches = db.cursor.fetchall()
    total_matches = len(all_matches)
    
    print(f"Found {total_matches} matches without odds data")
    
    if max_matches and max_matches < total_matches:
        all_matches = all_matches[:max_matches]
        print(f"Limited to processing {max_matches} matches")
    
    if not all_matches:
        print("No matches found that need odds data. Exiting.")
        db.close()
        return 0
    
    # Group matches by year
    matches_by_year = {}
    for row in all_matches:
        match_id = row['id']
        year = row['match_year']
        original_league_id = row['league_id']
        
        if year not in matches_by_year:
            matches_by_year[year] = []
        
        matches_by_year[year].append({
            'id': match_id,
            'original_league_id': original_league_id
        })
    
    print(f"Matches grouped by year: {', '.join([f'{year}: {len(matches)}' for year, matches in matches_by_year.items()])}")
    
    # Process matches year by year
    successful = 0
    
    for year, matches in matches_by_year.items():
        match_ids = [match['id'] for match in matches]
        print(f"\n----- Processing {len(match_ids)} matches from {year} -----")
        
        # For old matches (before current leagues), use historical league ID if available
        if year in historical_league_mapping:
            league_id = historical_league_mapping[year]
            print(f"Using historical league ID {league_id} for matches from {year}")
        else:
            # For recent matches, stick with their original league IDs
            print(f"Using original league IDs for matches from {year}")
        
        # Process in batches
        for i in range(0, len(match_ids), batch_size):
            batch = match_ids[i:i+batch_size]
            print(f"\nProcessing batch {i//batch_size + 1} of {(len(match_ids) + batch_size - 1) // batch_size} ({len(batch)} matches)")
            
            # Create tasks for individual requests
            tasks = [get_batch_match_odds_async(session, match_id, bookmaker) for match_id in batch]
            odds_results = await asyncio.gather(*tasks)
            
            if all([(results is None) for results in odds_results]):
                print("all requests failed; assuming request-limit reached!")
                # Final report
                print(f"\n===== HISTORICAL ODDS COLLECTION COMPLETE =====")
                print(f"Successfully added odds data for {successful} out of {len(all_matches)} matches")
                print(f"Database now has {db.get_match_count()} matches")
                db.close()
                return successful
            
            # Process results
            batch_successful = 0
            for j, match_id in enumerate(batch):
                odds_data = odds_results[j]
                
                if odds_data:
                    # Extract relevant odds information
                    odds_info = db.extract_odds_data(odds_data)
                    
                    if odds_info:
                        # Update match with odds data
                        if db.update_match_odds(match_id, odds_info):
                            batch_successful += 1
                            successful += 1
                            print(f"Updated odds for match {match_id}")
                    else:
                        print(f"No relevant odds data found for match {match_id}")
                else:
                    print(f"No odds data returned for match {match_id}")
            
            print(f"Updated odds for {batch_successful} out of {len(batch)} matches in this batch")
            
            # Add delay between batches to avoid rate limits
            await asyncio.sleep(CONFIG["api_delay"] * 2)
    
    # Final report
    print(f"\n===== HISTORICAL ODDS COLLECTION COMPLETE =====")
    print(f"Successfully added odds data for {successful} out of {len(all_matches)} matches")
    print(f"Database now has {db.get_match_count()} matches")
    
    db.close()
    return successful


async def process_individual_requests(session, match_ids, db, bookmaker):
    """Process matches individually when batch processing fails"""
    successful = 0
    
    # Create tasks for individual requests
    tasks = [db.get_match_odds_async(session, match_id, bookmaker) for match_id in match_ids]
    odds_results = await asyncio.gather(*tasks)
    
    # Process results
    for j, match_id in enumerate(match_ids):
        odds_data = odds_results[j]
        
        if odds_data:
            # Extract relevant odds information
            odds_info = db.extract_odds_data(odds_data)
            
            if odds_info:
                # Update match with odds data
                if db.update_match_odds(match_id, odds_info):
                    successful += 1
                    print(f"Updated odds for match {match_id} (individual request)")
        
    return successful



async def get_batch_match_odds_async(session, match_id, bookmaker="bet365"):
    """Fetch odds for a specific match - batch requests don't work as expected"""
    print(f"Fetching odds for match {match_id}")
    
    params = {
        "token": TT_KEY,
        "event_id": match_id,
        "source": bookmaker,
    }
    
    try:
        async with session.get(f"{BASE_URL}/v2/event/odds", params=params) as response:
            if response.status != 200:
                print(f"Error fetching odds for match {match_id}: {response.status}")
                return None
            
            result = await response.json()
            if result.get("success") != 1:
                print(f"Unsuccessful request for odds! Match ID: {match_id}")
                return None
            
            return result.get("results", {})
    except Exception as e:
        print(f"Exception fetching odds for match {match_id}: {str(e)}")
        return None

async def main():
    """Main function - run the selected operation mode"""
    print("=== Starting Table Tennis History Collector ===")
    print(f"Mode: {CONFIG['mode']}")
    
    async with aiohttp.ClientSession() as session:
        mode = CONFIG["mode"]
        
        if mode == "current_matches":
            for league_id, league_name in CONFIG["current_leagues"].items():
                await collect_current_matches(session, league_id, league_name)
        
        elif mode == "historical_matches":
            for league_id, info in CONFIG["historical_leagues"].items():
                await collect_historical_matches(
                    session,
                    league_id,
                    info["name"],
                    info["year"],
                    day_limit=None,
                )
        
        elif mode == "player_data":
            for player in CONFIG["players"]:
                player_matches = await collect_player_data(
                    session,
                    player["id"],
                    player["name"],
                    page_limit=None,
                )
                print(f"got {len(player_matches)} matches for {player}:")
                print(player_matches)
                print('\n\n')
                
        elif mode == "populate_sets":
            await populate_missing_sets(session)
        
        elif mode == "historical_odds":
            # Get configuration parameters
            batch_size = CONFIG.get("batch_size", 50)
            max_matches = CONFIG.get("max_matches", None)
            bookmaker = CONFIG.get("bookmaker", "bet365")
            
            await collect_historical_odds(
                session, 
                batch_size=batch_size, 
                max_matches=max_matches,
                bookmaker=bookmaker
            )
        
        else:
            print(f"Unknown mode: {mode}")
            print("Available modes: current_matches, historical_matches, player_data, populate_sets, historical_odds")

if __name__ == "__main__":
    asyncio.run(main())
