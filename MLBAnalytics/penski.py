from bs4 import BeautifulSoup
import requests
import pandas as pd
import time
from io import StringIO
import os
import lxml

def extract_team_tables(soup):
    toplevel = soup.find('div', class_="row gx-1 gx-lg-2 gx-xl-4 gx-xxl-3 align-self-auto justify-content-center")
    if not toplevel:
        print("Top level element not found")
        return []

    bottomlevels = toplevel.find_all('div', class_='row mt-2 g-2 p-2')
    if not bottomlevels:
        print("Bottom level elements not found")
        return []

    team_dataframes = []
    for bottomlevel in bottomlevels:
        team_name_tag = bottomlevel.find('h5', class_='mb-0')
        if not team_name_tag:
            continue
        team_name = team_name_tag.text.strip()
        dataframes = pd.read_html(StringIO(str(bottomlevel)))
        if dataframes:
            team_dataframes.append((team_name, dataframes[0]))

    return team_dataframes

if __name__ == "__main__":
    url = 'https://www.insidethepen.com/bullpen-usage.html'
    response = requests.get(url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        team_tables = extract_team_tables(soup)
        if team_tables:
            # Create the directory structure if it doesn't exist
            output_dir = os.path.join("MLBstats", "BPdata")
            os.makedirs(output_dir, exist_ok=True)

            for team_name, df in team_tables:
                # Format the current date as 'ddmmyyyy'
                date_str = time.strftime("%d%m%Y")
                filename = f"{team_name}_bullpen_stats_{date_str}.csv"
                # Clean up the filename by replacing spaces with underscores
                filename = filename.replace(' ', '_')
                file_path = os.path.join(output_dir, filename)
                df.to_csv(file_path, index=False)
                print(f"Data for {team_name} saved to {file_path}")
        else:
            print("No tables extracted")
    else:
        print(f"Failed to retrieve the webpage. Status code: {response.status_code}")
