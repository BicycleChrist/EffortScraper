from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import *
from time import sleep  # TODO: replace sleeps with selenium expected-conditions
import pprint
# profiling
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


def FindGameLinks(driver: webdriver.Firefox):
    #url = 'https://www.bovada.lv/sports/politics'
    url = 'https://www.bovada.lv/sports/baseball/mlb'
    driver.get(url)
    sleep(1)
    try:
        sp_path_event = driver.find_element(By.XPATH, "/html/body//div[@class='top-container']//main/div[@class='sp-main-area']//sp-path-event")
        grouped_events = sp_path_event.find_elements(By.XPATH, ".//div[@class='grouped-events']")
    except NoSuchElementException:
        print("No grouped events found on the page.")
        return []

    events = []
    for group in grouped_events:
        events.extend(group.find_elements(By.XPATH, ".//sp-coupon//a[@class='game-view-cta']"))

    game_links = [event.get_attribute('href') for event in events]
    print(f"\n game_links: {game_links} \n")
    return game_links


def VisitGameLink(driver:webdriver.Firefox, game_link):
    print(f"\nnavigating to: {game_link}")
    driver.get(game_link)
    # don't ever sleep here; you have to stop the JS immediately (otherwise stale elements)
    
    # this script overcomes the JS (otherwise stale elements)
    script = driver.pin_script("window.stop();")
    
    main_area = driver.find_element(By.CLASS_NAME, 'sp-main-area') 
    #print(f"found main area {main_area}")
    print(f"found main area")
    event = main_area.find_element(By.XPATH, './/sp-event')
    #print(f"found event {event}")
    print(f"found event")
    
    event_coupons = event.find_elements(By.TAG_NAME, 'sp-coupon')  
    #print(f"found event coupons {event_coupons}")
    print(f"found event coupons")
    
    # attempt to freeze the page here to prevent stale references
    # any earlier than this will prevent the window from loading entirely (implicit wait doesn't trigger until sp-coupon)
    print("freezing window")
    driver.execute_script(script)
    
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
            #print(new_dict)

    #print(results)
    return results


# For futures markets,(I.E politics and league championship winners), odds are contained on the initial page
# In the event that no game links are found for the given url, we'll parse the current page
def ScrapeCurrentPage(driver:webdriver.Firefox):
    print("no game links found, scraping current page")
    main_area = driver.find_element(By.CLASS_NAME, 'sp-main-area')
    print("found main area")
    event = main_area.find_element(By.CLASS_NAME, 'max-container')
    print("found max-container")

    event_coupons = event.find_elements(By.TAG_NAME, 'sp-coupon')
    print("found event coupons")
    article_lists = [coupon.find_elements(By.TAG_NAME, "article") for coupon in event_coupons]
    intermediate = [a for a in article_lists if len(a) > 0]
    article_lists = intermediate
    print(article_lists)

    results = []
    for articlelist in article_lists:
        for article in articlelist:
            new_dict = {}
            for thing in ('header', 'section'):
                new_dict[thing] = article.find_element(By.TAG_NAME, thing).text.splitlines()
            results.append(new_dict)
            #print(new_dict)

    #print(results)
    return results


import pathlib
def PrintProfileStats():
    profile_dir = pathlib.Path.cwd()/ "profiling"
    if not profile_dir.exists(): print("profile dir not found"); return
    dumpfiles = profile_dir.glob("*.dump")  # these are the ones we DON'T want?
    profile_files = [name for name in profile_dir.glob("*") if name not in dumpfiles]
    print("\n\nSTATS\n\n")
    for filename in profile_files:
        print(f"\n\n{filename}\n\n")
        stats = pstats.Stats(str(filename)).strip_dirs().sort_stats('cumulative', 'calls')
        stats.dump_stats(f"{filename}.dump")
        stats.sort_stats('cumulative', 'time').print_stats(25)  # only prints top 25 (usually all others are not important)
    return


if __name__ == "__main__":
    DOPROFILING = False
    options = FirefoxOptions()
    #options.page_load_strategy = 'eager'  # breaks everything
    options.add_argument('--headless')
    #firefox_profile = FirefoxProfile()  # not necessary
    #options.profile = firefox_profile
    #print(firefox_profile)
    #print(options.profile.path)
    
    driver = webdriver.Firefox(options=options)
    driver.implicitly_wait(1)
    # the implicit wait seems bugged here; increasing it significantly increases the time it takes to scrape the page for no benefit
    
    # Javascript that's supposed to freeze the page so that elements don't get invalidated by (JS) updates
    #script = driver.pin_script("window.stop();")
    #script = driver.pin_script("return window.stop")
    #driver.command_executor.execute()
    # this script was moved into the VisitGameLink function
    
    print("finding game links")
    game_links = FindGameLinks(driver)
    #driver.get('https://www.bovada.lv/sports/politics')
    #game_links = ['https://www.bovada.lv/sports/baseball/mlb/san-diego-padres-cincinnati-reds-202405231310']

    profiling_stats = []
    index = 0
    scraped_data = {}
    for game_link in game_links:
        try:
            results = []
            profile_filename = f"profiling/bigbov_stats{index}"
            index += 1
            if DOPROFILING:
                cProfile.runctx("results = VisitGameLink(driver, game_link)", globals(), locals(), profile_filename)
                profiling_stats.append((profile_filename, pstats.Stats(profile_filename).strip_dirs().sort_stats('cumulative', 'calls')))
                # https://docs.python.org/3.12/library/profile.html#pstats.Stats.sort_stats
            else:
                results = VisitGameLink(driver, game_link)
            scraped_data[game_link] = results
            print(f"#markets found: {len(results)}")
        except StaleElementReferenceException as stale_exception:
            print(f"selenium is tarding out: {stale_exception}")
            continue

    # printing scraped data
    print("\n\n scraping finished \n\n")
    for url, data in scraped_data.items():
        print(url)
        for data_dict in data:
            header = data_dict['header'][0]
            section = data_dict['section']
            if header in section: section.remove(header)  # sometimes the header is the first entry in the data
            # if there's an even number of lines, pair them
            if (len(section) % 2) == 0: 
                section = list(zip(section[0::2], section[1::2]))
            else: print("odd number of lines in data; not zipping")
            print(header)
            pprint.pprint(section, indent=2)
            print('\n')
    
    if DOPROFILING:
        PrintProfileStats()
    
    print("done")
    
    # printing stats about run
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

    driver.quit()
