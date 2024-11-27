from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import os
from datetime import datetime

def scrape_table(url):
    options = webdriver.FirefoxOptions()
    options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)

    try:
        print(f"Accessing webpage...")
        driver.get(url)

        wait = WebDriverWait(driver, 10)
        rows = wait.until(EC.presence_of_all_elements_located(
            (By.CLASS_NAME, "resp-table-body__row")))

        print(f"Found {len(rows)} rows")
        data = []

        for row in rows:
            try:
                row_data = {}

                id_cell = row.find_element(By.CLASS_NAME, "resp-table-body__item--main")
                if id_cell:
                    row_data['ID'] = id_cell.text.replace('ID:', '').strip()

                inline_cells = row.find_elements(By.CLASS_NAME, "resp-table-body__item--inline")

                for cell in inline_cells:
                    try:
                        label_span = cell.find_element(By.CLASS_NAME, "resp-table-body__label")
                        if label_span:
                            label = label_span.text.rstrip(':')
                            full_text = cell.text
                            value = full_text.replace(label_span.text, '').strip()
                            row_data[label] = value
                    except:
                        continue

                notes_div = row.find_elements(By.CSS_SELECTOR, "[id^='listing-notes-']")
                if notes_div:
                    row_data['Updated'] = notes_div[0].text.replace('Updated/Notes:', '').strip()

                data.append(row_data)

            except Exception as e:
                print(f"Error processing row: {e}")
                continue

        return data

    except Exception as e:
        print(f"Error during scraping: {e}")
        return None

    finally:
        driver.quit()

def save_data(data, base_filename):
    if not data:
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    df = pd.DataFrame(data)

    output_dir = 'PermitData'
    os.makedirs(output_dir, exist_ok=True)

    try:
        csv_filename = os.path.join(output_dir, f"{base_filename}_{timestamp}.csv")
        df.to_csv(csv_filename, index=False)
        print(f"Data saved to CSV: {csv_filename}")

        excel_filename = os.path.join(output_dir, f"{base_filename}_{timestamp}.xlsx")
        df.to_excel(excel_filename, index=False)
        print(f"Data saved to Excel: {excel_filename}")

        return True

    except Exception as e:
        print(f"Error saving files: {e}")
        return False

def main():
    urls = {
        'alaska_permits': "https://dockstreetbrokers.com/permits/alaska-permits",
        'Halibut_ifq': "https://dockstreetbrokers.com/longline-ifqs/halibut-ifqs",
        'Sablefish_ifq': "https://dockstreetbrokers.com/longline-ifqs/sablefish-ifqs"
    }

    for name, url in urls.items():
        print(f"\nStarting web scraping for {name}...")
        data = scrape_table(url)

        if data:
            print(f"Successfully scraped {len(data)} rows")
            save_data(data, name)
        else:
            print(f"No data was scraped")

if __name__ == "__main__":
    main()
