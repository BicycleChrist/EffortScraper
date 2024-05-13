from bs4 import BeautifulSoup
import requests

url = 'https://www.mlb.com/probable-pitchers/'
response = requests.get(url)

# Parse
soup = BeautifulSoup(response.text, 'lxml')

# Upper level Div containing what we want
pitcher_summaries = soup.find_all(class_='probable-pitchers__pitcher-summary')

# Pair up the pitchers
paired_pitchers = [pitcher_summaries[i:i+2] for i in range(0, len(pitcher_summaries), 2)]

# Iterate over each pair of pitchers
for pair in paired_pitchers:
    pitcher1 = pair[0]
    pitcher2 = pair[1]

    #  pitcher 1's info
    pitcher1_name = pitcher1.find(class_='probable-pitchers__pitcher-name').text.strip()
    pitcher1_hand = pitcher1.find(class_='probable-pitchers__pitcher-pitch-hand').text.strip()
    pitcher1_stats = pitcher1.find(class_='probable-pitchers__pitcher-stats-summary').text.strip()

    # pitcher 2's info
    pitcher2_name = pitcher2.find(class_='probable-pitchers__pitcher-name').text.strip()
    pitcher2_hand = pitcher2.find(class_='probable-pitchers__pitcher-pitch-hand').text.strip()
    pitcher2_stats = pitcher2.find(class_='probable-pitchers__pitcher-stats-summary').text.strip()

    # Guess we just pipe the output when called upon in GUI.
    print("Name:", pitcher1_name)
    print("Hand:", pitcher1_hand)
    print("Stats:", pitcher1_stats)
    print("VS.")
    print("Name:", pitcher2_name)
    print("Hand:", pitcher2_hand)
    print("Stats:", pitcher2_stats)
    print()
