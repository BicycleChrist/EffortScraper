from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from bs4 import BeautifulSoup
import pandas as pd

#TODO: extract headers from html, went on tilt and just hardcoded the shit
#TODO: save output from this and other MLB scripts to folder within MLBanalytics

def download_html_selenium(url, filename):
    service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('-headless')  # Uncomment if you run in headless mode
    options.add_argument("--window-size=1920x,1080")
    driver = webdriver.Firefox(service=service, options=options)

    driver.get(url)
    html = driver.page_source

    # parse the HTML
    soup = BeautifulSoup(html, 'html.parser')

    # Find all table rows with the specified class pattern
    table_rows = soup.find_all('tr', id=lambda x: x and x.startswith('parkFactors-tr_'))

    if table_rows:
        data = []

        # Iterate through all rows
        for row in table_rows:
            row_data = []
            for cell in row.find_all('td', class_='tr-data'):
                # Check if the cell has a span element with text
                span = cell.find('span')
                if span:
                    row_data.append(span.get_text())
                else:
                    row_data.append(cell.get_text())
            if row_data:  # Skip empty rows
                data.append(row_data)



        headers = ["Team", "Venue", "Year", "Park Factor", "wOBACon", "xwOBACon", "BACON", "xBACON","HardHit", "R", "OBP", "H", "1B", "2B", "3B", "HR", "BB", "SO", "PA"]

        # Convert the list of lists into a DataFrame
        df = pd.DataFrame(data, columns=headers)

        # Save DataFrame
        df.to_csv(filename, index=False)

        print("Data successfully scraped and saved to", filename)
    else:
        print("Table not found on the page.")


    driver.quit()


url = "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors?type=year&year=2024&batSide=&stat=index_wOBAcon&condition=All&rolling=no"
filename = "parkFactors.csv"
download_html_selenium(url, filename)
