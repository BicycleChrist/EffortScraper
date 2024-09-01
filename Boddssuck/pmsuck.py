from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException
import time

from PolymarketParsing import ParseHrefs

# Each market on the all markets page has its own small scroll bar which can interfere with the selenium action chain scrolling process.
# Making the window huge has ameliorated this issue but we must remember that it exists
#TODO: Decrease zoom level of window to 30-40% in initialize_driver function, allowing more content to load every scroll increasing scrape speed

def initialize_driver():
    options = FirefoxOptions()
    #options.add_argument('--headless')
    driver = webdriver.Firefox(options=options, keep_alive=True)
    driver.set_window_position(0,0)
    driver.set_window_size(5000,7500)
    driver.implicitly_wait(2)
    return driver

def ZoomOut(driver, zoom_level=0.5):
    print(f"Zooming out to {zoom_level * 100}%")
    driver.execute_script(f"document.body.style.MozTransform = 'scale({zoom_level})';")
    driver.execute_script(f"document.body.style.MozTransformOrigin = 'top left';")
    time.sleep(0.5)
    return

def GoToPage(driver, url):
    print(f"Getting page {url}")
    driver.get(url)
    #WebDriverWait(driver, 10).until(
    #    EC.presence_of_element_located((By.ID, "markets-grid-container"))
    #)
    print("finding markets")
    markets = driver.find_element(By.ID, 'markets-grid-container')
    print("found markets")
    return markets


def ScrollPage(driver, directionDown=True):
    scroll_key = Keys.PAGE_DOWN if directionDown else Keys.PAGE_UP
    ActionChains(driver).send_keys(scroll_key).perform()
    time.sleep(0.75)
    return



def scroll_until_end_of_results(driver, markets, max_scrolls=1000):
    print("Scrolling the page...")
    for _ in range(max_scrolls):
        ActionChains(driver).send_keys_to_element(markets, Keys.PAGE_DOWN).perform()

        # Check if we've reached the end of results
        if driver.find_elements(By.XPATH, "//p[contains(text(),'End of results')]"):
            print("End of results found.")
            return True

    print(f"Reached maximum number of scrolls ({max_scrolls}) without finding 'End of results'.")
    return False


def main():
    market_links = []
    driver = initialize_driver()
    try:
        markets = GoToPage(driver, "https://polymarket.com/markets/all")
        ZoomOut(driver, 0.75)  # Zoom out to 50%
        time.sleep(2)  # Wait for the page to adjust after zooming out

        # Use the new scroll function
        scroll_until_end_of_results(driver, markets)

    finally:
        # Gather all market links
        market_links.extend([elem.get_attribute('href') for elem in driver.find_elements(By.XPATH, "//a[contains(@href, '/event/')]") if not elem.get_attribute('href').endswith("#comments")])
        driver.quit()
    
    return market_links


if __name__ == "__main__":
    market_links = main()
    print(f"\nScrape complete \nFound {len(market_links)} market links\n\n")
    parsed = ParseHrefs(market_links)
    pprint(parsed)

