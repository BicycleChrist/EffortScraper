import pprint
import pathlib
import requests
from bs4 import BeautifulSoup

CHN_URL = "www.collegehockeynews.com"

# base function for navigating to table in webpage in BeautifulSoup
def GenericSearchmethod(soupdata, tagattrs):
    found_table = soupdata.find('table', attrs=tagattrs)
    return found_table

# maps to URL segment, and any attributes required for BeautifulSoup lookup
CHN_table_categories = {
    "skater": {
        'urlseg': 'stats',
        'searchmethod': GenericSearchmethod,
        'htmlattrs': {'class': 'data sortable sticky'},
    },
    "goalie": {
        'urlseg': 'stats',
        'searchmethod': GenericSearchmethod,
        'htmlattrs': {'id': 'goalies', 'class': 'data sortable'},
    },
    "schedule": {
        'urlseg': 'schedules',
        'searchmethod': GenericSearchmethod,
        'htmlattrs': {'class': 'data schedule'},
    },
}
# TODO: possibly write a class instead


# for convenience
def ConstructMethod(category):
    if not ValidateSelections([category]): return lambda *_ : "badlambda"
    method = CHN_table_categories[category]['searchmethod']
    attrs = CHN_table_categories[category]['htmlattrs']
    return lambda soupthing: method(soupthing, attrs)


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
reversedict_TeamIDs = {v:k for k,v in CHN_TeamIDs.items()}


# returns path for single subdirectory or all
def ExpectedSubdir(teamname=None, category=None):
    cwd = pathlib.Path.cwd()
    if cwd.name != "CHN":
        print("you're in the wrong directory")
        return []
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


def ExpectedPath(teamname, category, *, tosavedsource=False, ensure_exists=True):
    cwd = pathlib.Path.cwd()
    if cwd.name != "CHN":
        print("you're in the wrong directory")
        return None
    if not ValidateSelections([category]): return None
    subdir = cwd / 'teamdata' / teamname
    filename = f"{category}_table.html"
    if tosavedsource:
        subdir = cwd / 'savedsources' / CHN_table_categories[category]['urlseg']
        filename = f"{CHN_TeamIDs[teamname]}.html"
    if ensure_exists and not subdir.exists(): subdir.mkdir(parents=True)
    return subdir / filename


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


# returns list of dicts mapping category to (url, save_path)
def ConstructURLs(teamname, *categories):
    if not ValidateSelections(list(categories)):
        print("cancelling download")
        return None
    newfiles = []
    for arg in categories:
        baseurl = f"{CHN_URL}/{CHN_table_categories[arg]['urlseg']}"
        url = GetURLSuffix(teamname, baseurl)
        if not url.startswith('https://'): url = f"https://{url}"
        # TODO: check for repeat urls (skater and goalie come from same page)
        save_path = ExpectedPath(teamname, arg, tosavedsource=False)
        newfiles.append({arg: (url, save_path)})
        if len(categories) == 1:
            return (url, save_path)
    return newfiles


# pass a function to isolate the desired element within the webpage
# save_path is the location to dump the target element, not the webpage's source
def DownloadTeamData(url, save_path, searchmethod):
    response = requests.get(url, timeout=5)
    match response.status_code:
        case 200: pass
        case 404: print(f"404 URL not found: {url}"); return None
        case _: print(f"{response.status_code} URL returned not-good response: {url}"); return None
    soup = BeautifulSoup(response.text, 'html.parser')
    found_table = searchmethod(soup)
    with open(save_path, 'w', encoding='utf-8') as file:
        file.write(found_table.decode())
    return found_table


# TODO: get robots.txt
# When you want it all! CHN might get upset
#def download_all_team_data():  # TODO: You know 'GenerateAllURLs' was specifically written for this, right??
#    for team_name, team_id in CHN_TeamIDs.items():
#        download_skater_table(team_name)
#        download_goalie_table(team_name)


def Spidermethod(soupdata, linktext):
    box_score_links = soupdata.find_all('a', text=linktext)
    box_score_urls = [f"{CHN_URL}{link.get('href')}" for link in box_score_links]
    return box_score_urls


spidermap = {
    "boxscore": {
        "parentcategory": "schedule",
        "searchmethod": lambda soupthing: Spidermethod(soupthing, 'Box'),
    },
    "metrics": {
        "parentcategory": "schedule",
        "searchmethod": lambda soupthing: Spidermethod(soupthing, 'Metrics')
    },
}


# because this function only applies to schedule-pages, we know the filepath given only the teamname
def SpiderLinks(teamname, targetlinktype="boxscore"):
    # ensuring parent webpage is already downloaded
    parent_pagetype = spidermap[targetlinktype]["parentcategory"]
    expectedpath = ExpectedPath(teamname, parent_pagetype)
    if expectedpath.exists():
        with open(expectedpath, 'r', encoding='utf-8') as file:
            table = BeautifulSoup(file, 'html.parser')
    else:
        if newtargets := ConstructURLs(teamname, parent_pagetype):
            url, save_path = newtargets
            searchmethod = ConstructMethod(parent_pagetype)
            table = DownloadTeamData(url, save_path, searchmethod)
        else:
            print("ERROR: failed to construct target URLs")
            return []
    return spidermap[targetlinktype]["searchmethod"](table)

#def GetPages(teamname, category, **kwargs):
#    teams = kwargs.get('teams', [teamname])
#    categories = kwargs.get('categories', [category])
#    for teamname in teams:
#        if newtargets := ConstructURLs(teamname, *categories):
#            url, save_path = newtargets
#            searchmethod = CHN_table_categories[parent_pagetype]['searchmethod']
#            table = DownloadTeamData(url, save_path, searchmethod)

def GetPage(teamname, category, savesources=True):
    if newtargets := ConstructURLs(teamname, category):
        url, filepath = newtargets
        searchmethod = ConstructMethod(category)
        table = DownloadTeamData(url, filepath, searchmethod)
        if savesources:
            sourcepath = ExpectedPath(teamname, category, tosavedsource=True)
            assert sourcepath is not None
            with open(sourcepath, 'w', encoding='utf-8') as sourcedump:
                sourcedump.write(table.decode())
        return table
    # fallthrough if no new targets
    print("ERROR: failed to construct target URLs")
    return None


if __name__ == "__main__":
    TESTING_MODE_FLAG = False
    # testing-mode disables downloading and file-searching,
    # and bypasses normal program logic. Don't write real code like this.
    if TESTING_MODE_FLAG:
        print('\n', '='*10, "TESTING MODE", '='*10, '\n')
        from examples.ExampleSetup import LoadExample
        #examplesoup = LoadExample(usedump=False)  # webpage source
        examplesoup = LoadExample(usedump=True)  # dumpedtable
        testmethod = CHN_table_categories["schedule"]['searchmethod']
        testattrs = CHN_table_categories["schedule"]['htmlattrs']
        testparse = testmethod(examplesoup, testattrs)
        pprint.pprint(testparse)

        print('\n\nSpiderlinks:')
        example_boxscore_links = spidermap["boxscore"]["searchmethod"](examplesoup)
        pprint.pprint(example_boxscore_links)

    # real
    if not TESTING_MODE_FLAG:   # do NOT rewrite to an 'else' statement
        assert TESTING_MODE_FLAG == False
        newpage = GetPage("RIT", "skater")
        somelinks = SpiderLinks("Wisconsin", "metrics")
        print("success")
