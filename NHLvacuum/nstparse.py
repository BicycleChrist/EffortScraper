import requests
import time
from bs4 import BeautifulSoup
import pandas as pd
import os

nhl_teams = [
        'ANA', 'ARI', 'BOS', 'BUF', 'CGY', 'CAR', 'CHI', 'COL', 'CBJ', 'DAL', 'DET', 'EDM',
        'FLA', 'L.A', 'MIN', 'MTL', 'NSH', 'N.J', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'S.J',
        'SEA', 'STL', 'T.B', 'TOR', 'VGK', 'WSH', 'WPG', 'VAN'
    ]

def get_static_tables(team_abbr):
    url = f"https://www.naturalstattrick.com/teamreport.php?team={team_abbr}"
    folder_path = "nhlteamreports"  # Main folder
    team_folder_path = os.path.join(folder_path, team_abbr)  # Subfolder for the team

    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        tables = soup.find_all("table")

        # Create the main folder if it doesn't exist
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        # Create the subfolder for the team if it doesn't exist
        if not os.path.exists(team_folder_path):
            os.makedirs(team_folder_path)

        for k, table in enumerate(tables):
            df = pd.read_html(str(table))[0]
            file_name = f"{team_abbr}_static_table_{k+1}.csv"
            file_path = os.path.join(team_folder_path, file_name)
            df.to_csv(file_path, index=False)

        print(f"Static data for {team_abbr} saved successfully in CSV format in the {team_abbr} subfolder.")
    except requests.exceptions.RequestException as e:
        print(f"Error accessing the webpage: {e}")


def allofit():
    for team_abbr in nhl_teams:
        get_static_tables(team_abbr)
        time.sleep(0.2)


allofit()
# Example usage
#get_static_tables("TOR")
