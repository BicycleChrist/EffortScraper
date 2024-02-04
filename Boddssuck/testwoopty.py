import pathlib
from bs4 import BeautifulSoup
import argparse
import pprint


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
    header_row = top_container.find('div', attrs={'class': "ag-header"})
    toptable = top_container.find('div', attrs={'class': "ag-body-viewport"})
    return toptable, header_row


# we need aria-colindex to associate headers with cells;
# for some reason headers don't have 'col-id'; only the cells (and the don't match)

# TODO: parse the logo's filename out of the header cell's image url
# encoded in the 'style' attribute
# background-image: url("https://assets.unabated.com/sportsbooks/logos/bovada.svg");
# we can't use the 'title' because there are random things appended to it ("Real-Time", etc),
# and we need to know the file-extension

# Also, the page seems to unload any columns that are horizontally scrolled out of view
# make sure that isn't affecting the download script

# aria-colindex is defined in the same div as
# class="ag-header-cell" role="columnheader"
# the image-url is nested a few levels inside that, child of class "market-header"

# TODO: use simple string splitting instead of regex
import re

def dan_parse_header(soup):
    header_data = {}
    header_row = soup.find('div', {'class': 'ag-header-viewport'})  # this is the section that has all the logos
    # the left-side section of the header row ('Best-Line', 'Hold', score, etc.) is the sibling element 'ag-pinned-left-header'
    #header_cells = header_row.find_all('div', {'role': 'columnheader'})
    header_cells = header_row.find_all('div', {'class': 'ag-header-cell'})
    for header_cell in header_cells:
        aria_col_index = header_cell.get('aria-colindex', -1)
        col_index = header_cell.get('col-id', 'invalid')
        # Extract logo filename if present
        market_header = header_cell.find('div', {'class': 'market-header'})
        if market_header:
            child_div = list(market_header.descendants)[0]
            style = child_div.get('style', '')
            logo_url_match = re.search(r'url\("([^"]+)"\)', style)
            if logo_url_match:
                logo_url = logo_url_match.group(1)
                filename = logo_url.split('/')[-1]  # Extract filename from URL
                #header_data[aria_col_index] = (filename, col_index)
                header_data[aria_col_index] = filename
    return header_data


def dan_html_extractor(soup):
    all_cell_data = []
    row_elements = soup.find_all('div', {'role': 'row'})
    for row_element in row_elements:
        row_index = row_element.get('aria-rowindex', -1)
        for cell in row_element.find_all('div', {'role': 'gridcell'}):
            cell_data = {
                'aria-colindex': cell.get('aria-colindex', -1),
                'col-id': cell.get('col-id', -1),
                'value': cell.text.strip(),
                'comp-id': cell.get('comp-id', -1),
                'aria-rowindex': row_index
            }
            all_cell_data.append(cell_data)

    return all_cell_data


# TODO: apparently 'col-id' has a different use in the left and right sides of the table
# in the first it's the title of the column, in the right it's just a numeric UID?
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
    moneylinemap = {}  # storing the cells containing odds so we can lookup the sportsbook names and format by row
    formatted_strings = []
    working_string = ''
    rowmap = {}
    listindex = 0
    current_rowindex = 0

    for cell_data in all_cell_data:
        if cell_data['col-id'] == 'eventId':  # start new row
            if len(working_string) > 0:  # prevent pushing an empty string on first iteration
                formatted_strings.append(working_string)
                rowmap[current_rowindex] = {'listindex': listindex, 'aria-rowindex': current_rowindex}
                listindex += 1
            current_rowindex = cell_data['aria-rowindex']
            working_string = FormatColumnString(cell_data)
        # all the cells containing odds are listed AFTER you've traversed all the initial game-info columns
        # so you have to associate the odds with rows/games post-hoc
        elif cell_data['col-id'].isdigit():  # this attribute is used as an id in the right-side of the table
            bookname = booknames[cell_data['aria-colindex']]  # not doing lookup here because there's no easy way to pass it into this function
            bookname = bookname.split('.')[0]  # removing file-extension
            if cell_data['aria-rowindex'] not in moneylinemap:
                moneylinemap[cell_data['aria-rowindex']] = []
            if cell_data['value'] == '':
                continue
            moneylinemap[cell_data['aria-rowindex']].append(f"{bookname}: {cell_data['value']}")
            #working_string = working_string + f"{bookname}: {cell_data['value']} : {cell_data['aria-rowindex']}"
        else:
            working_string = working_string + '\n\t' + FormatColumnString(cell_data)

    formatted_strings.append(working_string)  # assume we have the full final row
    # jank workaround for finding rowindex for last line
    rowmap[current_rowindex] = {'listindex': listindex, 'aria-rowindex': current_rowindex}
    return formatted_strings, moneylinemap, rowmap


def write_to_text_file(inputtext, output_file="tickertapeformattedinfo.txt"):
    with open(output_file, 'w', encoding='utf-8') as file:
        for ttstringd_string in inputtext:
            file.write(f"{ttstringd_string}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--leagueselect", default="nhl", help="Select the league (default: nhl)")
    args = parser.parse_args()

    soup = LoadPagesource(args.leagueselect)
    table, header_row = ParseUB(soup)
    booknames = dan_parse_header(soup)
    #pprint.pprint(booknames)
    #pprint.pprint(header_row)

    intermediate_output = dan_html_extractor(table)
    formatted_output, moneylines, rowmapthing = FormatCellData(intermediate_output)
    #write_to_text_file(formatted_output, output_file="tickertapeformattedinfo.txt")
    #print(intermediate_output)

    #pprint.pprint(moneylines)
    #pprint.pprint(rowmapthing)
    for magicnumbers in rowmapthing.values():
        #print(magicnumbers)
        print(formatted_output[magicnumbers['listindex']])
        if len(moneylines) > 0:  # TODO: check if there's any odds listed. This doesn't work because all books have a default entry (an empty string)
            print(f"\t--------MONEYLINES--------")
        for odds in moneylines[magicnumbers['aria-rowindex']]:
            print(f"\t\t{odds}")
