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
# TODO: add boxscores
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
    "boxscores": {  # I don't think this really makes sense
        'urlseg': 'box',  # might need to append 'metrics.php' or /box/final/...
    },
}
# TODO: possibly write a class instead


# for convenience
def ConstructMethod(category):
    if not ValidateSelections([category]): return lambda *_ : "badlambda"
    method = CHN_table_categories[category]['searchmethod']
    attrs = CHN_table_categories[category]['htmlattrs']
    return lambda soupthing: method(soupthing, attrs)


# TODO: move this into the other file
CHN_TeamIDs = {
    "Air-Force": 1,
    #"American-International": 5,  # the site no longer uses this one, it redirects
    "American-Intl": 5,  # use this one to avoid redirect
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

# TODO: map team-names to abbreviations (mostly used internally for hrefs)
# you can potentially automate this by visiting each team's page and checking the url for their logo
# the xpath is:
# /html/body/div[1]/div[2]/div[1]/div/img
# from https://www.collegehockeynews.com/reports/team/Maine/25

# TODO: map team-ids to team-logos


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
    if prefix is None or prefix.startswith(('http://', 'https://')):
        return f"{prefix}/{suffix}"
    return f"https://{prefix}/{suffix}"




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
        baseurl = f"https://{CHN_URL}/{CHN_table_categories[arg]['urlseg']}"
        url = GetURLSuffix(teamname, baseurl)
        if not url.startswith('https://'):
            url = f"https://{url}"
        # TODO: check for repeat urls (skater and goalie come from same page)
        save_path = ExpectedPath(teamname, arg, tosavedsource=False)
        newfiles.append({arg: (url, save_path)})
        if len(categories) == 1:
            return (url, save_path)
    return newfiles


# pass a function to isolate the desired element within the webpage
# save_path is the location to dump the target element, not the webpage's source
def DownloadTeamData(url, save_path, searchmethod):
    try:
        response = requests.get(url, timeout=5, allow_redirects=False)
        if response.status_code == 200:
            if response.is_redirect:
                # Check if it's a redirect to the schedules page
                redirected_url = response.headers.get('Location', '')
                if "schedules" in redirected_url:
                    print(f"Skipping {url}, likely an exhibition match vs a scrub directional school.")
                    return None
            soup = BeautifulSoup(response.text, 'html.parser')
            found_table = searchmethod(soup)
            if found_table is not None:
                with open(save_path, 'w', encoding='utf-8') as file:
                    file.write(str(found_table))
                return found_table
            else:
                print(f"No table found on {url}.")
        elif response.status_code == 404:
            print(f"404 URL not found: {url}")
        elif response.status_code == 302:
            print(f"Skipping {url}, due to page redirect")
        else:
            print(f"{response.status_code} URL returned not-good response: {url}")
    except Exception as e:
        print(f"Error accessing {url}: {e}")

    return None



# TODO: get robots.txt
# When you want it all! CHN might get upset
#def download_all_team_data():  # TODO: You know 'GenerateAllURLs' was specifically written for this, right??
#    for team_name, team_id in CHN_TeamIDs.items():
#        download_skater_table(team_name)
#        download_goalie_table(team_name)


# TODO: exhibition games will provide a boxscore link, but the page just says 'Game Not Available'
    # and they don't provide a metrics link
def Spidermethod(soupdata, linktext):
    box_score_links = soupdata.find_all('a', text=linktext)
    box_score_urls = [f"{CHN_URL}{link.get('href')}" for link in box_score_links]
    return box_score_urls


# encodingmethod is the function that generates the filename from url
spidermap = {
    "boxscore": {
        "parentcategory": "schedule",
        "searchmethod": lambda soupthing: Spidermethod(soupthing, 'Box'),
        #"encodingmethod": lambda url:
        # TODO: encode the teamnames into the filename, under a folder that's just the date
    },
    "metrics": {
        "parentcategory": "schedule",
        "searchmethod": lambda soupthing: Spidermethod(soupthing, 'Metrics'),
        "encodingmethod": lambda url: url.split('gd=')[1],
    },
}
# TODO: somehow associate the metrics and boxscore with the schedule.html they came from, the date, and/or game-id
# and somehow make it possible to lookup games with any combination of those
# also, the schedule htmls will now need to encode the date-range/seasons they contain.


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


# TODO: schedules can be looked up by date(/schedules/?date=20231004) or season(?season=20232024)
# TODO: you can also just look at .../teamhistory/, or append the season to the team url, like:
# https://www.collegehockeynews.com/schedules/team/Air-Force/1/20232024
# TODO: create a list (or dict) from SeasonIndex.txt (those are the only valid values in the url when searching by season)
# Nevermind, it's literally just two years concatenated together. Validate it I guess.

# TODO: refactor into several functions
def GetPage(teamname, category, savesources=True):
    sourcepath = ExpectedPath(teamname, category, tosavedsource=True)
    extractpath = ExpectedPath(teamname, category)
    if extractpath.exists():  # load local file if it exists
        print("loading from local file")
        with open(extractpath, 'r', encoding='utf-8') as file:
            soup = BeautifulSoup(file, 'html.parser')
            searchmethod = ConstructMethod(category)
            found_table = searchmethod(soup)
        return found_table
    # TODO: re-parse saved pagesource instead of redownloading if extract doesn't exist
    if newtargets := ConstructURLs(teamname, category):
        url, filepath = newtargets
        searchmethod = ConstructMethod(category)
        print(f"Scraping {teamname} - {category}...")
        table = DownloadTeamData(url, filepath, searchmethod)
        if table is not None and savesources:
            assert sourcepath is not None
            with open(sourcepath, 'w', encoding='utf-8') as sourcedump:
                if table is not None:
                    sourcedump.write(str(table))
        print(f"Done scraping {teamname} - {category}")
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

    # real code
    if not TESTING_MODE_FLAG:   # do NOT rewrite to an 'else' statement
        assert TESTING_MODE_FLAG == False
        newpage = GetPage("American-Intl", "skater")
        MetricsLinks = SpiderLinks("Maine", "metrics")
        print("success")
        #print(newpage)
        print(MetricsLinks)
