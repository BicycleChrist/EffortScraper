import pprint
import pathlib
import requests
from bs4 import BeautifulSoup
import re


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

def download_skater_table(teamname):
    # Ensure the teamname is in the correct format for the URL
    formatted_teamname = teamname
    url = f"https://www.collegehockeynews.com/stats/team/{formatted_teamname}/{CHN_TeamIDs[teamname]}"

    # Specify the path for saving the HTML file
    file_path = pathlib.Path.cwd() / 'teamdata' / formatted_teamname / 'skater_table.html'

    # Send a GET request to the URL
    response = requests.get(url)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the table with class 'data sortable sticky'
        skater_table = soup.find('table', {'class': 'data sortable sticky'})

        # Save the HTML content to the specified file path
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(str(skater_table))

        print(f"Skater table for {teamname} saved at: {file_path}")

    else:
        print(f"Failed to download skater table for {teamname}. Status code: {response.status_code}")


def download_goalie_table(teamname):
    # Ensure the teamname is in the correct format for the URL
    formatted_teamname = teamname
    url = f"https://www.collegehockeynews.com/stats/team/{formatted_teamname}/{CHN_TeamIDs[teamname]}"

    # Specify the path for saving the HTML file
    file_path = pathlib.Path.cwd() / 'teamdata' / formatted_teamname / 'goalie_table.html'

    # Send a GET request to the URL
    response = requests.get(url)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the table with id 'goalies' and class 'data sortable border'
        goalie_table = soup.find('table', {'id': 'goalies', 'class': 'data sortable'})

        # Save the HTML content to the specified file path
        # Maybe convert to .csv at some point?
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(str(goalie_table))

        print(f"Goalie table for {teamname} saved at: {file_path}")

    else:
        print(f"Failed to download goalie table for {teamname}. Status code: {response.status_code}")


def download_schedule_table(teamname):
    # Use the original team name for the directory
    formatted_teamname = teamname
    url = f"https://www.collegehockeynews.com/schedules/team/{formatted_teamname}/{CHN_TeamIDs[teamname]}"

    # Specify the path for saving the HTML file
    file_path = pathlib.Path.cwd() / 'teamdata' / formatted_teamname / 'schedule_table.html'

    # Send a GET request to the URL
    response = requests.get(url)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the div with class 'norint nomobileonly' and get the parent table
        schedule_table = soup.find('table', {'class': 'data schedule'})


        # Save the HTML content to the specified file path
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(str(schedule_table))

        print(f"Schedule table for {teamname} saved at: {file_path}")

    else:
        print(f"Failed to download schedule table for {teamname}. Status code: {response.status_code}")



#TODO:Figure out how to suck up box score. Urls for box scores are formatted: "https://www.collegehockeynews.com/box/final/20231103/afa/nia/", with data and then team vs team. afa is Air-Force academy and nia is Niagara. Need more dictionary for team abbreviations I guess. All other necessary data can be sourced from the schedule tables.


def download_box_score_from_schedule(team_name):
    # Ensure team name is in the correct format for the URL
    formatted_team_name = team_name.replace("-", "").replace(" ", "")

    # Construct the schedule URL
    url = f"https://www.collegehockeynews.com/schedules/team/{formatted_team_name}"

    # Send a GET request to the URL
    response = requests.get(url)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the box score link with class 'noprint', 'lomobile', and 'centerb'
        box_score_link = soup.find('a', {'class': 'noprint.lomobile.center.b'})

        if box_score_link:
            # Box score link is present, download the box score
            box_score_url = box_score_link.get('href')
            download_box_score(team_name, box_score_url)
        else:
            # No box score link available
            print(f"No box score available for {team_name}")

    else:
        print(f"Failed to fetch schedule for {team_name}. Status code: {response.status_code}")

# Example usage for box score rip:
#team_name = "RIT"
#download_box_score_from_schedule(team_name)




# Example usage for schedule rip:
#download_schedule_table("RIT")


# Gather data for single team :
team_name = "Denver"
download_skater_table(team_name)
download_goalie_table(team_name)


# When you want it all! CHN might get upset
#def download_all_team_data():
    #for team_name, team_id in CHN_TeamIDs.items():
        #download_skater_table(team_name)
        #download_goalie_table(team_name)
#download_all_team_data()
