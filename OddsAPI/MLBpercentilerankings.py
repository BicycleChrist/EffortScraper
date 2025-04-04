import requests
import pandas as pd
import io

# Baseball Savant percentile leaderboard URLs
PITCHER_URL = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings?type=pitcher&csv=true"
HITTER_URL = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings?type=hitter&csv=true"

def fetch_leaderboard_data(url):
    """
    Downloads Baseball Savant leaderboard data.

    Parameters:
    -----------
    url : str
        Full CSV API URL

    Returns:
    --------
    pd.DataFrame
        Parsed leaderboard data
    """
    print(f"Fetching data from: {url}")
    response = requests.get(url)
    if response.status_code == 200:
        df = pd.read_csv(io.StringIO(response.text))
        df.columns = df.columns.str.strip()  # Clean column names
        print(f"✅ Retrieved {len(df)} rows")
        return df
    else:
        print(f"❌ Failed to fetch data: HTTP {response.status_code}")
        return None

def save_data(df, filename, label):
    """
    Saves full leaderboard data to CSV and prints first few rows.

    Parameters:
    -----------
    df : pd.DataFrame
        Full leaderboard dataframe
    filename : str
        Output filename
    label : str
        Descriptive label for logging
    """
    if df is not None:
        print(f"\n📊 Top 10 {label}:")
        print(df.head(10))
        df.to_csv(filename, index=False)
        print(f"📁 Saved to '{filename}'")

def main():
    print("⚾ Fetching MLB Percentile Rankings...")

    pitcher_df = fetch_leaderboard_data(PITCHER_URL)
    hitter_df = fetch_leaderboard_data(HITTER_URL)

    save_data(pitcher_df, "pitcher_percentiles_raw.csv", "Pitchers")
    save_data(hitter_df, "hitter_percentiles_raw.csv", "Hitters")

    print("\n✅ All data downloaded and saved.")

if __name__ == "__main__":
    main()
