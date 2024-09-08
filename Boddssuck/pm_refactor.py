from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException
import time
from urllib.parse import unquote
from tabulate import tabulate
import logging
import csv
import requests
from requests.exceptions import ConnectionError, HTTPError
from pprint import pprint
from PolymarketParsing import ParseHrefs
import pmsuck

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def scrape_single_url(url):
    driver = pmsuck.initialize_driver()
    try:
        result = scrape_market_data(driver, url)
        time.sleep(0.21)
        return url, result
    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        #failed_links.append(url)
        return url, None
    finally:
        driver.quit()

def filter_unique_urls(urls):
    base_urls = set()
    for url in urls:
        base_url = url.split('/event/')[0] + '/event/' + url.split('/event/')[1].split('/')[0]
        base_urls.add(base_url)
    return list(base_urls)

def scrape_market_data(driver, url):
    try:
        driver.get(url)

        # Wait for the page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.c-dhzjXW"))
        )

        # Check if it's a multi-outcome or single-outcome market
        is_multi_outcome = len(driver.find_elements(By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']")) > 0

        if is_multi_outcome:
            return scrape_multi_outcome_market(driver, url)  # Pass the url argument here
        else:
            return scrape_single_outcome_market(driver, url)

    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        return []
        #return None

def scrape_multi_outcome_market(driver, url) -> list[dict]|None:
    driver.get(url)
    try:
        # Wait for the page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.c-dhzjXW"))
        )
        WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']"))
        )
        market_items = driver.find_elements(By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']")
        market_data = []
        for index, item in enumerate(market_items):
            outcome = {'url': url}
            outcome['name'] = item.find_element(By.CSS_SELECTOR, "p.c-cZBbTr").text
            outcome['bet_amount'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-ihVLOVM-css").text
            outcome['probability'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-icEtPXM-css").text
            yes_selector = "div.c-gBrBnR-ifgGdkS-css" if index == 0 else "div.c-gBrBnR-ibMWmgq-css"
            outcome['yes_price'] = item.find_element(By.CSS_SELECTOR, yes_selector).text.split()[-1]
            outcome['no_price'] = item.find_element(By.CSS_SELECTOR, "div.c-gBrBnR-ikGeufU-css").text.split()[-1]
            market_data.append(outcome)
        logger.info(f"Successfully scraped {len(market_data)} outcomes from multi-outcome market")
        return market_data
    except Exception as e:
        logger.error(f"Error scraping multi-outcome market: {str(e)}")
        return None
        #return {'url': url, 'error': str(e)}

def scrape_single_outcome_market(driver, url) -> list[dict]|None:
    driver.get(url)
    try:
        WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.c-dhzjXW-iqUfiq-css p.c-dqzIym"))
        )
        outcome = {'url': url}
        outcome['name'] = driver.find_element(By.CSS_SELECTOR, "div.c-dhzjXW-iqUfiq-css p.c-dqzIym").text
        outcome['bet_amount'] = driver.find_element(By.CSS_SELECTOR, "div.c-dhzjXW-ijroWjL-css p.c-dqzIym-iUwoCw-css").text
        prob_gain_elem = driver.find_element(By.CSS_SELECTOR, "p.c-dqzIym-ibHwMTN-css")
        outcome['probability'] = prob_gain_elem.text.split('chance')[0].strip()
        gain_loss_span = prob_gain_elem.find_element(By.CSS_SELECTOR, "span.c-PJLV-ijwODCF-css, span.c-PJLV-iejnpxd-css")
        outcome['gain_loss'] = gain_loss_span.text
        outcome['yes_price'] = driver.find_element(By.CSS_SELECTOR, "div.c-fGHEql-eflaBF-isLeftCard-true span.c-bjtUDd").text.split()[-1]
        outcome['no_price'] = driver.find_element(By.CSS_SELECTOR, "div.c-fGHEql-fjcEzS-isRightCard-true span.c-bjtUDd").text.split()[-1]
        logger.info("Successfully scraped single-outcome market")
        return [outcome]
    except Exception as e:
        logger.error(f"Error scraping single-outcome market: {str(e)}")
        return None
        #return {'url': url, 'error': str(e)}


def old_main():
    base_url = "https://polymarket.com/markets/all"
    driver = pmsuck.initialize_driver(False)
    failed_links = []
    try:
        driver.get(base_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "markets-grid-container"))
        )
        all_loaded = pmsuck.scroll_until_end_of_results(driver)
        market_links = [elem.get_attribute('href') for elem in driver.find_elements(By.XPATH, "//a[contains(@href, '/event/')]") if not elem.get_attribute('href').endswith("#comments")]
        href_mapping = ParseHrefs(market_links)
        
        unique_market_links = filter_unique_urls(market_links)  # single-outcome markets and base-url of multi-outcome
        logger.info(f"Found {len(unique_market_links)} unique market links")
        
        results = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(scrape_single_url, url) for url in unique_market_links]
            for future in as_completed(futures):
                url, result = future.result()
                if result:
                    results.extend(result)
                else:
                    failed_links.append(url)

        logger.info(f"Scraped data for {len(results)} outcomes")

        single_outcome_results = []
        multi_outcome_results = []

        for item in results:
            if isinstance(item, list):  # Multi-outcome market
                multi_outcome_results.append(item)
            else:  # Single-outcome market
                single_outcome_results.append(item)

        all_results = single_outcome_results + [outcome for market in multi_outcome_results for outcome in market]

        print("Market Outcomes:")
        table_data = [
            [
                f"{outcome['name']} ({outcome['url']})" if 'url' in outcome else outcome['name'],
                outcome['bet_amount'],
                outcome['probability'],
                outcome.get('gain_loss', 'N/A'),
                outcome['yes_price'],
                outcome['no_price']
            ]
            for outcome in all_results
        ]
        headers = ["Name", "Bet Amount", "Probability", "Gain/Loss", "Yes Price", "No Price"]
        print(tabulate(table_data, headers=headers, tablefmt="grid"))

        with open("pm_results.csv", mode="w", newline='') as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(table_data)

        print("Final table saved to pm_results.csv")

    finally:
        driver.quit()

    if failed_links:
        print("\nFailed to scrape the following links:")
        for link in failed_links:
            print(link)
    else:
        print("\nAll links were scraped successfully.")


def BuildTable(results: list[dict]) -> list[str]:
    return [tabulate(outcome.items(), tablefmt="grid") for outcome in results]


# TODO: just pull all markets from https://polymarket.com/sitemap.xml
def main():
    market_links = pmsuck.main()
    logger.info(f"\nScrape complete \nFound {len(market_links)} market links\n\n")
    parsed_hrefs = ParseHrefs(market_links)
    pprint(parsed_hrefs)
    
    results = {}
    failed_links = []
    eventmap = {
        "single": (parsed_hrefs[""][0:3], scrape_single_outcome_market),
        "multi" : ([k for k in parsed_hrefs.keys() if k != ""][0:3], scrape_multi_outcome_market)
    }
    
    driver = pmsuck.initialize_driver()
    for outcome_type in "single", "multi":
        events, scrape_lambda = eventmap[outcome_type]
        for event in events:
            url = "https://polymarket.com/event/" + event
            resultlist = scrape_lambda(driver, url)
            if resultlist is not None: 
                results[event] = resultlist
                for t in BuildTable(resultlist): print(t)
            else: failed_links.append(url)
    
    driver.quit()
    logger.info(f"Scraped data for {len(results)} outcomes")
    return results, failed_links


if __name__ == "__main__":
    #href_mapping = pmsuck.main()
    #pprint(href_mapping)
    results, failed_links = main()
    #pprint(results)
    #print(len(results))
    pprint(f"failed links: {failed_links}")
    print("done")
