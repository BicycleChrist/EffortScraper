from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
import time

#TODO: Acquire live odds; pages are able to be parsed quite fast for bigly suck up
#TODO : Figure out name of header element for odds containers
service = FirefoxService()
options = FirefoxOptions()
options.add_argument('-headless')
driver = webdriver.Firefox(service=service, options=options)


url = 'https://www.bovada.lv/sports/baseball/mlb'
driver.get(url)


wait = WebDriverWait(driver, 10)
next_events_bucket = wait.until(EC.presence_of_element_located((By.CLASS_NAME, 'next-events-bucket')))


grouped_events = next_events_bucket.find_elements(By.CLASS_NAME, 'grouped-events')


game_links = []
for event in grouped_events:
    try:

        coupons = event.find_elements(By.XPATH, ".//sp-coupon")

        for coupon in coupons:
            try:
                # Find the href links to the game odds
                links = coupon.find_elements(By.XPATH, ".//a[@class='game-view-cta']")
                for link in links:
                    game_links.append(link.get_attribute('href'))
            except Exception as e:
                print(f"Failed to extract href link: {e}")

    except Exception as e:
        print(f"Failed to process event: {e}")

# Loop through each game link to extract data
for game_link in game_links:
    try:
        driver.get(game_link)
        time.sleep(1)

        # Wait until the sp-main-area element is loaded
        main_area = wait.until(EC.presence_of_element_located((By.CLASS_NAME, 'sp-main-area')))


        event_infos = main_area.find_elements(By.CLASS_NAME, 'event-info')

        for event_info in event_infos:
            try:
                #  event-coupons within event-info incredible
                event_coupons = event_info.find_elements(By.CLASS_NAME, 'event-coupons')

                for coupon in event_coupons:
                    # Extract coupon I guess fucking JS everytime
                    game_date = coupon.find_element(By.CLASS_NAME, 'period').text.strip()
                    game_time = coupon.find_element(By.CLASS_NAME, 'clock').text.strip()
                    teams = coupon.find_elements(By.CLASS_NAME, 'competitor-name')
                    team1 = teams[0].text.strip()
                    team2 = teams[1].text.strip()

                    # Extract odds data
                    outcomes = coupon.find_elements(By.XPATH, ".//sp-outcome")
                    for outcome in outcomes:
                        try:
                            bet_type_element = outcome.find_element(By.XPATH, ".//*[contains(@class, 'market-line') or contains(@class, 'bet-btn')]")
                            #bet_type_header = outcome.find_element(By.CLASS_NAME, 'league-header full-width')
                            bet_type = bet_type_element.text.strip()
                            #bet_header = bet_type_header.text.strip()
                            bet_price = outcome.find_element(By.CLASS_NAME, 'bet-price').text.strip()
                            print(f"{game_date} {game_time} | {team1} vs {team2} | {bet_type}: {bet_price}")
                        except Exception as e:
                            print(f"Failed to extract bet information: {e}")
                            print(f"Outcome element HTML: {outcome.get_attribute('innerHTML')}")
            except Exception as e:
                print(f"Failed to process event-info: {e}")
    except Exception as e:
        print(f"Failed to load game link {game_link}: {e}")


driver.quit()
