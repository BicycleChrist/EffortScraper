import pathlib
import shutil  # because pathlib.rmdir() only works on empty directories, for some reason
import pandas
from io import StringIO  # for pandas.read_html
import pprint
from time import sleep  # TODO: replace sleeps with selenium expected-conditions

HAS_SELENIUM = True
if HAS_SELENIUM:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    #from selenium.webdriver.support import expected_conditions as EC
    #from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
    #from selenium.webdriver.support.select import Select
    from selenium.common.exceptions import *
    import selenium.webdriver.remote.webelement
    # action-stuff for rotating Pitch3D canvases
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.mouse_button import MouseButton


# TODO: use these
SELENIUMERRORS = [
    ElementClickInterceptedException, ElementNotInteractableException, ElementNotSelectableException, ElementNotVisibleException, ImeActivationFailedException, ImeNotAvailableException, InsecureCertificateException, InvalidArgumentException,
    InvalidCookieDomainException, InvalidCoordinatesException, InvalidElementStateException, InvalidSelectorException, InvalidSessionIdException, InvalidSwitchToTargetException, JavascriptException,
    MoveTargetOutOfBoundsException, NoAlertPresentException, NoSuchAttributeException, NoSuchCookieException, NoSuchDriverException, NoSuchElementException, NoSuchFrameException, NoSuchShadowRootException, NoSuchWindowException, Optional,
    ScreenshotException, Sequence, SessionNotCreatedException, StaleElementReferenceException, TimeoutException, UnableToSetCookieException, UnexpectedAlertPresentException, UnexpectedTagNameException, UnknownMethodException, WebDriverException,
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
        #sleep(1)
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


def GetSaveDir(purpose: str) -> pathlib.Path | None:
    accepted_types = [ "SCREENSHOT", "PNG", "HTML", "DATAFRAME", "CSV", "PITCH3D", ]
    purpose = purpose.upper()
    
    # lenient fallback-matching; in case the user writes the plural form instead
    if (purpose not in accepted_types) and purpose.endswith('S'):
        purpose = purpose.removesuffix('S')
    
    match purpose:  # already uppercased
        case "SCREENSHOT" | "PNG": bottom_folder_name = "selenium_screenshots"
        case "DATAFRAME" | "CSV":  bottom_folder_name = "dataframes"
        case "HTML":               bottom_folder_name = "html"
        case "PITCH3D":            bottom_folder_name = "pitch3d"
        case _:
            print(f"ERROR: no known save-directory for: {purpose}")
            print(f"accepted types: {accepted_types}\n")
            return None
    
    # TODO: ensure cwd is correct
    cwd = pathlib.Path.cwd()
    if cwd.name != "MLBAnalytics":
        print("ERROR: you're running this script from the wrong directory!")
        return None
    
    savedir = cwd / "savedfiles_BBSavant" / bottom_folder_name
    if not savedir.exists(): savedir.mkdir(parents=True, exist_ok=True)
    elif not savedir.is_dir():
        print(f"ERROR: {savedir} exists but is not a directory!")
        return None
    
    return savedir


# TODO: add an option for 'ALL' to 'purposes'
def ClearSaveFolders(*purposes:str, simulate=False):
    cwd = pathlib.Path.cwd()  # basically only used for the deletion messages, and the cwd-check
    # can't rely on cwd being defined in main; this function would then fail when imported elsewhere
    # TODO: ensure cwd is really correct
    if cwd.name != "MLBAnalytics":  # TODO: better / more resilient handling
        print("ERROR: you're running this script from the wrong directory!")
        return False
    
    if len(purposes) == 0:
        print("WARNING: ClearSaveFolders() was called with zero targets (no effect)\n")
        return True  # should be valid to call without targets
    
    announcement = f"Clearing save folders for: {purposes}"
    if simulate: announcement = "[[SIMULATION]] " + announcement
    print(f"\n\n {announcement} \n\n")
    
    # this gets set to false to indicate that at least one input was invalid
    good_input_flag: bool = True
    for purpose in purposes:
        savedir = GetSaveDir(purpose)  # GetSaveDir() also (re-)creates the target dir when called
        if savedir is None:
            good_input_flag = False
            continue
        if simulate:
            print(f"(simulating) wiping ./{savedir.relative_to(cwd)}/ ...")
            # TODO: list files that would be deleted
        elif simulate == False:
            print(f"wiping ./{savedir.relative_to(cwd)}/ ...")
            shutil.rmtree(savedir)  # savedir should always exist here because it's recreated by GetSaveDir()
            savedir.mkdir()
    
    if not good_input_flag: print("input to ClearSaveFolders was bad.\n")
    return good_input_flag


def ScreenshotElement(element: selenium.webdriver.remote.webelement.WebElement, name:str, purpose="SCREENSHOT"):
    screenshot_dir = GetSaveDir(purpose)
    screenshot_filepath = screenshot_dir / f"{name}.png"
    print(f"writing {screenshot_filepath.name}...", end=" ")
    screenshot_filepath.touch()
    with open(screenshot_filepath, mode='wb') as screenshot_file:
        screenshot_file.write(element.screenshot_as_png)
    print(f"wrote {screenshot_filepath.name}")
    return


def SaveElementHTML(element: selenium.webdriver.remote.webelement.WebElement, name:str):
    html_savedir = GetSaveDir("HTML")
    assert html_savedir is not None, "HTML save-directory should exist"
    # 'outerHTML' includes the element div itself; 'innerHTML' does not
    element_html = element.get_attribute('outerHTML')
    filename = f"{name}.html"
    save_filepath = html_savedir / filename
    print(f"writing: {filename} ...")
    return_code = save_filepath.write_text(element_html, encoding="utf-8")  # what does the 'newline' arg do?
    #print(f"{filename} saved: return_code = {return_code}")
    return


def CollectPitch3D(driver: webdriver, container: selenium.webdriver.remote.webelement.WebElement):
    nav_buttons = container.find_element(By.ID, "nav-buttons")
    #real_buttons = nav_buttons.find_elements(By.XPATH, "./div")
    pitch3d_button = nav_buttons.find_elements(By.XPATH, "./div")[6]
    pitch3d_button.click()
    canvas = container.find_element(By.XPATH, ".//div[@class='pitch3d']/div[@class='App']//canvas")
    print(canvas.location_once_scrolled_into_view)
    
    iterations = 600
    iteration = 0
    while iteration < iterations:
        # must redefine on every iteration; otherwise it simply will not work
        RotateCanvas = ActionChains(driver, duration=0)\
        .move_to_element_with_offset(canvas, 516, 150)\
        .click_and_hold()\
        .move_by_offset(-16, 0)\
        .release()
        
        RotateCanvas.perform()
        # number needs to be padded to ensure correct ordering of images when fed to ffmpeg
        ScreenshotElement(canvas, f"canvas_{str(iteration).zfill(3)}", "PITCH3D")
        iteration += 1
    
    print("Pitch3D done")
    return


if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "MLBAnalytics"), "you're in the wrong directory"  # TODO: refactor 'cwd' checks
    
    # enables saving for different elements / formats
    #ENABLED_FILE_SAVES = ("SCREENSHOTS", "DATAFRAMES", "HTML")
    ENABLED_FILE_SAVES = ("PITCH3D",)
    # valid options: "SCREENSHOTS", "DATAFRAMES", "HTML"
    # Plural forms are optional ("SCREENSHOT" (instead of "SCREENSHOTS") will work fine)
    # uppercase is also optional ("screenshot" is fine)
    
    # TODO: refactor this into the Save functions instead?
    def isSaveEnabledFor(savetype:str)->bool:
        savetype = savetype.upper()
        UPPER_ENABLED = [S.upper() for S in ENABLED_FILE_SAVES]
        PLURAL_ENABLED = [S+'S' for S in UPPER_ENABLED if not S.endswith('S')]
        NONPLURAL_ENABLED = [S.removesuffix('S') for S in UPPER_ENABLED if S.endswith('S')]
        return ( savetype in UPPER_ENABLED  or 
                 savetype in PLURAL_ENABLED or
                 savetype in NONPLURAL_ENABLED )
    
    # wipe (and recreate) any enabled save-folders on startup
    return_flag = ClearSaveFolders(*ENABLED_FILE_SAVES)
    if not return_flag: exit(3)  # ragequit if we passed an unrecognized string into 'purposes'
    
    if not HAS_SELENIUM:
        print("early exit because you don't have selenium (HAS_SELENIUM = False)")
        exit(0)
    
    # Set up the Firefox options and WebDriver
    #service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')
    options.add_argument("--window-size=1920x1080")
    options.page_load_strategy = 'eager'
    #options.page_load_strategy = 'none'  # maybe required for window.stop? 
    # the 'window.stop' script seems to work on 'eager' as well? hard to tell.
    
    firefox_profile = FirefoxProfile()
    options.profile = firefox_profile
    print(firefox_profile)
    print(options.profile.path)
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(3)
    
    driver.set_window_size(1920, 1280)
    driver.maximize_window()
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
    #driver.execute_script(script)
    
    try:
        CollectPitch3D(driver, containers[0])
    except NoSuchElementException:
        print("Not real")
    except Exception as E:
        if E not in SELENIUMERRORS:
            print(f"real exception: {E}; closing driver\n")
            driver.quit()
            raise E
        print(f"SeleniumException: {E}\n")
    
    driver.quit()
    exit(0)
    
    # finding tables
    Tables = []
    for index, container in enumerate(containers):
        new_tables = Find_Tables(container)
        if new_tables is None: continue
        Tables.extend(new_tables)
        container_name = f"container_{index}"
        if isSaveEnabledFor("SCREENSHOTS"): ScreenshotElement(container, container_name)
        if isSaveEnabledFor("HTML"): SaveElementHTML(container, container_name)
        # print(index)
    
    
    # converting tables to Pandas Dataframe
    dataframes = []
    for index, table in enumerate(Tables):
        print(table.tag_name)
        table_name = f"table_{index}"
        if isSaveEnabledFor("SCREENSHOTS"): ScreenshotElement(table, table_name)
        if isSaveEnabledFor("HTML"): SaveElementHTML(table, table_name)
        # maybe SaveElementHTML should return the HTML?
        htmlski = table.get_attribute('outerHTML')  # includes the <table> tags; innerHTML does not
        try:  # TODO: this try-block can probably be removed; it was necessary only because pandas couldn't read (most of) the tables
            # read_html returns a list of dataframes
            new_dataframes = pandas.read_html(StringIO(htmlski), encoding="utf-8")  # don't use 'links="all"'
            dataframes.extend(new_dataframes)
        except Exception as bullshit:
            print(f"bullshit: {bullshit}")
        print('\n')
    
    
    if isSaveEnabledFor("DATAFRAMES"):
        # TODO: save-function for dataframes? choice of output file-types?
        dataframe_savedir = GetSaveDir("DATAFRAME")
        if dataframe_savedir is None: exit(2)
        
        print(f"\nfound: {len(dataframes)} dataframes\n")
        for index, dataframe in enumerate(dataframes):
            pprint.pprint(dataframe.to_dict, indent=2)
            df_filepath = dataframe_savedir / f"dataframe_{index}.csv"
            print(f"writing {df_filepath.name}")
            dataframe.to_csv(df_filepath, mode='w', encoding="utf-8", index=True)
            print('\n')
        
    else:  # if saving dataframes is NOT enabled
        pprint.pprint(dataframes, indent=2)
    
    print("about to quit")
    driver.quit()
