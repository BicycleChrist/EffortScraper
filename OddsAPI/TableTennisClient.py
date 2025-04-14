import asyncio
import aiohttp
import json
import pathlib
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from Creds import TT_KEY

# Constants
BASE_URL = "https://api.b365api.com"
SPORT_ID = 92  # table tennis
TARGET_LEAGUE_IDS = {
    22307: "Setka Cup",
    22742: "Czech Republic Liga Pro",
    22534: "TT CUP",
    24536: "Poland TT Elite Series",
}
DEFAULT_BOOKMAKER = "bet365"
TIME_WINDOW_HOURS = 6  # Only fetch events within next 6 hours


async def get_events_async(session: aiohttp.ClientSession, league_id: int, event_type: str) -> Dict:
    """
    Fetch upcoming or inplay events for a specific league
    """
    assert(event_type in ("upcoming", "inplay"))
    print(f"Getting {event_type} events [league_id: {league_id}]")
    
    params = {
        "token": TT_KEY,
        "sport_id": SPORT_ID,
        "league_id": league_id,
    }
    
    try:
        async with session.get(f"{BASE_URL}/v3/events/{event_type}", params=params) as response:
            if response.status != 200:
                print(f"Error fetching {event_type} events for league {league_id}: {response.status}")
                return {"success": 0, "results": []}
            
            data = await response.json()
            print(f"Response for {event_type} events, league {league_id}: {data.get('success')}")
            
            # Debug the first few results
            results = data.get("results", [])
            if results:
                print(f"First event structure: {results[0] if results else 'No events'}")
            else:
                print(f"No events found for league {league_id}")
                
            return data
    except Exception as e:
        print(f"Exception fetching {event_type} events for league {league_id}: {str(e)}")
        return {"success": 0, "results": []}


async def get_markets_async(session: aiohttp.ClientSession, event_id, bookmaker: str = DEFAULT_BOOKMAKER) -> Dict:
    """
    Fetch markets for a specific event
    """
    print(f"Fetching markets for event_id: {event_id}")
    
    params = {
        "token": TT_KEY,
        "event_id": event_id,
        "source": bookmaker,
    }
    
    try:
        async with session.get(f"{BASE_URL}/v2/event/odds", params=params) as response:
            if response.status != 200:
                print(f"Error fetching markets for event {event_id}: {response.status}")
                return {}
            
            result = await response.json()
            if result.get("success") != 1:
                print(f"Unsuccessful request for markets! Event ID: {event_id}")
                return {}
            
            return result.get("results", {})
    except Exception as e:
        print(f"Exception fetching markets for event {event_id}: {str(e)}")
        return {}


async def get_event_view_async(session: aiohttp.ClientSession, event_id: str) -> Dict:
    """
    Fetch detailed event view data including set scores and point-by-point timeline
    """
    print(f"Fetching event view for event_id: {event_id}")
    
    params = {
        "token": TT_KEY,
        "event_id": event_id,
    }
    
    try:
        async with session.get(f"{BASE_URL}/v1/event/view", params=params) as response:
            if response.status != 200:
                print(f"Error fetching event view for event {event_id}: {response.status}")
                return {}
            
            result = await response.json()
            if result.get("success") != 1:
                print(f"Unsuccessful request for event view! Event ID: {event_id}")
                return {}
            
            # Return the first result if available
            results = result.get("results", [])
            if results and len(results) > 0:
                event_data = results[0]
                
                # Process timeline data to make it easier to use for charting
                if "timeline" in event_data:
                    # Organize timeline by set (game)
                    processed_timeline = {}
                    
                    for point in event_data["timeline"]:
                        game_num = point.get("gm")
                        team = point.get("te")  # 0 for home, 1 for away
                        score = point.get("ss")
                        
                        if not game_num or team is None or not score:
                            continue
                            
                        if game_num not in processed_timeline:
                            processed_timeline[game_num] = []
                            
                        # Parse score (format: "3-2")
                        if "-" in score:
                            home_score, away_score = map(int, score.split("-"))
                            processed_timeline[game_num].append({
                                "point_num": len(processed_timeline[game_num]) + 1,
                                "team": int(team),
                                "home_score": home_score,
                                "away_score": away_score
                            })
                    
                    # Add the processed timeline to the event data
                    event_data["processed_timeline"] = processed_timeline
                    
                return event_data
            return {}
    except Exception as e:
        print(f"Exception fetching event view for event {event_id}: {str(e)}")
        return {}


async def get_history_async(session: aiohttp.ClientSession, event_id, quantity: int = 20) -> Dict:
    """
    Fetch historical head-to-head data for a specific event
    """
    print(f"Fetching history for event_id: {event_id}")
    
    params = {
        "token": TT_KEY,
        "event_id": event_id,
        "qty": quantity,  # Maximum allowed by the API
    }
    
    try:
        async with session.get(f"{BASE_URL}/v1/event/history", params=params) as response:
            if response.status != 200:
                print(f"Error fetching history for event {event_id}: {response.status}")
                return {}
            
            result = await response.json()
            if result.get("success") != 1:
                print(f"Unsuccessful request for history! Event ID: {event_id}")
                return {}
            
            return result.get("results", {})
    except Exception as e:
        print(f"Exception fetching history for event {event_id}: {str(e)}")
        return {}


async def fetch_detailed_h2h_data(session: aiohttp.ClientSession, history_data: Dict) -> Dict:
    """
    Enhance history data with detailed set scores and point-by-point timeline for each match
    """
    if not history_data or "h2h" not in history_data:
        return history_data
        
    h2h_matches = history_data.get("h2h", [])
    detailed_matches = []
    
    # Create tasks for fetching event details for each h2h match
    event_view_tasks = []
    for match in h2h_matches:
        match_id = match.get("id")
        if match_id:
            event_view_tasks.append(get_event_view_async(session, match_id))
        else:
            detailed_matches.append(match)  # Keep the original match if no ID
    
    # Execute all event view tasks concurrently
    if event_view_tasks:
        event_view_results = await asyncio.gather(*event_view_tasks)
        
        # Process results and enhance the h2h matches
        for i, match in enumerate(h2h_matches):
            match_id = match.get("id")
            if match_id and i < len(event_view_results):
                event_view_data = event_view_results[i]
                if event_view_data:
                    # Add the set scores to the match if available
                    if "scores" in event_view_data:
                        match["detailed_scores"] = event_view_data.get("scores", {})
                    
                    # Add the point-by-point timeline if available
                    if "processed_timeline" in event_view_data:
                        match["processed_timeline"] = event_view_data.get("processed_timeline", {})
                    elif "timeline" in event_view_data:
                        match["timeline"] = event_view_data.get("timeline", [])
                        
                detailed_matches.append(match)
            elif not match_id:
                detailed_matches.append(match)  # Keep the original match
    else:
        detailed_matches = h2h_matches
    
    # Replace the original h2h array with the enhanced one
    history_data["h2h"] = detailed_matches
    
    return history_data


async def save_json_async(data: Dict, name: str) -> None:
    """
    Save data to a JSON file asynchronously
    """
    savedir = pathlib.Path.cwd() / "TTT_savedata"
    if not savedir.exists():
        savedir.mkdir()
    
    filepath = savedir / f"{name.replace(' ', '-')}.json"
    print(f"Saving data to: {filepath}")
    
    # Run the file I/O in a thread to avoid blocking
    await asyncio.to_thread(write_json_to_file, data, filepath)
    print(f"Finished writing data to {filepath}")


def write_json_to_file(data: Dict, filepath: pathlib.Path) -> None:
    """
    Helper function to write JSON data to a file
    """
    with open(filepath, 'w', encoding='utf-8') as thefile:
        json.dump(data, thefile, indent=2)


def is_within_time_window(event_time, hours: int = TIME_WINDOW_HOURS) -> bool:
    """
    Check if an event is within the specified time window
    """
    # Ensure event_time is an integer
    if isinstance(event_time, str):
        try:
            event_time = int(event_time)
        except ValueError:
            return False
    
    current_time = int(time.time())
    max_time = current_time + (hours * 3600)  # Convert hours to seconds
    return current_time <= event_time <= max_time


def format_timestamp(timestamp) -> str:
    """
    Convert unix timestamp to human-readable format
    """
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return "Unknown time"


async def process_league(session: aiohttp.ClientSession, league_id: int, league_name: str) -> Dict:
    """
    Process a league: fetch events, filter by time, get markets and history
    """
    print(f"\n{'='*50}\nProcessing league: {league_name} (ID: {league_id})\n{'='*50}")
    
    # Get upcoming events for this league
    events_data = await get_events_async(session, league_id, "upcoming")
    
    if events_data.get("success") != 1:
        print(f"Failed to get events for league {league_name}")
        return {"success": 0, "results": [], "markets": [], "history": []}
    
    # Filter events within time window
    filtered_results = []
    for event in events_data.get("results", []):
        event_time = event.get("time", 0)
        # Debug the event time
        print(f"Event time for {event.get('id')}: {event_time} (type: {type(event_time)})")
        if is_within_time_window(event_time):
            try:
                event["formatted_time"] = format_timestamp(int(event_time))
            except (ValueError, TypeError):
                event["formatted_time"] = "Unknown time"
            filtered_results.append(event)
    
    events_data["results"] = filtered_results
    events_data["filtered_count"] = len(filtered_results)
    events_data["original_count"] = len(events_data.get("results", []))
    
    print(f"Found {len(filtered_results)} events within {TIME_WINDOW_HOURS} hour window (from {len(events_data.get('results', []))} total events)")
    
    # For each event, get markets and history
    market_entries = []
    history_entries = []
    
    # Create tasks for fetching markets and history
    market_tasks = []
    history_tasks = []
    
    for event in filtered_results:
        event_id = event.get("id")
        if event_id:  # Make sure we have a valid event ID
            print(f"Creating tasks for event ID: {event_id}")
            market_tasks.append(get_markets_async(session, event_id))
            history_tasks.append(get_history_async(session, event_id))
        else:
            print(f"Warning: Event without ID encountered: {event}")
    
    # Execute all market tasks concurrently
    if market_tasks:
        markets_results = await asyncio.gather(*market_tasks)
        for i, markets in enumerate(markets_results):
            if markets:
                market_entry = {
                    "event_id": filtered_results[i]["id"],
                    "event_name": f"{filtered_results[i].get('home', {}).get('name', '')} vs {filtered_results[i].get('away', {}).get('name', '')}",
                    "event_time": filtered_results[i].get("formatted_time", ""),
                    "markets": markets
                }
                market_entries.append(market_entry)
    
    # Execute all history tasks concurrently and add detailed set scores
    if history_tasks:
        history_results = await asyncio.gather(*history_tasks)
        
        # Create enhanced history entries with detailed scores
        for i, history in enumerate(history_results):
            if history:
                # Fetch detailed set scores for each h2h match
                enhanced_history = await fetch_detailed_h2h_data(session, history)
                
                history_entry = {
                    "event_id": filtered_results[i]["id"],
                    "event_name": f"{filtered_results[i].get('home', {}).get('name', '')} vs {filtered_results[i].get('away', {}).get('name', '')}",
                    "event_time": filtered_results[i].get("formatted_time", ""),
                    "history": enhanced_history
                }
                history_entries.append(history_entry)
    
    # Add markets and history to the events data
    events_data["markets"] = market_entries
    events_data["history"] = history_entries
    
    # Save the data
    await save_json_async(events_data, league_name)
    
    return events_data


async def main() -> None:
    """
    Main function to process all leagues
    """
    start_time = time.time()
    print(f"Starting table tennis data collection at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Fetching events for the next {TIME_WINDOW_HOURS} hours")
    print(f"Target leagues: {', '.join(TARGET_LEAGUE_IDS.values())}")
    
    # Create a client session
    async with aiohttp.ClientSession() as session:
        # Process each league concurrently
        tasks = [process_league(session, league_id, league_name) 
                 for league_id, league_name in TARGET_LEAGUE_IDS.items()]
        
        league_results = await asyncio.gather(*tasks)
        
        # Combine all league results
        all_events = {name: result for name, result in zip(TARGET_LEAGUE_IDS.values(), league_results)}
        
        # Save combined results
        await save_json_async(all_events, "all-upcoming-events")
    
    # Print summary
    end_time = time.time()
    elapsed_time = end_time - start_time
    
    print("\n" + "="*80)
    print(f"Data collection completed in {elapsed_time:.2f} seconds")
    
    # Count total events, markets, and history entries
    total_events = sum(len(result.get("results", [])) for result in league_results)
    total_markets = sum(len(result.get("markets", [])) for result in league_results)
    total_history = sum(len(result.get("history", [])) for result in league_results)
    
    print(f"Total events collected: {total_events}")
    print(f"Total markets collected: {total_markets}")
    print(f"Total history entries collected: {total_history}")
    print(f"All data saved to TTT_savedata directory")
    print("="*80)


if __name__ == "__main__":
    asyncio.run(main())
