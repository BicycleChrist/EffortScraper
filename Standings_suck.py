import requests
from bs4 import BeautifulSoup
import random
import time

#TODO: Despite the urls being consistent for all sports standings, different division/conference names mean different scraping logic for each standings page. This script also sucks
url = 'https://www.espn.com/nba/standings'

# Function to load the website with rotating user agents and proxies
def load_website(url):
    # List of user agents to rotate
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36",
        # Add more user agents as needed
    ]

    # List of proxy servers to rotate
    proxies = [
        {"http": "http://162.223.94.166:80"},
        {"http": "http://153.101.67.170:9002"},
        # Add more proxy servers as needed
    ]

    max_retries = 5
    for _ in range(max_retries):
        user_agent = random.choice(user_agents)
        proxy = random.choice(proxies)
        headers = {'User-Agent': user_agent}

        try:
            # Make a GET request to the website using the selected proxy and headers
            response = requests.get(url, headers=headers, proxies=proxy, timeout=5)
            if response.status_code == 200:
                # Parse the HTML content
                soup = BeautifulSoup(response.content, 'lxml')
                return soup
            else:
                print(f"Bad response: {response.status_code}")
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)  # Wait before retrying
    raise Exception("Failed to retrieve the webpage after multiple attempts.")


try:
    soup = load_website(url)


    def extract_standings(conference_title):
        # Find the conference div by its title
        conference_div = soup.find('div', class_='Table__Title', string=conference_title)
        if conference_div:
            conference_parent_div = conference_div.find_parent('div', class_='ResponsiveTable')
            if conference_parent_div:
                # Find the table body containing the standings
                table_body = conference_parent_div.find('tbody', class_='Table__TBODY')


                rows = table_body.find_all('tr', class_='Table__TR Table__TR--sm')
                standings = []


                for row in rows:
                    position = row.find('span', class_='team-position').text.strip()
                    team_name = row.find('a', class_='AnchorLink').text.strip()
                    standings.append((position, team_name))
                return standings
            else:
                print(f"Parent div not found for conference: {conference_title}")
        else:
            print(f"Conference div not found for title: {conference_title}")
        return []



    east_standings = extract_standings('Eastern Conference')
    west_standings = extract_standings('Western Conference')
    print(east_standings)
    print(west_standings)

    # Print the standings
    print("Eastern Conference Standings:")
    for position, team in east_standings:
        print(f"{position}: {team}")

    print("\nWestern Conference Standings:")
    for position, team in west_standings:
        print(f"{position}: {team}")

except Exception as e:
    print(e)
