import requests
import json
from bs4 import BeautifulSoup
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time

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
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        boxscore_url = f"https://www.espn.com/{sport_name.lower()}/boxscore/_/gameId/{game_id}"
        response = requests.get(boxscore_url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.content, 'html.parser')

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
        print(f"Error fetching boxscore for game {game_id}: {e}")
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

def scrape_espn_scores(url="https://www.espn.com/mlb/scoreboard", max_workers=5, sport_name="MLB"):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, 'html.parser')

    games = []
    game_containers = soup.find_all('section', class_='Card gameModules')
    
    # Extract game data from scoreboard - single pass with status extraction
    for container in game_containers:
        scoreboard_sections = container.find_all('section', class_='Scoreboard')

        for section in scoreboard_sections:
            game_id = section.get('id')
            scorecells = section.find_all('div', class_='ScoreboardScoreCell')

            for cell in scorecells:
                teams = cell.find_all('li', class_='ScoreboardScoreCell__Item')

                if len(teams) == 2:
                    away_team = teams[0]
                    home_team = teams[1]

                    away_name = away_team.find('div', class_='ScoreCell__TeamName')
                    home_name = home_team.find('div', class_='ScoreCell__TeamName')

                    away_record = away_team.find('span', class_='ScoreboardScoreCell__Record')
                    home_record = home_team.find('span', class_='ScoreboardScoreCell__Record')

                    # Get total scores - consistent across all sports
                    away_total_score = away_team.find('div', class_='ScoreCell__Score')
                    home_total_score = home_team.find('div', class_='ScoreCell__Score')

                    # Get period/quarter scores - these exist for all sports
                    away_period_scores = away_team.find_all('div', class_='ScoreboardScoreCell__Value')
                    home_period_scores = home_team.find_all('div', class_='ScoreboardScoreCell__Value')

                    if away_name and home_name and away_total_score and home_total_score:
                        # Extract game status directly from the scoreboard section (not the cell)
                        game_status = get_game_status_from_scoreboard(section)

                        # If game is "Live" but no detailed status, try boxscore
                        if game_status == 'Live' and game_id:
                            boxscore_status = get_game_status_from_boxscore(game_id, sport_name)
                            if boxscore_status:
                                game_status = boxscore_status

                        away_team_name = away_name.text.strip()
                        home_team_name = home_name.text.strip()
                        away_score = away_total_score.text.strip()
                        home_score = home_total_score.text.strip()

                        game_data = {
                            'game_id': game_id,
                            'away_team': away_team_name,
                            'home_team': home_team_name,
                            'away_record': away_record.text.strip() if away_record else '',
                            'home_record': home_record.text.strip() if home_record else '',
                            'away_score': {
                                'runs': away_score,
                                'period_scores': [score.text.strip() for score in away_period_scores]
                            },
                            'home_score': {
                                'runs': home_score,
                                'period_scores': [score.text.strip() for score in home_period_scores]
                            },
                            'status': game_status  # Use the unified status extraction
                        }
                        games.append(game_data)

    return games

def scrape_all_sports(max_workers=5):
    """Scrape live scores from all major sports leagues"""
    sports_config = {
        "MLB": {"url": "https://www.espn.com/mlb/scoreboard", "icon": "⚾"},
        "NBA": {"url": "https://www.espn.com/nba/scoreboard", "icon": "🏀"},
        "NFL": {"url": "https://www.espn.com/nfl/scoreboard", "icon": "🏈"},
        "NHL": {"url": "https://www.espn.com/nhl/scoreboard", "icon": "🏒"}
    }

    all_scores = {}

    for sport_name, config in sports_config.items():
        try:
            scores = scrape_espn_scores(url=config['url'], max_workers=max_workers, sport_name=sport_name)
            if scores:
                all_scores[sport_name] = {
                    "games": scores,
                    "icon": config["icon"]
                }
        except Exception as e:
            print(f"Error scraping {sport_name}: {e}")

    return all_scores

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
