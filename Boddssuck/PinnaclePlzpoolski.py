from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.common.exceptions import *
from time import sleep
import pathlib
import pprint
from concurrent.futures import ThreadPoolExecutor

#TODO Let Paul C know he should have tried harder.
#TODO Scrape the page faster, threading isnt enough ):

# Looks like the option for the odds-format is stored under the localstorage:
# Local Storage > Main:Preferences (Object) > Odds:Object > format:"decimal"

SELENIUMERRORS = [
    'ElementClickInterceptedException', 'ElementNotInteractableException', 'ElementNotSelectableException', 'ElementNotVisibleException', 'ImeActivationFailedException', 'ImeNotAvailableException', 'InsecureCertificateException', 'InvalidArgumentException',
    'InvalidCookieDomainException', 'InvalidCoordinatesException', 'InvalidElementStateException', 'InvalidSelectorException', 'InvalidSessionIdException', 'InvalidSwitchToTargetException', 'JavascriptException',
    'MoveTargetOutOfBoundsException', 'NoAlertPresentException', 'NoSuchAttributeException', 'NoSuchCookieException', 'NoSuchDriverException', 'NoSuchElementException', 'NoSuchFrameException', 'NoSuchShadowRootException', 'NoSuchWindowException', 'Optional',
    'ScreenshotException', 'Sequence', 'SessionNotCreatedException', 'StaleElementReferenceException', 'TimeoutException', 'UnableToSetCookieException', 'UnexpectedAlertPresentException', 'UnexpectedTagNameException', 'UnknownMethodException', 'WebDriverException',
    #'SUPPORT_MSG', 'ERROR_URL',    # not sure if these are actual error types
]


# holds (URL-sub-segment, URL-suffix)
URLbySport = {
    "NBA": ("basketball/nba", "#player-props"),
    "NHL": ("hockey/nhl",     "#game-props"),
    "NFL": ("football/nfl",   ""),
    "MLB": ("baseball/mlb",   ""),
}

def FindGameLinks(driver, sport):
    urlseg, urlsuffix = URLbySport[sport]
    baseurl = "https://www.pinnacle.com/en/" + urlseg
    driver.get(str(baseurl + "/matchups"))
    # not sure this wait condition is good enough (LiveContainer is not the one we really want)
    #contentBlocks = WebDriverWait(driver, 10).until(
    #    lambda x: [c for c in x.find_elements(By.CLASS_NAME, "contentBlock")
    #               if c.get_attribute("data-test-id") == 'LiveContainer']
    #)
    driver.implicitly_wait(0)
    contentBlocks = list(driver.find_elements(By.XPATH, "//div[@id='root']//div[contains(@class, 'contentBlock')]"))
    driver.implicitly_wait(1)
    gamelinks = []
    for block in contentBlocks:
        print(f"{block.get_attribute('class'), block.get_attribute('data-test-id')}")
        rows = [row.text for row in block.find_elements(By.CLASS_NAME, 'event-row-participant')]  # text for each team name
        hrefs = [link.get_attribute('href') for link in block.find_elements(By.XPATH, ".//a[@class=''][@href]")]
        # removing trailing slash after NBA to properly create URL's for props.
        gamelinks.extend([link[:-1] + urlsuffix for link in hrefs if (link.startswith(baseurl) and link.endswith('/'))])

        pprint.pprint(rows)
        #pprint.pprint(hrefs)

    pprint.pprint(gamelinks)
    return gamelinks


# TODO: test the handling of the 'Matchup not found' pages
def VisitPage(url, driver: webdriver.Firefox, sport):
    _, urlsuffix = URLbySport[sport]
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "languageId=2&priceStyle=american&linesTypeView=a&device=d&languageGroup=all"})  # never works
    driver.get(url)
    print(url)
    print(driver.get_cookie("UserPrefsCookie"))
    sleep(1)  # need to give some time for the page to redirect
    # if the game is live, the 'player-props' won't exist and it'll redirect us
    # TODO: handle cases where the page lands on 'Matchup not found.'; this doesn't change the URL so it's not handled by the redirect logic
    if not driver.current_url.endswith(urlsuffix):  # checking to see if we've been redirected
        print(f"redirected: {driver.current_url}")
        #return driver
    try:
        print("finding showAllButton...")
        showAllButton = driver.find_element(By.XPATH, "//div[@data-test-id='Collapse']//button")
        # TODO: figure out if this condition is actually acceptable
        # (there's a glitch where switching from 'player-props' to 'All' sets the wrong text for this button
        if showAllButton.text == "Show All":  # if everything is already shown, this changes to 'Hide All'
            print("click")
            showAllButton.click()
            sleep(1)
            assert (showAllButton.text == "Hide All")
    except Exception as E:
        print(E)
        # check if we're on a 'Matchup not found.' page (this might be a bad idea)
        noEventsBlock = driver.find_element(By.XPATH, "//div[contains(@class, 'noEvents')][@data-test-id='NoEvents-Container']")
        print(noEventsBlock.text)
        print("Matchup not found")
        return driver
    try:
        print("finding oddsFormatDropdown...")
        oddsFormatDropdown = driver.find_element(By.XPATH, "//div[i[contains(@class, 'icon-info')] and i[contains(@class, 'icon-down-arrow')]]")
        if oddsFormatDropdown.text not in ["Decimal Odds", "American Odds"]:
            print(f"wrong element found for oddsFormatDropdown; {driver.current_url}")
            print("early exit")
            return driver
        if oddsFormatDropdown.text != "American Odds":
            print("click")
            oddsFormatDropdown.click()  # open dropdown; might invalidate references
            sleep(1)
            stylelist = oddsFormatDropdown.find_element(By.XPATH, '../div/div[contains(@class, "tooltip")]/ul[@data-test-id="OddsFormat"]')
            driver.implicitly_wait(0)
            styles = stylelist.find_elements(By.XPATH, "./li/a")
            styles[1].click()   # the 'not-selected style' always appears second
            # note that changing the odds format doesn't close the dropdown menu, but it also doesn't invalidate any references (you can click the other style)
            sleep(1)
            print(styles)
            driver.implicitly_wait(1)
    except Exception as e:  # TODO: specifically catch selenium errors only
        print(e)
    return driver


def ScrapePage(driver: webdriver.Firefox):
    market_dict = {}
    try:
        matchup_market_groups = driver.find_element(By.XPATH, ".//div[contains(@class, 'matchup-market-groups')]")  # container for all the elements of the stats
    except Exception as E:
        print("ScrapePage failed to find matchup-market-groups")
        print(E)
        return
    driver.implicitly_wait(0)
    #market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"][@data-collapsed="false"]')
    market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"]')
    for group in market_groups:
        if group.get_attribute("data-collapsed") == "true":
            print("unexpanded element!!!")
            continue
        # TODO: on some pages ('#All'), the market-group content blocks will have some of their content hidden, with a clickable 'See more' footer (even if you've hit 'Show All')
        # Assuming there's only one title and content group per market-group
        driver.implicitly_wait(1)
        title = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-title')]").text.removesuffix('\nHide All')  # the 'Show/Hide All' button text will also be concatenated, if it exists
        content = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-content')]")
        try:
            expandMarketBtn = content.find_element(By.XPATH, "./button")  # the expand button is the only one directly nested under the group div
            print(f"Found expandMarketBtn: {expandMarketBtn.text}")
            if expandMarketBtn.text == "See more":
                expandMarketBtn.click()
                print("click")
                assert(expandMarketBtn.text == "See less")
        except NoSuchElementException:  # group doesn't have any hidden elements
            pass
        print(title)
        try:  # find subHeading if it exists (only present on Live-Odds pages?)
            subHeading = content.find_element(By.XPATH, "./ul[li and li]")  # ul with two child elements tagged li (which usually contain the team names)
            print(subHeading.text)
        except NoSuchElementException:
            subHeading = None
        driver.implicitly_wait(0)
        market_dict[title] = {"subHeading": subHeading, "content": []}
        market_buttons = content.find_elements(By.XPATH, ".//button[contains(@class, 'market-btn')]")
        for btn in market_buttons:
            temptxt = btn.text
            if len(temptxt) > 0:
                market_dict[title]["content"].append(btn.text.splitlines())
                print(temptxt)
            else:
                market_dict[title]["content"].append("Line Closed or Not Posted")
                print("Line Closed or Not Posted")
        print("")
        driver.implicitly_wait(1)
    return market_dict

# Initialize WebDriver for each thread
def initialize_driver():
    options = FirefoxOptions()
    options.add_argument('--headless')
    #firefox_profile = FirefoxProfile(profile_directory=ProfilePath())
    #options.profile = firefox_profile
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(2)
    return driver

def scrape_link(link, sport):
    driver = initialize_driver()
    current_state = VisitPage(link, driver, sport)
    market_data = ScrapePage(current_state)
    driver.quit()
    return market_data

if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "Boddssuck" and "you're in the wrong directory")
    default_sport = "MLB"

    driver = initialize_driver()
    with driver.context(driver.CONTEXT_CONTENT):
        links = FindGameLinks(driver, default_sport)
    driver.quit()


    with ThreadPoolExecutor(max_workers=None) as executor:
        results = list(executor.map(lambda link: scrape_link(link, default_sport), links))

    for result in results:
        pprint.pprint(result)
