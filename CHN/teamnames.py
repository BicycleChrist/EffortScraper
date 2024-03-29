
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


abbrevlist = ["afa", "aic", "aka", "akf", "arm", "asu", "aug", "bc_", "ben", "bgs", "bmj", "brn", "bu_", "cc_", "clg", "clk", "cns", "con", "cor", "dar", "den", "fsu", "har", "hcr", "lin", "liu", "lss", "mer", "mia", "mic", "min", "mnd", "mne", "mns", "mrc", "msu", "mtu", "ndk", "ndm", "nia", "nmu", "noe", "osu", "prn", "prv", "psu", "qui", "ren", "rit", "rmu", "sac", "stc", "stl", "stn", "stt", "uma", "uml", "unh", "uni", "uno", "ver", "wis", "wmu", "yal",]

# DAN's attempt:
# TODO: double-check this
dans_teamabbrevs = {
    1: "afa",
    5: "aic",
    3: "aka",
    4: "akf",
    6: "arm",
    61: "asu",
    64: "aug",
    9: "bc_",
    8: "ben",
    11: "bgs",
    7: "bmj",
    12: "brn",
    10: "bu_",
    16: "cc_",
    15: "clg",
    14: "clk",
    13: "cns",
    17: "con",
    18: "cor",
    19: "dar",
    20: "den",
    21: "fsu",
    22: "har",
    23: "hcr",
    433: "lin",
    62: "liu",
    24: "lss",
    29: "mer",
    30: "mia",
    31: "mic",
    34: "min",
    36: "mnd",
    25: "mne",
    35: "mns",
    28: "mrc",
    32: "msu",
    33: "mtu",
    40: "ndk",
    43: "ndm",
    39: "nia",
    42: "nmu",
    41: "noe",
    44: "osu",
    45: "prn",
    46: "prv",
    60: "psu",
    47: "qui",
    48: "ren",
    49: "rit",
    50: "rmu",
    51: "sac",
    52: "stc",
    53: "stl",
    422: "stn",
    63: "stt",
    27: "uma",
    26: "uml",
    38: "unh",
    54: "uni",
    37: "uno",
    55: "ver",
    58: "wis",
    57: "wmu",
    59: "yal"
}


# Dan's attempt to associate abbrevs with team names
maybeuseful = {
    "afa": "Air-Force",
    "aic": "American-Intl",
    "aka": "Alaska-Anchorage",
    "akf": "Alaska",
    "arm": "Army",
    "asu": "Arizona-State",
    "aug": "Augustana",
    "bc_": "Boston-College",
    "ben": "Bentley",
    "bgs": "Bowling-Green",
    "bmj": "Bemidji-State",
    "brn": "Brown",
    "bu_": "Boston-University",
    "cc_": "Colorado-College",
    "clg": "Colgate",
    "clk": "Clarkson",
    "cns": "Canisius",
    "con": "Connecticut",
    "cor": "Cornell",
    "dar": "Dartmouth",
    "den": "Denver",
    "fsu": "Ferris-State",
    "har": "Harvard",
    "hcr": "Holy-Cross",
    "lin": "Lindenwood",
    "liu": "Long-Island",
    "lss": "Lake-Superior",
    "mer": "Merrimack",
    "mia": "Miami",
    "mic": "Michigan",
    "min": "Minnesota",
    "mnd": "Minnesota-Duluth",
    "mne": "Maine",
    "mns": "Minnesota-State",
    "mrc": "Mercyhurst",
    "msu": "Michigan-State",
    "mtu": "Michigan-Tech",
    "ndk": "North-Dakota",
    "ndm": "Notre-Dame",
    "nia": "Niagara",
    "nmu": "Northern-Michigan",
    "noe": "Northeastern",
    "osu": "Ohio-State",
    "prn": "Princeton",
    "prv": "Providence",
    "psu": "Penn-State",
    "qui": "Quinnipiac",
    "ren": "Rensselaer",
    "rit": "RIT",
    "rmu": "Robert-Morris",
    "sac": "Sacred-Heart",
    "stc": "St-Cloud-State",
    "stl": "St-Lawrence",
    "stn": "Stonehill",
    "stt": "St-Thomas",
    "uma": "Massachusetts",
    "uml": "UMass-Lowell",
    "unh": "New-Hampshire",
    "uni": "Union",
    "uno": "Nebraska-Omaha",
    "ver": "Vermont",
    "wis": "Wisconsin",
    "wmu": "Western-Michigan",
    "yal": "Yale"
}


# convenience function to also perform lookup from name to ID
def NameToAbbrev(name):
    pass


def verifystuff():
    for name, teamid in CHN_TeamIDs.items():
        abbrev = dans_teamabbrevs[teamid]
        backtoname = maybeuseful[abbrev]
        assert name == backtoname
