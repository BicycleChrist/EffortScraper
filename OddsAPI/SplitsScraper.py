import aiohttp
import asyncio
from bs4 import BeautifulSoup
import json
import os
import time
import argparse
from datetime import datetime
import sys
from typing import Dict, List, Any, Optional, Tuple

# These urls should be consistent, but only time will tell


# Sport codes used to build the urls for each league offered
SPORT_CODES = {
    "nhl": "42133",
    "mlb": "84240",
    "nba": "42648",
    # "nfl": "88808",
    "ncaab": "92483",
    # "ncaaf": "87637",
    # "ufc": "9034",
    # "epl": "40253",
    # "champions league": "40685"
}


async def scrape_all_sports_async(max_retries=3, retry_delay=2, concurrency_limit=5):
    """
    Scrape betting data for all sports in the SPORT_CODES dictionary asynchronously.
    
    Args:
        max_retries: Number of retry attempts per sport
        retry_delay: Base delay between retries in seconds
        concurrency_limit: Maximum number of concurrent requests
    
    Returns:
        dict: Results of scraping each sport (success/failure)
    """
    results = {}
    
    print(f"Starting to scrape data for {len(SPORT_CODES)} sports asynchronously...")
    print(f"Concurrency limit: {concurrency_limit}")
    
    # Create a semaphore to limit concurrency
    semaphore = asyncio.Semaphore(concurrency_limit)
    
    # Create tasks for all sports
    tasks = []
    for sport in SPORT_CODES.keys():
        tasks.append(scrape_sport_async(sport, semaphore, max_retries, retry_delay))
    
    # Execute all tasks concurrently and gather results
    results_list = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    for i, sport in enumerate(SPORT_CODES.keys()):
        result = results_list[i]
        if isinstance(result, Exception):
            print(f"Error scraping {sport}: {result}")
            results[sport] = False
        else:
            results[sport] = result
    
    # Print summary
    print("\n" + "=" * 60)
    print("SCRAPING SUMMARY")
    print("=" * 60)
    successful = sum(1 for success in results.values() if success)
    print(f"Successfully scraped {successful}/{len(SPORT_CODES)} sports.")
    
    for sport, success in results.items():
        status = "✓ SUCCESS" if success else "✗ FAILED"
        print(f"{sport.upper()}: {status}")
    
    return results


async def scrape_sport_async(sport: str, semaphore: asyncio.Semaphore, max_retries=3, retry_delay=2) -> bool:
    """
    Scrape a single sport asynchronously.
    
    Args:
        sport: Sport identifier
        semaphore: Semaphore for concurrency control
        max_retries: Number of retry attempts
        retry_delay: Base delay between retries in seconds
    
    Returns:
        bool: Whether scraping was successful
    """
    async with semaphore:  # Limit concurrent requests
        print(f"\n{'=' * 60}")
        print(f"Scraping {sport.upper()} (code: {SPORT_CODES.get(sport, sport)})...")
        print(f"{'=' * 60}")
        
        try:
            return await main_async(sport=sport, max_retries=max_retries, retry_delay=retry_delay)
        except Exception as e:
            print(f"Error scraping {sport}: {e}")
            return False


def build_url(sport_code, date_range="n30days", mode="0"):
    """Build the URL for scraping based on sport code and parameters."""
    base_url = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
    return f"{base_url}?tb_eg={sport_code}&tb_edate={date_range}&tb_emt={mode}"


def scrape_betting_data(html_content: str) -> List[Dict[str, Any]]:
    """
    Parse HTML content and extract sports betting data.
    Returns structured data without odds information.
    Works for NHL, MLB, NBA, NFL, and other sports with similar HTML structure.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    games_data = []
    
    # Find all game sections
    game_sections = soup.select('div.tb-se')
    
    for game in game_sections:
        # Extract game info
        title_element = game.select_one('div.tb-se-title h5 a')
        if not title_element:
            continue
            
        game_title = title_element.text.strip()
        teams = game_title.split(' @ ')
        if len(teams) != 2:
            continue
            
        away_team, home_team = teams
        
        # Get date/time
        time_element = game.select_one('div.tb-se-title span')
        game_time = time_element.text.strip() if time_element else ""
        
        # Initialize game dictionary
        game_dict = {
            "away_team": away_team.strip(),
            "home_team": home_team.strip(),
            "game_time": game_time,
            "markets": {}
        }
        
        # Process each market (Moneyline, Spread, Total)
        market_sections = game.select('div.tb-market-wrap > div')
        
        for market_section in market_sections:
            market_head = market_section.select_one('div.tb-se-head div')
            if not market_head:
                continue
                
            market_type = market_head.text.strip()
            market_data = []
            
            # Process each betting option in the market
            betting_options = market_section.select('div.tb-sodd')
            for option in betting_options:
                option_name_elem = option.select_one('div.tb-slipline')
                if not option_name_elem:
                    continue
                    
                option_name = option_name_elem.text.strip()
                
                # Extract handle and bets percentages
                percentages = option.select('div:nth-child(3), div:nth-child(4)')
                if len(percentages) < 2:
                    continue
                    
                handle_pct = percentages[0].get_text().strip().split('%')[0].strip()
                bets_pct = percentages[1].get_text().strip().split('%')[0].strip()
                
                market_data.append({
                    "option": option_name,
                    "handle_percentage": handle_pct,
                    "bets_percentage": bets_pct
                })
            
            game_dict["markets"][market_type] = market_data
        
        games_data.append(game_dict)
    
    return games_data


def save_data(data, sport="betting", output_dir="SplitsData", output_format="json"):
    """
    Save extracted data to file in the specified format.
    Default is JSON but can be expanded for other formats.
    """
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if output_format.lower() == "json":
        # timestamped file 
        filename = os.path.join(output_dir, f"{sport}_betting_data_{timestamp}.json")
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        # save as latest.json
        latest_file = os.path.join(output_dir, f"{sport}_betting_latest.json")
        with open(latest_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            
        return filename
    else:
        raise ValueError(f"Unsupported output format: {output_format}")


async def main_async(sport="mlb", max_retries=0, retry_delay=1) -> bool:
    """
    Main function to scrape betting data asynchronously.
    
    Args:
        sport: String identifier for the sport (nhl, mlb, etc.)
        max_retries: Number of retry attempts
        retry_delay: Base delay between retries in seconds
        
    Returns:
        bool: Whether scraping was successful
    """
    # Get the sport code or use directly if provided
    sport = sport.lower()
    if sport in SPORT_CODES:
        sport_code = SPORT_CODES[sport]
        sport_name = sport
    else:
        # If a direct code was provided
        sport_code = sport
        # Find the sport name for the code
        sport_name = next((s for s, c in SPORT_CODES.items() if c == sport), "custom")
    
    # Build the URL
    url = build_url(sport_code)
    print(f"Scraping {sport_name.upper()} betting data...")
    
    # Set headers to mimic a browser request (Claude sure does love that Win 10 user agent LOL)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for attempt in range(max_retries):
            try:
                # Make the request
                print(f"Scraping data from {url}... (Attempt {attempt+1}/{max_retries})")
                async with session.get(url, timeout=10) as response:
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"HTTP Error {response.status}"
                        )
                    
                    html_content = await response.text()
                
                # Check if we got actual content
                if len(html_content) < 1000:
                    print("Warning: Received unusually small response. The site might be blocking scrapers.")
                    # Save the response for debugging
                    debug_dir = "debug"
                    if not os.path.exists(debug_dir):
                        os.makedirs(debug_dir)
                    with open(f"{debug_dir}/response_{sport}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html", "w", encoding="utf-8") as f:
                        f.write(html_content)
                    print(f"Saved response to debug directory for inspection.")
                    
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)
                        print(f"Waiting {wait_time} seconds before retrying...")
                        await asyncio.sleep(wait_time)
                        continue
                
                # Process data
                data = scrape_betting_data(html_content)
                
                if not data:
                    print(f"Warning: No data extracted for {sport}. The page structure might have changed or there are no games.")
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)
                        print(f"Waiting {wait_time} seconds before retrying...")
                        await asyncio.sleep(wait_time)
                        continue
                
                # Save data with sport identifier
                output_file = save_data(data, sport=sport)
                print(f"Successfully extracted data for {len(data)} {sport.upper()} games.")
                print(f"Data saved to: {output_file}")
                print(f"Latest data also available at: data/{sport}_betting_latest.json")
                
                
                return True
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"Request error for {sport}: {e}")
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)
                    print(f"Waiting {wait_time} seconds before retrying...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"Max retries reached for {sport}. Failed to scrape data.")
                    return False
            
            except Exception as e:
                print(f"Error scraping {sport}: {e}")
                return False
        
        return False


def main_sync(sport="mlb", max_retries=2, retry_delay=1):
    """
    Synchronous wrapper for main_async to maintain backwards compatibility.
    """
    return asyncio.run(main_async(sport, max_retries, retry_delay))


if __name__ == "__main__":
    # Set up command line argument parsing
    parser = argparse.ArgumentParser(description='Scrape betting data from DraftKings.')
    parser.add_argument('--sport', type=str, default='all', 
                        help='Sport to scrape (nhl, mlb, nba, nfl, all) or direct sport code')
    parser.add_argument('--retries', type=int, default=3,
                        help='Number of retry attempts if scraping fails')
    parser.add_argument('--delay', type=int, default=2,
                        help='Base delay between retries in seconds')
    parser.add_argument('--list', action='store_true',
                        help='List available sport codes and exit')
    parser.add_argument('--concurrency', type=int, default=5,
                        help='Maximum number of concurrent requests when scraping all sports')
    
    args = parser.parse_args()
    
    # If --list flag is used, just print the available sports and exit
    if args.list:
        print("Available sports and their codes:")
        for sport, code in SPORT_CODES.items():
            print(f"  {sport.upper()}: {code}")
        sys.exit(0)
    
    try:
        if args.sport.lower() == 'all':
            # Scrape all sports asynchronously
            results = asyncio.run(scrape_all_sports_async(
                max_retries=args.retries, 
                retry_delay=args.delay,
                concurrency_limit=args.concurrency
            ))
            # Exit with success if at least one sport was scraped successfully
            sys.exit(0 if any(results.values()) else 1)
        else:
            # Scrape just the specified sport
            success = main_sync(sport=args.sport, max_retries=args.retries, retry_delay=args.delay)
            sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nScraping canceled by user.")
        sys.exit(1)
