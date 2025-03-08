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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.nba.com/',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    def __init__(self, season="2023-24", season_type="Regular Season"):
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
            print(f"With params: {params}")
            
            response = requests.get(url, headers=self.DEFAULT_HEADERS, params=params, timeout=10)
            
            # Print response info for debugging
            print(f"Response status: {response.status_code}")
            
            # Check for successful response
            if response.status_code == 200:
                data = response.json()
                
                # Extract data and create DataFrame
                result_set = data['resultSets'][0]
                df = pd.DataFrame(result_set['rowSet'], columns=result_set['headers'])
                
                # Cache the result
                self.cache[cache_key] = df
                return df
            else:
                print(f"Error: Received status code {response.status_code}")
                return pd.DataFrame()
                
        except Exception as e:
            print(f"Error fetching data: {e}")
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
