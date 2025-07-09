"""
API-Sports Client for Multi-Sport Odds Retrieval
Supports: Baseball, Basketball, Football, Hockey, Formula-1, MMA, NBA
"""

import http.client
import json
from datetime import datetime
from typing import Dict, List, Optional, Union
from Creds import SAPI_KEY


class market_IDS:
    """
    Comprehensive OddIDs Library for SportsGameOdds API
    Based on official documentation for optimized entity usage
    """
    
    # ==================== BASKETBALL ODDS ====================
    
    # Game-Level Basketball Markets
    BASKETBALL_GAME_ODDS = [
        'points-all-game-ml-home',           # Moneyline home
        'points-all-game-ml-away',           # Moneyline away
        'points-all-game-sp-home',           # Spread home
        'points-all-game-sp-away',           # Spread away
        'points-all-game-ou-over',           # Total points over
        'points-all-game-ou-under',          # Total points under
        'points-home-game-ou-over',          # Team points home over
        'points-home-game-ou-under',         # Team points home under
        'points-away-game-ou-over',          # Team points away over
        'points-away-game-ou-under',         # Team points away under
        'points-all-game-eo-even',           # Total points even
        'points-all-game-eo-odd',            # Total points odd
        'points-home-game-eo-even',          # Team points home even
        'points-home-game-eo-odd',           # Team points home odd
        'points-away-game-eo-even',          # Team points away even
        'points-away-game-eo-odd',           # Team points away odd
    ]
    
    # Basketball Quarter Markets (1st, 2nd, 3rd, 4th)
    BASKETBALL_QUARTER_ODDS = [
        'points-all-1q-ml-home',             # 1st quarter moneyline home
        'points-all-1q-ml-away',             # 1st quarter moneyline away
        'points-all-1q-sp-home',             # 1st quarter spread home
        'points-all-1q-ou-over',             # 1st quarter total over
        'points-all-2q-ml-home',             # 2nd quarter moneyline home
        'points-all-2q-ml-away',             # 2nd quarter moneyline away
        'points-all-3q-ml-home',             # 3rd quarter moneyline home
        'points-all-3q-ml-away',             # 3rd quarter moneyline away
        'points-all-4q-ml-home',             # 4th quarter moneyline home
        'points-all-4q-ml-away',             # 4th quarter moneyline away
    ]
    
    # Basketball Half Markets
    BASKETBALL_HALF_ODDS = [
        'points-all-1h-ml-home',             # 1st half moneyline home
        'points-all-1h-ml-away',             # 1st half moneyline away
        'points-all-1h-sp-home',             # 1st half spread home
        'points-all-1h-ou-over',             # 1st half total over
        'points-all-2h-ml-home',             # 2nd half moneyline home
        'points-all-2h-ml-away',             # 2nd half moneyline away
        'points-all-2h-sp-home',             # 2nd half spread home
        'points-all-2h-ou-over',             # 2nd half total over
    ]
    
    # Basketball Player Props (use PLAYER_ID wildcard)
    BASKETBALL_PLAYER_PROPS = [
        'points-PLAYER_ID-game-ou-over',              # Player points over
        'rebounds-PLAYER_ID-game-ou-over',            # Player rebounds over
        'assists-PLAYER_ID-game-ou-over',             # Player assists over
        'blocks-PLAYER_ID-game-ou-over',              # Player blocks over
        'steals-PLAYER_ID-game-ou-over',              # Player steals over
        'turnovers-PLAYER_ID-game-ou-over',           # Player turnovers over
        'fouls-PLAYER_ID-game-ou-over',               # Player fouls over
        'fieldGoalsMade-PLAYER_ID-game-ou-over',      # Player FG made over
        'threePointersMade-PLAYER_ID-game-ou-over',   # Player 3PM over
        'freeThrowsMade-PLAYER_ID-game-ou-over',      # Player FTM over
        'pointsRebounds-PLAYER_ID-game-ou-over',      # Player pts+reb over
        'pointsAssists-PLAYER_ID-game-ou-over',       # Player pts+ast over
        'reboundsAssists-PLAYER_ID-game-ou-over',     # Player reb+ast over
        'pointsReboundsAssists-PLAYER_ID-game-ou-over', # Player pts+reb+ast over
        'blocksSeals-PLAYER_ID-game-ou-over',         # Player blk+stl over
        'fantasyScore-PLAYER_ID-game-ou-over',        # Player fantasy over
        'minutesPlayed-PLAYER_ID-game-ou-over',       # Player minutes over
    ]
    
    # Basketball Special Markets
    BASKETBALL_SPECIAL_ODDS = [
        'firstBasket-PLAYER_ID-game-yn-yes',          # First basket
        'firstScore-PLAYER_ID-game-yn-yes',           # First score
        'doubleDouble-PLAYER_ID-game-yn-yes',         # Double-double
        'tripleDouble-PLAYER_ID-game-yn-yes',         # Triple-double
    ]
    
    # ==================== BASEBALL ODDS ====================
    
    # Game-Level Baseball Markets
    BASEBALL_GAME_ODDS = [
        'points-all-game-ml-home',           # Moneyline home
        'points-all-game-ml-away',           # Moneyline away
        'points-all-game-sp-home',           # Spread (run line) home
        'points-all-game-sp-away',           # Spread (run line) away
        'points-all-game-ou-over',           # Total runs over
        'points-all-game-ou-under',          # Total runs under
        'points-home-game-ou-over',          # Team runs home over
        'points-home-game-ou-under',         # Team runs home under
        'points-away-game-ou-over',          # Team runs away over
        'points-away-game-ou-under',         # Team runs away under
        'points-all-game-eo-even',           # Total runs even
        'points-all-game-eo-odd',            # Total runs odd
        'points-home-game-eo-even',          # Team runs home even
        'points-home-game-eo-odd',           # Team runs home odd
        'points-away-game-eo-even',          # Team runs away even
        'points-away-game-eo-odd',           # Team runs away odd
    ]
    
    # Baseball Inning Markets (1st-9th innings)
    BASEBALL_INNING_ODDS = [
        'points-all-1i-ml-home',             # 1st inning moneyline home
        'points-all-1i-ml-away',             # 1st inning moneyline away
        'points-all-1i-ou-over',             # 1st inning total over
        'points-all-2i-ou-over',             # 2nd inning total over
        'points-all-3i-ou-over',             # 3rd inning total over
        'points-all-4i-ou-over',             # 4th inning total over
        'points-all-5i-ou-over',             # 5th inning total over
        'points-all-6i-ou-over',             # 6th inning total over
        'points-all-7i-ou-over',             # 7th inning total over
        'points-all-8i-ou-over',             # 8th inning total over
        'points-all-9i-ou-over',             # 9th inning total over
    ]
    
    # Baseball Half Markets
    BASEBALL_HALF_ODDS = [
        'points-all-1h-ml-home',             # 1st half (1-5 innings) moneyline home
        'points-all-1h-ml-away',             # 1st half moneyline away
        'points-all-1h-ou-over',             # 1st half total over
        'points-all-2h-ml-home',             # 2nd half (6-9 innings) moneyline home
        'points-all-2h-ml-away',             # 2nd half moneyline away
        'points-all-2h-ou-over',             # 2nd half total over
        'points-all-1ix7-ml-home',           # First 7 innings moneyline home
        'points-all-1ix7-ml-away',           # First 7 innings moneyline away
        'points-all-1ix7-ou-over',           # First 7 innings total over
    ]
    
    # Baseball Player Props
    BASEBALL_PLAYER_PROPS = [
        'points-PLAYER_ID-game-ou-over',              # Player runs over
        'batting_homeRuns-PLAYER_ID-game-ou-over',    # Player home runs over
        'batting_homeRuns-PLAYER_ID-game-yn-yes',     # Player anytime HR
        'pitching_strikeouts-PLAYER_ID-game-ou-over', # Pitcher strikeouts over
        'batting_hits-PLAYER_ID-game-ou-over',        # Player hits over
        'batting_rbi-PLAYER_ID-game-ou-over',         # Player RBIs over
        'batting_stolenBases-PLAYER_ID-game-ou-over', # Player stolen bases over
        'pitching_walks-PLAYER_ID-game-ou-over',      # Pitcher walks over
        'pitching_hitsAllowed-PLAYER_ID-game-ou-over', # Pitcher hits allowed over
    ]
    
    # ==================== FOOTBALL ODDS ====================
    
    # Game-Level Football Markets
    FOOTBALL_GAME_ODDS = [
        'points-all-game-ml-home',           # Moneyline home
        'points-all-game-ml-away',           # Moneyline away
        'points-all-game-sp-home',           # Spread home
        'points-all-game-sp-away',           # Spread away
        'points-all-game-ou-over',           # Total points over
        'points-all-game-ou-under',          # Total points under
        'points-home-game-ou-over',          # Team points home over
        'points-home-game-ou-under',         # Team points home under
        'points-away-game-ou-over',          # Team points away over
        'points-away-game-ou-under',         # Team points away under
        'touchdowns-all-game-ou-over',       # Total touchdowns over
        'touchdowns-home-game-ou-over',      # Team touchdowns home over
        'touchdowns-away-game-ou-over',      # Team touchdowns away over
    ]
    
    # Football Quarter Markets
    FOOTBALL_QUARTER_ODDS = [
        'points-all-1q-ml-home',             # 1st quarter moneyline home
        'points-all-1q-ml-away',             # 1st quarter moneyline away
        'points-all-1q-sp-home',             # 1st quarter spread home
        'points-all-1q-ou-over',             # 1st quarter total over
        'points-all-2q-ml-home',             # 2nd quarter moneyline home
        'points-all-3q-ml-home',             # 3rd quarter moneyline home
        'points-all-4q-ml-home',             # 4th quarter moneyline home
    ]
    
    # Football Half Markets
    FOOTBALL_HALF_ODDS = [
        'points-all-1h-ml-home',             # 1st half moneyline home
        'points-all-1h-ml-away',             # 1st half moneyline away
        'points-all-1h-sp-home',             # 1st half spread home
        'points-all-1h-ou-over',             # 1st half total over
        'points-all-2h-ml-home',             # 2nd half moneyline home
        'points-all-2h-ml-away',             # 2nd half moneyline away
    ]
    
    # Football Player Props
    FOOTBALL_PLAYER_PROPS = [
        'points-PLAYER_ID-game-ou-over',                    # Player points over
        'touchdowns-PLAYER_ID-game-ou-over',                # Player touchdowns over
        'rushing_receivingYards-PLAYER_ID-game-ou-over',    # Rush+rec yards over
        'turnovers-PLAYER_ID-game-ou-over',                 # Player turnovers over
        'receiving_touchdowns-PLAYER_ID-game-ou-over',      # Receiving TDs over
        'receiving_yards-PLAYER_ID-game-ou-over',           # Receiving yards over
        'receiving_receptions-PLAYER_ID-game-ou-over',      # Receptions over
        'rushing_yards-PLAYER_ID-game-ou-over',             # Rushing yards over
        'passing_yards-PLAYER_ID-game-ou-over',             # Passing yards over
        'passing_touchdowns-PLAYER_ID-game-ou-over',        # Passing TDs over
        'firstScore-PLAYER_ID-game-yn-yes',                 # First score
        'firstTouchdown-PLAYER_ID-game-yn-yes',             # First touchdown
        'lastTouchdown-PLAYER_ID-game-yn-yes',              # Last touchdown
    ]
    
    # ==================== HOCKEY ODDS ====================
    
    # Game-Level Hockey Markets
    HOCKEY_GAME_ODDS = [
        'points-all-game-ml-home',           # Moneyline home
        'points-all-game-ml-away',           # Moneyline away
        'points-all-game-3way-home',         # 3-way moneyline home
        'points-all-game-3way-away',         # 3-way moneyline away
        'points-all-game-3way-tie',          # 3-way moneyline tie
        'points-all-game-sp-home',           # Spread (puck line) home
        'points-all-game-sp-away',           # Spread (puck line) away
        'points-all-game-ou-over',           # Total goals over
        'points-all-game-ou-under',          # Total goals under
        'points-home-game-ou-over',          # Team goals home over
        'points-home-game-ou-under',         # Team goals home under
        'points-away-game-ou-over',          # Team goals away over
        'points-away-game-ou-under',         # Team goals away under
        'points-all-game-eo-even',           # Total goals even
        'points-all-game-eo-odd',            # Total goals odd
        'points-all-game-yn-yes',            # Any goals yes
    ]
    
    # Hockey Period Markets (1st, 2nd, 3rd periods)
    HOCKEY_PERIOD_ODDS = [
        'points-all-1p-ml-home',             # 1st period moneyline home
        'points-all-1p-ml-away',             # 1st period moneyline away
        'points-all-1p-3way-home',           # 1st period 3-way home
        'points-all-1p-ou-over',             # 1st period total over
        'points-all-2p-ml-home',             # 2nd period moneyline home
        'points-all-2p-ml-away',             # 2nd period moneyline away
        'points-all-3p-ml-home',             # 3rd period moneyline home
        'points-all-3p-ml-away',             # 3rd period moneyline away
    ]
    
    # Hockey Regulation Markets
    HOCKEY_REGULATION_ODDS = [
        'points-all-reg-ml-home',            # Regulation moneyline home
        'points-all-reg-ml-away',            # Regulation moneyline away
        'points-all-reg-3way-home',          # Regulation 3-way home
        'points-all-reg-ou-over',            # Regulation total over
    ]
    
    # Hockey Player Props
    HOCKEY_PLAYER_PROPS = [
        'points-PLAYER_ID-game-ou-over',            # Player points (goals+assists) over
        'goals-PLAYER_ID-game-ou-over',             # Player goals over
        'assists-PLAYER_ID-game-ou-over',           # Player assists over
        'shotsOnGoal-PLAYER_ID-game-ou-over',       # Player shots on goal over
        'hits-PLAYER_ID-game-ou-over',              # Player hits over
        'blockedShots-PLAYER_ID-game-ou-over',      # Player blocked shots over
        'powerPlayPoints-PLAYER_ID-game-ou-over',   # Player PP points over
        'faceoffsWon-PLAYER_ID-game-ou-over',       # Player faceoffs won over
        'minutesPlayed-PLAYER_ID-game-ou-over',     # Player minutes over
        'firstGoal-PLAYER_ID-game-yn-yes',          # First goal scorer
        'lastGoal-PLAYER_ID-game-yn-yes',           # Last goal scorer
    ]
    
    # Hockey Goalie Props
    HOCKEY_GOALIE_PROPS = [
        'saves-PLAYER_ID-game-ou-over',             # Goalie saves over
        'goalsAgainst-PLAYER_ID-game-ou-over',      # Goalie goals against over
        'saves-PLAYER_ID-game-eo-even',             # Goalie saves even
    ]
    
    # ==================== PRESET COMBINATIONS ====================
    
    # Essential odds for each sport (minimal entity usage)
    ESSENTIAL_BASKETBALL = [
        'points-all-game-ml-home',
        'points-all-game-sp-home', 
        'points-all-game-ou-over'
    ]
    
    ESSENTIAL_BASEBALL = [
        'points-all-game-ml-home',
        'points-all-game-sp-home',
        'points-all-game-ou-over'
    ]
    
    ESSENTIAL_FOOTBALL = [
        'points-all-game-ml-home',
        'points-all-game-sp-home',
        'points-all-game-ou-over'
    ]
    
    ESSENTIAL_HOCKEY = [
        'points-all-game-ml-home',
        'points-all-game-ou-over',
        'points-all-game-3way-home'
    ]
    
    # Popular player props (moderate entity usage)
    POPULAR_BASKETBALL_PROPS = [
        'points-PLAYER_ID-game-ou-over',
        'rebounds-PLAYER_ID-game-ou-over',
        'assists-PLAYER_ID-game-ou-over'
    ]
    
    POPULAR_BASEBALL_PROPS = [
        'batting_homeRuns-PLAYER_ID-game-ou-over',
        'pitching_strikeouts-PLAYER_ID-game-ou-over',
        'batting_hits-PLAYER_ID-game-ou-over'
    ]
    
    POPULAR_FOOTBALL_PROPS = [
        'receiving_yards-PLAYER_ID-game-ou-over',
        'rushing_yards-PLAYER_ID-game-ou-over',
        'passing_yards-PLAYER_ID-game-ou-over'
    ]
    
    POPULAR_HOCKEY_PROPS = [
        'points-PLAYER_ID-game-ou-over',
        'goals-PLAYER_ID-game-ou-over',
        'assists-PLAYER_ID-game-ou-over'
    ]
    
    # Comprehensive sets (high entity usage - use sparingly)
    ALL_BASKETBALL_ODDS = (BASKETBALL_GAME_ODDS + BASKETBALL_QUARTER_ODDS + 
                          BASKETBALL_HALF_ODDS + BASKETBALL_SPECIAL_ODDS)
    
    ALL_BASEBALL_ODDS = (BASEBALL_GAME_ODDS + BASEBALL_INNING_ODDS + 
                        BASEBALL_HALF_ODDS)
    
    ALL_FOOTBALL_ODDS = (FOOTBALL_GAME_ODDS + FOOTBALL_QUARTER_ODDS + 
                        FOOTBALL_HALF_ODDS)
    
    ALL_HOCKEY_ODDS = (HOCKEY_GAME_ODDS + HOCKEY_PERIOD_ODDS + 
                      HOCKEY_REGULATION_ODDS)


class APISportsClient:
    """
    Client for interacting with API-Sports endpoints across multiple sports.
    
    Supported sports:
    - Baseball (v1)
    - Basketball (v1) 
    - Football (v3)
    - Hockey (v1)
    - Formula-1 (v1)
    - MMA (v1)
    - NBA (v2)
    """
    
    # Sport configurations with their API versions and hostnames
    SPORTS_CONFIG = {
        'baseball': {
            'version': 'v1',
            'host': 'v1.baseball.api-sports.io',
            'mlb_league_id': 1
        },
        'basketball': {
            'version': 'v1',
            'host': 'v1.basketball.api-sports.io'
        },
        'football': {
            'version': 'v3',
            'host': 'v3.football.api-sports.io'
        },
        'hockey': {
            'version': 'v1',
            'host': 'v1.hockey.api-sports.io'
        },
        'formula1': {
            'version': 'v1',
            'host': 'v1.formula-1.api-sports.io'
        },
        'mma': {
            'version': 'v1',
            'host': 'v1.mma.api-sports.io'
        },
        'nba': {
            'version': 'v2',
            'host': 'v2.nba.api-sports.io'
        }
    }
    
    def __init__(self):
        """Initialize the API-Sports client."""
        self.api_key = SAPI_KEY
        
    def _make_request(self, sport: str, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """
        Make a request to the API-Sports endpoint.
        
        Args:
            sport: Sport name (e.g., 'baseball', 'basketball')
            endpoint: API endpoint (e.g., '/games', '/odds')
            params: Query parameters as dictionary
            
        Returns:
            JSON response as dictionary
        """
        if sport not in self.SPORTS_CONFIG:
            raise ValueError(f"Unsupported sport: {sport}. Supported: {list(self.SPORTS_CONFIG.keys())}")
            
        config = self.SPORTS_CONFIG[sport]
        
        # Build query string
        query_string = ""
        if params:
            query_params = []
            for key, value in params.items():
                if value is not None:
                    query_params.append(f"{key}={value}")
            if query_params:
                query_string = "?" + "&".join(query_params)
        
        # Create connection
        conn = http.client.HTTPSConnection(config['host'])
        
        headers = {
            'x-rapidapi-host': config['host'],
            'x-rapidapi-key': self.api_key
        }
        
        try:
            conn.request("GET", f"{endpoint}{query_string}", "", headers)
            res = conn.getresponse()
            data = res.read()
            
            return json.loads(data.decode('utf-8'))
            
        except Exception as e:
            return {
                'error': f"Request failed: {str(e)}",
                'sport': sport,
                'endpoint': endpoint,
                'params': params
            }
        finally:
            conn.close()
    
    def get_games(self, sport: str, date: Optional[str] = None, league: Optional[int] = None, 
                  season: Optional[int] = None, timezone: Optional[str] = None) -> Dict:
        """
        Get games for a specific sport and date.
        
        Args:
            sport: Sport name
            date: Date in YYYY-MM-DD format (defaults to today)
            league: League ID (optional)
            season: Season year (optional)
            timezone: Timezone (e.g., 'America/Los_Angeles', 'America/New_York')
            
        Returns:
            Games data from API
            
        Note:
            IMPORTANT: For best results, use only the date parameter. Adding season or timezone
            parameters can cause API conflicts and return errors. Use just date for current data.
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
            
        params = {
            'date': date,
            'league': league,
            'season': season,
            'timezone': timezone
        }
        
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        
        return self._make_request(sport, '/games', params)
    
    def get_odds(self, sport: str, game_id: int, bookmaker: Optional[int] = None, 
                 bet: Optional[int] = None) -> Dict:
        """
        Get odds for a specific game.
        
        Args:
            sport: Sport name
            game_id: Game ID
            bookmaker: Specific bookmaker ID (optional)
            bet: Specific bet type ID (optional)
            
        Returns:
            Odds data from API
            
        Note:
            API may return misleading "rate limit" errors when odds aren't available yet 
            for future games. Odds are typically only posted for same-day games.
        """
        params = {
            'game': game_id,
            'bookmaker': bookmaker,
            'bet': bet
        }
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        return self._make_request(sport, '/odds', params)
    
    def get_leagues(self, sport: str) -> Dict:
        """
        Get available leagues for a sport.
        
        Args:
            sport: Sport name
            
        Returns:
            Leagues data from API
        """
        return self._make_request(sport, '/leagues')
    
    def get_today_games_with_odds(self, sport: str, league_id: Optional[int] = None, timezone: Optional[str] = None) -> Dict:
        """
        Get today's games for a sport and their odds (if available).
        
        Args:
            sport: Sport name
            league_id: Specific league ID to filter by
            timezone: Timezone for date queries
            
        Returns:
            Dictionary with games and their odds
        """
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Get today's games
        games_response = self.get_games(sport, date=today, league=league_id, timezone=timezone)
        
        if 'error' in games_response:
            return games_response
            
        result = {
            'date': today,
            'sport': sport,
            'games_data': games_response,
            'games_with_odds': []
        }
        
        if 'response' in games_response and games_response['response']:
            for game in games_response['response']:
                game_id = game.get('id')
                if game_id:
                    # Get odds for this game
                    odds_response = self.get_odds(sport, game_id)
                    
                    game_with_odds = {
                        'game': game,
                        'odds': odds_response
                    }
                    result['games_with_odds'].append(game_with_odds)
        
        return result
    
    def get_upcoming_games(self, sport: str, league_id: Optional[int] = None, timezone: Optional[str] = None) -> List[Dict]:
        """
        Get upcoming (not started yet) games for today.
        
        Args:
            sport: Sport name
            league_id: Specific league ID to filter by
            timezone: Timezone (e.g., 'America/Los_Angeles')
            
        Returns:
            List of upcoming games that haven't started yet
        """
        import time
        games_response = self.get_games(sport, league=league_id, timezone=timezone)
        current_timestamp = int(time.time())
        
        upcoming_games = []
        if 'response' in games_response and games_response['response']:
            for game in games_response['response']:
                status = game.get('status', {}).get('short', '')
                timestamp = game.get('timestamp', 0)
                
                # True upcoming games: NS (Not Started) status
                is_upcoming = status == 'NS'
                
                if is_upcoming:
                    # Also filter by league if specified
                    if league_id is None or game.get('league', {}).get('id') == league_id:
                        upcoming_games.append(game)
        
        return upcoming_games
    
    # Sport-specific convenience methods
    def get_mlb_games_today(self, timezone: str = 'America/Los_Angeles') -> Dict:
        """Get today's MLB games with odds in specified timezone (default PST)."""
        return self.get_today_games_with_odds('baseball', league_id=1, timezone=timezone)
    
    def get_nba_games_today(self) -> Dict:
        """Get today's NBA games with odds."""
        return self.get_today_games_with_odds('nba')
    
    def get_nhl_games_today(self) -> Dict:
        """Get today's NHL games with odds."""
        return self.get_today_games_with_odds('hockey')
    
    def get_football_games_today(self, league_id: Optional[int] = None) -> Dict:
        """Get today's football games with odds."""
        return self.get_today_games_with_odds('football', league_id=league_id)
    
    def get_basketball_games_today(self, league_id: Optional[int] = None) -> Dict:
        """Get today's basketball games with odds."""
        return self.get_today_games_with_odds('basketball', league_id=league_id)
    
    def get_mma_events_today(self) -> Dict:
        """Get today's MMA events with odds."""
        return self.get_today_games_with_odds('mma')
    
    def get_f1_races_today(self) -> Dict:
        """Get today's Formula-1 races with odds."""
        return self.get_today_games_with_odds('formula1')
    
    # Player prop betting methods
    PLAYER_PROP_BETS = {
        49: "Player Total Bases",
        50: "Player Singles", 
        52: "Player Doubles",
        53: "Player Home Runs",
        54: "Player Triples",
        55: "Player Stolen Bases",
        73: "Player Runs",
        76: "Player Hits",
        77: "Player Runs Batted In"
    }
    
    def get_player_props(self, sport: str, game_id: int, bet_type_id: int = 53) -> Dict:
        """
        Get player prop odds for a specific game.
        
        Args:
            sport: Sport name
            game_id: Game ID
            bet_type_id: Player prop bet type ID (default: 53 = Player Home Runs)
            
        Returns:
            Player prop odds data
        """
        if bet_type_id not in self.PLAYER_PROP_BETS:
            raise ValueError(f"Invalid player prop bet type ID: {bet_type_id}. Valid IDs: {list(self.PLAYER_PROP_BETS.keys())}")
        
        return self.get_odds(sport, game_id, bet=bet_type_id)
    
    def get_all_player_props(self, sport: str, game_id: int, delay: float = 6.0) -> Dict:
        """
        Get all player prop types for a game (with rate limiting).
        
        Args:
            sport: Sport name
            game_id: Game ID
            delay: Delay between requests in seconds (default: 6s for 10/min limit)
            
        Returns:
            Dictionary with all player prop data
        """
        import time
        
        result = {
            'game_id': game_id,
            'sport': sport,
            'player_props': {}
        }
        
        for bet_id, bet_name in self.PLAYER_PROP_BETS.items():
            print(f"Getting {bet_name} (ID: {bet_id})...")
            
            props = self.get_odds(sport, game_id, bet=bet_id)
            result['player_props'][bet_id] = {
                'bet_name': bet_name,
                'data': props
            }
            
            # Rate limiting - wait between requests
            if delay > 0:
                time.sleep(delay)
        
        return result
    
    def print_game_summary(self, sport: str, league_id: Optional[int] = None) -> None:
        """
        Print a summary of today's games for a sport.
        
        Args:
            sport: Sport name
            league_id: Optional league filter
        """
        upcoming = self.get_upcoming_games(sport, league_id)
        
        print(f"\n=== {sport.upper()} Games Summary ===")
        print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
        print(f"Upcoming games: {len(upcoming)}")
        
        for i, game in enumerate(upcoming, 1):
            teams = game.get('teams', {})
            status = game.get('status', {})
            league_name = game.get('league', {}).get('name', 'Unknown League')
            
            home_team = teams.get('home', {}).get('name', 'Unknown')
            away_team = teams.get('away', {}).get('name', 'Unknown')
            game_status = status.get('short', 'Unknown')
            
            print(f"{i}. {away_team} @ {home_team} ({league_name}) - Status: {game_status}")


# Example usage (commented out to avoid token usage)
if __name__ == "__main__":
    client = APISportsClient()
    
    # Print summaries for different sports
    # client.print_game_summary('baseball', league_id=1)  # MLB
    # client.print_game_summary('basketball')
    # client.print_game_summary('hockey')
    # client.print_game_summary('football')
    
    # Get specific odds
    mlb_data = client.get_mlb_games_today()
    print(json.dumps(mlb_data, indent=2))
