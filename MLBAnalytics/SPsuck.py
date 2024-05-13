from bs4 import BeautifulSoup
import requests

# Send a GET request to the URL
url = 'https://www.mlb.com/probable-pitchers'
response = requests.get(url)

# Parse the HTML content
soup = BeautifulSoup(response.text, 'html.parser')

# Find all elements with the class 'probable-pitchers__matchup'
matchups = soup.find_all(class_='probable-pitchers__matchup')

# Iterate over each matchup
for matchup in matchups:
    # Extract team names
    team_names = matchup.find_all(class_='probable-pitchers__team-name')
    away_team = team_names[0].text.strip()
    home_team = team_names[2].text.strip()

    # Find all elements with the class 'probable-pitchers__pitcher-summary'
    pitcher_summaries = matchup.find_all(class_='probable-pitchers__pitcher-summary')

    # Pair up the pitchers
    paired_pitchers = [pitcher_summaries[i:i+2] for i in range(0, len(pitcher_summaries), 2)]

    # Iterate over each pair of pitchers
    for pair in paired_pitchers:
        pitcher1 = pair[0]
        pitcher2 = pair[1]

        # Extract pitcher 1's information
        pitcher1_name = pitcher1.find(class_='probable-pitchers__pitcher-name').text.strip()
        pitcher1_hand = pitcher1.find(class_='probable-pitchers__pitcher-pitch-hand').text.strip()
        pitcher1_stats = pitcher1.find(class_='probable-pitchers__pitcher-stats-summary').text.strip()

        # Extract pitcher 2's information
        pitcher2_name = pitcher2.find(class_='probable-pitchers__pitcher-name').text.strip()
        pitcher2_hand = pitcher2.find(class_='probable-pitchers__pitcher-pitch-hand').text.strip()
        pitcher2_stats = pitcher2.find(class_='probable-pitchers__pitcher-stats-summary').text.strip()

        # Print the matchup
        print(f"{away_team} vs {home_team}")
        print("Name:", pitcher1_name)
        print("Hand:", pitcher1_hand)
        print("Stats:", pitcher1_stats)
        print("VS.")
        print("Name:", pitcher2_name)
        print("Hand:", pitcher2_hand)
        print("Stats:", pitcher2_stats)
        print()
