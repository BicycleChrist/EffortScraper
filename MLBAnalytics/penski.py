from bs4 import BeautifulSoup
import requests
import pandas as pd
import time
from io import StringIO
import os
from concurrent.futures import ThreadPoolExecutor

#TODO: Organize the .csv files by team

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

def extract_player_links(soup):
    player_links = []
    table_rows = soup.find_all('th', scope="row")
    for row in table_rows:
        link_tag = row.find('a', class_='usage-link')
        if link_tag and 'href' in link_tag.attrs:
            player_name = link_tag.text.strip()
            player_links.append((player_name, link_tag['href']))
    return player_links

def scrape_player_details(base_url, player_name, href):
    player_url = base_url + href  # Keep the leading dot in href
    response = requests.get(player_url)
    if response.status_code == 200:
        player_soup = BeautifulSoup(response.content, 'lxml')
        
        # Extract Advanced Pitcher Traits
        adv_traits_div = player_soup.find('div', class_='col-12 p-2 px-lg-3 mb-2 border border-1 border-secondary bg-alt-white')
        adv_traits = {}
        if adv_traits_div:
            trait_keys = adv_traits_div.find_all('div', class_='col-9 p-1 ps-2 ps-xl-1 border-bottom border-alt-light')
            trait_values = adv_traits_div.find_all('div', class_='col-2 p-1 border-bottom border-alt-light')
            for key_div, value_div in zip(trait_keys, trait_values):
                key = key_div.text.strip()
                value = value_div.text.strip()
                adv_traits[key] = value
        
        # Extract Splits Stats Table
        splits_stats_table = player_soup.find('table', class_='table table-bordered table-striped table-width sticky-table pb-0 mb-0')
        splits_stats_df = pd.read_html(StringIO(str(splits_stats_table)))[0] if splits_stats_table else None
        
        return player_name, adv_traits, splits_stats_df
    else:
        print(f"Failed to retrieve the player's page {href}. Status code: {response.status_code}")
        return player_name, None, None

if __name__ == "__main__":
    base_url = 'https://www.insidethepen.com'
    main_url = f'{base_url}/bullpen-usage.html'
    response = requests.get(main_url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'lxml')
        
        # Extract team tables
        team_tables = extract_team_tables(soup)
        if team_tables:
            # create directory structure if it doesn't exist
            output_dir = os.path.join("MLBstats", "BPdata")
            os.makedirs(output_dir, exist_ok=True)

            for team_name, df in team_tables:
                # Format the current date as 'ddmmyyyy'
                date_str = time.strftime("%d%m%Y")
                filename = f"{team_name}_bullpen_stats_{date_str}.csv"
                # Clean up the filename, need to do some more organization
                filename = filename.replace(' ', '_')
                file_path = os.path.join(output_dir, filename)
                df.to_csv(file_path, index=False)
                print(f"Data for {team_name} saved to {file_path}")
            
            # extract player links and scrape details with multithreading
            player_links = extract_player_links(soup)
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(scrape_player_details, base_url, player_name, href) for player_name, href in player_links]
                
                for future in futures:
                    player_name, adv_traits, splits_stats_df = future.result()
                    if adv_traits:
                        adv_traits_filename = f"{player_name}_adv_traits_{date_str}.csv"
                        adv_traits_filepath = os.path.join(output_dir, adv_traits_filename)
                        adv_traits_df = pd.DataFrame(list(adv_traits.items()), columns=['Trait', 'Value'])
                        adv_traits_df.to_csv(adv_traits_filepath, index=False)
                        print(f"Advanced traits for {player_name} saved to {adv_traits_filepath}")
                    
                    if splits_stats_df is not None:
                        splits_stats_filename = f"{player_name}_splits_stats_{date_str}.csv"
                        splits_stats_filepath = os.path.join(output_dir, splits_stats_filename)
                        splits_stats_df.to_csv(splits_stats_filepath, index=False)
                        print(f"Splits stats for {player_name} saved to {splits_stats_filepath}")
        else:
            print("No tables extracted")
    else:
        print(f"Failed to retrieve the webpage. Status code: {response.status_code}")






