from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
#from selenium.webdriver.support import expected_conditions as EC
#from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
#from selenium.webdriver.support.select import Select
from selenium.common.exceptions import *
from time import sleep
import pathlib
import pprint
import pandas
from io import StringIO  # for pandas.read_html


SELENIUMERRORS = [
    'ElementClickInterceptedException', 'ElementNotInteractableException', 'ElementNotSelectableException', 'ElementNotVisibleException', 'ImeActivationFailedException', 'ImeNotAvailableException', 'InsecureCertificateException', 'InvalidArgumentException',
    'InvalidCookieDomainException', 'InvalidCoordinatesException', 'InvalidElementStateException', 'InvalidSelectorException', 'InvalidSessionIdException', 'InvalidSwitchToTargetException', 'JavascriptException',
    'MoveTargetOutOfBoundsException', 'NoAlertPresentException', 'NoSuchAttributeException', 'NoSuchCookieException', 'NoSuchDriverException', 'NoSuchElementException', 'NoSuchFrameException', 'NoSuchShadowRootException', 'NoSuchWindowException', 'Optional',
    'ScreenshotException', 'Sequence', 'SessionNotCreatedException', 'StaleElementReferenceException', 'TimeoutException', 'UnableToSetCookieException', 'UnexpectedAlertPresentException', 'UnexpectedTagNameException', 'UnknownMethodException', 'WebDriverException',
    #'SUPPORT_MSG', 'ERROR_URL',    # not sure if these are actual error types
]


def TryToFind(element, xpath_string):
    try:
        x = element.find_element(By.XPATH, xpath_string)
        return x
    except Exception as SeleniumBullshit:
        print(f"error trying to find: {xpath_string}")
        print(f"from: {element}")
        print(SeleniumBullshit)
    return element


# clicks on 'Click to Open' for each matchup under 'charts'
def ExpandAll(contents):
    matchups = contents.find_elements(By.XPATH, './div')
    new_containers = []
    for matchup in matchups:
        meta_container = TryToFind(matchup, "./div[@class='game-meta-container']")
        divs = meta_container.find_elements(By.XPATH, "./div")
        target_div = divs[2]
        # somehow this doesn't find the right one, and putting more than one class throws an exception
        #container_open = target_div.find_element(By.XPATH, "./span[contains(@class, container-open)]")
        # somehow this finds all of them even though only the last span has this class
        spans = target_div.find_elements(By.XPATH, "./span[contains(@class, container-open)]")
        for index, span in enumerate(spans):
             print(span.get_attribute("class"))
             print(index, span.text)
        # this could be more robust, but whatever, it should be the last one
        click_to_open = spans[-1]
        print(click_to_open.text)
        if click_to_open.text == "Click to Open ↓":
            print("click")
            click_to_open.click()
        sleep(1)
        # now that the thing is opened, we backtrack up the DOM to get the container for the info
        new_container = TryToFind(matchup, "./div[@class='game-view-container']")
        if new_container != matchup:  # if it failed to find the div, it would return matchup
            new_containers.append(new_container)
    return new_containers


def Find_Tables(container: webdriver.remote.webelement.WebElement):
    # TODO: select nav_buttons to click
    nav_buttons = container.find_element(By.ID, "nav-buttons")
    subdivs = container.find_elements(By.XPATH, "./div")
    active_divs = [div for div in subdivs if "display: none;" not in div.get_attribute("style")]
    for div in active_divs:
        print(div.get_attribute("id"))
    # filter out the nav-buttons
    active_divs = [div for div in active_divs if not 'nav-' in div.get_attribute('id')]
    if len(active_divs) > 1:
        print("should only have one active div")
        return None
    if len(active_divs) == 0:
        print("no active div")  # matchup hasn't started
        return None
    active_div = active_divs[0]
    tables = active_div.find_elements(By.XPATH, './/table')
    return tables

def GetMainContent(driver):
    main_content = TryToFind(driver, "/html/body/div[@class='article-template']/div[@id='games']")
    return main_content


if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "MLBAnalytics" and "you're in the wrong directory")
    
    # Set up the Firefox options and WebDriver
    #service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')  # Uncomment if you run in headless mode
    firefox_profile = FirefoxProfile()
    options.profile = firefox_profile
    print(firefox_profile)
    print(options.profile.path)
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(6)
    
    url = 'https://baseballsavant.mlb.com/gamefeed'
    driver.get(url)
    main_content = GetMainContent(driver)
    game_data = TryToFind(main_content, "./div[@id='game-data']")
    # 'charts' in the portion of the page that contains the game entries with "Click to Open"
    #charts = TryToFind(game_data, "./div[@id=charts]")
    charts = TryToFind(driver, "/html/body/div[@class='article-template']/div[@id='games']/div[@id='game-data']/div[@id='charts']")
    containers = ExpandAll(charts)
    Tables = []
    for container in containers:
        new_tables = Find_Tables(container)
        if new_tables is None: continue
        Tables.extend(new_tables)
    
    # converting tables to Pandas Dataframe
    dataframes = []
    for table in Tables:
        print(table.tag_name)
        htmlski = table.get_attribute('innerHTML')
        try:
            new_dataframe = pandas.read_html(StringIO(htmlski))
            dataframes.append(new_dataframe)
        except Exception as bullshit:
            print(f"bullshit: {bullshit}")
    print(len(dataframes))
    print(dataframes)
    print("about to quit")
    driver.quit()
