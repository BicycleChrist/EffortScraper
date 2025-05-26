from bs4 import BeautifulSoup
import requests
import pathlib
import pandas as pd
import time

def clean_fangraphs_hitting_columns(df):

    line_break_cols = [col for col in df.columns if 'Line Break' in str(col)]
    df_clean = df.drop(columns=line_break_cols)

    # Clean column renaming - focus on key hitting stats
    rename_map = {
        'Unnamed: 0': 'Index',
        '#': 'Rank',
        'Name': 'Name',
        'Team': 'Team',
        'GG - Games Played': 'Games',
        'PAPA - Plate Appearances': 'PA',
        'HRHR - Home Runs': 'HR',
        'RR - Runs': 'Runs',
        'RBIRBI - Runs Batted In': 'RBI',
        'SBSB - Stolen Bases': 'SB',
        'BB%BB% - Walk Percentage (BB/PA)': 'BB_Pct',
        'K%K% - Strikeout Percentage (SO/PA)': 'K_Pct',
        'ISOISO - Isolated Power (SLG-AVG)': 'ISO',
        'BABIPBABIP - Batting Average on Balls in Play': 'BABIP',
        'AVGAVG - Batting Average (H/AB)': 'AVG',
        'OBPOBP - On Base Percentage': 'OBP',
        'SLGSLG - Slugging Percentage': 'SLG',
        'wOBAwOBA - Weighted On Base Average (Linear Weights)': 'wOBA',
        'xwOBAxwOBA - Expected weighted on-base average': 'xwOBA',
        'wRC+wRC+ - Runs per PA scaled where 100 is average; both league and park adjusted; based on wOBA': 'wRC_Plus',
        'BsRBase Running - Base running runs above average, includes SB or CS': 'BaseRunning',
        'OffOffense - Batting and Base Running combined (above average)': 'Offense',
        'DefDefense - Fielding and Positional Adjustment combined (above average)': 'Defense',
        'WARWAR - Wins Above Replacement': 'WAR'
    }

    # Rename columns
    df_clean = df_clean.rename(columns=rename_map)

    # Convert percentage columns from strings to decimals
    for col in ['BB_Pct', 'K_Pct']:
        if col in df_clean.columns:
            df_clean[col] = pd.to_numeric(df_clean[col].astype(str).str.rstrip('%'), errors='coerce') / 100

    # Convert numeric columns to proper types
    numeric_cols = ['Games', 'PA', 'HR', 'Runs', 'RBI', 'SB', 'ISO', 'BABIP',
                   'AVG', 'OBP', 'SLG', 'wOBA', 'xwOBA', 'wRC_Plus', 'BaseRunning',
                   'Offense', 'Defense', 'WAR']

    for col in numeric_cols:
        if col in df_clean.columns:
            df_clean[col] = pd.to_numeric(df_clean[col], errors='coerce')

    return df_clean

def extract_table_data(soup):
    """Extract and clean table data from HTML"""
    toplevel = soup.find('div', class_="fg-data-grid table-type").extract()
    bottomlevel = toplevel.find('div', class_='table-wrapper-inner').extract()

    # Get raw dataframe
    dataframes = pd.read_html(bottomlevel.encode(), encoding="utf-8")
    df_raw = dataframes[0]  # Take the first table

    # Clean the column names
    df_clean = clean_fangraphs_hitting_columns(df_raw)

    return df_clean

def download_html_file(url):
    """Download HTML content from URL"""
    response = requests.get(url)
    if response.status_code != 200:
        print(f"Failed to download. Status code: {response.status_code}")
        return None
    return BeautifulSoup(response.content, 'lxml').extract()

def save_dataframe(dataframe, file_name):
    """Save dataframe to CSV with timestamp"""
    cwd = pathlib.Path().cwd()
    save_dir = cwd / "MLBstats"
    save_dir.mkdir(exist_ok=True)

    current_time = time.localtime()
    timestr = f"{current_time.tm_hour:02d}{current_time.tm_min:02d}{current_time.tm_sec:02d}"
    file_name_with_time = f"{file_name}{timestr}.csv"
    dump_path = save_dir / file_name_with_time

    dataframe.to_csv(dump_path, encoding="utf-8", index=False)

    # Also save without timestamp for consistent access
    clean_path = save_dir / f"{file_name}.csv"
    dataframe.to_csv(clean_path, encoding="utf-8", index=False)

    return dump_path, clean_path

if __name__ == "__main__":
    # Minimum number of at bats can be changed via the "qual=" option at end of URL
    url = 'https://www.fangraphs.com/leaders/major-league?pageitems=2000000000&qual=10'

    print("📊 Fetching and cleaning hitting data from FanGraphs...")

    soup = download_html_file(url)
    if soup:
        dataframe_clean = extract_table_data(soup)

        if dataframe_clean is not None and not dataframe_clean.empty:
            timestamped_path, clean_path = save_dataframe(dataframe_clean, "fangraph_hitting")

            print(f"✅ Clean hitting data saved to: {clean_path}")
            print(f"✅ Timestamped backup saved to: {timestamped_path}")
            print(f"📊 Data: {dataframe_clean.shape[0]} rows, {dataframe_clean.shape[1]} columns")

            # Show key columns
            key_cols = ['Name', 'Team', 'AVG', 'OBP', 'SLG', 'wOBA', 'wRC_Plus', 'WAR']
            available_cols = [col for col in key_cols if col in dataframe_clean.columns]
            print(f"🔍 Key columns: {available_cols}")

            # Show sample data
            print(f"\n📋 Sample hitting data:")
            print(dataframe_clean[available_cols].head(3).to_string())

        else:
            print("❌ Failed to extract hitting data")
    else:
        print("❌ Failed to download webpage")

    print("\n✅ Hitting data processing complete!")
