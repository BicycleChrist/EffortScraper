from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

options = FirefoxOptions()
options.add_argument('--headless')
firefox_profile = FirefoxProfile()  # not necessary
options.profile = firefox_profile

driver = webdriver.Firefox(options=options)

try:
    driver.get('https://baseballsavant.mlb.com/leaderboard/statcast?type=batter&year=2024&position=&team=&min=1&sort=barrels_per_pa&sortDir=desc')
    # url for pitcher id's:https://baseballsavant.mlb.com/leaderboard/statcast?type=pitcher&year=2024&position=&team=&min=1&sort=barrels_per_pa&sortDir=desc
    # url for hitter id's:https://baseballsavant.mlb.com/leaderboard/statcast?type=batter&year=2024&position=&team=&min=1&sort=barrels_per_pa&sortDir=desc
    # Wait for the leaderboard container to be visible
    leaderboard_div = WebDriverWait(driver, 20).until(
        EC.visibility_of_element_located((By.ID, 'evLeaderboard'))
    )
    
    # Find all player links within the leaderboard container
    player_links = leaderboard_div.find_elements(By.TAG_NAME, 'a')
    
    # Prepare a list to hold player names and IDs
    players = []
    
    # Loop through each player link and extract the name and ID
    for link in player_links:
        href = link.get_attribute('href')
        if '/savant-player/' in href:
            player_name = link.text.strip()
            player_id = href.split('/')[-1]
            players.append((player_name, player_id))
    
    # Print the player names and IDs
    for player in players:
        print(f"{player[0]}: {player[1]}")
    
    # Save the player names and IDs to a .txt file
    with open('playerpitcher_ids.txt', 'w') as file:
        for player in players:
            file.write(f"{player[0]}: {player[1]}\n")
    
    print("Player names and IDs have been saved to 'player_ids.txt'.")
    
except TimeoutException:
    print("The leaderboard container was not found or did not become visible in time.")
finally:
    driver.quit()

