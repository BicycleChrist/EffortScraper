from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.remote.webdriver import WebElement
from selenium.common.exceptions import *
from time import sleep
import pathlib
import pprint
from concurrent.futures import ThreadPoolExecutor, as_completed

#TODO Let Paul C know he should have tried harder.

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
    "MLB": ("baseball/mlb",   "#player-props"),
}


def TryToFind(element:WebElement, locator_string:str, locator_method=By.XPATH, print_onfail=False, find_multi=False) -> WebElement | list[WebElement] | None:
    try:
        copy_element = element
        if find_multi: return copy_element.find_elements(locator_method, locator_string)
        return copy_element.find_element(locator_method, locator_string)
    except NoSuchElementException:
        if print_onfail:
            print(f"error trying to find: {locator_string}, with method: {locator_method}")
            #print(f"from: {element}")
    return None


def FindGameLinks(driver, sport):
    urlseg, urlsuffix = URLbySport[sport]
    baseurl = "https://www.pinnacle.com/en/" + urlseg
    driver.get(str(baseurl + "/matchups"))
    # not sure this wait condition is good enough (LiveContainer is not the one we really want)
    #contentBlocks = WebDriverWait(driver, 10).until(
    #    lambda x: [c for c in x.find_elements(By.CLASS_NAME, "contentBlock")
    #               if c.get_attribute("data-test-id") == 'LiveContainer']
    #)
    #driver.implicitly_wait(0) # broken
    driver.implicitly_wait(3)
    contentBlocks = list(driver.find_elements(By.XPATH, "//div[@id='root']//div[contains(@class, 'contentBlock')]"))
    driver.implicitly_wait(1)
    gamelinks = []
    teamnames = []
    for block in contentBlocks:
        print(f"{block.get_attribute('class'), block.get_attribute('data-test-id'), block.get_attribute('data-mode')}")
        if block.get_attribute('data-mode') == 'live': continue
        if block.get_attribute('data-test-id') == 'LiveContainer': continue
        rows = [row.text for row in block.find_elements(By.CLASS_NAME, 'event-row-participant')]  # text for each team name
        teamnames.extend(rows)
        hrefs = [link.get_attribute('href') for link in block.find_elements(By.XPATH, ".//a[@class=''][@href]")]
        # removing trailing slash after NBA to properly create URL's for props.
        gamelinks.extend([link[:-1] + urlsuffix for link in hrefs if (link.startswith(baseurl) and link.endswith('/'))])
        
        #pprint.pprint(rows)
        #pprint.pprint(hrefs)
    
    # assuming that the two lists will always line up in the same way. And that they're always in the same order (relative to each other)
    gametitles = [f"{a} vs {b}" for a,b in zip(teamnames[0::2], teamnames[1::2])]
    mapped_gamelinks = {link: gametitle for link, gametitle in zip(gamelinks, gametitles)}
    
    #pprint.pprint(mapped_gamelinks)
    return mapped_gamelinks


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


# contentblock is the child div under the marketgroup div with the class 'collapse-content'
def SubdivideMarketGroup(contentblock: WebElement) -> dict:
    def Findlambda(locator_string:str, locator_method=By.XPATH, element=contentblock, find_multi=False):
        return TryToFind(element, locator_string, locator_method, find_multi=find_multi)
    
    webelements = {
        "subheadings"    : Findlambda("./ul/li",   find_multi=True),            # pretty sure this is the only element with this tag
        "buttonrows"     : lambda: Findlambda("./div/div", find_multi=True),    # in the HTML they're 'buttonRow', but they're grouped by column # Maybe delete this comment  
        "expandMarketBtn": Findlambda("./button"),                                          # the expand button is the only one directly nested under the g
    }
    
    return webelements


# webelements is supposed to be the dict reeturned from SubdivideMarketGroup
def ParseMarketGroup(webelements: dict) -> dict:
    subheadings, buttonrows, expand_button = webelements.values()
    if expand_button is not None:
        if expand_button.text == "See more":
            expand_button.click(); #print("click")
            # TODO: figure out if clicking the expand button invalidates the buttoncolumns
    
    def ParseButton(button: WebElement) -> dict:
        aria_value = button.get_attribute("aria-label")
        if aria_value.startswith("Money Line "):
            return {button.get_attribute("title"): int(aria_value.removeprefix("Money Line "))}
        return {}
    
    index = 0
    def GetNextIndex():
        nonlocal index; index += 1
        return index
    
    buttonrow_buttons = {
        GetNextIndex(): buttonrow.find_elements(By.XPATH, './div/button') for index, buttonrow in enumerate(buttonrows())
    }
    
    # for testing
    #subheadings = ("Houston Astros", "Chicago White Sox")
    #buttonrow_buttons[1] = list(reversed(buttonrow_buttons[0]))
    
    if len(subheadings) > 0:
        column_headers = [subheading.text for subheading in subheadings]
        return {
            column_headers[localindex%2] : { key:value }
            for buttonlist in buttonrow_buttons.values()
            for localindex, button in enumerate(buttonlist)
            for key, value in ParseButton(button).items()
        }
    
    # no subheading
    return {
        key: value
        for buttonlist in buttonrow_buttons.values()
        for button in buttonlist
        for key, value in ParseButton(button).items()
    }
# end of ParseMarketGroup


def SearchButDontScrewMe(element, *args):
    return (lambda copyelement=element: copyelement.find_element(*args))()


def ScrapePage(driver: webdriver.Firefox):
    market_dict = {}
    html_body = driver.find_element(By.XPATH, '/html/body')
    matchup_market_groups = TryToFind(html_body, ".//div[contains(@class, 'matchup-market-groups')]")
    if matchup_market_groups is None:
        print("ScrapePage failed to find matchup-market-groups")
        return
    
    driver.implicitly_wait(0)
    market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"][@data-collapsed="false"]')  # filtering out collapsed elements (maybe a bad idea)
    #market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"]')
    #driver.implicitly_wait(1)
    
    market_blocks = {
        # title : content (container for buttons and subheading, if any)
        SearchButDontScrewMe(group, By.XPATH, "./div[contains(@class, 'collapse-title')]").text.removesuffix('\nHide All') :
        SearchButDontScrewMe(group, By.XPATH, "./div[contains(@class, 'collapse-content')]")
        for group in market_groups
    }
    
    for title, block in market_blocks.items():
        print(title)
        group_contents = SubdivideMarketGroup(block)
        result = ParseMarketGroup(group_contents)
        market_dict[title] = result
    
    return market_dict

# 
# def Temp():
#     market_dict = {}
#     market_groups = []
#     for group in market_groups:
#         if group.get_attribute("data-collapsed") == "true":
#             print("unexpanded element!!!")
#             continue
#         # TODO: on some pages ('#All'), the market-group content blocks will have some of their content hidden, with a clickable 'See more' footer (even if you've hit 'Show All')
#         # Assuming there's only one title and content group per market-group
#         driver.implicitly_wait(1)
#         
#         title = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-title')]").text.removesuffix('\nHide All')  # the 'Show/Hide All' button text will also be concatenated, if it exists
#         content = group.find_element(By.XPATH, ".//div[contains(@class, 'collapse-content')]")
#         
#         expandMarketBtn = []
#         for button in expandMarketBtn:
#             expandMarketBtn = content.find_element(By.XPATH, "./button")  # the expand button is the only one directly nested under the group div
#             print(f"Found expandMarketBtn: {expandMarketBtn.text}")
#             if expandMarketBtn.text == "See more":
#                 expandMarketBtn.click()
#                 print("click")
#                 assert(expandMarketBtn.text == "See less")
#         print(title)
#         try:  # find subHeading if it exists (only present on Live-Odds pages?)
#             subHeading = content.find_element(By.XPATH, "./ul[li and li]")  # ul with two child elements tagged li (which usually contain the team names)
#             print(subHeading.text)
#         except NoSuchElementException:
#             subHeading = None
#         driver.implicitly_wait(0)
#         market_dict[title] = {"subHeading": subHeading, "content": []}
#         market_buttons = content.find_elements(By.XPATH, ".//button[contains(@class, 'market-btn')]")
#         for btn in market_buttons:
#             temptxt = btn.text
#             if len(temptxt) > 0:
#                 market_dict[title]["content"].append(btn.text.splitlines())
#                 print(temptxt)
#             else:
#                 market_dict[title]["content"].append("Line Closed or Not Posted")
#                 print("Line Closed or Not Posted")
#         print("")
#         driver.implicitly_wait(1)
#     return market_dict


# Initialize WebDriver for each thread
def initialize_driver():
    options = FirefoxOptions()
    options.add_argument('--headless')
    #options.page_load_strategy = 'eager'  # breaks everything
    
    #firefox_profile = FirefoxProfile(profile_directory=ProfilePath())
    #options.profile = firefox_profile
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(1)
    return driver

def scrape_link(link, sport):
    print(f"scraping {link}")
    driver = initialize_driver()
    try:
        current_state = VisitPage(link, driver, sport)
        market_data = ScrapePage(current_state)
    finally:
        driver.quit()
    return market_data


def Main_Multithreaded():
    default_sport = "MLB"
    
    driver = initialize_driver()
    with driver.context(driver.CONTEXT_CONTENT):
        mapped_gamelinks = FindGameLinks(driver, default_sport)
    driver.quit()
    
    def ScrapeLinkLambda(link, gametitle, sport):
        return {gametitle: scrape_link(link, sport)}
    
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = [executor.submit(ScrapeLinkLambda, link, gametitle, default_sport) for link, gametitle in mapped_gamelinks.items()]
    
    results = [future.result() for future in as_completed(futures)]
    
    # Merge all dictionaries in the results list
    return {k: v for result_dict in results for k, v in result_dict.items()}

def Main():
    default_sport = "MLB"
    driver = initialize_driver()
    with driver.context(driver.CONTEXT_CONTENT):
        mapped_gamelinks = FindGameLinks(driver, default_sport)
    driver.quit()
    
    results = {}
    for link, gametitle in mapped_gamelinks.items():
        # avoiding an overwrite / clobbering info across dates (games for today and tomorrow are both in the same container element)
        # note that this completely breaks multithreading; the assumption is that today's games always come first
        if gametitle in results.keys(): print(f"entry already exists for {gametitle}; skipping"); continue
        results[gametitle] = scrape_link(link, default_sport)
    return results


if __name__ == "__main__":
    scraped_data = Main()
    #scraped_data = Main_Multithreaded()
    pprint.pprint(scraped_data)
    
    
