from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support.select import Select
from time import sleep
from contextlib import contextmanager   # is this a part of selenium?
import pathlib
from distutils import dir_util  # copying folders


#TODO Literally all of it, if PaulC can do it so can we.


def DoTheThing(driver):
    driver.get("https://www.pinnacle.com/en/basketball/nba/matchups")
    contentBlocks = WebDriverWait(driver, 10).until(
        lambda x: [c for c in x.find_elements(By.CLASS_NAME, "contentBlock")
                   if c.get_attribute("data-test-id") == 'LiveContainer']
    )
    for block in contentBlocks:
        print(f"{block.get_attribute('class'), block.get_attribute('data-test-id')}")
        if block.get_attribute("data-test-id") == 'LiveContainer':  # redundant
            rows = block.find_elements(By.CLASS_NAME, 'event-row-participant')  # text for each team name
            things = block.find_elements(By.XPATH, "//a[@class='']")
            links = [thing.get_attribute("href") for thing in things if thing.get_attribute("href")]
            gamelinks = [link for link in links if link.startswith('https://www.pinnacle.com/en/basketball/nba/')]
            print(rows)
            #print(links)
            print(gamelinks)
            return gamelinks


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
    temp_profile_path = new_firefox_profile.profile_dir  # these folders only have a 'user.js' in them????
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

    # Set up the Firefox options and WebDriver
    #service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('--headless')  # Uncomment if you run in headless mode
    #driver = webdriver.Firefox(service=service, options=options)
    firefox_profile = FirefoxProfile(profile_directory=ProfilePath())
    options.profile = firefox_profile
    # firefox_profile.set_preference("javascript.enabled", False)
    print(firefox_profile)
    print(options.profile.profile_dir)

    # TODO: actually make it possible to persist changes to settings (especially ublock)
    # the problem is that FirefoxProfile ALWAYS copies the folder into a new temp profile (which is normally desirable)
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(6)

    #InstallUblock()
    with driver.context(driver.CONTEXT_CONTENT):  # not a typo
        DoTheThing(driver)

    print("about to quit")
    driver.quit()
