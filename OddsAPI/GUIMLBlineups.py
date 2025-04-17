import requests
from bs4 import BeautifulSoup
import traceback


def get_page(url) -> BeautifulSoup | None:
    """Fetch a web page and return a BeautifulSoup object, or None if the request fails."""
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            print(f"not-good response code: {response.status_code}")
            return None
        return BeautifulSoup(response.content, 'html.parser')
    except Exception as e:
        print(f"Error fetching page {url}: {e}")
        return None


def fetch_probable_pitchers():
    """Fetch and parse probable pitchers from MLB.com."""
    url = 'https://www.mlb.com/probable-pitchers'
    soup = get_page(url)
    if not soup:
        print("[fetch_probable_pitchers] WARNING: 'get_page' appears to have failed.")
        return {}

    return parse_probable_pitchers(soup)


def parse_probable_pitchers(soup):
    """Parse probable pitchers data from the fetched MLB.com page."""
    try:
        main_content = soup.find('main').find('div', class_="container").extract()

        classPrefix = 'probable-pitchers__'  # every HTML class starts with this
        # convenience functions to make searching easier (to write)
        def FIND(soupvar, html_tag, class_string='', **kwargs):
            if len(class_string) > 0:
                kwargs.update({"class_": f'{classPrefix + class_string}'})
            return soupvar.find(html_tag, **kwargs)

        def FINDALL(soupvar, html_tag, class_string='', **kwargs):
            if len(class_string) > 0:
                kwargs.update({"class_": f'{classPrefix + class_string}'})
            return soupvar.find_all(html_tag, **kwargs)

        def GRABATTR(soupvar, attr_name: str, into_dict: dict | None = None):
            if f"data-{attr_name}" not in soupvar.attrs:
                return {}
            newdict: dict = {attr_name: soupvar.attrs[f"data-{attr_name}"]}
            if into_dict is not None: into_dict.update(newdict)
            return newdict

        main_content = FIND(main_content, 'div', "container").extract()

        all_data = {}
        matchups = FINDALL(main_content, 'div', 'matchup')
        date_button = FIND(main_content, 'div', "datepicker", id="probable-pitchers__datepicker")
        if date_button:
            page_date = GRABATTR(date_button, "date")
            all_data.update(page_date)

        all_data["matchups"] = []
        for matchup in matchups:
            matchup_dict = {
                "title": str,  # team1 vs team2
                "gamepk": int,  # just an arbitrary number
                "teams": {
                    "away": {},
                    "home": {},
                },
                "pitchers": {},
            }
            GRABATTR(matchup, "gamepk", matchup_dict)

            ### --- BEGIN: 'game' --- ###
            game = FIND(matchup, 'div', "game").extract()
            game_info = FIND(game, 'div', "game-info")

            for teamside in ("away", "home"):
                teamdict = matchup_dict["teams"][teamside]  # aliasing the dict; writes will affect matchup_dict
                teamdict['side'] = teamside
                # finding teamname and ids
                GRABATTR(game, f"team-id-{teamside}", teamdict)
                name = FIND(game, 'span', f"team-name--{teamside}")
                if name:
                    teamdict["name"] = name.text.strip()

                # finding logo (which also has the record)
                logodict = teamdict["logo"] = {}
                logodiv = FIND(game_info, 'div', f"team-logo--{teamside}")
                if logodiv:
                    img = logodiv.find('img')
                    if img:
                        GRABATTR(img, "src", logodict)
                    record = FIND(logodiv, 'div', "team-record")  # this is literally under the logo
                    if record:
                        logodict['record'] = record.text.strip()
                        teamdict['record'] = record.text.strip()

            if "name" in matchup_dict['teams']['away'] and "name" in matchup_dict['teams']['home']:
                matchup_dict["title"] = f"{matchup_dict['teams']['away']['name']} vs " \
                                        f"{matchup_dict['teams']['home']['name']}"

            pitchers = FIND(matchup, 'div', "pitchers")
            if pitchers:
                pitchers = pitchers.extract()
                pitcher_summaries = FINDALL(pitchers, 'div', 'pitcher-summary')

                pitcher_dict = {}
                for summary in pitcher_summaries:
                    name_tag = summary.find('a')
                    if name_tag:
                        name = name_tag.text
                    else:
                        name_tag = FIND(summary, 'div', 'pitcher-name')
                        name = name_tag.text.strip() if name_tag and "TBD" in name_tag.text else "TBD"

                    player_dict = pitcher_dict[name] = {}
                    spans = FINDALL(summary, 'span')
                    for span in spans:
                        if "class" in span.attrs and len(span.attrs["class"]) > 0:
                            span_key = span.attrs["class"][0].removeprefix(f"{classPrefix}pitcher-")
                            span_val = span.text.strip()
                            player_dict[span_key] = span_val

                matchup_dict["pitchers"] = pitcher_dict
            all_data["matchups"].append(matchup_dict)

        return all_data
    except Exception as e:
        print(f"Error in parse_probable_pitchers: {e}")
        traceback.print_exc()
        return {"matchups": []}


def fetch_daily_lineups():
    """Fetch and parse daily lineups from Rotowire."""
    soup = get_page('https://www.rotowire.com/baseball/daily-lineups.php')
    if soup is None:
        return []

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
    umpire_texts = [umpire_info.get_text(strip=True) if umpire_info else "" for umpire_info in umpire_infos]
    weather_texts = ["Weather: {}".format(weather_info.get_text(strip=True)) if weather_info else "" for weather_info in weather_infos]

    teamlists_zipped = list(zip(team_abbrevs, players_homes, players_visits, umpire_texts, weather_texts))
    matchups = []
    for teams, players_h, players_v, umpiretext, weathertext in teamlists_zipped:
        if len(teams) < 2:
            continue  # Skip incomplete data

        # Away teams are always listed first
        away_team = teams[0].text.strip()
        home_team = teams[1].text.strip()

        def FormatPlayerData(player):
            segments = player.text.strip().split('\n')
            if len(segments) >= 3:
                return segments[1], segments[0], segments[2]
            return "", "", ""  # Return empty data for malformed entries

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


def get_todays_pitchers():
    """Extract just the pitcher names from the probable pitchers data."""
    try:
        pitcher_data = fetch_probable_pitchers()

        if not pitcher_data or "matchups" not in pitcher_data:
            print("No matchups found in pitcher data")
            return []

        # Extract just the pitcher names
        pitcher_names = []
        team_pitcher_map = {}

        for matchup in pitcher_data.get("matchups", []):
            away_team = matchup["teams"]["away"].get("name", "")
            home_team = matchup["teams"]["home"].get("name", "")

            for pitcher_name, pitcher_info in matchup.get("pitchers", {}).items():
                if pitcher_name != "TBD":
                    pitcher_names.append(pitcher_name)

                    # Try to determine which team this pitcher is on
                    team_name = None
                    for key, value in pitcher_info.items():
                        if key.lower() == "team":
                            team_name = value
                            break

                    # If we found a team, add to the map
                    if team_name:
                        if team_name == away_team:
                            team_pitcher_map[pitcher_name] = {"team": team_name, "side": "away"}
                        elif team_name == home_team:
                            team_pitcher_map[pitcher_name] = {"team": team_name, "side": "home"}
                        else:
                            team_pitcher_map[pitcher_name] = {"team": team_name, "side": "unknown"}

        print(f"Found {len(pitcher_names)} probable pitchers for today")
        return pitcher_names, team_pitcher_map

    except Exception as e:
        print(f"Error getting today's pitchers: {e}")
        traceback.print_exc()
        return [], {}


def get_weather_by_team_matchup():
    """Returns a dict mapping team matchups to weather descriptions."""
    try:
        lineup_data = fetch_daily_lineups()
        weather_map = {}

        for entry in lineup_data:
            matchup = entry.get("Matchup", "").strip()
            weather = entry.get("Weather", "").strip()
            if matchup and weather:
                weather_map[matchup] = weather

        return weather_map
    except Exception as e:
        print(f"Error extracting weather data: {e}")
        return {}


