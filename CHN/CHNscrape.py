import pprint
import pathlib

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
