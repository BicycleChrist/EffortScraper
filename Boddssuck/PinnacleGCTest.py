from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
#from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
#from selenium.webdriver.support.select import Select
from selenium.common.exceptions import *
from time import sleep
import pathlib
from distutils import dir_util  # copying folders
import pprint

#TODO Let Paul C know he should have tried harder.
#TODO Upon taking a peak at the books here at around 3 A.M, Bovada is posting player prop odds before Pinnacle.
# Pinnacle posts their odds for the first NBA game today (2/28) at ~2:55 AM
# If this is a consistent occurance (appears it is), it can likely be exploited

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


def FindGameLinks(sport):
    urlseg, urlsuffix = URLbySport[sport]
    baseurl = "https://www.pinnacle.com/en/" + urlseg
    driver.get(str(baseurl+"/matchups"))
    # not sure this wait condition is good enough (LiveContainer is not the one we really want)
    contentBlocks = WebDriverWait(driver, 10).until(
        lambda x: [c for c in x.find_elements(By.CLASS_NAME, "contentBlock")
                   if c.get_attribute("data-test-id") == 'LiveContainer']
    )
    contentBlocks = list(driver.find_elements(By.XPATH, "//div[@id='root']//div[contains(@class, 'contentBlock')]"))
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
def VisitPage(url, sport):
    _, urlsuffix = URLbySport[sport]
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "languageId=2&priceStyle=american&linesTypeView=a&device=d&languageGroup=all"})  # never works
    driver.get(url)
    print(url)
    print(driver.get_cookie("UserPrefsCookie"))
    sleep(3)  # need to give some time for the page to redirect
    # if the game is live, the 'player-props' won't exist and it'll redirect us
    # TODO: handle cases where the page lands on 'Matchup not found.'; this doesn't change the URL so it's not handled by the redirect logic
    if not driver.current_url.endswith(urlsuffix):  # checking to see if we've been redirected
        print(f"redirected: {driver.current_url}")
        # check if we're on a 'Matchup not found.' page (this might be a bad idea)
        #noEventsBlock = driver.find_element(By.XPATH,"//div[contains(@class, 'noEvents')][@data-test-id='NoEvents-Container']")
        #print(noEventsBlock.text)
        #print("Matchup not found")
        #return

    print("finding showAllButton...")
    showAllButton = driver.find_element(By.XPATH, "//div[@data-test-id='Collapse']//button")
    # TODO: figure out if this condition is actually acceptable
    # (there's a glitch where switching from 'player-props' to 'All' sets the wrong text for this button
    if showAllButton.text == "Show All":  # if everything is already shown, this changes to 'Hide All'
        print("Not clicking")  # deliberately not expanding this to get through the pages much faster
        #print("click")
        #showAllButton.click()
        #sleep(1)
        #assert (showAllButton.text == "Hide All")

    print("finding oddsFormatDropdown...")
    oddsFormatDropdown = driver.find_element(By.XPATH, "//div[i[contains(@class, 'icon-info')] and i[contains(@class, 'icon-down-arrow')]]")
    if oddsFormatDropdown.text not in ["Decimal Odds", "American Odds"]:
        print(f"wrong element found for oddsFormatDropdown; {driver.current_url}")
        print("early exit")
        return
    if oddsFormatDropdown.text != "American Odds":
        print("click")
        oddsFormatDropdown.click()  # open dropdown; might invalidate references
        sleep(1)
        stylelist = oddsFormatDropdown.find_element(By.XPATH, '../div/div[contains(@class, "tooltip")]/ul[@data-test-id="OddsFormat"]')
        styles = stylelist.find_elements(By.XPATH, "./li/a")
        styles[1].click()   # the 'not-selected style' always appears second
        # note that changing the odds format doesn't close the dropdown menu, but it also doesn't invalidate any references (you can click the other style)
        #sleep(1)
        print(styles)
    return


def ScrapePage():
    market_dict = {}
    try:
        matchup_market_groups = driver.find_element(By.XPATH, ".//div[contains(@class, 'matchup-market-groups')]")  # container for all the elements of the stats
    except Exception as E:
        print("ScrapePage failed to find matchup-market-groups")
        print(E)
        return market_dict
    #market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"][@data-collapsed="false"]')
    market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"]')
    for group in market_groups:
        if group.get_attribute("data-collapsed") == "true":
            print("unexpanded element!!!")
            continue
        # TODO: on some pages ('#All'), the market-group content blocks will have some of their content hidden, with a clickable 'See more' footer (even if you've hit 'Show All')
        # Assuming there's only one title and content group per market-group
        title = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-title')]").text.removesuffix('\nHide All')  # the 'Show/Hide All' button text will also be concatenated, if it exists
        content = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-content')]")
        try:
            expandMarketBtn = content.find_element(By.XPATH, "./button")  # the expand button is the only one directly nested under the group div
            print(f"Found expandMarketBtn: {expandMarketBtn.text}")
            if expandMarketBtn.text == "See more":
                expandMarketBtn.click()
                print("click")
                assert (expandMarketBtn.text == "See less")
        except NoSuchElementException:  # group doesn't have any hidden elements
            pass
        print(title)
        title = title.removesuffix('\nShow All')
        title = title.removesuffix('\nHide All')
        try:  # find subHeading if it exists (only present on Live-Odds pages?)
            subHeadingElement = content.find_element(By.XPATH, "./ul[li and li]")  # ul with two child elements tagged li (which usually contain the team names)
            subHeading = subHeadingElement.text
            print(subHeading)
        except NoSuchElementException:
            subHeading = ""
        market_dict[title] = {"subHeading": subHeading, "content": []}
        market_buttons = content.find_elements(By.XPATH, ".//button[contains(@class, 'market-btn')]")
        # you can check if the button is disabled (line closed) by checking this attribute: aria-label="Currently Offline"
        for btn in market_buttons:
            temptxt = btn.text
            if len(temptxt) > 0:
                market_dict[title]["content"].append(btn.text.splitlines())
                print(temptxt)
            else:  # assume "Line closed or not posted"
                market_dict[title]["content"].append("Line Closed or Not Posted")
                print("Line Closed or Not Posted")
        print("")  # implied newline
    return market_dict


# we can jump to the 'player-props' page by editing the end of the URL:
# normally the URL looks like this:
#  https://www.pinnacle.com/en/basketball/nba/orlando-magic-vs-detroit-pistons/1585983667/#player-props
# but for some reason it will rewrite the URL back and refuse to reload if you try to edit it
# but you can trick it if you also remove the trailing '/' (still a valid URL)
# ....detroit-pistons/1585983667#player-props









if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "Boddssuck" and "you're in the wrong directory")
    default_sport = "NHL"

    # Set up the Chrome options and WebDriver
    chrome_options = Options()
    chrome_options.add_argument('--log-level=3')  # Set log level to 3 (INFO)
    chrome_options.add_argument('--headless')  # Uncomment if you want to run in headless mode
    # two other headles modes exist, that are less restricitve; headless=new and headless=chrome
    # https://stackoverflow.com/questions/45631715/downloading-with-chrome-headless-and-selenium/73840130#73840130
    chrome_options.page_load_strategy = 'eager'  # Only waits until 'DOMContentLoaded'
    chrome_options.acceptInsecureCerts = False  # Disable accepting insecure certificates


    driver = webdriver.Chrome(options=chrome_options)
    driver.implicitly_wait(3)

    #driver.get("https://www.pinnacle.com")  # need to navigate to domain first to set cookie
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "languageId=2&priceStyle=american&linesTypeView=a&device=d&languageGroup=all"})   # doesn't work
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "priceStyle=american"})  # setting odds-format to american   # doesn't work
    #print(driver.get_cookie("UserPrefsCookie"))
    print(driver.capabilities)
    print(driver.caps)

    #InstallUblock()

    # TODO Handle instances where lines are closed or not posted, as the script exits upon encountering either of these things
    # This can happen despite other lines for the same player being correctly posted and accsessible, so our check for the URL redirect isnt going to work here

    links = FindGameLinks(default_sport)
    print("\n\n")
    totalnum = len(links)
    scraped = []  # list of dictionaries
    # with driver.context(driver.CONTEXT_CONTENT):
    for index, link in enumerate(links):
        print(f"visiting {link} [{index} of {totalnum}]...")
        scraped.append({"index": index, "link": link, "data": {}})
        print(driver.session_id)
        print(driver.capabilities)
        print(driver.caps)
        try:
            VisitPage(link, default_sport)
            scraped[-1]["data"] = ScrapePage()
        except KeyboardInterrupt:
            print("keyboard interrupt")
            print(f"cancelled on: {link}\n [{index} of {totalnum}]")
            #continue
        except Exception as E:
            print(f"exception on: {link}\n [{index} of {totalnum}]")
            if str(E) in SELENIUMERRORS:
                print(f"caught selenium exception:\n {E}; continuing")
                #continue
            else:
                print(f"caught non-selenium exception {E}")
                #break
        print(f"finished visiting {link} [{index} of {totalnum}]\n\n")

    print(f"visited all links [{totalnum}]")
    print("\n\n RESULTS: \n\n")
    pprint.pprint(scraped)
    print("about to quit")
    driver.quit()

