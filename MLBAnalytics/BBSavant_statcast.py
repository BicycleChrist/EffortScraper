import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import BBSplayer_ids 

options = FirefoxOptions()
options.add_argument('--headless')
firefox_profile = FirefoxProfile()  # Not necessary
options.profile = firefox_profile


def scrape(name: str, player_id, take_screenshots: bool = False) -> dict:
    print(f"\nscraping {name}: {player_id}")
    if player_id is None:
        print("aborting scrape: you fucked up the player_id")
        return {}
    driver = webdriver.Firefox(options=options)
    base_url = "https://baseballsavant.mlb.com/savant-player/"

    player_name_url_part = name.lower().replace(' ', '-').replace(',', '')
    url = f"{base_url}{player_name_url_part}-{player_id}?stats=statcast-r-pitching-mlb"
    driver.get(url)
    
    timeout = 5
    scraped_data = {}
    try:
        if take_screenshots:
            print(f"screenshotting {name}\n")
            try:
                pitch_distribution = WebDriverWait(driver, timeout).until(
                    EC.visibility_of_element_located((By.ID, 'svg-pitch-distribution-mini'))
                )
            except TimeoutException:
                print("Pitch distribution element not found within 5 seconds.")
                pitch_distribution = None

            try:
                trending_div = WebDriverWait(driver, timeout).until(
                    EC.visibility_of_element_located((By.ID, 'trending'))
                )
            except TimeoutException:
                print("Trending div element not found within 5 seconds.")
                trending_div = None

            cwd = pathlib.Path.cwd()
            for purpose, element in (("pitch_distribution", pitch_distribution), ("trending_div", trending_div)):
                if element is not None:
                    filepath = cwd / "MLBstats" / f"{name.replace(', ', '_')}_{purpose}.png"
                    element.screenshot(str(filepath))
                    print(f"Screenshots for {purpose} taken and saved as {filepath.relative_to(cwd)}")
                else:
                    print(f"No screenshot taken for {purpose} as element was not found.")

        
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
        print(f"Scrape: TimeoutException: Elements not found for player {name}.")
    finally:
        driver.quit()

    return scraped_data



def DDOS_the_site():
    
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
    DDOS_the_site()
    print(len(results))

