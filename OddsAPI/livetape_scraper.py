import requests
import json
from bs4 import BeautifulSoup
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time

def get_inning_data(game_id, away_runs, home_runs):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    pbp_url = f"https://www.espn.com/mlb/playbyplay/_/gameId/{game_id}"
    try:
        # Add small delay to avoid hammering ESPN
        time.sleep(0.1)
        response = requests.get(pbp_url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        inning_element = soup.find('span', class_='HalfInningHeader__period')
        
        if inning_element and inning_element.text.strip():
            inning_text = inning_element.text.strip()
            
            # Check for winning/losing pitcher indicators - best way to detect final games
            win_loss_elements = soup.find_all('span', class_='Athlete__Header', text=re.compile(r'(win|loss)', re.IGNORECASE))
            
            if win_loss_elements:
                # Found win/loss pitcher info - game is final
                return 'Final'
            else:
                # No win/loss info - game is live
                return inning_text
        else:
            # Element is empty or doesn't exist - check if game has started
            runs_away = int(away_runs) if away_runs.isdigit() else 0
            runs_home = int(home_runs) if home_runs.isdigit() else 0
            
            if runs_away == 0 and runs_home == 0:
                return 'Pre-Game'
            else:
                return 'Final'
    except:
        return 'Pre-Game'

def fetch_game_inning(game_data):
    """Fetch inning data for a single game"""
    game_id = game_data.get('game_id')
    if game_id:
        away_runs = game_data['away_score']['runs']
        home_runs = game_data['home_score']['runs']
        game_data['inning'] = get_inning_data(game_id, away_runs, home_runs)
    else:
        game_data['inning'] = 'Pre-Game'
    return game_data

def scrape_espn_scores(url="https://www.espn.com/mlb/scoreboard", max_workers=5):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, 'html.parser')
    
    games = []
    game_containers = soup.find_all('section', class_='Card gameModules')
    
    # First pass: collect basic game data without inning info
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
                    
                    away_scores = away_team.find_all('div', class_='ScoreboardScoreCell__Value')
                    home_scores = home_team.find_all('div', class_='ScoreboardScoreCell__Value')
                    
                    if away_name and home_name and len(away_scores) >= 3 and len(home_scores) >= 3:
                        game_data = {
                            'game_id': game_id,
                            'away_team': away_name.text.strip(),
                            'home_team': home_name.text.strip(),
                            'away_record': away_record.text.strip() if away_record else '',
                            'home_record': home_record.text.strip() if home_record else '',
                            'away_score': {
                                'runs': away_scores[0].text.strip(),
                                'hits': away_scores[1].text.strip(),
                                'errors': away_scores[2].text.strip()
                            },
                            'home_score': {
                                'runs': home_scores[0].text.strip(),
                                'hits': home_scores[1].text.strip(),
                                'errors': home_scores[2].text.strip()
                            }
                        }
                        games.append(game_data)
    
    # Second pass: fetch inning data concurrently
    if games:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all inning fetch tasks
            future_to_game = {executor.submit(fetch_game_inning, game): game for game in games}
            
            # Collect results as they complete
            updated_games = []
            for future in as_completed(future_to_game):
                try:
                    updated_game = future.result()
                    updated_games.append(updated_game)
                except Exception as e:
                    # If inning fetch fails, use original game data with default inning
                    original_game = future_to_game[future]
                    original_game['inning'] = 'Pre-Game'
                    updated_games.append(original_game)
            
            return updated_games
    
    return games

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
