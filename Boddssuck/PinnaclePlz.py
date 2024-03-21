from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
#from selenium.webdriver.support import expected_conditions as EC
#from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
#from selenium.webdriver.support.select import Select
#from selenium.common.exceptions import *
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


def FindGameLinks(driver, sport):
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
def VisitPage(url, driver: webdriver.Firefox, sport):
    _, urlsuffix = URLbySport[sport]
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "languageId=2&priceStyle=american&linesTypeView=a&device=d&languageGroup=all"})  # never works
    driver.get(url)
    sleep(3)  # need to give some time for the page to redirect
    # if the game is live, the 'player-props' won't exist and it'll redirect us
    # TODO: handle cases where the page lands on 'Matchup not found.'; this doesn't change the URL so it's not handled by the redirect logic
    if not driver.current_url.endswith(urlsuffix):  # checking to see if we've been redirected
        print(f"redirected: {driver.current_url}")
        return {}
    try:
        print(driver.get_cookie("UserPrefsCookie"))
        showAllButton = driver.find_element(By.XPATH, "//div[@data-test-id='Collapse']//button")
    except Exception as E:
        # assuming the exception is caused by not finding the 'showAllButton'
        print(E)
        # check if we're on a 'Matchup not found.' page (this might be a bad idea)
        noEventsBlock = driver.find_element(By.XPATH, "//div[contains(@class, 'noEvents')][@data-test-id='NoEvents-Container']")
        print(noEventsBlock.text)
        print("Matchup not found")
        return {}

    if showAllButton.text == "Show All":  # if everything is already shown, this changes to 'Hide All'
        showAllButton.click()
        sleep(1)
    # TODO: click the menu to change the odds format to american
    try:
        oddsFormatDropdown = driver.find_element(By.XPATH, "//div[i[contains(@class, 'icon-info')] and i[contains(@class, 'icon-down-arrow')]]")
        if oddsFormatDropdown.text not in ["Decimal Odds", "American Odds"]:
            print(f"wrong element found for oddsFormatDropdown; {driver.current_url}")
            print("early exit")
            return {}
        if oddsFormatDropdown.text != "American Odds":
            oddsFormatDropdown.click()  # open dropdown; might invalidate references
            sleep(1)
            stylelist = oddsFormatDropdown.find_element(By.XPATH, '../div/div[contains(@class, "tooltip")]/ul[@data-test-id="OddsFormat"]')
            styles = stylelist.find_elements(By.XPATH, "./li/a")
            styles[1].click()   # the 'not-selected style' always appears second
            # note that changing the odds format doesn't close the dropdown menu, but it also doesn't invalidate any references (you can click the other style)
            sleep(1)
            print(styles)
    except Exception as e:
        print(e)

    print("plsbreak")
    # TODO: split this function in half

    market_dict = {}
    matchup_market_groups = driver.find_element(By.XPATH, ".//div[contains(@class, 'matchup-market-groups')]")  # container for all the elements of the stats
    market_groups = matchup_market_groups.find_elements(By.XPATH, './/div[@data-test-id="Collapse"][@data-collapsed="false"]')
    for group in market_groups:
        # so we don't do a for-loop becauase it'll repeat everything 100 times for no reason
        titles = [element.text for element in group.find_elements(By.XPATH, ".//div[contains(@class, 'collapse-title')]")]
        contents = group.find_elements(By.XPATH, ".//div[contains(@class, 'collapse-content')]")
        for content in contents:
            market_buttons = content.find_elements(By.XPATH, ".//button[contains(@class, 'market-btn')]")
            for index, title in enumerate(titles):
                if (index*2) >= len(market_buttons):
                    print("oob index")
                    break
                # TODO: figure out why titles ending in 'e' have the last letter missing
                # TODO: actually just rewrite this back to using nested loops instead of indecies (market_buttons are relative to contents)
                stptitle = title.rstrip('\nHide All')
                market_dict[stptitle] = {}  # the first one contains the 'show-all' button as well
                associated_content = [market_buttons[index*2], market_buttons[(index*2)+1]]
                print(stptitle)
                # you can check if the button is disabled (line closed) by checking this attribute: aria-label="Currently Offline"
                for btn in associated_content:
                    tmpvar = btn.text.splitlines()
                    if len(tmpvar) == 2:
                        moneyline, value = tmpvar
                        print(moneyline, value)
                        market_dict[stptitle][moneyline] = value
                    else:  # less than two strings
                        print("Line closed or not posted")
                        continue

    return market_dict


# we can jump to the 'player-props' page by editing the end of the URL:
# normally the URL looks like this:
#  https://www.pinnacle.com/en/basketball/nba/orlando-magic-vs-detroit-pistons/1585983667/#player-props
# but for some reason it will rewrite the URL back and refuse to reload if you try to edit it
# but you can trick it if you also remove the trailing '/' (still a valid URL)
# ....detroit-pistons/1585983667#player-props

# you'll have to manually update the filter lists; auto-update is disabled
def InstallUblock():
    print("Installing uBlock")
    cwd = pathlib.Path.cwd()
    ublock_path = cwd / ".seleniumstorage" / "uBlock0@raymondhill.net.xpi"
    assert (ublock_path.exists() and ublock_path.is_file())
    ext_identifier = driver.install_addon(ublock_path.absolute(), temporary=False)
    print(ext_identifier)   # uBlock0@raymondhill.net


def ProfilePath():
    selenium_profile_path = cwd / ".seleniumstorage" / "scraperprofile"
    # use the local folder if it exists and is not empty
    if selenium_profile_path.exists() and (len(list(selenium_profile_path.glob('*'))) > 0):
        return str(selenium_profile_path)
    print("ignoring local profile folder! creating new profile")
    new_firefox_profile = FirefoxProfile()  # create a new temp one
    temp_profile_path = new_firefox_profile.path  # these folders only have a 'user.js' in them????
    # maybe we have to actually run the browser to populate the folder?
    return temp_profile_path


# copies the temp profile folder into our seleniumstorage folder
def CopyProfile(forceOverwrite=False):
    selenium_profile_path = cwd / ".seleniumstorage" / "scraperprofile"
    if selenium_profile_path.exists() and (len(list(selenium_profile_path.glob('*'))) > 0):
        print("not overwriting current profile; directory not empty")
        # TODO: implement forceOverwrite
        return []
    # temp_profile_path = firefox_profile.profile_dir   # these folders only have a 'user.js' in them????
    temp_profile_path = pathlib.Path("/tmp/rust_mozprofileAwAtu2")  # an actual profile dir
    assert (temp_profile_path.exists() and temp_profile_path.is_dir())  # probably false
    print(temp_profile_path)
    print(selenium_profile_path)
    filelist = dir_util.copy_tree(str(temp_profile_path), str(selenium_profile_path),
                                  preserve_symlinks=1, verbose=1, dry_run=0)
    print(filelist)
    return filelist


if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "Boddssuck" and "you're in the wrong directory")
    default_sport = "NBA"
    # TOOD: sport as cmdline arg

    # Set up the Firefox options and WebDriver
    #service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')  # Uncomment if you run in headless mode
    #driver = webdriver.Firefox(service=service, options=options)
    firefox_profile = FirefoxProfile(profile_directory=ProfilePath())
    options.profile = firefox_profile
    # firefox_profile.set_preference("javascript.enabled", False)
    print(firefox_profile)
    #print(options.profile.profile_dir)  # in older versions of selenium
    print(options.profile.path)

    # TODO: actually make it possible to persist changes to settings (especially ublock)
    # the problem is that FirefoxProfile ALWAYS copies the folder into a new temp profile (which is normally desirable)
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(6)
    #driver.get("https://www.pinnacle.com")  # need to navigate to domain first to set cookie
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "languageId=2&priceStyle=american&linesTypeView=a&device=d&languageGroup=all"})   # doesn't work
    #driver.add_cookie({"name": "UserPrefsCookie", "value": "priceStyle=american"})  # setting odds-format to american   # doesn't work
    #print(driver.get_cookie("UserPrefsCookie"))

    #InstallUblock()

    # TODO Handle instances where lines are closed or not posted, as the script exits upon encountering either of these things
    # This can happen despite other lines for the same player being correctly posted and accsessible, so our check for the URL redirect isnt going to work here

    with driver.context(driver.CONTEXT_CONTENT):
        links = FindGameLinks(driver, default_sport)
        print("\n\n")
        for link in links:
            VisitPage(link, driver, default_sport)

    print("about to quit")
    driver.quit()
