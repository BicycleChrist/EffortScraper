import requests
from bs4 import BeautifulSoup
import pandas as pd

# ESPN commie JS users, NBC sports chad html table studs
urls = {
    'NFL': 'https://www.nbcsports.com/nfl/standings',
    'NBA': 'https://www.nbcsports.com/nba/standings',
    'MLB': 'https://www.nbcsports.com/mlb/standings',
    'NHL': 'https://www.nbcsports.com/nhl/standings'
}


def extract_table_data(rows):
    table_data = []
    for row in rows:
        cells = row.find_all(['th', 'td'])
        row_data = [cell.get_text(strip=True) for cell in cells]
        table_data.append(row_data)
    return table_data

# a dictionary of DataFrames, aka border line retardation
def scrape_page(url, conference_titles):
    response = requests.get(url)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    group_titles = soup.find_all('div', class_='TableGroup-title')

    division_dataframes = {}

    for group_title in group_titles:
        group_name = group_title.get_text(strip=True)
        if group_name not in conference_titles:
            continue

        group = group_title.find_next_sibling('div', class_='TableGroup-tables')
        tables = group.find_all('bsp-table', class_='TableB')

        for table in tables:
            division_title = table.find('div', class_='Table-title').get_text(strip=True)
            rows = table.find_all('tr')
            data = extract_table_data(rows)
            if data:
                dataframe = pd.DataFrame(data[1:], columns=data[0])
                division_name = f'{group_name} {division_title}'
                division_dataframes[division_name] = dataframe

    return division_dataframes

# Dictionary to hold all DataFrames for NFL, NBA, MLB, and NHL
all_dataframes = {}

# Define conference titles
#TODO: NHL and NBA conferences get printed one after the other
# Need to overcome naming conflict

nfl_conferences = ['NFC', 'AFC']
nba_conferences = ['Eastern Conference', 'Western Conference']
mlb_conferences = ['AL', 'NL']
nhl_conferences = ['Eastern Conference', 'Western Conference']


nfl_data = scrape_page(urls['NFL'], nfl_conferences)
all_dataframes.update(nfl_data)


nba_data = scrape_page(urls['NBA'], nba_conferences)
all_dataframes.update(nba_data)


mlb_data = scrape_page(urls['MLB'], mlb_conferences)
all_dataframes.update(mlb_data)


nhl_data = scrape_page(urls['NHL'], nhl_conferences)
all_dataframes.update(nhl_data)

# Print the DataFrames for each division
for division, df in all_dataframes.items():
    print(f'{division} Table:')
    print(df)
    print('\n')

    # Optionally, save each DataFrame to a CSV file
    df.to_csv(f'{division.lower().replace(" ", "_")}_table.csv', index=False)
