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


def ConstructURL(name: str, player_id:int) -> str:
    base_url = "https://baseballsavant.mlb.com/savant-player/"
    
    # TODO: make sure this is HTML safe (are periods safe? apostrophies could also be a problem)
    player_name_url_part = name.lower().replace(' ', '-').replace(',', '')
    url = f"{base_url}{player_name_url_part}-{player_id}?stats=statcast-r-pitching-mlb"
    return url


def StartDriver():
    options = FirefoxOptions()
    options.add_argument('--headless')
    #firefox_profile = FirefoxProfile()  # Not necessary
    #options.profile = firefox_profile
    
    driver = webdriver.Firefox(options=options)
    return driver


# TODO: figure out why the screenshots crash occasionally
def Scrape(name: str, player_id: int, take_screenshots: bool = False) -> dict:
    print(f"\nscraping {name}: {player_id}")
    if player_id < 100000: # seems like all players on the site have 6-digit IDs
        print(f"aborting scrape: bad player_id: {player_id}")
        return {}

    url = ConstructURL(name, player_id)
    driver = StartDriver()
    driver.get(url)

    wait_timeout = 5
    scraped_data = {}
    try:
        if take_screenshots:
            driver.implicitly_wait(wait_timeout)

            pitch_distribution_temp = driver.find_elements(By.ID, 'svg-pitch-distribution-mini')
            trending_div_temp = driver.find_elements(By.ID, 'trending')
            things = []
            for temp in (pitch_distribution_temp, trending_div_temp):
                if len(temp) == 0: things.append(None); continue
                if len(temp) > 1: print("found more than one")
                things.append(temp[0])
            pitch_distribution, trending_div = things

            cwd = pathlib.Path.cwd()
            for purpose, element in (("pitch_distribution", pitch_distribution), ("trending_div", trending_div)):
                if element is not None:
                    filepath = cwd / "MLBstats" / f"{name.replace(', ', '_')}_{purpose}.png"
                    element.screenshot(str(filepath))
                    print(f"Screenshots for {purpose} taken and saved as {filepath.relative_to(cwd)}")
                else:
                    print(f"No screenshot taken for {purpose} as element was not found.")
            driver.implicitly_wait(0)  # resetting

        pitcher_value_groups = driver.find_elements(By.CSS_SELECTOR, 'g.group.pitcherValue')
        pitching_groups = driver.find_elements(By.CSS_SELECTOR, 'g.group.pitching')

        for group in pitcher_value_groups + pitching_groups:
            metrics = group.find_elements(By.CSS_SELECTOR, 'g.metric')
            for metric in metrics:
                header = metric.find_element(By.CSS_SELECTOR, 'text').text

                # Try to find the value element, if it exists
                value_elements = metric.find_elements(By.CSS_SELECTOR, 'g.circle-bulb')
                value = value_elements[0].text if value_elements else 'N/A'

                # Try to find the stat_text element, if it exists
                stat_text_elements = metric.find_elements(By.CSS_SELECTOR, 'text.text-stat')
                stat_text = stat_text_elements[0].text if stat_text_elements else 'N/A'

                scraped_data[header] = {'value': value, 'stat': stat_text}

        print(f"Scraped data for {name}: {scraped_data}")
    except TimeoutException:
        print(f"TimeoutException: Elements not found for player {name}.")
    finally:
        driver.quit()

    return scraped_data




def DDOS_the_site():
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = {executor.submit(Scrape, name, player_id, True): (name, player_id) for name, player_id in BBSplayer_ids.pitchers.items()}
        for future in as_completed(futures):
            name, player_id = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                print(f"An error occurred for player {name}: {exc}")
    
    print("All screenshots have been taken and data scraped.")
    return


if __name__ == "__main__":
    test_name, player_id = list(BBSplayer_ids.pitchers.items())[0]
    results = Scrape(test_name, player_id, False)
    #DDOS_the_site()
    print(len(results))
    print("plzdontexit")
