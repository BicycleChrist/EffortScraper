import ncaa_cbb_teamnames

DEFAULT_LEAGUE_SELECT = "NHL"
FOURNUMBER_SPREAD_LEAGUES = ["NBA", "NFL", "NCAAB"]
# TODO: replaces usages of 'FOURNUMBER_SPREAD_LEAGUES' with a lookup into OddsFormat

OddsFormat = {
    "Moneyline": {
        "FOURNUMBER_FORMAT": False,
        "NumberFormat": [int, float, int, float],
    },
    "Spread": {
        "FOURNUMBER_FORMAT": True,
    },
    "Total": {
        "FOURNUMBER_FORMAT": True,
    },
    "Combined": {
        "FOURNUMBER_FORMAT": True,
    },
}

default_format_map = {
    "NHL": "Moneyline",
    "NBA": "Spread",
    "NFL": "Spread",
    "MLB": "Moneyline",
    "NCAAB": "Spread",
}

# we intend to use the NumberFormat like this:
def ConvertTextToNumbers(listofstrings=['55', '1.3', '99', '2.2'], OddsFormatSelection="Moneyline"):
    actualnumbers = []
    for numtype, numtext in zip(OddsFormat[OddsFormatSelection]["NumberFormat"], listofstrings):
        actualnumber = numtype(numtext)
        actualnumbers.append(actualnumber)
        print(actualnumber)
        print(f"actualnumber x 2 = {actualnumber * 2}")
    return actualnumbers
# beware of the mutable default-argument bullshit (listofstrings)


nhl_teams = [
    'Anaheim Ducks', 'Arizona Coyotes', 'Boston Bruins', 'Buffalo Sabres',
    'Calgary Flames', 'Carolina Hurricanes', 'Chicago Blackhawks',
    'Colorado Avalanche', 'Columbus Blue Jackets', 'Dallas Stars', 'Detroit Red Wings',
    'Edmonton Oilers', 'Florida Panthers', 'Los Angeles Kings', 'Minnesota Wild',
    'Montreal Canadiens', 'Nashville Predators', 'New Jersey Devils', 'New York Islanders',
    'New York Rangers', 'Ottawa Senators', 'Philadelphia Flyers', 'Pittsburgh Penguins',
    'San Jose Sharks', 'Seattle Kraken', 'St. Louis Blues', 'Tampa Bay Lightning',
    'Toronto Maple Leafs', 'Vegas Golden Knights', 'Washington Capitals',
    'Winnipeg Jets', 'Vancouver Canucks'
]

nba_teams = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "LA Clippers": "LAC",  # #2 team in LA so the get the abbrev
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS"
}

nfl_teams = {
    'Arizona Cardinals': 'ARI',
    'Atlanta Falcons': 'ATL',
    'Baltimore Ravens': 'BAL',
    'Buffalo Bills': 'BUF',
    'Carolina Panthers': 'CAR',
    'Chicago Bears': 'CHI',
    'Cincinnati Bengals': 'CIN',
    'Cleveland Browns': 'CLE',
    'Dallas Cowboys': 'DAL',
    'Denver Broncos': 'DEN',
    'Detroit Lions': 'DET',
    'Green Bay Packers': 'GB',
    'Houston Texans': 'HOU',
    'Indianapolis Colts': 'IND',
    'Jacksonville Jaguars': 'JAX',
    'Kansas City Chiefs': 'KC',
    'Las Vegas Raiders': 'LV',
    'Los Angeles Chargers': 'LAC',
    'Los Angeles Rams': 'LAR',
    'Miami Dolphins': 'MIA',
    'Minnesota Vikings': 'MIN',
    'New England Patriots': 'NE',
    'New Orleans Saints': 'NO',
    'New York Giants': 'NYG',
    'New York Jets': 'NYJ',
    'Philadelphia Eagles': 'PHI',
    'Pittsburgh Steelers': 'PIT',
    'San Francisco 49ers': 'SF',
    'Seattle Seahawks': 'SEA',
    'Tampa Bay Buccaneers': 'TB',
    'Tennessee Titans': 'TEN',
    'Washington Commanders': 'WAS'
}

mlb_teams = {
    'Arizona Diamondbacks': 'Phoenix',
    'Atlanta Braves': 'Atlanta',
    'Baltimore Orioles': 'Baltimore',
    'Boston Red Sox': 'Boston',
    'Chicago White Sox': 'Chicago',
    'Chicago Cubs': 'Chicago',
    'Cincinnati Reds': 'Cincinnati',
    'Cleveland Guardians': 'Cleveland',
    'Colorado Rockies': 'Denver',
    'Detroit Tigers': 'Detroit',
    'Houston Astros': 'Houston',
    'Kansas City Royals': 'Kansas City',
    'Los Angeles Angels': 'Los Angeles',
    'Los Angeles Dodgers': 'Los Angeles',
    'Miami Marlins': 'Miami',
    'Milwaukee Brewers': 'Milwaukee',
    'Minnesota Twins': 'Minneapolis',
    'New York Yankees': 'New York',
    'New York Mets': 'New York',
    'Oakland Athletics': 'Oakland',
    'Philadelphia Phillies': 'Philadelphia',
    'Pittsburgh Pirates': 'Pittsburgh',
    'San Diego Padres': 'San Diego',
    'San Francisco Giants': 'San Francisco',
    'Seattle Mariners': 'Seattle',
    'St. Louis Cardinals': 'St. Louis',
    'Tampa Bay Rays': 'Tampa Bay',
    'Texas Rangers': 'Arlington',
    'Toronto Blue Jays': 'Toronto',
    'Washington Nationals': 'Washington, D.C.'
}


leaguemap = {
    "NHL": nhl_teams,
    "NBA": nba_teams.keys(),
    "NFL": nfl_teams.keys(),
    "MLB": mlb_teams.keys(),
    "NCAAB": ncaa_cbb_teamnames.ncaa_college_teams
}

