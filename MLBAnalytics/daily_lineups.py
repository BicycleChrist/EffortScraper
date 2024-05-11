import requests
from bs4 import BeautifulSoup
import pprint


# either returns 'soup' or 'None' (if bad response code)
def GetPage(url) -> BeautifulSoup | None:
    response = requests.get(url)
    if response.status_code != 200:
        print(f"not-good response code: {response.status_code}")
        return None
    return BeautifulSoup(response.content, 'html.parser')


def Main():
    soup = GetPage('https://www.rotowire.com/baseball/daily-lineups.php')
    if soup is None: exit(0)
    
    # List of bottom level div classes, with game state determining name of div
    bottom_level_classes = [
        'lineup is-mlb has-started not-in-slate', 
        'lineup is-mlb has-started', 
        'lineup is-mlb', 
        'lineup is-mlb is-postponed has-started not-in-slate'
    ]
    bottom_level_divs = [soup.find_all('div', class_=class_name) for class_name in bottom_level_classes]
    
    lineup_boxes = []
    for resultset in bottom_level_divs:
        lineup_boxes.extend(div.find('div', class_='lineup__box') for div in resultset)
    
    # Extract team abbreviations
    team_abbrevs = [lineup_box.find_all('div', class_='lineup__abbr') for lineup_box in lineup_boxes]
    
    # Find relevant lineup lists
    # the 'POSTPONED' games will obviously cause problems when searching for things underneath them
    lineup_mains = [lineup_box.find('div', class_='lineup__main') for lineup_box in lineup_boxes if not "POSTPONED" in lineup_box.text]
    lineup_list_visits = [lineup_main.find('ul', class_='lineup__list is-visit') for lineup_main in lineup_mains]
    lineup_list_homes  = [lineup_main.find('ul', class_='lineup__list is-home')  for lineup_main in lineup_mains]
    players_visits = [lineup_list_visit.find_all('li', class_='lineup__player') for lineup_list_visit in lineup_list_visits]
    players_homes   = [lineup_list_home.find_all('li', class_='lineup__player') for lineup_list_home in lineup_list_homes]
    
    # Find and process data in lineup__bottom div
    lineup_bottoms = [div.find('div', class_='lineup__bottom') for div in lineup_boxes] 
    umpire_infos = [lineup_bottom.find('div', class_='lineup__umpire') for lineup_bottom in lineup_bottoms]
    weather_infos = [lineup_bottom.find('div', class_='lineup__weather') for lineup_bottom in lineup_bottoms]
    umpire_texts = [umpire_info.get_text(strip=True) for umpire_info in umpire_infos]
    weather_texts = ["Weather: {}".format(weather_info.get_text(strip=True)) for weather_info in weather_infos]
    
    teamlists_zipped = list(zip(team_abbrevs, players_homes, players_visits, umpire_texts, weather_texts))
    matchups = []
    for teams, players_h, players_v, umpiretext, weathertext in teamlists_zipped:
        # looks like these actually give three things, seperated by newline?
        # away teams are always listed first
        away_team = teams[0].text.strip()
        home_team = teams[1].text.strip()
        
        def FormatPlayerData(player):
            segments = player.text.strip().split('\n')
            return segments[1], segments[0], segments[2]
        
        matchup = {
            "Matchup": "{} vs {}".format(away_team, home_team),
            "Team_Lineups": {
                # away teams are always listed first
                away_team: [FormatPlayerData(player) for player in players_v],
                home_team: [FormatPlayerData(player) for player in players_h],
            },
            "Umpire": umpiretext.removeprefix("Umpire:"),
            "Weather": weathertext.removeprefix("Weather:"),
        }
        matchups.append(matchup)
    
    return matchups


if __name__ == "__main__":
    result = Main()
    pprint.pprint(result, indent=2)
    
