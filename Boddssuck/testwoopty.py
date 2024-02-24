import pathlib
from bs4 import BeautifulSoup
import argparse
import pprint
import more_itertools

from LeagueMap import *
TEAMLIST = leaguemap[DEFAULT_LEAGUE_SELECT]


def GetSavePath(leagueselect, purpose="source", oddsformat="Spread"):
    cwd = pathlib.Path.cwd()
    assert cwd.name == "Boddssuck", "you're in the wrong directory"
    subdirs = {
        "source": "pagesource",
        "parsed": "parsedpage",
    }
    if leagueselect not in leaguemap.keys():
        print(f"invalid selection for league: {leagueselect}")
        print(f"valid options are: {(leagueselect.keys())}")
        return None
    if purpose not in subdirs.keys():
        print(f"invalid selection for purpose: {purpose}")
        print(f"valid options are: {(subdirs.keys())}")
        return None
    if oddsformat not in OddsFormat.keys():
        print(f"invalid selection for odds-format: {oddsformat}")
        print(f"valid options are: {(OddsFormat.keys())}")
        return None
    savepath = cwd / subdirs[purpose] / leagueselect / f"{oddsformat}.html"
    return savepath


def LoadPagesource(leagueselect, oddsformat="ALL"):
    # TODO: incorporate this logic (copied from DownloadUB) to select multiple files
    wantedsubpages = []
    if oddsformat == "ALL":
        wantedsubpages = [*OddsFormat.keys()]
    elif oddsformat in OddsFormat.keys():
        wantedsubpages.append(oddsformat)
    elif oddsformat not in OddsFormat.keys():
        print(f"invalid selection for odds-format: {oddsformat}")
        print(f"valid options are: {(OddsFormat.keys())}")
        return
    filepath = GetSavePath(leagueselect, "source", oddsformat)
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
            if cell_data['col-id'] == 'eventStart':  # TODO: might need to handle this differently if the game started ('final' and stuff)
                deep = cell
                lastchild = list(deep.descendants)
                if 'OT' in cell_data['value']:  # game in overtime
                    overtime_str = lastchild[-1]
                    # -4 == 'Final'
                    finalscores = lastchild[-10], lastchild[-7]
                    cell_data['value'] = f"Final ({overtime_str}) {finalscores[0]} - {finalscores[1]}"
                elif 'Final' in cell_data['value']:
                    finalscores = lastchild[-7], lastchild[-4]
                    cell_data['value'] = f"Final {finalscores[0]} - {finalscores[1]}"
                else:
                    time, date = lastchild[-1], lastchild[-3]  # -2 is the span containing the time
                    cell_data['value'] = f"{time}, {date}"
            all_cell_data.append(cell_data)
    return all_cell_data


def SplitNumbers(text):
    # we're splitting on 'P' because sometimes the odds lines have 'PK'; meaning "Pick'em"
    # 'o' and 'u' are over/under; used in the 'Total' format (and 'Combined')
    splits = [*more_itertools.split_before(text, lambda c: (c == '+' or c == '-' or c == 'P' or c == 'o' or c == 'u'))]
    if len(splits) == 2:
        firstline = str(''.join(splits[0]))
        secondline = str(''.join(splits[1]))
    elif len(splits) == 4:
        firstline = str(''.join(splits[0])) + str(''.join(splits[1])) + '\n'
        secondline = str(''.join(splits[2])) + str(''.join(splits[3]))
    elif len(splits) == 0:
        return []
    else:
        print("unexpected length post-split")
        return []
    return [firstline, secondline]


unrecognized_set = set()

# TODO: apparently 'col-id' has a different use in the left and right sides of the table
# in the first it's the title of the column, in the right it's just a numeric UID?
def FormatColumnString(cell_data):
    if cell_data['col-id'] == -1:
        return "invalid"
    if cell_data['col-id'] == 'eventId':
        line = cell_data['value'].strip('1234567890 ')
        # Find the teams in the row
        matching_team_names = [team for team in TEAMLIST if line.startswith(team) or line.endswith(team)]
        longest_matches = [None, None]
        for name in matching_team_names:
            if line.startswith(name):
                targetindex = 0
            elif line.endswith(name):
                targetindex = 1
            if longest_matches[targetindex] is None:
                longest_matches[targetindex] = name
            elif len(longest_matches[targetindex]) < len(name):
                longest_matches[targetindex] = name
        team_names = [name for name in longest_matches if name is not None]
        #assert (len(team_names) == 2)
        if not (len(team_names) == 2):
            global unrecognized_set
            if len(team_names) == 0:
                stripped_line = cell_data['value'].strip('1234567890 ')
                unrecognized_set.add(stripped_line)
                print(f"did not find either team; \n\t unrecognized: {stripped_line}\n")
            for recognized in team_names:
                unrecognized = ''.join((cell_data['value'].partition(recognized)[0],
                                       cell_data['value'].partition(recognized)[2]))
                unrecognized = unrecognized.strip('1234567890 ')
                print(f"did not find two teams; \n\trecognized: {recognized}\n\t unrecognized: {unrecognized}\n")
                unrecognized_set.add(unrecognized)
            return f"{team_names}"
        return f"{team_names[0]} vs {team_names[1]}"

    if cell_data['col-id'] == 'eventStart':
        if 'Final' in cell_data['value']:
            # cell_data['Status'] = 'Final'
            event_start = cell_data['value'].replace('Final', '').strip()
            # add a space between the digits
            if event_start.isdigit() and len(event_start) == 2:
                event_start = f"{event_start[0]} {event_start[1]}"
            #return f" - Final Score: {event_start[:2]} {event_start[2:]}"
            return f" - Final Score: {event_start}"
        else:   # TODO: more complex formatting for all possible layouts
            return f"eventStart: {cell_data['value']}"

    # TODO: associate bestline odds with their sportsbook (logo is included in cell)
    if cell_data['col-id'] == "bestLine":
        bestodds = SplitNumbers(cell_data['value'])
        if len(bestodds) == 0:
            return f" - Best Odds: empty"
        additionalspaces = len('- Best Odds: ')
        bestodds[1] = '\t ' + ' '*additionalspaces + bestodds[1]
        #odds = [s for s in cell_data['value'].split(' ') if len(s) > 0]  # there are two spaces in the string
        return f" - Best Odds: {bestodds[0]}{bestodds[1]}"

    return f"{cell_data['col-id']}: {cell_data['value']}"


# TODO: parameterize this function with the Odds-Format type somehow
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
        # so you have to associate the odds with rows/games ad-hoc
        elif cell_data['col-id'].isdigit():  # this attribute is used as an id in the right-side of the table
            bookname = booknames[cell_data['aria-colindex']]  # not doing lookup here because there's no easy way to pass it into this function
            bookname = bookname.split('.')[0]  # removing file-extension
            if cell_data['aria-rowindex'] not in moneylinemap:
                moneylinemap[cell_data['aria-rowindex']] = []
            if cell_data['value'] == '':
                continue
            # if it's just a number instead of a label, we're dealing with one of the 'odds' cells
            oddslines = SplitNumbers(cell_data['value'])
            # additional formatting to indent second line for oddslines with 4 numbers in them
            if DEFAULT_LEAGUE_SELECT in FOURNUMBER_SPREAD_LEAGUES:
                additionalspaces = len(bookname)
                oddslines[1] = '\t\t  ' + ' '*additionalspaces + oddslines[1]
            resultstring = ''.join(x for x in oddslines)
            moneylinemap[cell_data['aria-rowindex']].append(f"{bookname}: {resultstring}")
            #working_string = working_string + f"{bookname}: {cell_data['value']} : {cell_data['aria-rowindex']}"
        else:
            working_string = working_string + '\n\t' + FormatColumnString(cell_data)

    formatted_strings.append(working_string)  # assume we have the full final row
    # jank workaround for finding rowindex for last line
    rowmap[current_rowindex] = {'listindex': listindex, 'aria-rowindex': current_rowindex}
    return formatted_strings, moneylinemap, rowmap


def write_tickertape(inputtext):
    cwd = pathlib.Path.cwd()
    output_file = cwd / "tickertape_outputs" / f"{DEFAULT_LEAGUE_SELECT}.txt"
    with open(output_file, 'w', encoding='utf-8') as file:
        for ttstringd_string in inputtext:
            file.write(f"{ttstringd_string}\n")


def capitalize(somestring):
    return somestring.capitalize()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("leagueselect", default=DEFAULT_LEAGUE_SELECT,
                        help=f"Select the league (default: {DEFAULT_LEAGUE_SELECT})",
                        choices=leaguemap.keys())
    parser.add_argument("odds", default=None,
                        help=f"Select the oddsformat (default is league-dependent",
                        choices=OddsFormat.keys(),
                        type=capitalize
                        )
    args = parser.parse_args()
    TEAMLIST = leaguemap[args.leagueselect]
    # updating the default doesn't actually affect anything; default-parameters in functions will still use the original value
    DEFAULT_LEAGUE_SELECT = args.leagueselect
    oddsformat = args.odds
    if oddsformat is None:
        oddsformat = default_format_map[DEFAULT_LEAGUE_SELECT]
    print(f"LEAGUE: {DEFAULT_LEAGUE_SELECT}")
    print(f"ODDS: {oddsformat}\n")

    soup = LoadPagesource(args.leagueselect, oddsformat)
    table, header_row = ParseUB(soup)
    booknames = dan_parse_header(soup)
    #pprint.pprint(booknames)
    #pprint.pprint(header_row)

    intermediate_output = dan_html_extractor(table)
    formatted_output, moneylines, rowmapthing = FormatCellData(intermediate_output)
    output_storage = []
    #write_to_text_file(formatted_output, output_file="tickertapeformattedinfo.txt")
    #print(intermediate_output)

    #pprint.pprint(moneylines)
    #pprint.pprint(rowmapthing)
    for magicnumbers in rowmapthing.values():
        #print(magicnumbers)
        print(formatted_output[magicnumbers['listindex']])
        output_storage.append(formatted_output[magicnumbers['listindex']])
        if len(moneylines) > 0:  # TODO: check if there's any odds listed. This doesn't work because all books have a default entry (an empty string)
            print(f"\t--------MONEYLINES--------")
        for odds in moneylines[magicnumbers['aria-rowindex']]:
            print(f"\t\t{odds}")
            #output_storage.append(moneylines[magicnumbers['aria-rowindex']])
        print("\n")

    write_tickertape(output_storage)
    print("plsdontexit")
    if len(unrecognized_set) > 0:
        print("unrecognized names:")
        for name in unrecognized_set:
            print(f'\t"{name}",')
