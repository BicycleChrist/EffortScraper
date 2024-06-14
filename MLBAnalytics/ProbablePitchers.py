import requests
from bs4 import BeautifulSoup
import pprint
from concurrent.futures import ThreadPoolExecutor
from BBSavant_statcast import scrape
from daily_lineups import GetPage
from BBSplayer_ids import pitchers  # Import the pitchers dictionary

# global variable to store pitcher data results, probably a dumb idea 
_pitcher_data_results = None

def FetchProbablePitchers() -> BeautifulSoup | None:
    url = 'https://www.mlb.com/probable-pitchers'
    soup = GetPage(url)
    return soup

def ReformatPitcherNames(pitcher_names):
    formatted_pitchers = {}
    for name in pitcher_names:
        parts = name.split()
        if len(parts) == 2:
            formatted_name = f"{parts[1]}, {parts[0]}"
        else:
            formatted_name = name  # handle cases where the name doesn't split into two parts

        # Perform lookup in the pitchers dictionary
        player_id = pitchers.get(formatted_name)
        if player_id:
            formatted_pitchers[formatted_name] = player_id
        else:
            formatted_pitchers[formatted_name] = None  # handle cases where ID is not found

    return formatted_pitchers

def ParseProbablePitchers(soup):
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
        newdict: dict = {attr_name: soupvar.attrs[f"data-{attr_name}"]}
        if into_dict is not None: into_dict.update(newdict)
        return newdict

    main_content = FIND(main_content, 'div', "container").extract()
    del soup

    all_data = {}
    matchups = FINDALL(main_content, 'div', 'matchup')
    date_button = FIND(main_content, 'div', "datepicker", id="probable-pitchers__datepicker")
    page_date = GRABATTR(date_button, "date")
    print(page_date)

    game_selector = FIND(main_content, 'div', "scores--game-selector")
    # unfortunately, the gameslist is loaded via javascript, so we can't get this
    # gameslist = game_selector.find('section').find('ul', class_="mlb-scores__list mlb-scores__list--games")
    # note: all times Eastern (EST?)

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
        game_details = FIND(game_info, 'div', "game-details")
        # TODO: game_details

        for teamside in ("away", "home"):
            teamdict = matchup_dict["teams"][teamside]  # aliasing the dict; writes will affect matchup_dict
            teamdict['side'] = teamside
            # finding teamname and ids
            GRABATTR(game, f"team-id-{teamside}", teamdict)
            name = FIND(game, 'span', f"team-name--{teamside}")
            teamdict["name"] = name.text.strip()

            # finding logo (which also has the record)
            logodict = teamdict["logo"] = {}
            logodiv = FIND(game_info, 'div', f"team-logo--{teamside}")
            img = logodiv.find('img')
            GRABATTR(img, "src", logodict)
            record = FIND(logodiv, 'div', "team-record")  # this is literally under the logo
            logodict['record'] = record.text.strip()
            teamdict['record'] = record.text.strip()

        matchup_dict["title"] = f"{matchup_dict['teams']['away']['name']} vs " \
                                f"{matchup_dict['teams']['home']['name']}"

        pitchers = FIND(matchup, 'div', "pitchers").extract()
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
                span_key = span.attrs["class"][0].removeprefix(f"{classPrefix}pitcher-")
                span_val = span.text.strip()
                player_dict[span_key] = span_val

        matchup_dict["pitchers"] = pitcher_dict
        all_data["matchups"].append(matchup_dict)
    return all_data

def scrape_pitcher_data():
    global _pitcher_data_results
    if _pitcher_data_results is not None:
        return _pitcher_data_results
    
    soup = FetchProbablePitchers()
    if not soup:
        return None
    pitcherData = ParseProbablePitchers(soup)
    #print("\nParsed Pitcher Data:")
    #pprint.pprint(pitcherData, indent=2)

    all_pitcher_names = []
    for matchup in pitcherData["matchups"]:
        all_pitcher_names.extend(matchup["pitchers"].keys())

    formatted_pitcher_id_map = ReformatPitcherNames(all_pitcher_names)

    pitcher_data_results = {}
    with ThreadPoolExecutor(max_workers=None) as executor:
        futures = {
            executor.submit(scrape, pitcher_name, player_id, True): pitcher_name
            for pitcher_name, player_id in formatted_pitcher_id_map.items() if player_id is not None
        }
        pitcher_data_results = {
            pitcher_name: future.result()
            for future, pitcher_name in futures.items()
        }

    print("\nPitcher Data Results:")
    pprint.pprint(pitcher_data_results, indent=2)
    _pitcher_data_results = pitcher_data_results
    return _pitcher_data_results

if __name__ == "__main__":
    scrape_pitcher_data()


