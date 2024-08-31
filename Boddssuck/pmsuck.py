from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException
import time

# Each market on the all markets page has its own small scroll bar which can interfere with the selenium action chain scrolling process.
# Making the window huge has ameliorated this issue but we must remember that it exists
#TODO: Decrease zoom level of window to 30-40% in initialize_driver function, allowing more content to load every scroll increasing scrape speed

def initialize_driver():
    options = FirefoxOptions()
    options.add_argument('--headless')
    driver = webdriver.Firefox(options=options)
    # center browser window
    driver.set_window_position(0,0)
    # Increase window size
    driver.set_window_size(7500,5240)



    return driver

def scroll_until_end_of_results(driver, max_scrolls=1000):
    print("Scrolling the page...")
    body = driver.find_element(By.TAG_NAME, 'body')


    for _ in range(max_scrolls):
        ActionChains(driver).send_keys_to_element(body, Keys.PAGE_DOWN).perform()
        time.sleep(0.5)  # Short pause to allow content to load

        if driver.find_elements(By.XPATH, "//p[contains(text(),'End of results')]"):
            print("End of results found.")
            return True
        else:
            continue

    print(f"Reached maximum number of scrolls ({max_scrolls}) without finding 'End of results'.")
    return False

def main():
    driver = initialize_driver()
    try:
        driver.get("https://polymarket.com/markets/all")

        # wait for the market grid to be visible
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "markets-grid-container"))
        )

        # keep scrolling until end of results
        all_loaded = scroll_until_end_of_results(driver)

        # extract all href attributes for the markets after scrolling
        market_links = [elem.get_attribute('href') for elem in driver.find_elements(By.XPATH, "//a[contains(@href, '/event/')]")]

        print(f"Found {len(market_links)} market links:")
        for link in market_links:
            print(link)

        if not all_loaded:
            print("Warning: Not all markets may have been loaded.")

    finally:
        # Wait a smidge before quit
        time.sleep(2)
        driver.quit()

if __name__ == "__main__":
    main()
