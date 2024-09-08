from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions
from selenium.common.exceptions import *
from time import sleep

from PolymarketParsing import *


def InitializeDriver(useHeadless=True, window_size=(1920, 1080)):
    options = FirefoxOptions()
    if useHeadless: options.add_argument('--headless')
    driver = webdriver.Firefox(options=options, keep_alive=True)
    driver.set_window_position(0,0)
    driver.set_window_size(*window_size)
    driver.implicitly_wait(2)
    return driver


# this version only grabs the top-level hrefs
def GetMarketLinks(useHeadless=True, useZoomOut=False):
    market_links = []
    driver = InitializeDriver(useHeadless)
    driver.get("https://polymarket.com/markets/all")
    
    if useHeadless and useZoomOut: print("\nZoomOut only works without headless. Disabling.\n"); useZoomOut=False
    if useZoomOut:  # Linux only, non-headless only (currently)
        sleep(1) # wait for page load
        ZoomOutFirefox()
        sleep(2)  # Wait for the page to adjust after zooming out
    
    marketsContainer_xpath = "/html/body/div[@id='__next']//div[@id='__pm_markets_grid']/div[@id='markets-grid-container']"
    loadMoreButton_xpath = marketsContainer_xpath + "/../div[2]/button/div/span[2]" # same parent as markets-container (which is first child div), under the second div
    endOfResults_xpath = marketsContainer_xpath + "/../div[2]/p[text()='End of results']" # same path as 'loadMoreButton', but element is 'p' instead
    hrefs_xpath = marketsContainer_xpath + "/div[@id='quick-buy-card']/div[1]/div[2]/a[@href]"  # first div on card is header, second is body. under header, first div is image, second is title.
    
    ignoreThese = [
        ElementNotInteractableException, ElementClickInterceptedException, 
        InvalidElementStateException, InvalidSwitchToTargetException, InvalidCoordinatesException,
        TimeoutException
    ]
    
    endOfPage = False
    didTimeout = False
    while not endOfPage:
        try:
            loadmore = WebDriverWait(driver, 5, ignored_exceptions=ignoreThese).until(expected_conditions.element_to_be_clickable((By.XPATH, loadMoreButton_xpath)))
            if loadmore: print(loadmore.text)
            else: endOfPage = WebDriverWait(driver, 5, ignored_exceptions=ignoreThese).until(expected_conditions.presence_of_element_located((By.XPATH, endOfResults_xpath)))
            if endOfPage: print(endOfPage.text); market_links = [a.get_attribute('href') for a in driver.find_elements(By.XPATH, hrefs_xpath)]; break
            
            clickfailedCount = 0
            clicked = False
            while not clicked:
                try: # with contextlib.suppress(*ignoreThese):
                    loadmore.click()
                    clicked = True
                except (*ignoreThese,):
                    print("failed to click; ignoring exception")
                    clickfailedCount += 1
                    if clickfailedCount > 20: break
                    continue
            if clicked: print("clicked"); continue
            else: print("not clicked")
        except TimeoutException: print(f"timeout"); didTimeout=True; break
        except Exception as E: print(f"real bad exception: {type(E)} \n {E}"); break
    # assume end of page on timeout. For some reason it can't detect endOfPage without unzooming, and therefore always timeouts
    if didTimeout: market_links = [a.get_attribute('href') for a in driver.find_elements(By.XPATH, hrefs_xpath)]
    
    driver.quit()
    return market_links


if __name__ == "__main__":
    market_links = GetMarketLinks(useHeadless=True)  # useZoomOut is Linux only (bash-script), non-headless only (currently)
    if len(market_links) == 0: print("Error: No market links found"); exit(1)
    print(f"\nScrape complete \nFound {len(market_links)} market links\n\n")
    parsed = ParseHrefs(market_links)
    pprint(parsed)
    print(f"\nScrape complete \nFound {len(market_links)} market links\n\n")
    

