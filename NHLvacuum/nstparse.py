import requests
import time
from datetime import datetime
from bs4 import BeautifulSoup
import pandas as pd
import os
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

nhl_teams = [
    'ANA', 'ARI', 'BOS', 'BUF', 'CGY', 'CAR', 'CHI', 'COL', 'CBJ', 'DAL', 'DET', 'EDM',
    'FLA', 'L.A', 'MIN', 'MTL', 'NSH', 'N.J', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'S.J',
    'SEA', 'STL', 'T.B', 'TOR', 'VGK', 'WSH', 'WPG', 'VAN'
]

def get_static_tables(team_abbr, date_folder):
    url = f"https://www.naturalstattrick.com/teamreport.php?team={team_abbr}"
    base_folder_path = "nhlteamreports"  # Main folder
    general_data_folder = "generalTRdata"  # Folder for general team data
    team_folder_path = os.path.join(base_folder_path, team_abbr, general_data_folder, date_folder)  # Subfolder for the team's general data with date

    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        tables = soup.find_all("table")

        os.makedirs(team_folder_path, exist_ok=True)  # Create the main folder and subfolders if they don't exist

        for k, table in enumerate(tables):
            df = pd.read_html(str(table))[0]
            file_name = f"{team_abbr}_static_table_{k+1}.csv"
            file_path = os.path.join(team_folder_path, file_name)
            df.to_csv(file_path, index=False)

        print(f"Static data for {team_abbr} saved successfully in CSV format in the {team_folder_path} subfolder.")
    except requests.exceptions.RequestException as e:
        print(f"Error accessing the webpage: {e}")



def allofit(date_folder):
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(get_static_tables, team, date_folder) for team in nhl_teams]
        for future in futures:
            future.result()



def download_file(team_abbr, link, base_url, date_folder):
    directory_path = os.path.join('nhlteamreports', team_abbr, 'generalTRdata', date_folder, 'rollingavggraphs')

    if not os.path.exists(directory_path):
        os.makedirs(directory_path)

    full_url = urljoin(base_url, link['href'])

    with requests.get(full_url, stream=True) as file_response:
        file_response.raise_for_status()
        file_path = os.path.join(directory_path, link['href'])

        with open(file_path, 'wb') as f:
            for chunk in file_response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded {link['href']} into {directory_path}/")

def download_charts(base_url, date_folder):
    response = requests.get(base_url)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    links = [link for link in soup.find_all('a', href=True) if link['href'].endswith('.png')]

    with ThreadPoolExecutor(max_workers=5) as executor:
        for link in links:
            parts = link['href'].split('-')
            if len(parts) < 2 or not parts[1].isupper():
                continue

            team_abbr = parts[1]
            executor.submit(download_file, team_abbr, link, base_url, date_folder)

    print("All files have been downloaded.")


def fetch_season_data(start_year, end_year):
    base_url = "https://www.naturalstattrick.com/teamtable.php"
    eng_data_folder = "ENGdataYbY"

    # Create the ENGdataYbY directory if it does not exist
    eng_data_path = os.path.join('nhlteamreports', eng_data_folder)
    if not os.path.exists(eng_data_path):
        os.makedirs(eng_data_path)

    for year in range(start_year, end_year, 3):
        from_season = f"{year}{year + 1:02d}"
        thru_season = f"{min(year + 2, end_year - 1)}{min(year + 3, end_year):02d}"
        url = f"{base_url}?fromseason={from_season}&thruseason={thru_season}&stype=2&sit=ena&score=all&rate=n&team=all&loc=B&gpf=410&fd=&td="

        response = requests.get(url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            tables = soup.find_all("table")
            if tables:
                df = pd.read_html(str(tables[0]))[0]
                # Save each season's data to a separate CSV file
                csv_filename = f"{from_season}-{thru_season}_ENGdata.csv"
                df.to_csv(os.path.join(eng_data_path, csv_filename), index=False)
                print(f"Data for seasons {from_season}-{thru_season} saved to {csv_filename}")
            else:
                print(f"No table found for seasons {from_season}-{thru_season}")
        else:
            print(f"Failed to retrieve data for seasons {from_season}-{thru_season}")





# Main execution logic
if __name__ == "__main__":
    current_date = datetime.now().strftime('%Y-%m-%d')
    allofit(current_date)
    download_charts("https://www.naturalstattrick.com/teams/20232024/charts/pos_rolling/", current_date)
    # Fetch data from 2007-2008 to 2023-2024
    #season_data = fetch_season_data(2007, 2024)
