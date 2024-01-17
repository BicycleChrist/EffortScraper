import pathlib
from bs4 import BeautifulSoup
import argparse


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
    all_cell_data = []
    row_elements = soup.find_all('div', {'role': 'row'})
    for row_element in row_elements:
        # Skip if it's a header row
        if 'header' in row_element.get('class', ''):
            continue
        # TODO: associate rows with cells
        for cell in row_element.find_all('div', {'role': 'gridcell'}):
            cell_data = {}
            cell_data['col-id'] = cell.get('col-id', -1)
            cell_data['value'] = cell.text.strip()
            all_cell_data.append(cell_data)
    return all_cell_data


def FormatColumnString(cell_data):
    if cell_data['col-id'] == -1:
        return "invalid"
    if cell_data['col-id'] == 'eventId':
        # Find the teams in the row
        team_names = [team for team in nhl_teams if team in cell_data['value']]
        #assert (len(team_names) == 2)
        if not (len(team_names) == 2):
            return f"{team_names}"
        team_name = team_names[0]
        opposing_team_name = team_names[1]
        return f"{team_name} vs {opposing_team_name}"

    if cell_data['col-id'] == 'eventStart':
        if 'Final' in cell_data['value']:
            # cell_data['Status'] = 'Final'
            event_start = cell_data['value'].replace('Final', '').strip()
            # add a space between the digits
            if event_start.isdigit() and len(event_start) == 2:
                event_start = f"{event_start[0]} {event_start[1]}"
            return f" - Final Score: {event_start[:2]} {event_start[2:]}"
        else:   # TODO: more complex formatting for all possible layouts
            return f"eventStart: {cell_data['value']}"

    if cell_data['col-id'] == "bestLine":
        odds = [s for s in cell_data['value'].split(' ') if len(s) > 0]  # there are two spaces in the string
        #return f" - Best Odds: ({odds[0], odds[1]})"
        return f" - Best Odds: ({odds})"

    return f"{cell_data['col-id']}: {cell_data['value']}"


def FormatCellData(all_cell_data):
    formatted_strings = []
    working_string = ''
    for cell_data in all_cell_data:
        if cell_data['col-id'] == 'eventId':  # start new row
            if len(working_string) > 0:
                formatted_strings.append(working_string)
            working_string = FormatColumnString(cell_data)
        else:
            working_string = working_string + '\n\t' + FormatColumnString(cell_data)
    formatted_strings.append(working_string)  # assume we have the full final row
    return formatted_strings


def write_to_text_file(inputtext, output_file="tickertapeformattedinfo.txt"):
    with open(output_file, 'w', encoding='utf-8') as file:
        for ttstringd_string in inputtext:
            file.write(f"{ttstringd_string}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--leagueselect", default="nhl", help="Select the league (default: nhl)")
    args = parser.parse_args()

    soup = LoadPagesource(args.leagueselect)
    table = ParseUB(soup)
    intermediate_output = dan_html_extractor(table)
    formatted_output = FormatCellData(intermediate_output)
    write_to_text_file(formatted_output, output_file="tickertapeformattedinfo.txt")

    for formatted_string in formatted_output:
        print(formatted_string)

