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


def scroll_until_end_of_results(driver, max_scrolls=1000):
    print("Scrolling the page...")
    body = driver.find_element(By.TAG_NAME, 'body')

    for _ in range(max_scrolls):
        ActionChains(driver).send_keys_to_element(body, Keys.PAGE_DOWN).perform()
        time.sleep(0.5)  # Short pause to allow content to load

        if driver.find_elements(By.XPATH, "//p[contains(text(),'End of results')]"):
            print("End of results found.")
            return True
        else:
            continue

    print(f"Reached maximum number of scrolls ({max_scrolls}) without finding 'End of results'.")
    return False

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
            return scrape_single_outcome_market(driver)

    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        return None

def scrape_multi_outcome_market(driver, url):
    WebDriverWait(driver, 2).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']"))
        )
    market_items = driver.find_elements(By.CSS_SELECTOR, "div[data-scroll-anchor^='event-detail-accordion-item']")
    market_data = []

    for index, item in enumerate(market_items):
        outcome = {'url': url}  # Add the URL to each outcome

        try:
            outcome['name'] = item.find_element(By.CSS_SELECTOR, "p.c-cZBbTr").text
            outcome['bet_amount'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-ihVLOVM-css").text
            outcome['probability'] = item.find_element(By.CSS_SELECTOR, "p.c-dqzIym-icEtPXM-css").text

            yes_selector = "div.c-gBrBnR-ifgGdkS-css" if index == 0 else "div.c-gBrBnR-ibMWmgq-css"
            outcome['yes_price'] = item.find_element(By.CSS_SELECTOR, yes_selector).text.split()[-1]
            outcome['no_price'] = item.find_element(By.CSS_SELECTOR, "div.c-gBrBnR-ikGeufU-css").text.split()[-1]

            market_data.append(outcome)
        except Exception as e:
            logger.warning(f"Error scraping outcome {index}: {str(e)}")

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

def main():
    base_url = "https://polymarket.com/markets/all"
    driver = initialize_driver()
    try:
        driver.get(base_url)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "markets-grid-container"))
        )

        all_loaded = scroll_until_end_of_results(driver)
        market_links = [elem.get_attribute('href') for elem in driver.find_elements(By.XPATH, "//a[contains(@href, '/event/')]") if not elem.get_attribute('href').endswith("#comments")]
        unique_market_links = filter_unique_urls(market_links)[:-1]  #Set num of links to scrape

        logger.info(f"Found {len(unique_market_links)} unique market links")

        results = []
        with ThreadPoolExecutor(max_workers=6) as executor:  # Adjust max_workers as needed
            future_to_url = {executor.submit(scrape_single_url, url): url for url in unique_market_links}
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
