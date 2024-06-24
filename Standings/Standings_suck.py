import requests
from bs4 import BeautifulSoup
import pandas as pd
import pathlib


def extract_table_data(rows):
    table_data = []
    for row in rows:
        cells = row.find_all(['th', 'td'])
        row_data = [cell.get_text(strip=True) for cell in cells]
        table_data.append(row_data)
    return table_data


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


if __name__ == "__main__":
    # Dictionary to hold all DataFrames for NFL, NBA, MLB, and NHL
    all_dataframes = {}
    
    # ESPN commie JS users, NBC sports chad html table studs
    urls = {
        'NFL': 'https://www.nbcsports.com/nfl/standings',
        'NBA': 'https://www.nbcsports.com/nba/standings',
        'MLB': 'https://www.nbcsports.com/mlb/standings',
        'NHL': 'https://www.nbcsports.com/nhl/standings',
    }
    
    # Define conference titles
    #TODO: NHL and NBA conferences get printed one after the other
    # Need to overcome naming conflict
    
    nfl_conferences = ['NFC', 'AFC']
    nba_conferences = ['Eastern Conference', 'Western Conference']
    mlb_conferences = ['AL', 'NL']
    nhl_conferences = ['Eastern Conference', 'Western Conference']
    
    nfl_data = scrape_page(urls['NFL'], nfl_conferences)
    all_dataframes.update(nfl_data)
    nfl_combined_dataframe = pd.concat((nfl_data.values()))
    
    nba_data = scrape_page(urls['NBA'], nba_conferences)
    all_dataframes.update(nba_data)
    nba_combined_dataframe = pd.concat((nba_data.values()))
    
    mlb_data = scrape_page(urls['MLB'], mlb_conferences)
    all_dataframes.update(mlb_data)
    mlb_combined_dataframe = pd.concat((mlb_data.values()))
    mlb_combined_dataframe['RSRuns Scored'] = pd.to_numeric(mlb_combined_dataframe['RSRuns Scored'], errors='coerce')
    mlb_combined_dataframe['RARuns Allowed'] = pd.to_numeric(mlb_combined_dataframe['RARuns Allowed'], errors='coerce')
    mlb_combined_dataframe['WWins'] = pd.to_numeric(mlb_combined_dataframe['WWins'], errors='coerce')
    mlb_combined_dataframe['LLosses'] = pd.to_numeric(mlb_combined_dataframe['LLosses'], errors='coerce')

    mlb_combined_dataframe['Pythagorean Win %'] = round(mlb_combined_dataframe['RSRuns Scored']**2 / (mlb_combined_dataframe['RSRuns Scored']**2 + mlb_combined_dataframe['RARuns Allowed']**2), 3)
    mlb_combined_dataframe['Total Games Played'] = mlb_combined_dataframe['WWins'] + mlb_combined_dataframe['LLosses']

    # Calculate Adjusted Wins
    mlb_combined_dataframe['Adjusted Wins'] = round(mlb_combined_dataframe['Total Games Played'] * mlb_combined_dataframe['Pythagorean Win %'], 2)

    # Calculate Adjusted Losses
    mlb_combined_dataframe['Adjusted Losses'] = round(mlb_combined_dataframe['Total Games Played'] - mlb_combined_dataframe['Adjusted Wins'], 2)

    # Calculate Wins - Pythag wins
    mlb_combined_dataframe['Wins - Pythagwins'] = round(mlb_combined_dataframe['WWins'] - mlb_combined_dataframe['Adjusted Wins'], 2)

    mlb_combined_dataframe['']
    
    nhl_data = scrape_page(urls['NHL'], nhl_conferences)
    all_dataframes.update(nhl_data)
    nhl_combined_dataframe = pd.concat((nhl_data.values()))
    
    cwd = pathlib.Path.cwd()
    savedir = cwd / "dataframes"
    if not savedir.exists():
        savedir.mkdir()
    
    for division_name, dataframe in (
            ("nfl", nfl_combined_dataframe), 
            ("nba", nba_combined_dataframe), 
            ("mlb", mlb_combined_dataframe), 
            ("nhl", nhl_combined_dataframe),
        ):
        savefile = savedir / f'{division_name}_table.csv'
        # Optionally, save each DataFrame to a CSV file
        dataframe.to_csv(savefile, index=False)
