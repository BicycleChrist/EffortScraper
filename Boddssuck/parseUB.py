import pathlib
from bs4 import BeautifulSoup
from datetime import datetime
import argparse

# TODO: add "both"
# options are "source", "parsed"
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


from bs4 import NavigableString

def IsFlexColumn(tag):
    if tag.has_attr("class") and not isinstance(tag, NavigableString):
        return "flex-column" in tag["class"]
    return False


def IsFlexRow(tag):
    if tag.has_attr("class") and not isinstance(tag, NavigableString):
        return "ag-row" in tag["class"]
    return False


# Assuming 'page_source' is a BeautifulSoup object containing the provided HTML
def ParseUB(page_source: BeautifulSoup):
    # top-level element containing table
    top_container = page_source.find('div', attrs={'class': 'd-flex flex-grow-1'})
    # child element containing only table (no sidebar crap)
    top_container = top_container.find('div', attrs={'class': 'ag-theme-alpine-dark'})
    # page divides the table into 3 sections of columns
    toptable = top_container.find('div', attrs={'class': "ag-body-viewport"})
    return toptable
    # TODO: get table headers
    # for some reason it doesn't have 'right'
    columns = [toptable.find('div', attrs={'name': f"{part}"}) for part in ["left", "center", "right", "fullWidth"]]

    firstrows = [d for d in columns[0].children if "role" in d.attrs and d["role"] == "row"]

    flexcolumns = columns[0].find_all(IsFlexColumn)
    allcolumns = toptable.find_all(IsFlexColumn)
    allrows = toptable.find_all(IsFlexRow)
    lookuptable = {}

    #for row in allrows:
    #    lookuptable[allrows.attrs["row-id"]] = data

    # fullWidth still doesn't have any text?
    # look for col_id and comp_id in each column and build a dict or something
    # odds_elements = top_container.find_all('div', attrs={'class': 'odds-hover-cell'})
    # print(odds_elements)
    print(team_container)
    print("endofparse")
    #return columns




# you need to feed the toptable to this function, it doesn't work on the whole HTML page
def dan_html_extractor(soup):
    row_elements = soup.find_all('div', {'role': 'row'})
    if len(row_elements) == 0:
        return None
    everything = {}
    for row_element in row_elements:
        row_id = row_element.get('row-id')

        # Extract data from each cell
        cell_data = {}
        for cell in row_element.find_all('div', {'role': 'gridcell'}):
            col_id = cell.get('col-id')
            cell_value = cell.text.strip()
            cell_data[col_id] = cell_value



        # Print the row_id and cell_data for debugging
        print(f"Row ID: {row_id}, Cell Data: {cell_data}")

        # Associate row ID with all cell data
        everything[row_id] = cell_data

    return everything







import pprint
# TODO: accept leagueselect as a command-line argument
if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert cwd.name == "Boddssuck", "you're in the wrong directory"

    soup = LoadPagesource("nhl")
    table = ParseUB(soup)
    dandict = dan_html_extractor(table)
    #pprint.pprint(dandict)

    parser = argparse.ArgumentParser()
    parser.add_argument("--leagueselect", default="nhl", help="Select the league (default: nhl)")
    args = parser.parse_args()
    newsoup = LoadPagesource(args.leagueselect)
    print("plsdon'texit")
