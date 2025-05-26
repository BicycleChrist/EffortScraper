import requests
from bs4 import BeautifulSoup
import pandas as pd
from pathlib import Path

url = "https://www.fangraphs.com/leaders/major-league?month=0&pos=all&stats=pit&type=1&qual=5&sortcol=20&sortdir=asc&pagenum=1&pageitems=2000000000"

def clean_fangraphs_columns(df):
    """
    Clean messy FanGraphs column headers efficiently
    """
    # Remove "Line Break" columns first
    line_break_cols = [col for col in df.columns if 'Line Break' in str(col)]
    df_clean = df.drop(columns=line_break_cols)

    # Simple column renaming - only the key stats we need
    rename_map = {
        'K/9K/9 - Strikeouts per 9 Innings ((SO*9)/IP)': 'K_9',
        'BB/9BB/9 - Walks per 9 Innings ((BB*9)/IP)': 'BB_9',
        'K/BBK/BB - Strikeout to Walk Ratio (SO/BB)': 'K_BB',
        'HR/9HR/9 - Home Runs per 9 Innings ((HR*9)/IP)': 'HR_9',
        'K%K% - Strikeout Percentage (SO/TBF)': 'K_Pct',
        'BB%BB% - Walk Percentage (BB/TBF)': 'BB_Pct',
        'WHIPWHIP - Walks + Hits divided by Innings Pitched': 'WHIP',
        'AVGAVG - Batting Average Against': 'AVG_Against',
        'ERA-ERA- - ERA adjusted for park and league where 100 is average and lower is better': 'ERA_Minus',
        'FIP-FIP- - FIP adjusted for park and league where 100 is average and lower is better': 'FIP_Minus',
        'xFIP-xFIP- - xFIP adjusted by league where 100 is average and lower is better': 'xFIP_Minus',
        'ERAERA - Earned Run Average ((ER*9)/IP)': 'ERA',
        'FIPFIP - Fielder Independent Pitching on an ERA scale': 'FIP',
        'xFIPxFIP - Expected Fielder Independent Pitching where Home Runs are calculated as 10.5% of Fly Balls induced': 'xFIP'
    }

    # Rename columns
    df_clean = df_clean.rename(columns=rename_map)

    # Convert percentage columns
    for col in ['K_Pct', 'BB_Pct']:
        if col in df_clean.columns:
            df_clean[col] = pd.to_numeric(df_clean[col].astype(str).str.rstrip('%'), errors='coerce') / 100

    return df_clean

def Main():
    response = requests.get(url)

    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'lxml')
        toplevel = soup.find('div', class_='fg-data-grid table-type').extract()
        bottomlevel = toplevel.find('div', class_='table-wrapper-inner').extract()

        if toplevel:
            df = pd.read_html(str(bottomlevel))[0]

            output_folder = Path("MLBstats")
            output_folder.mkdir(exist_ok=True)

            # Clean and save only the clean version
            df_clean = clean_fangraphs_columns(df)
            df_clean.to_csv(output_folder / "fangraphs_advpitching.csv", index=False)

            print("✅ Clean pitching data saved to MLBstats/fangraphs_advpitching.csv")
            print(f"📊 Data: {df_clean.shape[0]} rows, {df_clean.shape[1]} columns")
            print(f"🔍 Key columns: {[col for col in df_clean.columns if col in ['Name', 'Team', 'ERA', 'FIP', 'K_9', 'BB_9', 'ERA_Minus']]}")

            # Show sample of key pitcher stats
            print(f"\n📋 Sample pitcher data:")
            sample_cols = ['Name', 'Team', 'ERA', 'FIP', 'K_9', 'BB_9', 'ERA_Minus']
            available_cols = [col for col in sample_cols if col in df_clean.columns]
            print(df_clean[available_cols].head(3).to_string())

        else:
            print("No table found on the page.")
    else:
        print(f"Failed to retrieve the page. Status code: {response.status_code}")

if __name__ == "__main__":
    Main()
