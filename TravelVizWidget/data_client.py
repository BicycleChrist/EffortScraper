import requests
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from PyQt6.QtCore import QObject, pyqtSignal
from bs4 import BeautifulSoup
import re


# Update mapping for LA kings (schedule url uses la not lak)

class GameStatus(Enum):
    """Game status enumeration"""
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress" 
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


@dataclass
class TeamInfo:
    """Team information"""
    team_id: str
    abbreviation: str
    display_name: str
    location: str
    color: str
    alternate_color: str
    logo_url: Optional[str] = None
    division: Optional[str] = None
    league: Optional[str] = None
    conference: Optional[str] = None


@dataclass
class Venue:
    """Venue information"""
    venue_id: str
    name: str
    city: str
    state: str
    country: str
    latitude: float
    longitude: float
    capacity: Optional[int] = None
    timezone: Optional[str] = None


@dataclass
class GameData:
    """Individual game data"""
    game_id: str
    date: datetime
    home_team: TeamInfo
    away_team: TeamInfo
    venue: Venue
    status: GameStatus
    week: Optional[int] = None
    season_type: Optional[str] = None
    league: str = "MLB"
    season: Optional[str] = None
    series_description: Optional[str] = None


@dataclass
class TeamTravelData:
    """Team travel data inferred from schedule"""
    team_name: str
    team_id: str
    departure_city: str
    arrival_city: str
    game_date: datetime
    travel_date: datetime
    departure_airport: str
    arrival_airport: str
    confidence: str = "schedule_inferred"
    game_id: Optional[str] = None
    opponent: Optional[str] = None
    series_game_number: Optional[int] = None
    homestand_game_number: Optional[int] = None


class ESPNScheduleScraper:
    """Scraper for ESPN team schedule pages supporting MLB, NBA, and NHL with proper MLB half handling"""
    
    def __init__(self):
        # League-specific URL patterns - MLB requires half parameter, others don't
        self.url_patterns = {
            'MLB': "https://www.espn.com/mlb/team/schedule/_/name/{team}/seasontype/2/half/{half}",
            'NBA': "https://www.espn.com/nba/team/schedule/_/name/{team}/seasontype/2",
            'NHL': "https://www.espn.com/nhl/team/schedule/_/name/{team}/seasontype/2"
        }
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Initialize league-specific data
        self.team_airports = self.load_team_airports()
        self.league_teams = self.get_all_league_teams()
        self.city_coordinates = self.load_city_coordinates()
    
    def get_all_league_teams(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Get all teams for all supported leagues"""
        return {
            'MLB': self.get_mlb_teams(),
            'NBA': self.get_nba_teams(),
            'NHL': self.get_nhl_teams()
        }
    
    def get_mlb_teams(self) -> Dict[str, Dict[str, str]]:
        """Get MLB team abbreviations and info for ESPN URLs"""
        return {
            # American League East
            "bal": {"name": "Baltimore Orioles", "city": "Baltimore", "division": "AL East", "conference": "American League"},
            "bos": {"name": "Boston Red Sox", "city": "Boston", "division": "AL East", "conference": "American League"},
            "nyy": {"name": "New York Yankees", "city": "New York", "division": "AL East", "conference": "American League"},
            "tb": {"name": "Tampa Bay Rays", "city": "Tampa", "division": "AL East", "conference": "American League"},
            "tor": {"name": "Toronto Blue Jays", "city": "Toronto", "division": "AL East", "conference": "American League"},
            
            # American League Central
            "chw": {"name": "Chicago White Sox", "city": "Chicago", "division": "AL Central", "conference": "American League"},
            "cle": {"name": "Cleveland Guardians", "city": "Cleveland", "division": "AL Central", "conference": "American League"},
            "det": {"name": "Detroit Tigers", "city": "Detroit", "division": "AL Central", "conference": "American League"},
            "kc": {"name": "Kansas City Royals", "city": "Kansas City", "division": "AL Central", "conference": "American League"},
            "min": {"name": "Minnesota Twins", "city": "Minneapolis", "division": "AL Central", "conference": "American League"},
            
            # American League West
            "hou": {"name": "Houston Astros", "city": "Houston", "division": "AL West", "conference": "American League"},
            "laa": {"name": "Los Angeles Angels", "city": "Los Angeles", "division": "AL West", "conference": "American League"},
            "ath": {"name": "Athletics", "city": "Sacramento", "division": "AL West", "conference": "American League"},
            "sea": {"name": "Seattle Mariners", "city": "Seattle", "division": "AL West", "conference": "American League"},
            "tex": {"name": "Texas Rangers", "city": "Dallas", "division": "AL West", "conference": "American League"},
            
            # National League East
            "atl": {"name": "Atlanta Braves", "city": "Atlanta", "division": "NL East", "conference": "National League"},
            "mia": {"name": "Miami Marlins", "city": "Miami", "division": "NL East", "conference": "National League"},
            "nym": {"name": "New York Mets", "city": "New York", "division": "NL East", "conference": "National League"},
            "phi": {"name": "Philadelphia Phillies", "city": "Philadelphia", "division": "NL East", "conference": "National League"},
            "wsh": {"name": "Washington Nationals", "city": "Washington", "division": "NL East", "conference": "National League"},
            
            # National League Central
            "chc": {"name": "Chicago Cubs", "city": "Chicago", "division": "NL Central", "conference": "National League"},
            "cin": {"name": "Cincinnati Reds", "city": "Cincinnati", "division": "NL Central", "conference": "National League"},
            "mil": {"name": "Milwaukee Brewers", "city": "Milwaukee", "division": "NL Central", "conference": "National League"},
            "pit": {"name": "Pittsburgh Pirates", "city": "Pittsburgh", "division": "NL Central", "conference": "National League"},
            "stl": {"name": "St. Louis Cardinals", "city": "St. Louis", "division": "NL Central", "conference": "National League"},
            
            # National League West
            "ari": {"name": "Arizona Diamondbacks", "city": "Phoenix", "division": "NL West", "conference": "National League"},
            "col": {"name": "Colorado Rockies", "city": "Denver", "division": "NL West", "conference": "National League"},
            "lad": {"name": "Los Angeles Dodgers", "city": "Los Angeles", "division": "NL West", "conference": "National League"},
            "sd": {"name": "San Diego Padres", "city": "San Diego", "division": "NL West", "conference": "National League"},
            "sf": {"name": "San Francisco Giants", "city": "San Francisco", "division": "NL West", "conference": "National League"},
        }
    
    def get_nba_teams(self) -> Dict[str, Dict[str, str]]:
        """Get NBA team abbreviations and info for ESPN URLs"""
        return {
            # Eastern Conference - Atlantic Division
            "bos": {"name": "Boston Celtics", "city": "Boston", "division": "Atlantic", "conference": "Eastern"},
            "bkn": {"name": "Brooklyn Nets", "city": "New York", "division": "Atlantic", "conference": "Eastern"},
            "ny": {"name": "New York Knicks", "city": "New York", "division": "Atlantic", "conference": "Eastern"},
            "phi": {"name": "Philadelphia 76ers", "city": "Philadelphia", "division": "Atlantic", "conference": "Eastern"},
            "tor": {"name": "Toronto Raptors", "city": "Toronto", "division": "Atlantic", "conference": "Eastern"},
            
            # Eastern Conference - Central Division
            "chi": {"name": "Chicago Bulls", "city": "Chicago", "division": "Central", "conference": "Eastern"},
            "cle": {"name": "Cleveland Cavaliers", "city": "Cleveland", "division": "Central", "conference": "Eastern"},
            "det": {"name": "Detroit Pistons", "city": "Detroit", "division": "Central", "conference": "Eastern"},
            "ind": {"name": "Indiana Pacers", "city": "Indianapolis", "division": "Central", "conference": "Eastern"},
            "mil": {"name": "Milwaukee Bucks", "city": "Milwaukee", "division": "Central", "conference": "Eastern"},
            
            # Eastern Conference - Southeast Division
            "atl": {"name": "Atlanta Hawks", "city": "Atlanta", "division": "Southeast", "conference": "Eastern"},
            "cha": {"name": "Charlotte Hornets", "city": "Charlotte", "division": "Southeast", "conference": "Eastern"},
            "mia": {"name": "Miami Heat", "city": "Miami", "division": "Southeast", "conference": "Eastern"},
            "orl": {"name": "Orlando Magic", "city": "Orlando", "division": "Southeast", "conference": "Eastern"},
            "wsh": {"name": "Washington Wizards", "city": "Washington", "division": "Southeast", "conference": "Eastern"},
            
            # Western Conference - Northwest Division
            "den": {"name": "Denver Nuggets", "city": "Denver", "division": "Northwest", "conference": "Western"},
            "min": {"name": "Minnesota Timberwolves", "city": "Minneapolis", "division": "Northwest", "conference": "Western"},
            "okc": {"name": "Oklahoma City Thunder", "city": "Oklahoma City", "division": "Northwest", "conference": "Western"},
            "por": {"name": "Portland Trail Blazers", "city": "Portland", "division": "Northwest", "conference": "Western"},
            "utah": {"name": "Utah Jazz", "city": "Salt Lake City", "division": "Northwest", "conference": "Western"},
            
            # Western Conference - Pacific Division
            "gs": {"name": "Golden State Warriors", "city": "San Francisco", "division": "Pacific", "conference": "Western"},
            "lac": {"name": "LA Clippers", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "lal": {"name": "Los Angeles Lakers", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "phx": {"name": "Phoenix Suns", "city": "Phoenix", "division": "Pacific", "conference": "Western"},
            "sac": {"name": "Sacramento Kings", "city": "Sacramento", "division": "Pacific", "conference": "Western"},
            
            # Western Conference - Southwest Division
            "dal": {"name": "Dallas Mavericks", "city": "Dallas", "division": "Southwest", "conference": "Western"},
            "hou": {"name": "Houston Rockets", "city": "Houston", "division": "Southwest", "conference": "Western"},
            "mem": {"name": "Memphis Grizzlies", "city": "Memphis", "division": "Southwest", "conference": "Western"},
            "no": {"name": "New Orleans Pelicans", "city": "New Orleans", "division": "Southwest", "conference": "Western"},
            "sa": {"name": "San Antonio Spurs", "city": "San Antonio", "division": "Southwest", "conference": "Western"},
        }
    
    def get_nhl_teams(self) -> Dict[str, Dict[str, str]]:
        """Get NHL team abbreviations and info for ESPN URLs"""
        return {
            # Eastern Conference - Atlantic Division
            "bos": {"name": "Boston Bruins", "city": "Boston", "division": "Atlantic", "conference": "Eastern"},
            "buf": {"name": "Buffalo Sabres", "city": "Buffalo", "division": "Atlantic", "conference": "Eastern"},
            "det": {"name": "Detroit Red Wings", "city": "Detroit", "division": "Atlantic", "conference": "Eastern"},
            "fla": {"name": "Florida Panthers", "city": "Sunrise", "division": "Atlantic", "conference": "Eastern"},
            "mtl": {"name": "Montreal Canadiens", "city": "Montreal", "division": "Atlantic", "conference": "Eastern"},
            "ott": {"name": "Ottawa Senators", "city": "Ottawa", "division": "Atlantic", "conference": "Eastern"},
            "tb": {"name": "Tampa Bay Lightning", "city": "Tampa", "division": "Atlantic", "conference": "Eastern"},
            "tor": {"name": "Toronto Maple Leafs", "city": "Toronto", "division": "Atlantic", "conference": "Eastern"},
            
            # Eastern Conference - Metropolitan Division
            "car": {"name": "Carolina Hurricanes", "city": "Raleigh", "division": "Metropolitan", "conference": "Eastern"},
            "cbj": {"name": "Columbus Blue Jackets", "city": "Columbus", "division": "Metropolitan", "conference": "Eastern"},
            "njd": {"name": "New Jersey Devils", "city": "Newark", "division": "Metropolitan", "conference": "Eastern"},
            "nyi": {"name": "New York Islanders", "city": "New York", "division": "Metropolitan", "conference": "Eastern"},
            "nyr": {"name": "New York Rangers", "city": "New York", "division": "Metropolitan", "conference": "Eastern"},
            "phi": {"name": "Philadelphia Flyers", "city": "Philadelphia", "division": "Metropolitan", "conference": "Eastern"},
            "pit": {"name": "Pittsburgh Penguins", "city": "Pittsburgh", "division": "Metropolitan", "conference": "Eastern"},
            "wsh": {"name": "Washington Capitals", "city": "Washington", "division": "Metropolitan", "conference": "Eastern"},
            
            # Western Conference - Central Division
            "utah": {"name": "Utah Hockey Club", "city": "Utah", "division": "Central", "conference": "Western"},
            "chi": {"name": "Chicago Blackhawks", "city": "Chicago", "division": "Central", "conference": "Western"},
            "col": {"name": "Colorado Avalanche", "city": "Denver", "division": "Central", "conference": "Western"},
            "dal": {"name": "Dallas Stars", "city": "Dallas", "division": "Central", "conference": "Western"},
            "min": {"name": "Minnesota Wild", "city": "Minneapolis", "division": "Central", "conference": "Western"},
            "nsh": {"name": "Nashville Predators", "city": "Nashville", "division": "Central", "conference": "Western"},
            "stl": {"name": "St. Louis Blues", "city": "St. Louis", "division": "Central", "conference": "Western"},
            "wpg": {"name": "Winnipeg Jets", "city": "Winnipeg", "division": "Central", "conference": "Western"},
            
            # Western Conference - Pacific Division
            "ana": {"name": "Anaheim Ducks", "city": "Anaheim", "division": "Pacific", "conference": "Western"},
            "cgy": {"name": "Calgary Flames", "city": "Calgary", "division": "Pacific", "conference": "Western"},
            "edm": {"name": "Edmonton Oilers", "city": "Edmonton", "division": "Pacific", "conference": "Western"},
            "la": {"name": "Los Angeles Kings", "city": "Los Angeles", "division": "Pacific", "conference": "Western"},
            "sj": {"name": "San Jose Sharks", "city": "San Jose", "division": "Pacific", "conference": "Western"},
            "sea": {"name": "Seattle Kraken", "city": "Seattle", "division": "Pacific", "conference": "Western"},
            "van": {"name": "Vancouver Canucks", "city": "Vancouver", "division": "Pacific", "conference": "Western"},
            "vgk": {"name": "Vegas Golden Knights", "city": "Las Vegas", "division": "Pacific", "conference": "Western"},
        }
    
    def load_team_airports(self) -> Dict[str, str]:
        """Load mapping of team cities to airport codes for all leagues"""
        return {
            # Major US Cities
            "New York": "LGA", "Los Angeles": "LAX", "Chicago": "ORD", "San Francisco": "SFO",
            "Boston": "BOS", "Philadelphia": "PHL", "Atlanta": "ATL", "Houston": "IAH",
            "Miami": "MIA", "Washington": "DCA", "St. Louis": "STL", "Milwaukee": "MKE",
            "Denver": "DEN", "Phoenix": "PHX", "San Diego": "SAN", "Baltimore": "BWI",
            "Tampa": "TPA", "Cleveland": "CLE", "Detroit": "DTW", "Minneapolis": "MSP",
            "Kansas City": "MCI", "Seattle": "SEA", "Oakland": "OAK", "Dallas": "DFW",
            "Cincinnati": "CVG", "Pittsburgh": "PIT",
            
            # NBA-specific cities
            "Indianapolis": "IND", "Charlotte": "CLT", "Orlando": "MCO", "Portland": "PDX",
            "Sacramento": "SMF", "Salt Lake City": "SLC", "Oklahoma City": "OKC",
            "Memphis": "MEM", "New Orleans": "MSY", "San Antonio": "SAT",
            
            # NHL-specific cities
            "Buffalo": "BUF", "Sunrise": "FLL", "Raleigh": "RDU", "Columbus": "CMH",
            "Newark": "EWR", "Nashville": "BNA", "Anaheim": "SNA", "Las Vegas": "LAS",
            "San Jose": "SJC",
            
            # Canadian cities
            "Toronto": "YYZ", "Montreal": "YUL", "Vancouver": "YVR", "Calgary": "YYC",
            "Edmonton": "YEG", "Ottawa": "YOW", "Winnipeg": "YWG"
        }
    
    def load_city_coordinates(self) -> Dict[str, Tuple[float, float]]:
        """Enhanced city coordinates including NBA and NHL cities"""
        return {
            # Existing MLB cities
            "Phoenix": (33.4484, -112.0740), "Atlanta": (33.7490, -84.3880),
            "Baltimore": (39.2904, -76.6122), "Boston": (42.3601, -71.0589),
            "Chicago": (41.8781, -87.6298), "Cincinnati": (39.1031, -84.5120),
            "Cleveland": (41.4993, -81.6944), "Denver": (39.7392, -104.9903),
            "Detroit": (42.3314, -83.0458), "Houston": (29.7604, -95.3698),
            "Kansas City": (39.0997, -94.5786), "Los Angeles": (34.0522, -118.2437),
            "Miami": (25.7617, -80.1918), "Milwaukee": (43.0389, -87.9065),
            "Minneapolis": (44.9778, -93.2650), "New York": (40.7128, -74.0060),
            "Oakland": (37.8044, -122.2712), "Philadelphia": (39.9526, -75.1652),
            "Pittsburgh": (40.4406, -79.9959), "San Diego": (32.7157, -117.1611),
            "San Francisco": (37.7749, -122.4194), "Seattle": (47.6062, -122.3321),
            "St. Louis": (38.6270, -90.1994), "Tampa": (27.9506, -82.4572),
            "Dallas": (32.7767, -96.7970), "Washington": (38.9072, -77.0369),
            
            # Additional NBA cities
            "Indianapolis": (39.7684, -86.1581), "Charlotte": (35.2271, -80.8431),
            "Orlando": (28.5383, -81.3792), "Portland": (45.5152, -122.6784),
            "Sacramento": (38.5816, -121.4944), "Salt Lake City": (40.7608, -111.8910),
            "Oklahoma City": (35.4676, -97.5164), "Memphis": (35.1495, -90.0490),
            "New Orleans": (29.9511, -90.0715), "San Antonio": (29.4241, -98.4936),
            
            # Additional NHL cities
            "Buffalo": (42.8864, -78.8784), "Sunrise": (26.1354, -80.2373),
            "Raleigh": (35.7796, -78.6382), "Columbus": (39.9612, -82.9988),
            "Newark": (40.7357, -74.1724), "Nashville": (36.1627, -86.7816),
            "Anaheim": (33.8366, -117.9143), "Las Vegas": (36.1699, -115.1398),
            "San Jose": (37.3382, -121.8863),
            
            # Canadian cities
            "Toronto": (43.6532, -79.3832), "Montreal": (45.5017, -73.5673),
            "Vancouver": (49.2827, -123.1207), "Calgary": (51.0447, -114.0719),
            "Edmonton": (53.5461, -113.4938), "Ottawa": (45.4215, -75.6972),
            "Winnipeg": (49.8951, -97.1384)
        }
    
    def format_season_for_league(self, year: int, league: str) -> str:
        """Format season string based on league conventions"""
        if league in ['NBA', 'NHL']:
            next_year = str(year + 1)[2:]  # Get last 2 digits
            return f"{year}-{next_year}"   # e.g., "2024-25"
        else:  # MLB
            return str(year)               # e.g., "2024"
    
    def get_current_season_for_league(self, league: str) -> str:
        """Get current season string for a league based on current date"""
        now = datetime.now()
        current_year = now.year
        
        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons start in October and end in June of next year
            if now.month >= 10:  # October or later
                return self.format_season_for_league(current_year, league)
            else:  # Before October
                return self.format_season_for_league(current_year - 1, league)
        else:  # MLB
            # MLB season is calendar year
            return self.format_season_for_league(current_year, league)
    
    def scrape_team_schedule(self, team_abbrev: str, league: str, season: str = None) -> List[GameData]:
        """Scrape season schedule for a team in specified league with proper MLB half handling"""
        if season is None:
            season = self.get_current_season_for_league(league)
        
        all_games = []
        
        if league == 'MLB':
            # MLB SPECIAL HANDLING: ESPN divides MLB season into two halves
            print(f"Scraping MLB {team_abbrev} schedule for {season} season (both halves)...")
            
            for half in [1, 2]:
                try:
                    url = self.url_patterns['MLB'].format(team=team_abbrev, half=half)
                    print(f"  → Scraping MLB {team_abbrev} half {half}: {url}")
                    
                    response = self.session.get(url, timeout=15)
                    response.raise_for_status()
                    
                    table_rows = self._parse_schedule_page(response.text, team_abbrev, league, season)
                    
                    if table_rows and len(table_rows) > 1:
                        half_games = self.parse_table_to_games(table_rows, team_abbrev, league, season)
                        all_games.extend(half_games)
                        print(f"  ✓ Half {half}: Found {len(half_games)} games")
                    else:
                        print(f"  ✗ Half {half}: No schedule data found")
                    
                    time.sleep(0.75)
                    
                except requests.exceptions.RequestException as e:
                    print(f"  ✗ Network error scraping MLB {team_abbrev} half {half}: {e}")
                    continue
                except Exception as e:
                    print(f"  ✗ Error parsing MLB {team_abbrev} half {half}: {e}")
                    continue
            
            print(f"MLB {team_abbrev} total games scraped: {len(all_games)}")
        
        else:  # NBA or NHL - Single schedule page
            try:
                url = self.url_patterns[league].format(team=team_abbrev)
                print(f"Scraping {league} {team_abbrev} schedule: {url}")
                
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                
                table_rows = self._parse_schedule_page(response.text, team_abbrev, league, season)
                
                if table_rows and len(table_rows) > 1:
                    games = self.parse_table_to_games(table_rows, team_abbrev, league, season)
                    all_games.extend(games)
                    print(f"  ✓ Found {len(games)} {league} games")
                else:
                    print(f"  ✗ No {league} schedule data found")
                
                time.sleep(0.75)
                
            except requests.exceptions.RequestException as e:
                print(f"  ✗ Network error scraping {league} {team_abbrev}: {e}")
            except Exception as e:
                print(f"  ✗ Error parsing {league} {team_abbrev}: {e}")
        
        return all_games
    
    def scrape_league_schedule(self, league: str, season: str = None) -> List[GameData]:
        """Scrape schedules for all teams in a specific league"""
        if league not in self.league_teams:
            raise ValueError(f"Unsupported league: {league}. Supported: {list(self.league_teams.keys())}")
        
        if season is None:
            season = self.get_current_season_for_league(league)
        
        print(f"\n=== Scraping {league} {season} League Schedule ===")
        
        all_games = []
        seen_games = set()
        league_teams = self.league_teams[league]
        total_teams = len(league_teams)
        
        for team_idx, (team_abbrev, team_info) in enumerate(league_teams.items(), 1):
            try:
                print(f"\n[{team_idx}/{total_teams}] Scraping {league} {team_info['name']} ({team_abbrev})...")
                
                team_games = self.scrape_team_schedule(team_abbrev, league, season)
                
                games_added = 0
                for game in team_games:
                    game_key = f"{game.date.strftime('%Y-%m-%d')}_{game.home_team.abbreviation}_{game.away_team.abbreviation}"
                    
                    if game_key not in seen_games:
                        all_games.append(game)
                        seen_games.add(game_key)
                        games_added += 1
                
                print(f"  → Added {games_added} unique games (found {len(team_games)} total)")
                
                if team_idx < total_teams:
                    time.sleep(1.5)
                
            except Exception as e:
                print(f"  ✗ Error scraping {league} team {team_abbrev}: {e}")
                continue
        
        print(f"\n=== {league} {season} Scraping Complete ===")
        print(f"Total unique games scraped: {len(all_games)}")
        
        return all_games
    
    def _parse_schedule_page(self, html_content: str, team_abbrev: str, league: str, season: str) -> List[List[str]]:
        """Parse ESPN schedule page HTML to extract table data"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            tables = soup.find_all('table')
            
            schedule_table = None
            for table in tables:
                rows = table.find_all('tr')
                if len(rows) > 5:
                    first_few_rows_text = ' '.join([row.get_text().lower() for row in rows[:3]])
                    schedule_keywords = ['date', 'opponent', 'result', 'vs', '@', 'matchup', 'game']
                    
                    if any(keyword in first_few_rows_text for keyword in schedule_keywords):
                        schedule_table = table
                        break
            
            if not schedule_table:
                all_rows = soup.find_all('tr')
                if len(all_rows) > 5:
                    rows = all_rows
                else:
                    print(f"  ✗ No schedule data found for {league} {team_abbrev}")
                    return []
            else:
                rows = schedule_table.find_all('tr')
            
            table_data = []
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if cells:
                    cell_texts = [cell.get_text(strip=True) for cell in cells]
                    if cell_texts and len(cell_texts) >= 3:
                        table_data.append(cell_texts)
            
            print(f"  → Extracted {len(table_data)} rows from schedule page")
            return table_data
            
        except Exception as e:
            print(f"  ✗ Error parsing schedule page for {league} {team_abbrev}: {e}")
            return []
    
    def parse_table_to_games(self, table_rows: List[List[str]], team_abbrev: str, league: str, season: str) -> List[GameData]:
        """Convert table rows to GameData objects"""
        games = []
        
        if not table_rows or len(table_rows) < 2:
            return games
        
        print(f"  → Processing {len(table_rows)-1} {league} game rows for {team_abbrev}...")
        
        valid_games = 0
        rejected_games = 0
        
        for i, row in enumerate(table_rows[1:], 1):
            try:
                game = self.parse_game_row(row, team_abbrev, league, season)
                if game:
                    games.append(game)
                    valid_games += 1
                    if valid_games <= 3:
                        print(f"    ✓ Game {valid_games}: {game.away_team.abbreviation} @ {game.home_team.abbreviation} on {game.date.strftime('%m/%d')}")
                    elif valid_games == 4:
                        print(f"    ... processing remaining games ...")
                else:
                    rejected_games += 1
                        
            except Exception as e:
                rejected_games += 1
                continue
        
        print(f"  → Successfully parsed {valid_games} valid {league} games for {team_abbrev}")
        if rejected_games > 0:
            print(f"  ⚠️  Rejected {rejected_games} rows")
                
        return games
    
    def parse_game_row(self, row: List[str], team_abbrev: str, league: str, season: str) -> Optional[GameData]:
        """Parse individual game row into GameData"""
        if len(row) < 3:
            return None
            
        date_str = row[0].strip()
        opponent_str = row[1].strip()
        result_str = row[2].strip() if len(row) > 2 else ""
        
        if not date_str or not opponent_str:
            return None
        
        skip_entries = [
            'all-star', 'break', 'tbd', 'postponed', 'cancelled',
            'spring training', 'exhibition', 'world baseball classic'
        ]
        
        if any(skip_word in opponent_str.lower() for skip_word in skip_entries):
            return None
        
        if any(skip_word in date_str.lower() for skip_word in skip_entries):
            return None
        
        try:
            game_date = self.parse_date_for_league(date_str, league, season)
            if not game_date:
                return None
        except Exception:
            return None
            
        try:
            is_home, opponent_abbrev = self.parse_opponent(opponent_str, league)
            if not opponent_abbrev:
                return None
        except Exception:
            return None
            
        home_team_abbrev = team_abbrev if is_home else opponent_abbrev
        away_team_abbrev = opponent_abbrev if is_home else team_abbrev
        
        try:
            home_team = self.create_team_info(home_team_abbrev, league)
            away_team = self.create_team_info(away_team_abbrev, league)
            venue = self.create_venue_info(home_team_abbrev, league)
        except Exception:
            return None
        
        status = GameStatus.SCHEDULED if not result_str or result_str == '-' else GameStatus.FINAL
        game_id = f"{league}_{season}_{game_date.strftime('%Y%m%d')}_{away_team_abbrev}_{home_team_abbrev}"
        
        return GameData(
            game_id=game_id,
            date=game_date,
            home_team=home_team,
            away_team=away_team,
            venue=venue,
            status=status,
            league=league,
            season=season
        )
    
    def parse_date_for_league(self, date_str: str, league: str, season: str) -> Optional[datetime]:
        """Parse ESPN date format with league-specific logic"""
        if not date_str or not isinstance(date_str, str):
            return None
            
        try:
            if ',' in date_str:
                date_part = date_str.split(',', 1)[1].strip()
            else:
                date_part = date_str.strip()
            
            skip_entries = ['tbd', 'postponed', 'cancelled', 'all-star', 'break']
            if any(skip in date_part.lower() for skip in skip_entries):
                return None
                
            parts = date_part.split()
            if len(parts) == 2:
                month_str, day_str = parts[0], parts[1]
            elif '/' in date_part:
                date_parts = date_part.split('/')
                if len(date_parts) == 2:
                    month_num, day_str = date_parts[0], date_parts[1]
                    month_str = self._number_to_month(int(month_num))
                    if not month_str:
                        return None
                else:
                    return None
            else:
                return None
            
            month_map = {
                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
                'January': 1, 'February': 2, 'March': 3, 'April': 4, 
                'May': 5, 'June': 6, 'July': 7, 'August': 8, 
                'September': 9, 'October': 10, 'November': 11, 'December': 12
            }
            
            month = month_map.get(month_str)
            if not month:
                return None
            
            day_str = re.sub(r'[^0-9]', '', day_str)
            if not day_str:
                return None
                
            day = int(day_str)
            if day < 1 or day > 31:
                return None
            
            if league in ['NBA', 'NHL'] and '-' in season:
                start_year, end_year_short = season.split('-')
                start_year = int(start_year)
                end_year = int('20' + end_year_short)
                
                if month >= 10:
                    year = start_year
                else:
                    year = end_year
            else:  # MLB
                year = int(season)
                
                current_date = datetime.now()
                if month <= 3 and current_date.month >= 10:
                    year += 1
            
            default_hours = {
                'MLB': 19,
                'NBA': 20,
                'NHL': 19
            }
            default_hour = default_hours.get(league, 19)
                
            return datetime(year, month, day, default_hour, 0)
            
        except Exception as e:
            return None
    
    def _number_to_month(self, month_num: int) -> Optional[str]:
        """Convert month number to abbreviated month name"""
        month_names = {
            1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
            7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
        }
        return month_names.get(month_num)
    
    def parse_opponent(self, opponent_str: str, league: str) -> Tuple[bool, Optional[str]]:
        """Parse opponent string with league-specific team name resolution"""
        if not opponent_str:
            return False, None
            
        if opponent_str.startswith('vs'):
            is_home = True
            opponent_name = opponent_str[2:].strip()
        elif opponent_str.startswith('@'):
            is_home = False
            opponent_name = opponent_str[1:].strip()
        else:
            is_home = True
            opponent_name = opponent_str.strip()
            
        opponent_abbrev = self.get_team_abbrev_from_name(opponent_name, league)
        
        return is_home, opponent_abbrev
    
    def get_team_abbrev_from_name(self, team_name: str, league: str) -> Optional[str]:
        """Convert team city/name to abbreviation for specific league"""
        if not team_name:
            return None
            
        team_name = team_name.strip().lower()
        league_teams = self.league_teams.get(league, {})
        
        name_to_abbrev = {}
        
        for abbrev, team_info in league_teams.items():
            city = team_info['city'].lower()
            name_to_abbrev[city] = abbrev
            
            full_name = team_info['name'].lower()
            name_to_abbrev[full_name] = abbrev
            
            name_parts = full_name.split()
            if len(name_parts) > 1:
                nickname = name_parts[-1]
                name_to_abbrev[nickname] = abbrev
            
            name_to_abbrev[abbrev.lower()] = abbrev
        
        # League-specific mappings
        if league == 'MLB':
            name_to_abbrev.update({
                'losangeles': 'lad',  
                'newyork': 'nyy',
                'chicago': 'chc',
                'angels': 'laa',
                'athletics': 'ath',
                'whitesox': 'chw',
                'redsox': 'bos',
                'bluejays': 'tor',
                'diamondbacks': 'ari',
                'rockies': 'col',
                'royals': 'kc',
                'twins': 'min',
                'rangers': 'tex',
                'mariners': 'sea',
                'rays': 'tb',
                'orioles': 'bal',
                'guardians': 'cle',
                'tigers': 'det',
                'astros': 'hou',
                'braves': 'atl',
                'marlins': 'mia',
                'mets': 'nym',
                'phillies': 'phi',
                'nationals': 'wsh',
                'cubs': 'chc',
                'reds': 'cin',
                'brewers': 'mil',
                'pirates': 'pit',
                'cardinals': 'stl',
                'dodgers': 'lad',
                'padres': 'sd',
                'giants': 'sf',
                'yankees': 'nyy',
            })
        elif league == 'NBA':
            name_to_abbrev.update({
                'losangeles': 'lal',
                'newyork': 'ny',
                'goldenstatewarriors': 'gs',
                'goldenstate': 'gs',
                'clippers': 'lac',
                'lakers': 'lal',
                'warriors': 'gs',
            })
        elif league == 'NHL':
            name_to_abbrev.update({
                'losangeles': 'la',
                'newyork': 'nyr',
                'vegasgoldenknights': 'vgk',
                'vegas': 'vgk',
                'goldenknights': 'vgk',
                'kings': 'la',
                'rangers': 'nyr',
            })
        
        if team_name in name_to_abbrev:
            return name_to_abbrev[team_name]
        
        clean_name = re.sub(r'[^a-z]', '', team_name)
        if clean_name in name_to_abbrev:
            return name_to_abbrev[clean_name]
        
        for name_key, abbrev in name_to_abbrev.items():
            if clean_name in name_key or name_key in clean_name:
                return abbrev
        
        if len(clean_name) >= 3:
            potential_abbrev = clean_name[:3]
            if potential_abbrev in league_teams:
                return potential_abbrev
        
        return None
    
    def create_team_info(self, team_abbrev: str, league: str) -> TeamInfo:
        """Create TeamInfo object from abbreviation and league"""
        league_teams = self.league_teams.get(league, {})
        team_data = league_teams.get(team_abbrev.lower(), {})
        
        return TeamInfo(
            team_id=team_abbrev.lower(),
            abbreviation=team_abbrev.upper(),
            display_name=team_data.get('name', f'{league} Team {team_abbrev.upper()}'),
            location=team_data.get('city', ''),
            color='#000000',
            alternate_color='#FFFFFF',
            division=team_data.get('division', ''),
            league=league,
            conference=team_data.get('conference', '')
        )
    
    def create_venue_info(self, home_team_abbrev: str, league: str) -> Venue:
        """Create Venue object for home team's venue"""
        league_teams = self.league_teams.get(league, {})
        team_data = league_teams.get(home_team_abbrev.lower(), {})
        city = team_data.get('city', '')
        coords = self.city_coordinates.get(city, (0.0, 0.0))
        
        venue_suffixes = {
            'MLB': 'Stadium',
            'NBA': 'Arena', 
            'NHL': 'Arena'
        }
        
        venue_suffix = venue_suffixes.get(league, 'Stadium')
        
        return Venue(
            venue_id=f"{home_team_abbrev}_{league}_venue",
            name=f"{city} {venue_suffix}",
            city=city,
            state='',
            country='USA' if city not in ['Toronto', 'Montreal', 'Vancouver', 'Calgary', 'Edmonton', 'Ottawa', 'Winnipeg'] else 'Canada',
            latitude=coords[0],
            longitude=coords[1]
        )


class TravelInferenceEngine:
    """Engine to infer team travel patterns from game schedules"""
    
    def __init__(self, airport_mappings: Dict[str, str]):
        self.airport_mappings = airport_mappings
    
    def infer_travel_from_games(self, games: List[GameData], league: str) -> List[TeamTravelData]:
        """Infer team travel patterns from game schedule"""
        travel_data = []
        
        team_schedules = {}
        for game in games:
            home_team_id = game.home_team.team_id
            away_team_id = game.away_team.team_id
            
            if home_team_id not in team_schedules:
                team_schedules[home_team_id] = []
            if away_team_id not in team_schedules:
                team_schedules[away_team_id] = []
            
            team_schedules[home_team_id].append((game, 'home'))
            team_schedules[away_team_id].append((game, 'away'))
        
        for team_id, schedule in team_schedules.items():
            schedule.sort(key=lambda x: x[0].date)
            team_travel = self._infer_team_travel(schedule, league)
            travel_data.extend(team_travel)
        
        return travel_data
    
    def _infer_team_travel(self, team_schedule: List[Tuple[GameData, str]], league: str) -> List[TeamTravelData]:
        """Infer travel for a specific team's schedule"""
        travel_data = []
        
        travel_patterns = {
            'MLB': {'advance_days': 1, 'return_days': 1},
            'NBA': {'advance_days': 1, 'return_days': 0},
            'NHL': {'advance_days': 1, 'return_days': 0}
        }
        
        pattern = travel_patterns.get(league, travel_patterns['MLB'])
        
        for i, (game, home_away) in enumerate(team_schedule):
            if home_away == 'away':
                team_info = game.away_team
                departure_city = self._get_clean_city_name(team_info.location)
                arrival_city = self._get_clean_city_name(game.venue.city)
                
                if not departure_city or not arrival_city or departure_city == arrival_city:
                    continue
                
                game_date = game.date
                if hasattr(game_date, 'tzinfo') and game_date.tzinfo is not None:
                    game_date = game_date.replace(tzinfo=None)
                
                travel_date = game_date - timedelta(days=pattern['advance_days'])
                
                dep_airport = self.airport_mappings.get(departure_city, departure_city[:3].upper())
                arr_airport = self.airport_mappings.get(arrival_city, arrival_city[:3].upper())
                
                travel = TeamTravelData(
                    team_name=team_info.display_name,
                    team_id=team_info.team_id,
                    departure_city=departure_city,
                    arrival_city=arrival_city,
                    game_date=game_date,
                    travel_date=travel_date,
                    departure_airport=dep_airport,
                    arrival_airport=arr_airport,
                    confidence="schedule_inferred",
                    game_id=game.game_id,
                    opponent=game.home_team.display_name
                )
                travel_data.append(travel)
                
                if pattern['return_days'] >= 0:
                    return_date = game_date + timedelta(days=pattern['return_days'])
                    return_travel = TeamTravelData(
                        team_name=team_info.display_name,
                        team_id=team_info.team_id,
                        departure_city=arrival_city,
                        arrival_city=departure_city,
                        game_date=game_date,
                        travel_date=return_date,
                        departure_airport=arr_airport,
                        arrival_airport=dep_airport,
                        confidence="schedule_inferred",
                        game_id=game.game_id,
                        opponent=game.home_team.display_name
                    )
                    travel_data.append(return_travel)
        
        return travel_data
    
    def _get_clean_city_name(self, city_name: str) -> str:
        """Clean and validate city name"""
        if not city_name or not isinstance(city_name, str):
            return ""
        
        clean_name = city_name.strip()
        if not clean_name:
            return ""
        
        city_mappings = {
            "St. Petersburg": "Tampa", "Anaheim": "Los Angeles", "Arlington": "Dallas",
            "Queens": "New York", "Bronx": "New York", "Brooklyn": "New York",
            "Sunrise": "Miami", "Newark": "New York", "Raleigh": "Charlotte"
        }
        
        return city_mappings.get(clean_name, clean_name)


class ESPNSportsDataAggregator(QObject):
    """Multi-league ESPN sports data aggregator with database integration"""
    
    dataUpdated = pyqtSignal(list)
    progressUpdated = pyqtSignal(int)
    errorOccurred = pyqtSignal(str)
    seasonDataLoaded = pyqtSignal(str, str, int)  # season, league, game_count
    
    def __init__(self, config: Dict[str, str], db_path: str = "sports_data.db"):
        super().__init__()
        
        try:
            from database_manager import DatabaseManager
            self.db = DatabaseManager(db_path)
        except ImportError:
            print("Error: DatabaseManager not found. Please ensure database_manager.py is available.")
            raise
        
        self.espn_scraper = ESPNScheduleScraper()
        self.inference_engine = TravelInferenceEngine(self.espn_scraper.team_airports)
        
        self.current_league = "MLB"
        self.current_season = None
        self.all_season_games = []
        self.current_travel_data = []
        self.teams_cache = {}
        
        self.load_teams_for_all_leagues()
        self.set_league(self.current_league)
    
    def set_league(self, league: str):
        """Set current league and update current season"""
        if league not in ['MLB', 'NBA', 'NHL']:
            raise ValueError(f"Unsupported league: {league}")
        
        self.current_league = league
        self.current_season = self.espn_scraper.get_current_season_for_league(league)
        print(f"Set league to {league}, current season: {self.current_season}")
    
    def load_teams_for_all_leagues(self):
        """Load team information for all leagues"""
        supported_leagues = ['MLB', 'NBA', 'NHL']
        
        for league in supported_leagues:
            teams = self.db.load_teams(league)
            
            if not teams:
                print(f"No {league} teams found in database, initializing from scraper...")
                teams = []
                league_teams = self.espn_scraper.league_teams.get(league, {})
                
                for abbrev, info in league_teams.items():
                    team = TeamInfo(
                        team_id=abbrev,
                        abbreviation=abbrev.upper(),
                        display_name=info['name'],
                        location=info['city'],
                        color='#000000',
                        alternate_color='#FFFFFF',
                        division=info['division'],
                        league=league,
                        conference=info.get('conference', '')
                    )
                    teams.append(team)
                
                if teams:
                    self.db.save_teams(teams, league)
            
            self.teams_cache[league] = {team.team_id: team for team in teams}
            print(f"Loaded {len(teams)} {league} teams")
    
    def load_full_season_schedule(self, season: str = None, league: str = None, force_refresh: bool = False):
        """Load complete season schedule for specified league"""
        if league is None:
            league = self.current_league
        
        if season is None:
            season = self.espn_scraper.get_current_season_for_league(league)
        
        try:
            if not self.db.should_refresh_season(season, league, force_refresh):
                print(f"Loading {league} {season} season from database cache...")
                self.progressUpdated.emit(20)
                
                games = self.db.load_games(season, league)
                travel_data = self.db.load_travel_data(season, league)
                
                if games and travel_data:
                    self.all_season_games = games
                    self.current_travel_data = travel_data
                    self.dataUpdated.emit(travel_data)
                    self.seasonDataLoaded.emit(season, league, len(games))
                    self.progressUpdated.emit(100)
                    print(f"Loaded {len(games)} {league} games and {len(travel_data)} travel records from database")
                    return
                else:
                    print(f"No cached {league} data found for {season}, will scrape...")
            
            print(f"Scraping {league} {season} season schedule from ESPN...")
            self.progressUpdated.emit(10)
            
            self.db.clear_season_data(season, league)
            
            season_games = self.espn_scraper.scrape_league_schedule(league, season)
            
            if season_games:
                self.progressUpdated.emit(60)
                
                self.db.save_games(season_games, season, league)
                self.progressUpdated.emit(70)
                
                travel_data = self.inference_engine.infer_travel_from_games(season_games, league)
                self.db.save_travel_data(travel_data, season, league)
                self.progressUpdated.emit(90)
                
                self.all_season_games = season_games
                self.current_travel_data = travel_data
                
                self.dataUpdated.emit(travel_data)
                self.seasonDataLoaded.emit(season, league, len(season_games))
                
                print(f"Scraped and saved {len(season_games)} {league} games and {len(travel_data)} travel records")
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No {league} games found for {season} season")
                
        except Exception as e:
            error_msg = f"Failed to load {league} {season} season: {str(e)}"
            print(error_msg)
            self.errorOccurred.emit(error_msg)
            self.progressUpdated.emit(0)
    
    def load_team_season_schedule(self, team_id: str, season: str = None, league: str = None):
        """Load schedule for specific team"""
        if league is None:
            league = self.current_league
        
        if season is None:
            season = self.espn_scraper.get_current_season_for_league(league)
        
        try:
            self.progressUpdated.emit(20)
            
            team_travel = self.db.load_travel_data(season, league, team_id)
            
            if team_travel:
                print(f"Loaded {len(team_travel)} {league} travel records for {team_id} from database")
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
                return
            
            is_cached, _ = self.db.is_season_cached(season, league)
            if not is_cached:
                self.load_full_season_schedule(season, league)
                return
            
            team_travel = self.db.load_travel_data(season, league, team_id)
            if team_travel:
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No {league} travel data found for team {team_id} in {season}")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load {league} team schedule: {str(e)}")
    
    def get_current_week_schedule(self, league: str = None):
        """Get current week games from database for specified league"""
        if league is None:
            league = self.current_league
        
        try:
            season = self.espn_scraper.get_current_season_for_league(league)
            
            is_cached, _ = self.db.is_season_cached(season, league)
            
            if not is_cached:
                print(f"No {league} {season} season data found, loading full season...")
                self.load_full_season_schedule(season, league)
                return
            
            today = datetime.now()
            week_start = today - timedelta(days=3)
            week_end = today + timedelta(days=4)
            
            all_travel = self.db.load_travel_data(season, league)
            current_week_travel = [
                travel for travel in all_travel
                if week_start <= travel.travel_date <= week_end
            ]
            
            if current_week_travel:
                self.dataUpdated.emit(current_week_travel)
                print(f"Loaded {len(current_week_travel)} {league} travel records for current week")
            else:
                self.errorOccurred.emit(f"No {league} current week travel found")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load {league} current week: {str(e)}")
    
    def get_travel_by_date_range(self, start_date: datetime, end_date: datetime, league: str = None) -> List[TeamTravelData]:
        """Filter travel data by date range"""
        if league is None:
            league = self.current_league
        
        try:
            season = self.espn_scraper.get_current_season_for_league(league)
            all_travel = self.db.load_travel_data(season, league)
            
            filtered_travel = [
                travel for travel in all_travel
                if start_date <= travel.travel_date <= end_date
            ]
            
            return filtered_travel
            
        except Exception as e:
            print(f"Error filtering {league} travel by date range: {e}")
            return []
    
    def get_team_info(self, team_id: str, league: str = None) -> Optional[TeamInfo]:
        """Get team information by ID and league"""
        if league is None:
            league = self.current_league
        
        league_teams = self.teams_cache.get(league, {})
        return league_teams.get(team_id)
    
    def get_all_teams(self, league: str = None) -> List[TeamInfo]:
        """Get all cached teams for specified league"""
        if league is None:
            league = self.current_league
        
        league_teams = self.teams_cache.get(league, {})
        return list(league_teams.values())
    
    def get_supported_leagues(self) -> List[str]:
        """Get list of supported leagues"""
        return list(self.teams_cache.keys())
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        return self.db.get_database_stats()
    
    def get_cached_seasons(self, league: str = None) -> List[Dict[str, Any]]:
        """Get information about cached seasons for specified league"""
        if league is None:
            league = self.current_league
        
        return self.db.get_cached_seasons(league)
    
    def clear_season_cache(self, season: str, league: str = None):
        """Clear cache for specific season and league"""
        if league is None:
            league = self.current_league
        
        try:
            self.db.clear_season_data(season, league)
            print(f"Cleared {league} cache for {season} season")
        except Exception as e:
            print(f"Error clearing {league} season cache: {e}")


def test_multi_league_integration():
    """Test function to verify multi-league integration works"""
    print("Testing Multi-League Integration...")
    
    config = {}
    aggregator = ESPNSportsDataAggregator(config, "test_multi_sports.db")
    
    leagues = ['MLB', 'NBA', 'NHL']
    
    for league in leagues:
        print(f"\n=== Testing {league} ===")
        
        aggregator.set_league(league)
        
        current_season = aggregator.espn_scraper.get_current_season_for_league(league)
        print(f"{league} current season: {current_season}")
        
        teams = aggregator.get_all_teams(league)
        print(f"{league} teams: {len(teams)}")
        
        is_cached, last_updated = aggregator.db.is_season_cached(current_season, league)
        print(f"{league} season cached: {is_cached}, last updated: {last_updated}")
    
    stats = aggregator.get_database_stats()
    print(f"\nDatabase stats: {stats}")


if __name__ == "__main__":
    test_multi_league_integration()
