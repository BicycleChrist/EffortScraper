import pprint
import pathlib
import requests
from bs4 import BeautifulSoup

CHN_URL = "www.collegehockeynews.com"

# maps to URL segment, and any attributes required for BeautifulSoup lookup
CHN_table_categories = {
    "skater": {
        'urlseg': 'stats',
        'htmlattrs': {'class': 'data sortable sticky'},
    },
    "goalie": {
        'urlseg': 'stats',
        'htmlattrs': {'id': 'goalies', 'class': 'data sortable'},
    },
    "schedule": {
        'urlseg': 'schedules',
        'htmlattrs': {'class': 'data schedule'},
    },
}
# TODO: possibly write a class instead

CHN_TeamIDs = {
    "Air-Force": 1,
    "American-International": 5,
    "Army": 6,
    "Bentley": 8,
    "Canisius": 13,
    "Holy-Cross": 23,
    "Mercyhurst": 28,
    "Niagara": 39,
    "RIT": 49,
    "Robert-Morris": 50,
    "Sacred-Heart": 51,
    "Michigan": 31,
    "Michigan-State": 32,
    "Minnesota": 34,
    "Notre-Dame": 43,
    "Ohio-State": 44,
    "Penn-State": 60,
    "Wisconsin": 58,
    "Augustana": 64,
    "Bemidji-State": 7,
    "Bowling-Green": 11,
    "Ferris-State": 21,
    "Lake-Superior": 24,
    "Michigan-Tech": 33,
    "Minnesota-State": 35,
    "Northern-Michigan": 42,
    "St-Thomas": 63,
    "Brown": 12,
    "Clarkson": 14,
    "Colgate": 15,
    "Cornell": 18,
    "Dartmouth": 19,
    "Harvard": 22,
    "Princeton": 45,
    "Quinnipiac": 47,
    "Rensselaer": 48,
    "St-Lawrence": 53,
    "Union": 54,
    "Yale": 59,
    "Boston-College": 9,
    "Boston-University": 10,
    "Connecticut": 17,
    "Maine": 25,
    "Massachusetts": 27,
    "UMass-Lowell": 26,
    "Merrimack": 29,
    "New-Hampshire": 38,
    "Northeastern": 41,
    "Providence": 46,
    "Vermont": 55,
    "Colorado-College": 16,
    "Denver": 20,
    "Miami": 30,
    "Minnesota-Duluth": 36,
    "Nebraska-Omaha": 37,
    "North-Dakota": 40,
    "St-Cloud-State": 52,
    "Western-Michigan": 57,
    "Alaska": 4,
    "Alaska-Anchorage": 3,
    "Arizona-State": 61,
    "Lindenwood": 433,
    "Long-Island": 62,
    "Stonehill": 422,
    # Note: both of these (still under Independent) were commented out in the HTML
    #"Utica": 356,
    #"Alabama-Huntsville": 2,
}

# reverse-lookup (teamname by ID)
reversedict = {v:k for k,v in CHN_TeamIDs.items()}

# returns path for single subdirectory or all
def ExpectedSubdir(teamname=None):
    cwd = pathlib.Path.cwd()
    if cwd.name != "CHN":
        print("you're in the wrong directory")
        SystemExit()
    # return a single path if a teamname is given
    if teamname is not None:
        return cwd / 'teamdata' / teamname
    # return all
    return [pathlib.Path(cwd / 'teamdata' / teamname) for teamname in CHN_TeamIDs.keys()]


def CreateSubdirectories():
    for newpath in ExpectedSubdir():
        newpath.mkdir(exist_ok=True, parents=True)
        # creates any missing parent directories as well! make sure you check current path!
    return


def GenerateAllURLs(stats=True, schedules=True):
    baseURL = "www.collegehockeynews.com"
    subpaths = []
    if stats: subpaths.append(f"{baseURL}/stats/team")
    if schedules: subpaths.append(f"{baseURL}/schedules/team")
    GeneratedURLs = {}
    for name, ID in CHN_TeamIDs.items():
        suffix = f"{name}/{ID}"
        GeneratedURLs[name] = [f"{prefix}/{suffix}" for prefix in subpaths]
    return GeneratedURLs


# 'team/teamname/ID' is often appended to URLs
# if you pass a base URL, it will return a concatenated string
def GetURLSuffix(teamname, prefix=None):
    suffix = f"team/{teamname}/{CHN_TeamIDs[teamname]}"
    if prefix is None: return suffix
    if prefix.endswith('/'): return f"{prefix}{suffix}"
    return f"{prefix}/{suffix}"

def ValidateSelections(categories:list):
    if len(categories) == 0:
        print("nothing selected")
        return False
    isValid=True
    for arg in categories:
        if arg not in CHN_table_categories.keys():
            print(f"invalid category: {arg}")
            print(f"available options are: {CHN_table_categories.keys()}")
            isValid = False
    return isValid

def DownloadTeamData(teamname, *categories):
    if not ValidateSelections(list(categories)):
        print("cancelling download")
        return
    for arg in categories:
        baseurl = f"{CHN_URL}/{CHN_table_categories[arg]['urlseg']}"
        url = GetURLSuffix(teamname, baseurl)
        # TODO: check for repeat urls (skater and goalie come from same page)
        save_path = ExpectedSubdir(teamname) / f"{arg}_table.html"

        response = requests.get(url)
        match response.status_code:
            case 200: pass
            case 404: print(f"404 URL not found: {url}"); continue
            case _: print(f"{response.status_code} URL returned not-good response: {url}"); continue
        # TODO: refactor into seperate functions
        soup = BeautifulSoup(response.text, 'html.parser')
        found_table = soup.find('table', attrs=CHN_table_categories[arg]['htmlattrs'])
        with open(save_path, 'w', encoding='utf-8') as file:
            file.write(found_table.decode(pretty_print=True))
        print(f"{arg} table for {teamname} saved at: {save_path}")


# TODO: get robots.txt
# When you want it all! CHN might get upset
#def download_all_team_data():  # TODO: You know 'GenerateAllURLs' was specifically written for this, right??
#    for team_name, team_id in CHN_TeamIDs.items():
#        download_skater_table(team_name)
#        download_goalie_table(team_name)


# could probably just add this as another category
def FindBoxScoreURLs(filepath):
    with open(filepath, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')
        # this is the same table you find in 'DownloadTeamData' with the 'schedule' category
        table = soup.find(attrs={'class': 'data schedule'})
        box_score_links = table.find_all('a', text='Box')
        box_score_urls = [f"{CHN_URL}{link.get('href')}" for link in box_score_links]
    return box_score_urls


if __name__ == "__main__":
    schedulefile = pathlib.Path.cwd() / "examples" / "schedule.html"
    if not schedulefile.exists():
        print(f"ERROR: schedulefile not found at: {schedulefile.absolute()}")
        SystemExit()  # TODO: add error handling more proper than 'print & SystemExit' to this file

    FindBoxScoreURLs(schedulefile)
