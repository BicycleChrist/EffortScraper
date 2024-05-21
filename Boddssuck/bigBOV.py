from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import *
from time import sleep  # TODO: replace sleeps with selenium expected-conditions



def TryToFind(element, xpath_string):
    try:
        x = element.find_element(By.XPATH, xpath_string)
        return x
    except Exception as SeleniumBullshit:
        print(f"error trying to find: {xpath_string}")
        print(f"from: {element}")
        print(SeleniumBullshit)
    return element


def FindGameLinks(driver: webdriver.Firefox):
    url = 'https://www.bovada.lv/sports/politics'
    #url = 'https://www.bovada.lv/sports/baseball/mlb'
    driver.get(url)
    sleep(1)
    try:
        sp_path_event = driver.find_element(By.XPATH, "/html/body//div[@class='top-container']//main/div[@class='sp-main-area']//sp-path-event")
        grouped_events = sp_path_event.find_elements(By.XPATH, ".//div[@class='grouped-events']")
    except NoSuchElementException:
        print("No grouped events found on the page.")
        return []

    events = []
    for group in grouped_events:
        events.extend(group.find_elements(By.XPATH, ".//sp-coupon//a[@class='game-view-cta']"))

    game_links = [event.get_attribute('href') for event in events]
    print(f"\n game_links: {game_links} \n")
    return game_links


def VisitGameLink(driver:webdriver.Firefox, game_link):
    print(f"navigating to: {game_link}")
    driver.get(game_link)
    sleep(1)
    print("got link")
    main_area = driver.find_element(By.CLASS_NAME, 'sp-main-area')
    print("found main area")
    event = main_area.find_element(By.XPATH, './/sp-event')
    print("found event")

    event_coupons = event.find_elements(By.TAG_NAME, 'sp-coupon')
    print("found event coupons")
    article_lists = [coupon.find_elements(By.TAG_NAME, "article") for coupon in event_coupons]
    intermediate = [a for a in article_lists if len(a) > 0]
    article_lists = intermediate
    print(article_lists)

    results = []
    for articlelist in article_lists:
        for article in articlelist:
            new_dict = {}
            for thing in ('header', 'section'):
                new_dict[thing] = article.find_element(By.TAG_NAME, thing).text.splitlines()
            results.append(new_dict)
            print(new_dict)

    print(results)
    return results


# For futures markets,(I.E politics and league championship winners), odds are contained on the initial page
# In the event that no game links are found for the given url, we'll parse the current page
def ScrapeCurrentPage(driver:webdriver.Firefox):
    print("no game links found, scraping current page")
    main_area = driver.find_element(By.CLASS_NAME, 'sp-main-area')
    print("found main area")
    event = main_area.find_element(By.CLASS_NAME, 'max-container')
    print("found event")

    event_coupons = event.find_elements(By.TAG_NAME, 'sp-coupon')
    print("found event coupons")
    article_lists = [coupon.find_elements(By.TAG_NAME, "article") for coupon in event_coupons]
    intermediate = [a for a in article_lists if len(a) > 0]
    article_lists = intermediate
    print(article_lists)

    results = []
    for articlelist in article_lists:
        for article in articlelist:
            new_dict = {}
            for thing in ('header', 'section'):
                new_dict[thing] = article.find_element(By.TAG_NAME, thing).text.splitlines()
            results.append(new_dict)
            print(new_dict)

    print(results)
    return results


if __name__ == "__main__":
    options = FirefoxOptions()
    options.page_load_strategy = 'eager'
    options.add_argument('--headless')
    firefox_profile = FirefoxProfile()
    options.profile = firefox_profile
    print(firefox_profile)
    print(options.profile.path)

    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(1)
    print("finding game links")
    game_links = FindGameLinks(driver)

    if game_links:
        for game_link in game_links:
            try:
                VisitGameLink(driver, game_link)
            except StaleElementReferenceException as stale_exception:
                print(f"selenium is tarding out: {stale_exception}")
                continue
    else:
        ScrapeCurrentPage(driver)

    print("done")
    driver.quit()
