import requests
from bs4 import BeautifulSoup
import random
import time


# Function to scrape the website
def LoadWebsite(url):
    #  user agents to rotate
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        # Add more user agents as needed
    ]
    
    #  list of proxy servers to rotate
    proxies = [
        {"socks4": "103.112.234.33:5678", "http": "116.206.56.142:8080"},
        {"http": "101.109.119.24:8080", "socks4": "116.232.32.244:5678"},
        # Add more proxy servers as needed
    ]
    
    # Select a random user agent and proxy
    user_agent = random.choice(user_agents)
    proxy = random.choice(proxies)

    # Define headers with the selected user agent
    headers = {'User-Agent': user_agent}

    # Make a GET request to the website using the selected proxy and headers
    response = requests.get(url, headers=headers, proxies=proxy)
    if response.status_code != 200:
        print(f"not good response: {response.status_code}")
        raise Exception
    html_content = response.content

    # Parse the HTML content
    soup = BeautifulSoup(html_content, "html.parser")
    return soup


def scrape_website(soup):
    # Find goalie information
    section = soup.find('section')  # there is only ever one of these
    containers = [div for div in section.find_all('div', class_="items-center") 
        #if 'flex' in div.attrs['class']
        #and 'flex-col' in div.attrs['class']
    ]
    
    statuses = ("Confirmed", "Unconfirmed")
    results = []
    
    articles = [container.find_all('article') for container in containers if len(container.find_all('article')) > 0]
    articles = articles[0] # assuming that only one container has articles on the page
    for article in articles:
        rowdiv = article.find('div', class_="flex flex-row")
        coldivs = [div for div in rowdiv.find_all('div', class_="items-center") if 'flex' in div.attrs['class'] and 'flex-col' in div.attrs['class']]
        center_justified = [div for div in coldivs if 'justify-center' in div.attrs['class'] and 'items-center' in div.attrs['class']]
        sometext = [div.text for div in center_justified]
        goalie_names = sometext[0], sometext[2]
        statuses = [span.text for span in article.find_all('span', class_="text-center") if span.text in statuses]
        for result in zip(goalie_names, statuses):
            results.append({"name": result[0], "status": result[1]})
    
    return results
    

def main():
    # Define the number of retries
    max_retries = 5
    url = "https://www.dailyfaceoff.com/starting-goalies/"
    
    soup = None
    # Perform scraping with retries
    for _ in range(max_retries):
        try:
            soup = LoadWebsite(url)
            break  # Break the loop if successful
        except Exception as e:
            print(f"Error: {e}")
            print("Retrying...")
            time.sleep(1)  # Wait for a few seconds before retrying
    
    goalie_info = scrape_website(soup)
    # Print the scraped goalie information
    for goalie in goalie_info:
        print("Name:", goalie["name"])
        print("Status:", goalie["status"])
        print()


if __name__ == "__main__":
    main()
