import pathlib
from bs4 import BeautifulSoup

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
        print(f"valid options are: {subdirs.keys}")
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
    # top-level element containing table
    top_container = page_source.find('div', attrs={'class': 'd-flex flex-grow-1'})
    # child element containing only table (no sidebar crap)
    top_container = top_container.find('div', attrs={'class': 'ag-theme-alpine-dark'})
    # page divides the table into 3 sections of columns
    toptable = top_container.find('div', attrs={'class': "ag-body-viewport"})
    # TODO: get table headers
    # for some reason it doesn't have 'right'
    columns = [toptable.find('div', attrs={'name': f"{part}"}) for part in ["left", "center", "fullWidth"]]
    # fullWidth still doesn't have any text?
    # look for col_id and comp_id in each column and build a dict or something
    # odds_elements = top_container.find_all('div', attrs={'class': 'odds-hover-cell'})
    # print(odds_elements)
    return columns

# TODO: accept leagueselect as a command-line argument
if __name__ == "__main__":
    cwd = pathlib.Path.cwd()
    assert cwd.name == "Boddssuck", "you're in the wrong directory"

    soup = LoadPagesource("nhl")

    # Find all div elements with the specified class name
    odds_elements = soup.find_all('div', attrs={'class': 'odds-hover-cell'})

    # Create a filename with the current date
    import datetime
    current_date = datetime.datetime.now().strftime("%Y%m%d")
    filename = f"nhlodds_{current_date}.txt"

    # Save parsed output in the "parsedpage" folder
    with open(cwd / "parsedpage" / filename, 'w') as file:
        for i in range(0, len(odds_elements), 3):
            # Extract relevant information
            odds_team_1_span = odds_elements[i].find('span', attrs={'class': 'pr-1', 'style': 'opacity: 0.7;'})
            odds_team_2_span = odds_elements[i + 1].find('span', attrs={'class': 'pr-1', 'style': 'opacity: 0.7;'})
            team_1_raw = odds_elements[i + 2].find_next('div').get_text(strip=True)
            team_2 = odds_elements[i + 2].find_next('div').find_next('div').get_text(strip=True)

            # Check if the span element is found before extracting text
            odds_team_1 = odds_team_1_span.get_text(strip=True) if odds_team_1_span else "N/A"
            odds_team_2 = odds_team_2_span.get_text(strip=True) if odds_team_2_span else "N/A"

            # Extract the second part of team_1 after removing the leading numbers
            team_1 = ' '.join(team_1_raw.split()[1:])

            # Write the formatted output to the file
            file.write(f"{odds_team_1} {team_1}\n{odds_team_2} {team_2}\n\n")
            print(f"{odds_team_1} {team_1}")
            print(f"{odds_team_2} {team_2}")
            print(f"Results saved to: {cwd / 'parsedpage' / filename}")




