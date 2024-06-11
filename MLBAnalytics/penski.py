from bs4 import BeautifulSoup
import requests
import pandas as pd
import time
from io import StringIO
import pathlib
import concurrent.futures
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
            df = dataframes[0]
            df.columns = df.columns.str.strip()  # Strip whitespace from column names
            team_dataframes.append((team_name, df))

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
        if splits_stats_df is not None:
            splits_stats_df.columns = splits_stats_df.columns.str.strip()
        
        return player_name, adv_traits, splits_stats_df
    else:
        print(f"Failed to retrieve the player's page {href}. Status code: {response.status_code}")
        return player_name, None, None


# 'purpose' is 'bullpen_stats' or' adv_traits'
def GetFilepath(purpose:str, name:str, append_date=True) -> pathlib.Path | list[pathlib.Path]:
    purposes = ('bullpen_stats', 'adv_traits', 'splits_stats')
    if purpose not in purposes:
        print(f"Error: GetFilepath did not understand the purpose argument: '{purpose}'.\nExiting.")
        exit(1)
    cwd = pathlib.Path.cwd()
    output_dir = cwd / "MLBstats" / "BPdata"
    output_dir.mkdir(exist_ok=True, parents=True)
    name = name.strip().replace(' ', '_')  # clean up the input
    middle_segment = '_' + purpose + '_'
    datestring = '*'
    if append_date: datestring = time.strftime("%d%m%Y")
    else: middle_segment.removesuffix('_')
    filename = f"{name}{middle_segment}{datestring}.csv"
    if not append_date: # assume we're doing a lookup instead of save if we're not appending date
        filenames = list(output_dir.glob(filename))
        return filenames
    print(f"filename: {filename}")
    return output_dir / filename
    

def Main():
    base_url = 'https://www.insidethepen.com'
    main_url = f'{base_url}/bullpen-usage.html'
    response = requests.get(main_url)
    if response.status_code != 200:
        print(f"Failed to retrieve the webpage. Status code: {response.status_code}")
        exit(1)
    
    print("soup")
    soup = BeautifulSoup(response.content, 'lxml')
    
    # Extract team tables
    team_tables = extract_team_tables(soup)
    print("team tables extracted")
    if not team_tables: 
        print("No tables extracted")
        exit(1)
    
    for team_name, df in team_tables:
        filepath = GetFilepath('bullpen_stats', team_name)
        df.to_csv(filepath, index=False)
        print(f"Data for {team_name} saved to {filepath}")
    
    # extract player links and scrape details with multithreading
    player_links = extract_player_links(soup)
    print(f"extracted player links")
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = [executor.submit(scrape_player_details, base_url, player_name, href) for player_name, href in player_links]
        for future in concurrent.futures.as_completed(futures):
            player_name, adv_traits, splits_stats_df = future.result()
            if adv_traits:
                filepath = GetFilepath('adv_traits', player_name)
                adv_traits_df = pd.DataFrame(list(adv_traits.items()), columns=['Trait', 'Value'])
                adv_traits_df['Trait'] = adv_traits_df['Trait'].str.strip()  # Strip whitespace from Trait column values
                adv_traits_df.to_csv(filepath, index=False)
                print(f"Advanced traits for {player_name} saved to {filepath}")
            if splits_stats_df is not None:
                filepath = GetFilepath('splits_stats', player_name)
                splits_stats_df.to_csv(filepath, index=False)
                print(f"Splits stats for {player_name} saved to {filepath}")
    
    return


if __name__ == "__main__":
    Main()
    print("Done")
