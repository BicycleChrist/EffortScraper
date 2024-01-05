from selenium import webdriver
from selenium.webdriver.firefox.options import Options
#from datetime import datetime
from time import sleep
import pathlib


def DownloadUBpage(leagueselect="nhl"):
    url = f"https://unabated.com/{leagueselect}/odds"
    driver.get(url)
    sleep(5)  # Wait for the JavaScript to execute (you may need to adjust the wait time)
    # TODO: better wait condition (find element consistently-present across pages to wait on)

    #page_source = driver.page_source
    #current_date = datetime.now().strftime("%Y%m%d")
    #filename = f"nhlodds_{current_date}.txt"
    savepath = cwd / "pagesource" / f"{leagueselect}.html"
    with open(savepath, 'w', encoding='utf-8') as file:
        file.write(driver.page_source)
    print(f"Results saved to: {savepath}")


# TODO: accept leagueselect as command-line argument
if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert (cwd.name == "Boddssuck" and "you're in the wrong directory")

    options = Options()
    options.add_argument("--headless")
    driver = webdriver.Firefox(options=options, keep_alive=True)
    driver.implicitly_wait(6)
    DownloadUBpage("nhl")
    driver.quit()
