import pathlib
from bs4 import BeautifulSoup
import argparse

# List to store formatted strings
formatted_strings = []


def GetSavePath(leagueselect="nhl", purpose="source"):
    cwd = pathlib.Path.cwd()
    assert cwd.name == "Boddssuck", "you're in the wrong directory"
    subdirs = {
        "source": "pagesource",
        "parsed": "parsedpage",
    }
    if not purpose in subdirs.keys():
        print(f"invalid selection for purpose: {purpose}")
        print(f"valid options are: {(subdirs.keys())}")
        return None
    savepath = cwd / subdirs[purpose] / f"{leagueselect}.html"
    return savepath


def LoadPagesource(leagueselect="nhl"):
    filepath = GetSavePath(leagueselect, "source")
    if not filepath:
        print(f"LoadPagesource could not get filepath for: {leagueselect}")
        return None
    with open(filepath, 'r', encoding='utf-8') as page_source:
        soup = BeautifulSoup(page_source, 'html.parser')
        return soup


def ParseUB(page_source: BeautifulSoup):
    top_container = page_source.find('div', attrs={'class': 'd-flex flex-grow-1'})
    top_container = top_container.find('div', attrs={'class': 'ag-theme-alpine-dark'})
    toptable = top_container.find('div', attrs={'class': "ag-body-viewport"})
    return toptable


def dan_html_extractor(soup):
    global formatted_strings  # Use the global variable

    processed_teams = set()

    # List of NHL teams
    nhl_teams = [
        'Anaheim Ducks', 'Arizona Coyotes', 'Boston Bruins', 'Buffalo Sabres',
        'Calgary Flames', 'Carolina Hurricanes', 'Chicago Blackhawks',
        'Colorado Avalanche', 'Columbus Blue Jackets', 'Dallas Stars', 'Detroit Red Wings',
        'Edmonton Oilers', 'Florida Panthers', 'Los Angeles Kings', 'Minnesota Wild',
        'Montreal Canadiens', 'Nashville Predators', 'New Jersey Devils', 'New York Islanders',
        'New York Rangers', 'Ottawa Senators', 'Philadelphia Flyers', 'Pittsburgh Penguins',
        'San Jose Sharks', 'Seattle Kraken', 'St. Louis Blues', 'Tampa Bay Lightning',
        'Toronto Maple Leafs', 'Vegas Golden Knights', 'Washington Capitals',
        'Winnipeg Jets'
    ]

    row_elements = soup.find_all('div', {'role': 'row'})
    if len(row_elements) == 0:
        return formatted_strings

    for row_element in row_elements:
        # Skip if it's a header row
        if 'header' in row_element.get('class', ''):
            continue

        cell_data = {}
        for cell in row_element.find_all('div', {'role': 'gridcell'}):
            col_id = cell.get('col-id')
            cell_value = cell.text.strip()
            cell_data[col_id] = cell_value

        if 'eventStart' in cell_data:
            if 'Final' in cell_data['eventStart']:
                cell_data['Status'] = 'Final'
                event_start = cell_data['eventStart'].replace('Final', '').strip()
                if event_start.isdigit() and len(event_start) == 2:
                    event_start = f"{event_start[0]} {event_start[1]}"

                # Find the team in the row
                team_name = next((team for team in nhl_teams if team in cell_data.get('eventId', '')), None)

                if team_name and team_name not in processed_teams:
                    processed_teams.add(team_name)

                    # Find the opposing team
                    opposing_team_name = next(
                        (opposing_team for opposing_team in nhl_teams if
                         opposing_team in cell_data.get('eventId', '') and opposing_team != team_name), None)

                    if opposing_team_name:
                        odds_team1, odds_team2 = cell_data.get('bestLine', '').split()
                        formatted_strings.append(
                            f"{team_name} vs {opposing_team_name} - Final Score: {event_start[:2]} {event_start[2:]} - Best Odds: ({odds_team1}, {odds_team2})"
                        )

    return formatted_strings


def write_to_text_file(output_file="tickertapeformattedinfo.txt"):
    with open(output_file, 'w', encoding='utf-8') as file:
        for ttstringd_string in formatted_strings:
            file.write(f"{formatted_string}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--leagueselect", default="nhl", help="Select the league (default: nhl)")
    args = parser.parse_args()

    soup = LoadPagesource(args.leagueselect)
    table = ParseUB(soup)
    dan_html_extractor(table)
    write_to_text_file(output_file="tickertapeformattedinfo.txt")

    for formatted_string in formatted_strings:
        print(formatted_string)








