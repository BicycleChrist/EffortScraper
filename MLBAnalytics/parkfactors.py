import pathlib

from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from bs4 import BeautifulSoup
import pandas as pd


#TODO: extract headers from html, went on tilt and just hardcoded the shit
#TODO: save output from this and other MLB scripts to folder within MLBanalytics

def download_html_selenium(driver, url):
    driver.get(url) # table doesn't load without JS
    html = driver.page_source
    
    # parse the HTML
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find all table rows with the specified class pattern
    table_rows = soup.find_all('tr', id=lambda x: x and x.startswith('parkFactors-tr_'))
    if not table_rows: return None;
    
    data = []
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
    
    return data


def main(single_year:int=None, rolling_years:int=3):
    print("PARKFACTORS MAIN!!!!!!!!!!!!")
    service = FirefoxService()
    options = FirefoxOptions()
    options.add_argument('-headless')  # Uncomment if you run in headless mode
    options.add_argument("--window-size=1920x,1080")
    driver = webdriver.Firefox(service=service, options=options)
    
    savedata_folder = pathlib.Path(__file__).parent / "parkfactors_savedata"
    csv_folder = savedata_folder / "csv"
    json_folder = savedata_folder / "json"
    if not savedata_folder.exists(): savedata_folder.mkdir()
    if not csv_folder.exists(): csv_folder.mkdir()
    if not json_folder.exists(): json_folder.mkdir()
    
    new_json_files = []
    dataframes = []
    base_url = "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors"
    headers = ["Team", "Venue", "Year", "Park Factor", "wOBACon", "xwOBACon", "BACON", "xBACON","HardHit", "R", "OBP", "H", "1B", "2B", "3B", "HR", "BB", "SO", "PA"]
    years = range(1, (rolling_years+1))
    if single_year is not None: years = [single_year]
    for year in years:
        data =  download_html_selenium(driver, f"{base_url}?rolling={year}")
        if data is None: print("Table not found on the page."); continue;
        df = pd.DataFrame(data, columns=headers) # Convert the list of lists into a DataFrame
        df.to_csv((csv_file := (csv_folder/f"parkfactors_rolling{year}.csv")), index=False) # Save DataFrame
        print(f"Data successfully scraped and saved to: {csv_file}")
        df.to_json(json_file := (json_folder/f"parkfactors_rolling{year}.json"))
        print(f"Data successfully scraped and saved to: {json_file}")
        new_json_files.append(json_file)
        dataframes.append(df)
    
    driver.quit()
    return dataframes


if __name__ == "__main__":
    main(rolling_years=3)
