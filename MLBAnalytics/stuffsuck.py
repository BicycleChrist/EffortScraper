from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
import pandas as pd
from bs4 import BeautifulSoup
import time

# ganked the selenium options/imports from boundFetch

# Thought we could get away w/ just BS4 and req but no dice
#TODO:save html files to own folder along with data files

def download_html_selenium(url, filename):
    service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('-headless')  # Uncomment if you run in headless mode
    options.add_argument("--window-size=1920x,1080")
    driver = webdriver.Firefox(service=service, options=options)


    driver.get(url)

    # Get page source
    html_content = driver.page_source

    # Write html to file
    with open(filename, 'w') as file:
        file.write(html_content)

    # Close WebDriver
    driver.quit()

    print(f"HTML file downloaded successfully as '{filename}'.")
    #

# Extract tableski data from HTML
def extract_table_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    table_rows = soup.find_all('tr', class_='is-selected__invalid')

    data = []
    for row in table_rows:
        cols = row.find_all('td')
        cols_data = {}
        for col in cols:
            col_id = col.get('data-col-id')  # Use .get() method to handle missing attributes
            col_data = col.text.strip()
            if col_id:
                cols_data[col_id] = col_data
        data.append(cols_data)

    return data

# convert table data to CSV
def convert_to_csv(table_data, filename):
    df = pd.DataFrame(table_data)
    current_time = time.strftime("%Y-%m-%d %H-%M-%S")  # Get current date and time
    new_filename = f"{filename[:-4]}_{current_time}.csv"  # Append date and time to filename
    df.to_csv(new_filename, index=False)
    print(f"Table data saved to '{new_filename}'.")


def main():
    url = 'https://www.fangraphs.com/leaders/major-league?type=36&pos=all&stats=pit&sortcol=3&sortdir=default&qual=1&pagenum=1&pageitems=2000000000'  # Replace 'your_url_here' with the URL of the webpage containing the table
    html_file = 'FG.html'
    csv_file = 'table_data.csv'

    # Download HTML file
    download_html_selenium(url, html_file)

    # Extract table data
    with open(html_file, 'r') as file:
        html_content = file.read()
        table_data = extract_table_data(html_content)
        convert_to_csv(table_data, csv_file)
        print("Table extracted and saved successfully.")

if __name__ == "__main__":
    main()
