from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from bs4 import BeautifulSoup
import pandas as pd

#TODO: Table uses .svg logos to denote team instead of 3 letter abbrev, need to populate team row

def download_html_selenium(url, filename):
    service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('-headless')  # Uncomment if you run in headless mode
    options.add_argument("--window-size=1920x,1080")
    driver = webdriver.Firefox(service=service, options=options)


    driver.get(url)
    html = driver.page_source


    soup = BeautifulSoup(html, 'html.parser')

    # Find table rows
    rows = soup.find_all('tr', class_='default-table-row')

    # Initialize data lists
    data = []

    # Extract data
    for row in rows:
        row_data = []
        for td in row.find_all('td'):
            row_data.append(td.get_text(strip=True))
        if len(row_data) == 15:  # Adjust this according to your data
            data.append(row_data)

    # Convert to DataFrame
    column_names = ["Rank", "Player", "Team", "PA", "Pos", "BIP", "BA", "xBA", "Diff_BA", "SLG", "xSLG", "Diff_SLG", "wOBA", "xwOBA", "Diff_wOBA"]
    df = pd.DataFrame(data, columns=column_names)

    # Save DataFrame to CSV
    df.to_csv(filename, index=False)


    driver.quit()


url = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
filename = "table_data_sav_hit.csv"
download_html_selenium(url, filename)
