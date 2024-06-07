import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import BBSplayer_ids  # Assuming the file is in the same directory

options = FirefoxOptions()
options.add_argument('--headless')
firefox_profile = FirefoxProfile()  # Not necessary
options.profile = firefox_profile


def scrape(name, player_id, take_screenshots=False):
    driver = webdriver.Firefox(options=options)
    base_url = "https://baseballsavant.mlb.com/savant-player/"

    player_name_url_part = name.lower().replace(' ', '-').replace(',', '')
    url = f"{base_url}{player_name_url_part}-{player_id}?stats=statcast-r-pitching-mlb"
    
    scraped_data = {}
    
    try:
        driver.get(url)
        if take_screenshots:
            pitch_distribution_svg = WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.ID, 'svg-pitch-distribution-mini'))
            )
            trending_div = WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.ID, 'trending'))
            )
    
            svg_filename = os.path.join('MLBstats', f"{name.replace(', ', '_')}_pitch_distribution_svg.png")
            pitch_distribution_svg.screenshot(svg_filename)
    
            trending_filename = os.path.join('MLBstats', f"{name.replace(', ', '_')}_trending_div.png")
            trending_div.screenshot(trending_filename)
            print(f"Screenshots for {name} taken and saved as {svg_filename} and {trending_filename}.")
        
        
        pitcher_value_groups = driver.find_elements(By.CSS_SELECTOR, 'g.group.pitcherValue')
        pitching_groups = driver.find_elements(By.CSS_SELECTOR, 'g.group.pitching')

        for group in pitcher_value_groups + pitching_groups:
            metrics = group.find_elements(By.CSS_SELECTOR, 'g.metric')
            for metric in metrics:
                header = metric.find_element(By.CSS_SELECTOR, 'text').text
                value = metric.find_element(By.CSS_SELECTOR, 'g.circle-bulb').text
                stat_text_element = metric.find_elements(By.CSS_SELECTOR, 'text.text-stat')
                stat_text = stat_text_element[0].text if stat_text_element else 'N/A'
                scraped_data[header] = {'value': value, 'stat': stat_text}
        
        print(f"Scraped data for {name}: {scraped_data}")
    
    except TimeoutException:
        print(f"TimeoutException: Elements not found for player {name}.")
    finally:
        driver.quit()
    
    return scraped_data


def DDOS_the_site():
    # Use ThreadPoolExecutor to manage threads
    with ThreadPoolExecutor(max_workers=24) as executor:
        futures = {executor.submit(scrape, name, player_id, True): (name, player_id) for name, player_id in BBSplayer_ids.pitchers.items()}
    
        for future in as_completed(futures):
            name, player_id = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"An error occurred for player {name}: {exc}")
    
    print("All screenshots have been taken and data scraped.")


if __name__ == "__main__":
    test_name, id = list(BBSplayer_ids.pitchers.items())[0]
    results = scrape(test_name, id, False)
    print(len(results))
