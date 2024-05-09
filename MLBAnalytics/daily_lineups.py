import requests
from bs4 import BeautifulSoup

# URL of  page
url = 'https://www.rotowire.com/baseball/daily-lineups.php'

# Fetch URL
response = requests.get(url)

# Check if the request was successful (status code 200)
if response.status_code == 200:
    # Parse the HTML content of the page
    soup = BeautifulSoup(response.content, 'html.parser')

    # List of bottom level div classes, with game state determining name of div
    bottom_level_classes = ['lineup is-mlb has-started not-in-slate', 'lineup is-mlb has-started', 'lineup is-mlb', 'lineup is-mlb is-postponed has-started not-in-slate']

    # Iterate through each bottom level div class
    for class_name in bottom_level_classes:
        # Find all bottom level divs with the current class
        bottom_level_divs = soup.find_all('div', class_=class_name)

        # Iterate through each bottom level div
        for div in bottom_level_divs:
            # Find the relevant data within the div
            lineup_box = div.find('div', class_='lineup__box')
            if lineup_box:
                # Extract team abbreviations
                team_abbrevs = lineup_box.find_all('div', class_='lineup__abbr')
                if len(team_abbrevs) == 2:
                    away_team = team_abbrevs[0].text.strip()
                    home_team = team_abbrevs[1].text.strip()
                else:
                    print("Unable to find team abbreviations")
                    continue

                print("Matchup: {} vs {}".format(away_team, home_team))

                # Find relevant lineup lists
                lineup_main = lineup_box.find('div', class_='lineup__main')
                if lineup_main:
                    lineup_list_visit = lineup_main.find('ul', class_='lineup__list is-visit')
                    lineup_list_home = lineup_main.find('ul', class_='lineup__list is-home')

                    # visiting team lineup
                    if lineup_list_visit:
                        players_visit = lineup_list_visit.find_all('li', class_='lineup__player')
                        print("Visiting Team Lineup ({}):".format(away_team))
                        for player in players_visit[:9]:  # Limit to first 9 players
                            player_data = player.text.strip()
                            print(player_data)

                    # home team lineup
                    if lineup_list_home:
                        players_home = lineup_list_home.find_all('li', class_='lineup__player')
                        print("Home Team Lineup ({}):".format(home_team))
                        for player in players_home[:9]:  # Limit to first 9 players
                            player_data = player.text.strip()
                            print(player_data)

                    # Find and process data in lineup__bottom div
                    lineup_bottom = div.find('div', class_='lineup__bottom')
                    if lineup_bottom:
                        # Umpire
                        umpire_info = lineup_bottom.find('div', class_='lineup__umpire')
                        if umpire_info:
                            umpire_text = umpire_info.get_text(strip=True)
                            print(format(umpire_text))

                        # Weather
                        weather_info = lineup_bottom.find('div', class_='lineup__weather')
                        if weather_info:
                            weather_text = weather_info.get_text(strip=False)
                            print("Weather: {}".format(weather_text))

                        # More odds LOL
                        #odds_info = lineup_bottom.find('div', class_='lineup__odds')
                        #if odds_info:
                            #odds_text = odds_info.get_text(strip=True)
                            #print("Odds: {}".format(odds_text))
                    else:
                        print("No lineup__bottom found for this bottom level div")
                else:
                    print("No lineup main found for this bottom level div")
            else:
                print("No lineup box found for this bottom level div")
else:
    print("Failed to retrieve the webpage. Status code:", response.status_code)
