import concurrent.futures
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import BBSplayer_ids

def scrape_players(url):
    options = FirefoxOptions()
    options.add_argument('--headless')
    firefox_profile = FirefoxProfile()  # not necessary
    options.profile = firefox_profile

    driver = webdriver.Firefox(options=options)
    players = {}

    try:
        driver.get(url)
        leaderboard_div = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((By.ID, 'evLeaderboard'))
        )
        player_links = leaderboard_div.find_elements(By.TAG_NAME, 'a')

        
        for link in player_links:
            href = link.get_attribute('href')
            if '/savant-player/' in href:
                player_name = link.text.strip()
                player_id = int(href.split('/')[-1])  # Convert player ID to an integer
                # Format the player name as "LastName, FirstName"
                if ", " not in player_name:
                    parts = player_name.split(' ')
                    player_name = f"{parts[-1]}, {' '.join(parts[:-1])}"
                players[player_name] = player_id

    except TimeoutException:
        print(f"TimeoutException: The leaderboard container was not found or did not become visible in time for URL: {url}")
    finally:
        driver.quit()

    return players

if __name__ == "__main__":
    pitchers_url = 'https://baseballsavant.mlb.com/leaderboard/statcast?type=pitcher&year=2024&position=&team=&min=1&sort=barrels_per_pa&sortDir=desc'
    hitters_url = 'https://baseballsavant.mlb.com/leaderboard/statcast?type=batter&year=2024&position=&team=&min=1&sort=barrels_per_pa&sortDir=desc'

    urls = [pitchers_url, hitters_url]
    recent_pitchers = {}
    recent_hitters = {}

   
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(scrape_players, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                players = future.result()
                if url == pitchers_url:
                    recent_pitchers = players
                else:
                    recent_hitters = players
            except Exception as exc:
                print(f"Generated an exception: {exc}")

    # save player names and IDs to a .py file as dicts
    with open('player_ids.py', 'w') as file:
        file.write("recent_pitchers = {\n")
        for player_name, player_id in recent_pitchers.items():
            file.write(f"    \"{player_name}\": {player_id},\n")
        file.write("}\n\n")
        file.write("recent_hitters = {\n")
        for player_name, player_id in recent_hitters.items():
            file.write(f"    \"{player_name}\": {player_id},\n")
        file.write("}\n")

    print("Player names and IDs have been saved to 'player_ids.py'.")

    # compare new player IDs with the master dictionary's pitcher dictionary
    master_pitchers = BBSplayer_ids.pitchers
    new_pitchers = recent_pitchers

    # find differences
    new_pitchers_set = set(new_pitchers.items())
    master_pitchers_set = set(master_pitchers.items())

    new_ids = new_pitchers_set - master_pitchers_set
    missing_ids = master_pitchers_set - new_pitchers_set

    print("\nNew IDs not in the master dictionary:")
    for name, player_id in new_ids:
        print(f"{name}: {player_id}")

    print("\nIDs in the master dictionary but not in the new scrape:")
    for name, player_id in missing_ids:
        print(f"{name}: {player_id}")
