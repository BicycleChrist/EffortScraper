import requests
import pandas as pd
import time
from datetime import datetime

class SimpleNBAStatsClient:
    """
    A simplified client for accessing NBA.com's advanced stats API
    """
    # Big ups to chitown88 from SOF, thanks boss
    
    
    # Base URLs
    BASE_URL = "https://stats.nba.com/stats/"
    
    # Common request headers to mimic browser behavior
    DEFAULT_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
    }
    
    def __init__(self, season="2025-26", season_type="Regular Season"):
        """Initialize the NBA Stats client"""
        self.season = season
        self.season_type = season_type
        self.cache = {}  # Simple in-memory cache
    
    def get_passing_stats(self, per_mode="PerGame"):
        """Get passing statistics for all players"""
        url = f"{self.BASE_URL}leaguedashptstats"
        
        params = {
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PerMode': per_mode,
            'PlayerExperience': '',
            'PlayerOrTeam': 'Player',
            'PlayerPosition': '',
            'PtMeasureType': 'Passing',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'StarterBench': '',
            'TeamID': '0'
        }
        
        return self._fetch_data(url, params)
    
    def get_rebounding_stats(self, per_mode="PerGame"):
        """Get rebounding statistics for all players"""
        url = f"{self.BASE_URL}leaguedashptstats"
        
        params = {
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PerMode': per_mode,
            'PlayerExperience': '',
            'PlayerOrTeam': 'Player',
            'PlayerPosition': '',
            'PtMeasureType': 'Rebounding',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'StarterBench': '',
            'TeamID': '0'
        }
        
        return self._fetch_data(url, params)
    
    def get_drives_stats(self, per_mode="PerGame"):
        """Get drives statistics for all players"""
        url = f"{self.BASE_URL}leaguedashptstats"
        
        params = {
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PerMode': per_mode,
            'PlayerExperience': '',
            'PlayerOrTeam': 'Player',
            'PlayerPosition': '',
            'PtMeasureType': 'Drives',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'StarterBench': '',
            'TeamID': '0'
        }
        
        return self._fetch_data(url, params)
    
    def get_touches_stats(self, per_mode="PerGame"):
        """Get touches statistics for all players"""
        url = f"{self.BASE_URL}leaguedashptstats"
        
        params = {
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PerMode': per_mode,
            'PlayerExperience': '',
            'PlayerOrTeam': 'Player',
            'PlayerPosition': '',
            'PtMeasureType': 'Possessions',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'StarterBench': '',
            'TeamID': '0'
        }
        
        return self._fetch_data(url, params)
    
    def get_defense_stats(self, per_mode="PerGame"):
        """Get defensive statistics for all players"""
        url = f"{self.BASE_URL}leaguedashptstats"
        
        params = {
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PerMode': per_mode,
            'PlayerExperience': '',
            'PlayerOrTeam': 'Player',
            'PlayerPosition': '',
            'PtMeasureType': 'Defense',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'StarterBench': '',
            'TeamID': '0'
        }
        
        return self._fetch_data(url, params)
    
    def get_traditional_stats(self, per_mode="PerGame"):
        """Get traditional statistics for all players"""
        url = f"{self.BASE_URL}leaguedashplayerstats"
        
        params = {
            'College': '',
            'Conference': '',
            'Country': '',
            'DateFrom': '',
            'DateTo': '',
            'Division': '',
            'DraftPick': '',
            'DraftYear': '',
            'GameScope': '',
            'GameSegment': '',
            'Height': '',
            'LastNGames': '0',
            'LeagueID': '00',
            'Location': '',
            'MeasureType': 'Base',
            'Month': '0',
            'OpponentTeamID': '0',
            'Outcome': '',
            'PORound': '0',
            'PaceAdjust': 'N',
            'PerMode': per_mode,
            'Period': '0',
            'PlayerExperience': '',
            'PlayerPosition': '',
            'PlusMinus': 'N',
            'Rank': 'N',
            'Season': self.season,
            'SeasonSegment': '',
            'SeasonType': self.season_type,
            'ShotClockRange': '',
            'StarterBench': '',
            'TeamID': '0',
            'TwoWay': '0',
            'VsConference': '',
            'VsDivision': '',
            'Weight': ''
        }
        
        return self._fetch_data(url, params)
    
    def _fetch_data(self, url, params):
        """Fetch data from the NBA API with caching and retry logic"""
        # Generate cache key
        cache_key = f"{url}_{str(params)}"

        # Check cache first
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Add a small delay to avoid rate limiting
        time.sleep(0.6)

        try:
            print(f"Fetching data from: {url}")
            print(f"Season: {params.get('Season', 'N/A')}, MeasureType: {params.get('PtMeasureType', params.get('MeasureType', 'N/A'))}")

            response = requests.get(url, headers=self.DEFAULT_HEADERS, params=params, timeout=20)

            # Print response info for debugging
            print(f"Response status: {response.status_code}")

            # Check for successful response
            if response.status_code == 200:
                data = response.json()

                # Check if we have result sets
                if not data.get('resultSets') or not data['resultSets']:
                    print(f"Warning: No result sets in response for season {params.get('Season')}")
                    return pd.DataFrame()

                # Extract data and create DataFrame
                result_set = data['resultSets'][0]

                # Check if we have data
                if not result_set.get('rowSet'):
                    print(f"Warning: No data in result set for season {params.get('Season')}")
                    return pd.DataFrame()

                df = pd.DataFrame(result_set['rowSet'], columns=result_set['headers'])
                print(f"Successfully fetched {len(df)} rows")

                # Cache the result
                self.cache[cache_key] = df
                return df
            else:
                print(f"Error: Received status code {response.status_code}")
                print(f"Response: {response.text[:200]}")
                return pd.DataFrame()

        except requests.Timeout:
            print(f"Error: Request timed out after 20 seconds. The NBA stats API may be slow or down.")
            return pd.DataFrame()
        except requests.RequestException as e:
            print(f"Error fetching data (network issue): {e}")
            return pd.DataFrame()
        except Exception as e:
            print(f"Error fetching data: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
    
    def save_to_csv(self, df, filename=None):
        """Save DataFrame to CSV with timestamp if no filename provided"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"nba_stats_{timestamp}.csv"
        
        df.to_csv(filename, index=False)
        print(f"Data saved to {filename}")
        return filename


# Example usage
if __name__ == "__main__":
    client = SimpleNBAStatsClient()
    
    # Get passing stats
    print("\nFetching passing stats...")
    passing_df = client.get_passing_stats()
    if not passing_df.empty:
        print(f"Passing stats shape: {passing_df.shape}")
        print(f"Columns: {passing_df.columns.tolist()}")
        client.save_to_csv(passing_df, "nba_passing_stats.csv")
    
    # Get rebounding stats
    print("\nFetching rebounding stats...")
    rebounding_df = client.get_rebounding_stats()
    if not rebounding_df.empty:
        print(f"Rebounding stats shape: {rebounding_df.shape}")
        print(f"Columns: {rebounding_df.columns.tolist()}")
        client.save_to_csv(rebounding_df, "nba_rebounding_stats.csv")
    
    # Get touches stats
    print("\nFetching touches stats...")
    touches_df = client.get_touches_stats()
    if not touches_df.empty:
        print(f"Touches stats shape: {touches_df.shape}")
        client.save_to_csv(touches_df, "nba_touches_stats.csv")
    
    # Get traditional stats
    print("\nFetching traditional stats...")
    trad_df = client.get_traditional_stats()
    if not trad_df.empty:
        print(f"Traditional stats shape: {trad_df.shape}")
        client.save_to_csv(trad_df, "nba_traditional_stats.csv")
