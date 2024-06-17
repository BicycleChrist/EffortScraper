from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import *
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep
import pprint
import pathlib
import cProfile
import pstats



def TryToFind(element, xpath_string):
    try:
        x = element.find_element(By.XPATH, xpath_string)
        return x
    except Exception as SeleniumBullshit:
        print(f"error trying to find: {xpath_string}")
        print(f"from: {element}")
        print(SeleniumBullshit)
    return element


def FindGameLinks(driver: webdriver.Firefox, url):
    all_game_links = []
    try:
        sp_path_event = driver.find_element(By.XPATH, "/html/body//div[@class='top-container']//main/div[@class='sp-main-area']//sp-path-event")
        sp_next_events = driver.find_elements(By.XPATH, ".//div[@class='sp-next-events']")  
        grouped_events = sp_path_event.find_elements(By.XPATH, ".//div[@class='grouped-events']")
    except NoSuchElementException:
        print(f"No grouped events found on the page: {url}")
        return []
    
    events = []
    for group in grouped_events:
        events.extend(group.find_elements(By.XPATH, ".//sp-coupon//a[@class='game-view-cta']"))
    
    game_links = [event.get_attribute('href') for event in events]
    print(f"\n game_links for {url}: {game_links} \n")
    all_game_links.extend(game_links)
    return all_game_links



def VisitGameLink(driver: webdriver.Firefox, game_link):
    print(f"\nnavigating to: {game_link}")
    driver.get(game_link)
    script = driver.pin_script("window.stop();")
    
    main_area = driver.find_element(By.CLASS_NAME, 'sp-main-area')
    #print(f"found main area")
    event = main_area.find_element(By.XPATH, './/sp-event')
    #print(f"found event")
    
    event_coupons = event.find_elements(By.TAG_NAME, 'sp-coupon')
    #print(f"found event coupons")
    
    #print("freezing window")
    driver.execute_script(script)
    driver.implicitly_wait(0)  # before calling any recursive search
    
    try:
        article_lists = [coupon.find_elements(By.TAG_NAME, "article") for coupon in event_coupons]
        intermediate = [a for a in article_lists if len(a) > 0]
        article_lists = intermediate
        print(f"found {len(article_lists)} article_lists")
    except StaleElementReferenceException as stale_exception:
        print(f"selenium is tarding out: {stale_exception}")
        return []
    
    results = []
    for articlelist in article_lists:
        for article in articlelist:
            new_dict = {}
            for thing in ('header', 'section'):
                new_dict[thing] = article.find_element(By.TAG_NAME, thing).text.splitlines()
            results.append(new_dict)
    
    return results


def PrintProfileStats():
    profile_dir = pathlib.Path.cwd() / "profiling"
    if not profile_dir.exists():
        print("profile dir not found")
        return
    dumpfiles = profile_dir.glob("*.dump")
    profile_files = [name for name in profile_dir.glob("*") if name not in dumpfiles]
    print("\n\nSTATS\n\n")
    for filename in profile_files:
        print(f"\n\n{filename}\n\n")
        stats = pstats.Stats(str(filename)).strip_dirs().sort_stats('cumulative', 'calls')
        stats.dump_stats(f"{filename}.dump")
        stats.sort_stats('cumulative', 'time').print_stats(25)
    return


def VisitGameLinkWithDriver(game_link):
    options = FirefoxOptions()
    options.add_argument('--headless')
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(1)
    results = []
    try:
        results = VisitGameLink(driver, game_link)
    except Exception as e:
        print(f"Exception occurred while visiting {game_link}: {e}")
        driver.quit()
        return (game_link, [])
    finally:
        driver.quit()
    return (game_link, results)


def Main():
    DOPROFILING = False
    options = FirefoxOptions()
    options.add_argument('--headless')
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(1)
    
    urls = [
        'https://www.bovada.lv/sports/baseball/mlb',
        'https://www.bovada.lv/sports/hockey/nhl',
        'https://www.bovada.lv/sports/basketball/nba'
    ]
    tab_handles = []
    for url in urls:
        driver.switch_to.new_window('tab')
        driver.get(url)
        tab_handles.append(driver.current_window_handle)
    
    game_links = []
    for tab in tab_handles:
        driver.switch_to.window(tab)
        #print(driver.current_url)
        found_links = FindGameLinks(driver, driver.current_url)
        game_links.extend(found_links)
    
    scraped_data = {}
    # max_workers=None : make as many threads as cores
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = [executor.submit(VisitGameLinkWithDriver, game_link) for game_link in game_links]
        for future in as_completed(futures):
            try:
                url, data = future.result()
            except Exception as e:
                print(f"Exception occurred for {url}: {e}")
                continue
            scraped_data[url] = data
    
    print("\n\n scraping finished \n\n")
    return scraped_data


def PrintScrapedData(scraped_data):
    for url, data in scraped_data.items():
        print(f"number of articles found for {url}: {len(data)}")
        for data_dict in data:
            header = data_dict['header'][0]
            section = data_dict['section']
            if header in section:
                section.remove(header)
            if (len(section) % 2) == 0:
                section = list(zip(section[0::2], section[1::2]))
            else:
                print("odd number of lines in data; not zipping")
            print(header)
            pprint.pprint(section, indent=2)
            print('\n')
    
    print("stats: ")
    print(f"number of pages scraped: {len(scraped_data)}")
    total_linecount = 0
    for url, data in scraped_data.items():
        print(url)
        print(f"number of lines in markets: {len(data)}")
        page_line_total = 0
        for data_dict in data:
            print(data_dict['header'])
            lines = len(data_dict['section'])
            total_linecount += lines
            page_line_total += lines
        print(f"number of lines scraped on page: {page_line_total}\n")

    print(f"\n\n total_linecount number of lines scraped across all pages: {total_linecount}")
    

if __name__ == "__main__":
    results = Main()
    PrintScrapedData(results)
    print("exiting")
    
