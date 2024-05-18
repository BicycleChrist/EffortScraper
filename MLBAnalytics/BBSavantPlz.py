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
import shutil  # because rmdir only works if dir is empty, for some reason
import selenium.webdriver.remote.webelement


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


# TODO: "Open All button on top of page"
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
    # this is the right-side table
    savant_table = active_div.find_element(By.CLASS_NAME, "table-savant").find_element(By.TAG_NAME, 'table')
    tables.append(savant_table)
    return tables

def GetMainContent(driver):
    main_content = TryToFind(driver, "/html/body/div[@class='article-template']/div[@id='games']")
    return main_content

def GetScreenshotDir()->pathlib.Path:
    cwd = pathlib.Path.cwd()
    screenshot_dir = cwd / "selenium_screenshots"
    if not screenshot_dir.exists(): screenshot_dir.mkdir()
    return screenshot_dir

# looks like screenshots will capture the parent element? for most tables
def ScreenshotElement(element: selenium.webdriver.remote.webelement.WebElement, name):
    screenshot_dir = GetScreenshotDir()
    screenshot_filepath = screenshot_dir / f"{name}.png"
    print(f"writing {screenshot_filepath.name}...")
    screenshot_filepath.touch()
    with open(screenshot_filepath, mode='wb') as screenshot_file:
        screenshot_file.write(container.screenshot_as_png)
    print(f"wrote {screenshot_filepath.name}")
    return

def DeleteScreenshotFolder():
    screenshot_dir = GetScreenshotDir()
    print(f"deleting {screenshot_dir}")
    shutil.rmtree(screenshot_dir)
    screenshot_dir.mkdir()
    return

def GetDataframeDir()->pathlib.Path:
    cwd = pathlib.Path.cwd()
    dataframe_dir = cwd / "dataframes"
    if not dataframe_dir.exists(): dataframe_dir.mkdir()
    return dataframe_dir


if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "MLBAnalytics" and "you're in the wrong directory")

    DeleteScreenshotFolder()
    
    # Set up the Firefox options and WebDriver
    #service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')
    options.page_load_strategy = 'eager'
    #options.page_load_strategy = 'none'  # maybe required for window.stop?
    
    firefox_profile = FirefoxProfile()
    options.profile = firefox_profile
    print(firefox_profile)
    print(options.profile.path)
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(5)
    
    # Javascript that's supposed to freeze the page so that elements don't get invalidated by (JS) updates
    script = driver.pin_script("window.stop();")
    #script = driver.pin_script("return window.stop")
    #driver.command_executor.execute()
    
    url = 'https://baseballsavant.mlb.com/gamefeed'
    driver.get(url)
    main_content = GetMainContent(driver)
    game_data = TryToFind(main_content, "./div[@id='game-data']")
    # 'charts' in the portion of the page that contains the game entries with "Click to Open"
    #charts = TryToFind(game_data, "./div[@id=charts]")
    charts = TryToFind(driver, "/html/body/div[@class='article-template']/div[@id='games']/div[@id='game-data']/div[@id='charts']")
    containers = ExpandAll(charts)
    
    # attempt to freeze the page here
    driver.execute_script(script)
    
    Tables = []
    for index, container in enumerate(containers):
        new_tables = Find_Tables(container)
        if new_tables is None: continue
        Tables.extend(new_tables)
        ScreenshotElement(container, f"container_{index}")
    
    # converting tables to Pandas Dataframe
    dataframes = []
    for index, table in enumerate(Tables):
        print(table.tag_name)
        #ScreenshotElement(table, f"table_{index}")  # screenshots parent element?
        htmlski = table.get_attribute('outerHTML')  # includes the <table> tags; innerHTML does not
        try:
            # read_html returns a list of dataframes
            new_dataframes = pandas.read_html(StringIO(htmlski), encoding="utf-8")  # don't use 'links="all"'
            dataframes.extend(new_dataframes)
        except Exception as bullshit:
            print(f"bullshit: {bullshit}")
        print('\n')
    
    dataframe_dir = GetDataframeDir()
    print(f"\nfound: {len(dataframes)} dataframes\n")
    for index, dataframe in enumerate(dataframes):
        pprint.pprint(dataframe.to_dict, indent=2)
        df_filepath = dataframe_dir / f"dataframe_{index}.csv"
        print(f"writing {df_filepath.name}")
        dataframe.to_csv(df_filepath, mode='w', encoding="utf-8", index=True)
        print('\n')
    
    print("about to quit")
    driver.quit()
