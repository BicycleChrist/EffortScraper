from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.select import Select
#from datetime import datetime
from time import sleep
import pathlib

from LeagueMap import *
TEAMLIST = leaguemap[DEFAULT_LEAGUE_SELECT]

from testwoopty import GetSavePath


def DownloadUBpage(leagueselect, oddsformat="ALL"):
    wantedsubpages = []  # selections we want to go through in the odds-format dropdown
    if oddsformat == "ALL":
        wantedsubpages = [*OddsFormat.keys()]
    elif oddsformat in OddsFormat.keys():
        wantedsubpages.append(oddsformat)
    elif oddsformat not in OddsFormat.keys():
        print(f"invalid selection for odds-format: {oddsformat}")
        print(f"valid options are: {(OddsFormat.keys())}")
        return

    print(f"getting pages for {leagueselect}...")
    url = f"https://unabated.com/{leagueselect.lower()}/odds"   # url must be lowercase
    driver.get(url)
    sleep(5)  # Wait for the JavaScript to execute (you may need to adjust the wait time)
    # TODO: better wait condition (find element consistently-present across pages to wait on)

    # find dropdown
    parents = driver.find_elements(By.XPATH, "//div[@class='mr-2 dropdown']")
    parent = parents[1]
    print(parent.get_attribute("class"))
    #print(parent.text)
    #dropdowns = driver.find_elements(By.XPATH, "//div[@class='mr-2 dropdown-menu']")
    #dropdown = dropdowns[1]

    for subthing in wantedsubpages:
        print(f"downloading {oddsformat}...")
        parent.click()  # need to click this before getting items; DOM gets updated and references invalidated
        dropdown_items = parent.find_elements(By.CLASS_NAME, "dropdown-item")
        for item in dropdown_items:
            if item.text == subthing:
                item.click()
                sleep(3)  # wait for new format to load  # TODO: use selenium's 'wait-until' thing
                break
        savepath = GetSavePath(leagueselect, "source", subthing)
        if savepath is None:
            print("invalid savepath; skipping")
            continue
        if not savepath.parent.exists():
            savepath.parent.mkdir(parents=True, exist_ok=True)
        with open(savepath, 'w', encoding='utf-8') as file:
            file.write(driver.page_source)
        print(f"Results saved to: {savepath}")
    print("done")


# TODO: accept leagueselect as command-line argument
# TODO: accept Oddsformat as a command-line argument
if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "Boddssuck" and "you're in the wrong directory")

    # Set up the Firefox options and WebDriver
    service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')  # Uncomment if you run in headless mode
    options.add_argument("--window-size=5760x3240")
    driver = webdriver.Firefox(service=service, options=options, keep_alive=True)
    driver.implicitly_wait(6)
    driver.set_window_size(5760, 3240)  # forces the whole table to load (no horizontal scrolling)
    DownloadUBpage(DEFAULT_LEAGUE_SELECT, "ALL")
    driver.quit()
