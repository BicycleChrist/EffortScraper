import requests
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QThread, QDateTime, QMutex
from bs4 import BeautifulSoup
import re


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
    """Scraper for ESPN team schedule pages"""
    
    def __init__(self):
        self.base_url = "https://www.espn.com/mlb/team/schedule/_/name/{team}/seasontype/2/half/{half}"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.team_airports = self.load_team_airports()
        self.mlb_teams = self.get_mlb_team_mappings()
        self.team_mappings = self.get_team_data()
        self.city_coordinates = self.load_city_coordinates()
        self.espn_url_mappings = self.get_espn_url_mappings()
        
    def get_mlb_team_mappings(self) -> Dict[str, Dict[str, str]]:
        """Get MLB team abbreviations and info for ESPN URLs"""
        return {
            # American League East
            "bal": {"name": "Baltimore Orioles", "city": "Baltimore", "division": "AL East"},
            "bos": {"name": "Boston Red Sox", "city": "Boston", "division": "AL East"},
            "nyy": {"name": "New York Yankees", "city": "New York", "division": "AL East"},
            "tb": {"name": "Tampa Bay Rays", "city": "Tampa", "division": "AL East"},
            "tor": {"name": "Toronto Blue Jays", "city": "Toronto", "division": "AL East"},
            
            # American League Central
            "cws": {"name": "Chicago White Sox", "city": "Chicago", "division": "AL Central"},
            "cle": {"name": "Cleveland Guardians", "city": "Cleveland", "division": "AL Central"},
            "det": {"name": "Detroit Tigers", "city": "Detroit", "division": "AL Central"},
            "kc": {"name": "Kansas City Royals", "city": "Kansas City", "division": "AL Central"},
            "min": {"name": "Minnesota Twins", "city": "Minneapolis", "division": "AL Central"},
            
            # American League West
            "hou": {"name": "Houston Astros", "city": "Houston", "division": "AL West"},
            "laa": {"name": "Los Angeles Angels", "city": "Los Angeles", "division": "AL West"},
            "ath": {"name": "Oakland Athletics", "city": "Oakland", "division": "AL West"},
            "sea": {"name": "Seattle Mariners", "city": "Seattle", "division": "AL West"},
            "tex": {"name": "Texas Rangers", "city": "Dallas", "division": "AL West"},
            
            # National League East
            "atl": {"name": "Atlanta Braves", "city": "Atlanta", "division": "NL East"},
            "mia": {"name": "Miami Marlins", "city": "Miami", "division": "NL East"},
            "nym": {"name": "New York Mets", "city": "New York", "division": "NL East"},
            "phi": {"name": "Philadelphia Phillies", "city": "Philadelphia", "division": "NL East"},
            "wsh": {"name": "Washington Nationals", "city": "Washington", "division": "NL East"},
            
            # National League Central
            "chc": {"name": "Chicago Cubs", "city": "Chicago", "division": "NL Central"},
            "cin": {"name": "Cincinnati Reds", "city": "Cincinnati", "division": "NL Central"},
            "mil": {"name": "Milwaukee Brewers", "city": "Milwaukee", "division": "NL Central"},
            "pit": {"name": "Pittsburgh Pirates", "city": "Pittsburgh", "division": "NL Central"},
            "stl": {"name": "St. Louis Cardinals", "city": "St. Louis", "division": "NL Central"},
            
            # National League West
            "ari": {"name": "Arizona Diamondbacks", "city": "Phoenix", "division": "NL West"},
            "col": {"name": "Colorado Rockies", "city": "Denver", "division": "NL West"},
            "lad": {"name": "Los Angeles Dodgers", "city": "Los Angeles", "division": "NL West"},
            "sd": {"name": "San Diego Padres", "city": "San Diego", "division": "NL West"},
            "sf": {"name": "San Francisco Giants", "city": "San Francisco", "division": "NL West"},
        }

    def get_espn_url_mappings(self) -> Dict[str, str]:
        """Get mapping from our team IDs to ESPN URL team codes"""
        return {
            # American League East
            "bal": "bal",
            "bos": "bos", 
            "nyy": "nyy",
            "tb": "tb",
            "tor": "tor",
            
            # American League Central
            "cws": "chw",  # Chicago White Sox: our ID "cws" -> ESPN URL "chw"
            "cle": "cle",
            "det": "det", 
            "kc": "kc",
            "min": "min",
            
            # American League West
            "hou": "hou",
            "laa": "laa",
            "ath": "ath",
            "sea": "sea", 
            "tex": "tex",
            
            # National League East
            "atl": "atl",
            "mia": "mia",
            "nym": "nym",
            "phi": "phi",
            "wsh": "wsh",
            
            # National League Central
            "chc": "chc",
            "cin": "cin",
            "mil": "mil",
            "pit": "pit",
            "stl": "stl",
            
            # National League West
            "ari": "ari",
            "col": "col",
            "lad": "lad",
            "sd": "sd",
            "sf": "sf",
        }
        
    def load_team_airports(self) -> Dict[str, str]:
        """Load mapping of team cities to airport codes"""
        return {
            "New York": "LGA",
            "Los Angeles": "LAX",
            "Chicago": "ORD",
            "San Francisco": "SFO",
            "Boston": "BOS",
            "Philadelphia": "PHL",
            "Atlanta": "ATL",
            "Houston": "IAH",
            "Miami": "MIA",
            "Washington": "DCA",
            "St. Louis": "STL",
            "Milwaukee": "MKE",
            "Denver": "DEN",
            "Phoenix": "PHX",
            "San Diego": "SAN",
            "Baltimore": "BWI",
            "Tampa": "TPA",
            "Toronto": "YYZ",
            "Cleveland": "CLE",
            "Detroit": "DTW",
            "Minneapolis": "MSP",
            "Kansas City": "MCI",
            "Seattle": "SEA",
            "Oakland": "OAK",
            "Dallas": "DFW",
            "Cincinnati": "CVG",
            "Pittsburgh": "PIT",
        }
    
    def get_team_data(self) -> Dict[str, Dict[str, str]]:
        """Get team data for parsing"""
        return {
            "ari": {"name": "Arizona Diamondbacks", "city": "Phoenix", "division": "NL West"},
            "atl": {"name": "Atlanta Braves", "city": "Atlanta", "division": "NL East"},
            "bal": {"name": "Baltimore Orioles", "city": "Baltimore", "division": "AL East"},
            "bos": {"name": "Boston Red Sox", "city": "Boston", "division": "AL East"},
            "chc": {"name": "Chicago Cubs", "city": "Chicago", "division": "NL Central"},
            "cws": {"name": "Chicago White Sox", "city": "Chicago", "division": "AL Central"},
            "cin": {"name": "Cincinnati Reds", "city": "Cincinnati", "division": "NL Central"},
            "cle": {"name": "Cleveland Guardians", "city": "Cleveland", "division": "AL Central"},
            "col": {"name": "Colorado Rockies", "city": "Denver", "division": "NL West"},
            "det": {"name": "Detroit Tigers", "city": "Detroit", "division": "AL Central"},
            "hou": {"name": "Houston Astros", "city": "Houston", "division": "AL West"},
            "kc": {"name": "Kansas City Royals", "city": "Kansas City", "division": "AL Central"},
            "laa": {"name": "Los Angeles Angels", "city": "Los Angeles", "division": "AL West"},
            "lad": {"name": "Los Angeles Dodgers", "city": "Los Angeles", "division": "NL West"},
            "mia": {"name": "Miami Marlins", "city": "Miami", "division": "NL East"},
            "mil": {"name": "Milwaukee Brewers", "city": "Milwaukee", "division": "NL Central"},
            "min": {"name": "Minnesota Twins", "city": "Minneapolis", "division": "AL Central"},
            "nym": {"name": "New York Mets", "city": "New York", "division": "NL East"},
            "nyy": {"name": "New York Yankees", "city": "New York", "division": "AL East"},
            "oak": {"name": "Oakland Athletics", "city": "Oakland", "division": "AL West"},
            "phi": {"name": "Philadelphia Phillies", "city": "Philadelphia", "division": "NL East"},
            "pit": {"name": "Pittsburgh Pirates", "city": "Pittsburgh", "division": "NL Central"},
            "sd": {"name": "San Diego Padres", "city": "San Diego", "division": "NL West"},
            "sf": {"name": "San Francisco Giants", "city": "San Francisco", "division": "NL West"},
            "sea": {"name": "Seattle Mariners", "city": "Seattle", "division": "AL West"},
            "stl": {"name": "St. Louis Cardinals", "city": "St. Louis", "division": "NL Central"},
            "tb": {"name": "Tampa Bay Rays", "city": "Tampa", "division": "AL East"},
            "tex": {"name": "Texas Rangers", "city": "Dallas", "division": "AL West"},
            "tor": {"name": "Toronto Blue Jays", "city": "Toronto", "division": "AL East"},
            "wsh": {"name": "Washington Nationals", "city": "Washington", "division": "NL East"},
        }
    
    def load_city_coordinates(self) -> Dict[str, Tuple[float, float]]:
        """City coordinates for venue locations"""
        return {
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
            "Dallas": (32.7767, -96.7970), "Toronto": (43.6532, -79.3832),
            "Washington": (38.9072, -77.0369),
        }
    
    def scrape_team_schedule(self, team_abbrev: str, season: str = "2025") -> List[GameData]:
        """Scrape full season schedule for a team"""
        all_games = []
        
        # Get the ESPN URL team code (handles cases like cws -> chw)
        espn_team_code = self.espn_url_mappings.get(team_abbrev, team_abbrev)
        
        for half in [1, 2]:  # First and second half
            try:
                url = self.base_url.format(team=espn_team_code, half=half)
                print(f"Scraping {team_abbrev} (ESPN code: {espn_team_code}) schedule (half {half}): {url}")
                
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                
                # Parse the HTML and extract table data
                table_rows = self._parse_schedule_page(response.text, team_abbrev, season)
                
                if table_rows and len(table_rows) > 1:
                    print(f"Got {len(table_rows)} rows, parsing to GameData objects...")
                    games = self.parse_table_to_games(table_rows, team_abbrev, season)
                    all_games.extend(games)
                    print(f"Converted to {len(games)} GameData objects")
                else:
                    print(f"No table data found for {team_abbrev} half {half}")
                
                time.sleep(0.5)
                
            except Exception as e:
                print(f"Error scraping {team_abbrev} half {half}: {e}")
                continue
        
        print(f"Scraped {len(all_games)} games for {team_abbrev}")
        return all_games
    
    def scrape_all_teams_schedule(self, season: str = "2025") -> List[GameData]:
        """Scrape schedules for all MLB teams"""
        all_games = []
        seen_games = set()  # To avoid duplicates since each game appears on 2 team schedules
        
        for team_abbrev, team_info in self.mlb_teams.items():
            try:
                print(f"Scraping schedule for {team_info['name']} ({team_abbrev})...")
                team_games = self.scrape_team_schedule(team_abbrev, season)
                
                # Add unique games only
                for game in team_games:
                    game_key = f"{game.date.strftime('%Y-%m-%d')}_{game.home_team.abbreviation}_{game.away_team.abbreviation}"
                    if game_key not in seen_games:
                        all_games.append(game)
                        seen_games.add(game_key)
                
                # Longer delay between teams to be respectful
                time.sleep(1.0)
                
            except Exception as e:
                print(f"Error scraping team {team_abbrev}: {e}")
                continue
        
        print(f"Total unique games scraped: {len(all_games)}")
        return all_games
    
    def _parse_schedule_page(self, html_content: str, team_abbrev: str, season: str) -> List[List[str]]:
        """Parse ESPN schedule page HTML to extract table data"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find schedule table
            tables = soup.find_all('table')
            
            schedule_table = None
            for table in tables:
                rows = table.find_all('tr')
                if len(rows) > 5:  # Should have header + multiple game rows
                    # Check if this looks like a schedule table
                    first_few_rows_text = ' '.join([row.get_text().lower() for row in rows[:3]])
                    if any(keyword in first_few_rows_text for keyword in ['date', 'opponent', 'result', 'vs', '@']):
                        schedule_table = table
                        break
            
            if not schedule_table:
                all_rows = soup.find_all('tr')
                if len(all_rows) > 5:
                    rows = all_rows
                else:
                    print(f"No schedule data found for {team_abbrev}")
                    return []
            else:
                rows = schedule_table.find_all('tr')
            
            # Extract table data
            table_data = []
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if cells:
                    cell_texts = [cell.get_text(strip=True) for cell in cells]
                    if cell_texts and len(cell_texts) >= 3:  # Need at least date, opponent, result
                        table_data.append(cell_texts)
            
            print(f"Extracted {len(table_data)} rows for {team_abbrev}")
            return table_data
            
        except Exception as e:
            print(f"Error parsing schedule page for {team_abbrev}: {e}")
            return []
    
    def parse_table_to_games(self, table_rows: List[List[str]], team_abbrev: str, season: str = "2025") -> List[GameData]:
        """Convert table rows to GameData objects"""
        games = []
        
        if not table_rows or len(table_rows) < 2:
            return games
            
        print(f"Processing {len(table_rows)-1} games for {team_abbrev}...")
        
        # Skip header row (row 0), process data rows
        for i, row in enumerate(table_rows[1:], 1):
            try:
                game = self.parse_game_row(row, team_abbrev, season)
                if game:
                    games.append(game)
                    print(f"  ✓ Game {i}: {game.away_team.abbreviation} @ {game.home_team.abbreviation} on {game.date.strftime('%m/%d')}")
            except Exception as e:
                print(f"  ✗ Error parsing row {i}: {e}")
                continue
                
        print(f"Successfully parsed {len(games)} games for {team_abbrev}")
        return games
    
    def parse_game_row(self, row: List[str], team_abbrev: str, season: str) -> Optional[GameData]:
        """Parse individual game row into GameData"""
        if len(row) < 3:  # Need at least DATE, OPPONENT, RESULT
            return None
            
        date_str = row[0].strip()
        opponent_str = row[1].strip()
        result_str = row[2].strip() if len(row) > 2 else ""
        
        # Parse date
        game_date = self.parse_date(date_str, season)
        if not game_date:
            return None
            
        # Parse opponent and determine home/away
        is_home, opponent_abbrev = self.parse_opponent(opponent_str)
        if not opponent_abbrev:
            return None
            
        # Create team info objects
        home_team_abbrev = team_abbrev if is_home else opponent_abbrev
        away_team_abbrev = opponent_abbrev if is_home else team_abbrev
        
        home_team = self.create_team_info(home_team_abbrev)
        away_team = self.create_team_info(away_team_abbrev)
        venue = self.create_venue_info(home_team_abbrev)
        
        # Determine game status from result
        status = GameStatus.SCHEDULED if not result_str else GameStatus.FINAL
        
        # Create game ID
        game_id = f"{season}_{game_date.strftime('%Y%m%d')}_{away_team_abbrev}_{home_team_abbrev}"
        
        return GameData(
            game_id=game_id,
            date=game_date,
            home_team=home_team,
            away_team=away_team,
            venue=venue,
            status=status,
            league="MLB",
            season=season
        )
    
    def parse_date(self, date_str: str, season: str) -> Optional[datetime]:
        """Parse ESPN date format: 'Thu, Mar 27'"""
        try:
            # Remove day of week if present
            if ',' in date_str:
                date_part = date_str.split(',', 1)[1].strip()
            else:
                date_part = date_str.strip()
                
            # Parse "Mar 27" format
            parts = date_part.split()
            if len(parts) != 2:
                return None
                
            month_str, day_str = parts[0], parts[1]
            
            # Month mapping
            month_map = {
                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
            }
            
            month = month_map.get(month_str)
            if not month:
                return None
                
            day = int(day_str)
            year = int(season)
            
            # Handle year rollover for spring training / early season
            current_date = datetime.now()
            if month <= 3 and current_date.month >= 10:  # Spring training next year
                year += 1
                
            return datetime(year, month, day, 19, 0)  # Default 7 PM game time
            
        except Exception as e:
            return None
    
    def parse_opponent(self, opponent_str: str) -> Tuple[bool, Optional[str]]:
        """Parse opponent string: 'vsChicago' or '@Chicago'"""
        if not opponent_str:
            return False, None
            
        # Determine home/away
        if opponent_str.startswith('vs'):
            is_home = True
            opponent_name = opponent_str[2:].strip()
        elif opponent_str.startswith('@'):
            is_home = False
            opponent_name = opponent_str[1:].strip()
        else:
            # Fallback: assume opponent name without prefix
            is_home = True  # Default to home
            opponent_name = opponent_str.strip()
            
        # Convert opponent name to abbreviation
        opponent_abbrev = self.get_team_abbrev_from_name(opponent_name)
        
        return is_home, opponent_abbrev
    
    def get_team_abbrev_from_name(self, team_name: str) -> Optional[str]:
        """Convert team city/name to MLB abbreviation"""
        # Clean the team name
        team_name = team_name.strip().lower()
        
        # Direct city/team name to abbreviation mapping
        name_to_abbrev = {
            # By city name
            'arizona': 'ari', 'atlanta': 'atl', 'baltimore': 'bal', 'boston': 'bos',
            'chicago': 'chc',  # Default to Cubs, will need context for White Sox
            'cincinnati': 'cin', 'cleveland': 'cle', 'colorado': 'col', 'detroit': 'det',
            'houston': 'hou', 'kansas': 'kc', 'losangeles': 'lad',  # Default to Dodgers
            'miami': 'mia', 'milwaukee': 'mil', 'minnesota': 'min', 'newyork': 'nyy',  # Default to Yankees
            'oakland': 'oak', 'philadelphia': 'phi', 'pittsburgh': 'pit', 'sandiego': 'sd',
            'sanfrancisco': 'sf', 'seattle': 'sea', 'stlouis': 'stl', 'tampabay': 'tb',
            'texas': 'tex', 'toronto': 'tor', 'washington': 'wsh',
            
            # By team name
            'diamondbacks': 'ari', 'braves': 'atl', 'orioles': 'bal', 'redsox': 'bos',
            'cubs': 'chc', 'whitesox': 'cws', 'reds': 'cin', 'guardians': 'cle',
            'rockies': 'col', 'tigers': 'det', 'astros': 'hou', 'royals': 'kc',
            'angels': 'laa', 'dodgers': 'lad', 'marlins': 'mia', 'brewers': 'mil',
            'twins': 'min', 'mets': 'nym', 'yankees': 'nyy', 'athletics': 'oak',
            'phillies': 'phi', 'pirates': 'pit', 'padres': 'sd', 'giants': 'sf',
            'mariners': 'sea', 'cardinals': 'stl', 'rays': 'tb', 'rangers': 'tex',
            'bluejays': 'tor', 'nationals': 'wsh'
        }
        
        # Remove spaces and special characters
        clean_name = re.sub(r'[^a-z]', '', team_name)
        
        return name_to_abbrev.get(clean_name)
    
    def create_team_info(self, team_abbrev: str) -> TeamInfo:
        """Create TeamInfo object from abbreviation"""
        team_data = self.team_mappings.get(team_abbrev.lower(), {})
        
        return TeamInfo(
            team_id=team_abbrev.lower(),
            abbreviation=team_abbrev.upper(),
            display_name=team_data.get('name', f'Team {team_abbrev.upper()}'),
            location=team_data.get('city', ''),
            color='#000000',
            alternate_color='#FFFFFF',
            division=team_data.get('division', '')
        )
    
    def create_venue_info(self, home_team_abbrev: str) -> Venue:
        """Create Venue object"""
        team_data = self.team_mappings.get(home_team_abbrev.lower(), {})
        city = team_data.get('city', '')
        coords = self.city_coordinates.get(city, (0.0, 0.0))
        
        return Venue(
            venue_id=f"{home_team_abbrev}_stadium",
            name=f"{city} Stadium",
            city=city,
            state='',
            country='USA',
            latitude=coords[0],
            longitude=coords[1]
        )


class TravelInferenceEngine:
    """Engine to infer team travel patterns from game schedules"""
    
    def __init__(self, airport_mappings: Dict[str, str]):
        self.airport_mappings = airport_mappings
    
    def infer_travel_from_games(self, games: List[GameData]) -> List[TeamTravelData]:
        """Infer team travel patterns from game schedule"""
        travel_data = []
        
        # Group games by team
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
        
        # Infer travel for each team
        for team_id, schedule in team_schedules.items():
            # Sort by date
            schedule.sort(key=lambda x: x[0].date)
            
            team_travel = self._infer_team_travel(schedule)
            travel_data.extend(team_travel)
        
        return travel_data
    
    def _infer_team_travel(self, team_schedule: List[Tuple[GameData, str]]) -> List[TeamTravelData]:
        """Infer travel for a specific team's schedule"""
        travel_data = []
        
        for i, (game, home_away) in enumerate(team_schedule):
            if home_away == 'away':
                # Team is traveling to an away game
                team_info = game.away_team
                departure_city = self._get_clean_city_name(team_info.location)
                arrival_city = self._get_clean_city_name(game.venue.city)
                
                # Skip if we don't have valid city names
                if not departure_city or not arrival_city:
                    continue
                
                # Skip if departure and arrival are the same
                if departure_city == arrival_city:
                    continue
                
                # Estimate travel date (typically 1 day before game)
                game_date = game.date
                if hasattr(game_date, 'tzinfo') and game_date.tzinfo is not None:
                    game_date = game_date.replace(tzinfo=None)
                
                travel_date = game_date - timedelta(days=1)
                
                # Get airport codes
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
                
                # Return trip (day after game)
                return_date = game_date + timedelta(days=1)
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
        
        # Handle common city name issues
        city_mappings = {
            "St. Petersburg": "Tampa",
            "Anaheim": "Los Angeles",
            "Arlington": "Dallas",
            "Queens": "New York",
            "Bronx": "New York",
        }
        
        return city_mappings.get(clean_name, clean_name)


class ESPNSportsDataAggregator(QObject):
    """ESPN sports data aggregator with database integration"""
    
    dataUpdated = pyqtSignal(list)
    progressUpdated = pyqtSignal(int)
    errorOccurred = pyqtSignal(str)
    seasonDataLoaded = pyqtSignal(str, int)
    
    def __init__(self, config: Dict[str, str], db_path: str = "sports_data.db"):
        super().__init__()
        
        # Import DatabaseManager here to avoid circular imports
        try:
            from database_manager import DatabaseManager
            self.db = DatabaseManager(db_path)
        except ImportError:
            print("Error: DatabaseManager not found. Please ensure database_manager.py is available.")
            raise
        
        # Initialize scraper and inference engine
        self.espn_scraper = ESPNScheduleScraper()
        self.inference_engine = TravelInferenceEngine(self.espn_scraper.team_airports)
        
        # Current state
        self.current_season = str(datetime.now().year)
        self.current_league = "MLB"
        self.all_season_games = []
        self.current_travel_data = []
        self.teams_cache = {}
        
        # Load teams from database or initialize
        self.load_teams()
    
    def load_teams(self):
        """Load team information from database or initialize from scraper"""
        # Try to load from database first
        teams = self.db.load_teams()
        
        if not teams:
            # No teams in database, load from scraper and save
            print("No teams found in database, initializing from scraper...")
            teams = []
            for abbrev, info in self.espn_scraper.mlb_teams.items():
                team = TeamInfo(
                    team_id=abbrev,
                    abbreviation=abbrev.upper(),
                    display_name=info['name'],
                    location=info['city'],
                    color='#000000',
                    alternate_color='#FFFFFF',
                    division=info['division'],
                    league="MLB"
                )
                teams.append(team)
            
            # Save teams to database
            self.db.save_teams(teams)
        
        self.teams_cache = {team.team_id: team for team in teams}
        print(f"Loaded {len(teams)} MLB teams from database")
    
    def load_full_season_schedule(self, season: str = None, force_refresh: bool = False):
        """Load complete season schedule - from database if available, otherwise scrape"""
        if season is None:
            season = self.current_season
        
        try:
            # Check if we should use cached data
            if not self.db.should_refresh_season(season, force_refresh):
                print(f"Loading {season} season from database cache...")
                self.progressUpdated.emit(20)
                
                # Load from database
                games = self.db.load_games(season, self.current_league)
                travel_data = self.db.load_travel_data(season)
                
                if games and travel_data:
                    self.all_season_games = games
                    self.current_travel_data = travel_data
                    self.dataUpdated.emit(travel_data)
                    self.seasonDataLoaded.emit(season, len(games))
                    self.progressUpdated.emit(100)
                    print(f"Loaded {len(games)} games and {len(travel_data)} travel records from database")
                    return
                else:
                    print(f"No cached data found for {season}, will scrape...")
            
            # Need to scrape new data
            print(f"Scraping {season} season schedule from ESPN...")
            self.progressUpdated.emit(10)
            
            # Clear existing data for this season
            self.db.clear_season_data(season, self.current_league)
            
            # Scrape all teams
            season_games = self.espn_scraper.scrape_all_teams_schedule(season)
            
            if season_games:
                self.progressUpdated.emit(60)
                
                # Save games to database
                self.db.save_games(season_games, season, self.current_league)
                self.progressUpdated.emit(70)
                
                # Generate and save travel patterns
                travel_data = self.inference_engine.infer_travel_from_games(season_games)
                self.db.save_travel_data(travel_data, season)
                self.progressUpdated.emit(90)
                
                # Update current state
                self.all_season_games = season_games
                self.current_travel_data = travel_data
                
                # Emit signals
                self.dataUpdated.emit(travel_data)
                self.seasonDataLoaded.emit(season, len(season_games))
                
                print(f"Scraped and saved {len(season_games)} games and {len(travel_data)} travel records")
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No games found for {season} season")
                
        except Exception as e:
            error_msg = f"Failed to load {season} season: {str(e)}"
            print(error_msg)
            self.errorOccurred.emit(error_msg)
            self.progressUpdated.emit(0)
    
    def load_team_season_schedule(self, team_id: str, season: str = None):
        """Load schedule for specific team from database or scrape if needed"""
        if season is None:
            season = self.current_season
        
        try:
            self.progressUpdated.emit(20)
            
            # Try to load from database first
            team_travel = self.db.load_travel_data(season, team_id)
            
            if team_travel:
                print(f"Loaded {len(team_travel)} travel records for {team_id} from database")
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
                return
            
            # No cached data, scrape if needed
            print(f"No cached data for {team_id}, checking full season data...")
            
            # Check if we have any season data
            is_cached, _ = self.db.is_season_cached(season)
            if not is_cached:
                # Need to load full season first
                self.load_full_season_schedule(season)
                return
            
            # Load team-specific data from existing season data
            team_travel = self.db.load_travel_data(season, team_id)
            if team_travel:
                self.dataUpdated.emit(team_travel)
                self.progressUpdated.emit(100)
            else:
                self.errorOccurred.emit(f"No travel data found for team {team_id} in {season}")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load team schedule: {str(e)}")
    
    def get_current_week_schedule(self):
        """Get current week games from database or load season if needed"""
        try:
            # Check if we have current season data
            is_cached, _ = self.db.is_season_cached(self.current_season)
            
            if not is_cached:
                # Load full season first
                print("No current season data found, loading full season...")
                self.load_full_season_schedule(self.current_season)
                return
            
            # Load current week from database
            today = datetime.now()
            week_start = today - timedelta(days=3)
            week_end = today + timedelta(days=4)
            
            # Load all travel data and filter for current week
            all_travel = self.db.load_travel_data(self.current_season)
            current_week_travel = [
                travel for travel in all_travel
                if week_start <= travel.travel_date <= week_end
            ]
            
            if current_week_travel:
                self.dataUpdated.emit(current_week_travel)
                print(f"Loaded {len(current_week_travel)} travel records for current week")
            else:
                self.errorOccurred.emit("No current week travel found")
                
        except Exception as e:
            self.errorOccurred.emit(f"Failed to load current week: {str(e)}")
    
    def get_travel_by_date_range(self, start_date: datetime, end_date: datetime) -> List[TeamTravelData]:
        """Filter travel data by date range from database"""
        try:
            # Load travel data for current season
            all_travel = self.db.load_travel_data(self.current_season)
            
            # Filter by date range
            filtered_travel = [
                travel for travel in all_travel
                if start_date <= travel.travel_date <= end_date
            ]
            
            return filtered_travel
            
        except Exception as e:
            print(f"Error filtering travel by date range: {e}")
            return []
    
    def get_team_info(self, team_id: str) -> Optional[TeamInfo]:
        """Get team information by ID"""
        return self.teams_cache.get(team_id)
    
    def get_all_teams(self) -> List[TeamInfo]:
        """Get all cached teams"""
        return list(self.teams_cache.values())
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        return self.db.get_database_stats()
    
    def get_cached_seasons(self) -> List[Dict[str, Any]]:
        """Get information about cached seasons"""
        return self.db.get_cached_seasons(self.current_league)
    
    def clear_season_cache(self, season: str):
        """Clear cache for specific season"""
        try:
            self.db.clear_season_data(season, self.current_league)
            print(f"Cleared cache for {season} season")
        except Exception as e:
            print(f"Error clearing season cache: {e}")


#############################AMADEUS FLIGH########################################################################


