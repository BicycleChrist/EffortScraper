import requests
from bs4 import BeautifulSoup

url = "https://www.naturalstattrick.com/playerlist.php"

def download_html(url, file_name):
    response = requests.get(url)
    response.raise_for_status()  # Raises an HTTPError if the HTTP request returned an unsuccessful status code

    with open(file_name, 'w', encoding='utf-8') as file:
        file.write(response.text)


def parse_player_names_ids(file_path):
    # Read the HTML content from the file
    with open(file_path, 'r', encoding='utf-8') as file:
        html_content = file.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    players = {}

    # Find all 'a' tags with 'href' attributes containing 'playerreport.php'
    player_links = soup.find_all('a', href=lambda href: href and 'playerreport.php' in href)

    # Iterate over each link, extract the player name and ID, and add to the dictionary
    for link in player_links:
        if 'Summary' in link.text:
            # Extract the player ID from the 'href' attribute
            player_id = link['href'].split('&')[2].split('=')[1]
            # The player's name is in a previous 'td', find the first parent 'tr' then find all 'td' and get the first
            player_name = link.find_previous('tr').find_all('td')[0].text.strip()
            # Add to dictionary
            players[player_name] = player_id

    return players

# Function to write player data to a file
def write_players_to_file(players_dict, output_file):
    with open(output_file, 'w', encoding='utf-8') as file:
        for name, player_id in players_dict.items():
            file.write(f"{name}: {player_id}\n")

# Parse the HTML and get a dictionary of player names and IDs
download_html(url, "playerlist.html")
file_path = "playerlist.html"  # HTML file path
players_dict = parse_player_names_ids(file_path)
# Write the dictionary to 'player_ids.txt'
output_file = 'player_ids.txt'
write_players_to_file(players_dict, output_file)

