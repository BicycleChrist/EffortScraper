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
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from pprint import pprint
from PolymarketParsing import ParseHrefs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

failed_links = []

def initialize_driver():
    options = FirefoxOptions()
    options.add_argument('--headless')
    driver = webdriver.Firefox(options=options)
    driver.set_window_position(0, 0)
    driver.set_window_size(7500, 5240)
    return driver


def fetch_urls_from_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.content, 'xml')
    
    urls = []
    for url in soup.find_all('url'):
        loc = url.find('loc')
        changefreq = url.find('changefreq')
        
        if loc and changefreq and changefreq.text == 'always':
            urls.append(loc.text)
    
    return urls

def scrape_single_url(url):
    driver = initialize_driver()
    try:
        result = scrape_market_data(driver, url)
        time.sleep(0.21)
        return url, result
    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        failed_links.append(url)
        return url, None
    finally:
        driver.quit()

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
            return scrape_single_outcome_market(driver)

    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        return None

def scrape_multi_outcome_market(driver, url):
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']"))
    )
    market_items = driver.find_elements(By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']")
    market_data = []

    for index, item in enumerate(market_items):
        outcome = {'url': url}

        try:
            # Try multiple selectors for the outcome name
            name_selectors = [
                "p.c-cZBbTr",
                "p.c-dqzIym",  # A more generic selector
                "div[data-scroll-anchor^='event-detail-accordion-item'] > div > p"  # Even more generic
            ]
            
            for selector in name_selectors:
                try:
                    outcome['name'] = item.find_element(By.CSS_SELECTOR, selector).text
                    break
                except NoSuchElementException:
                    continue
            
            if 'name' not in outcome:
                raise ValueError("Could not find outcome name")

            outcome['bet_amount'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-ihVLOVM-css").text
            outcome['probability'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-icEtPXM-css").text

            # Try different selectors for Yes and No prices
            yes_selectors = [
                "div.c-gBrBnR-ifgGdkS-css",  # First outcome Yes
                "div.c-gBrBnR-ibMWmgq-css",  # Other outcomes Yes
                "button[data-testid='yes-button']"  # Generic Yes button
            ]
            no_selectors = [
                "div.c-gBrBnR-ikGeufU-css",  # Common No selector
                "button[data-testid='no-button']"  # Generic No button
            ]

            for yes_selector in yes_selectors:
                try:
                    yes_element = item.find_element(By.CSS_SELECTOR, yes_selector)
                    outcome['yes_price'] = yes_element.text.split()[-1]
                    break
                except NoSuchElementException:
                    continue

            for no_selector in no_selectors:
                try:
                    no_element = item.find_element(By.CSS_SELECTOR, no_selector)
                    outcome['no_price'] = no_element.text.split()[-1]
                    break
                except NoSuchElementException:
                    continue

            if 'yes_price' not in outcome or 'no_price' not in outcome:
                raise ValueError("Could not find Yes or No price")

            market_data.append(outcome)
        except Exception as e:
            logger.warning(f"Error scraping outcome {index}: {str(e)}")
            continue

        time.sleep(0.1)  # Small delay between scraping each outcome

    logger.info(f"Successfully scraped {len(market_data)} outcomes from multi-outcome market")
    return market_data

def scrape_single_outcome_market(driver):
    try:
        WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.c-dhzjXW-iqUfiq-css p.c-dqzIym"))
        )
        outcome = {}

        # Extract the market question (name)
        name_elem = driver.find_element(By.CSS_SELECTOR, "div.c-dhzjXW-iqUfiq-css p.c-dqzIym")
        outcome['name'] = name_elem.text if name_elem else 'N/A'

        # Extract bet amount
        bet_amount_elem = driver.find_element(By.CSS_SELECTOR, "div.c-dhzjXW-ijroWjL-css p.c-dqzIym-iUwoCw-css")
        outcome['bet_amount'] = bet_amount_elem.text if bet_amount_elem else 'N/A'

        # Extract probability and gain/loss
        prob_gain_elem = driver.find_element(By.CSS_SELECTOR, "p.c-dqzIym-ibHwMTN-css")
        if prob_gain_elem:
            prob_text = prob_gain_elem.text.split('chance')[0].strip()
            outcome['probability'] = prob_text if prob_text else 'N/A'

            # Extract gain/loss from the span
            gain_loss_span = prob_gain_elem.find_element(By.CSS_SELECTOR, "span.c-PJLV-ijwODCF-css, span.c-PJLV-iejnpxd-css")
            if gain_loss_span:
                gain_loss_text = gain_loss_span.text
                outcome['gain_loss'] = gain_loss_text if gain_loss_text else 'N/A'
            else:
                outcome['gain_loss'] = 'N/A'
        else:
            outcome['probability'] = 'N/A'
            outcome['gain_loss'] = 'N/A'

        # Extract Yes price
        yes_elem = driver.find_element(By.CSS_SELECTOR, "div.c-fGHEql-eflaBF-isLeftCard-true span.c-bjtUDd")
        outcome['yes_price'] = yes_elem.text.split()[-1] if yes_elem else 'N/A'

        # Extract No price
        no_elem = driver.find_element(By.CSS_SELECTOR, "div.c-fGHEql-fjcEzS-isRightCard-true span.c-bjtUDd")
        outcome['no_price'] = no_elem.text.split()[-1] if no_elem else 'N/A'

        logger.info("Successfully scraped single-outcome market")
        return [outcome]
    except NoSuchElementException as e:
        logger.error(f"Error scraping single-outcome market - Element not found: {str(e)}")
    except Exception as e:
        logger.error(f"Error scraping single-outcome market: {str(e)}")
    return None

def is_valid_market_url(url):
    # Check if the URL is a valid Polymarket market URL
    return url.startswith("https://polymarket.com/market/")

def main():
    sitemap_url = "https://polymarket.com/sitemap.xml"
    driver = initialize_driver()
    try:
        market_links = fetch_urls_from_sitemap(sitemap_url)
        parsed_links = ParseHrefs(market_links)
        
        # Flatten the parsed links dictionary into a list
        unique_market_links = [k for k in parsed_links.keys() if k] + \
                              [item for sublist in parsed_links.values() for item in sublist]

        # Filter out invalid URLs
        valid_market_links = [url for url in unique_market_links if is_valid_market_url(url)]

        logger.info(f"Found {len(valid_market_links)} valid unique market links")

        # Print the gathered URLs
        print("\nGathered URLs:")
        for url in valid_market_links:
            print(url)
        print("\nStarting scrape...\n")

        results = []
        with ThreadPoolExecutor(max_workers=6) as executor:  # Adjust max_workers as needed
            future_to_url = {executor.submit(scrape_single_url, url): url for url in valid_market_links}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    _, result = future.result()
                    if result:
                        results.extend(result)
                except Exception as exc:
                    logger.error(f"{url} generated an exception: {exc}")
                    failed_links.append(url)

        logger.info(f"Scraped data for {len(results)} outcomes")

        single_outcome_results = []
        multi_outcome_results = []

        for item in results:
            if isinstance(item, list):  # Multi-outcome market
                multi_outcome_results.append(item)
            else:  # Single-outcome market
                single_outcome_results.append(item)

        # Combine single-outcome and multi-outcome results
        all_results = single_outcome_results + [outcome for market in multi_outcome_results for outcome in market]

        # Print all results in a single table
        print("Market Outcomes:")
        table_data = [
            [
                f"{outcome['name']} ({outcome['url']})" if 'url' in outcome else outcome['name'],
                outcome['bet_amount'],
                outcome['probability'],
                outcome.get('gain_loss', 'N/A'),  # Use 'N/A' if 'gain_loss' key is not present
                outcome['yes_price'],
                outcome['no_price']
            ]
            for outcome in all_results
        ]
        headers = ["Name", "Bet Amount", "Probability", "Gain/Loss", "Yes Price", "No Price"]
        print(tabulate(table_data, headers=headers, tablefmt="grid"))

        # save output to .csv
        with open("pm_results.csv", mode="w", newline='') as file:
            writer = csv.writer(file)
            # Write the header
            writer.writerow(headers)
            # Write the data rows
            writer.writerows(table_data)

        print("Final table saved to pm_results.csv")

    finally:
        driver.quit()

    # Print failed links at the end
    if failed_links:
        print("\nFailed to scrape the following links:")
        for link in failed_links:
            print(link)
    else:
        print("\nAll links were scraped successfully.")

if __name__ == "__main__":
    main()
    #scrape_market_data()
    print("done")
