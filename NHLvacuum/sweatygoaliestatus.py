import requests
from bs4 import BeautifulSoup
import random
import time


url = "https://www.dailyfaceoff.com/starting-goalies/2024-05-12"

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

# Function to scrape the website
def scrape_website():
    # Select a random user agent and proxy
    user_agent = random.choice(user_agents)
    proxy = random.choice(proxies)

    # Define headers with the selected user agent
    headers = {'User-Agent': user_agent}

    # Make a GET request to the website using the selected proxy and headers
    response = requests.get(url, headers=headers, proxies=proxy)
    html_content = response.text

    # Parse the HTML content
    soup = BeautifulSoup(html_content, "html.parser")

    # Extract goalie information
    goalie_divs = soup.find_all("div", class_="flex flex-col justify-start p-2 xl:justify-start xl:flex-row-reverse")
    goalie_info = []

    for goalie_div in goalie_divs:
        goalie_name = goalie_div.find("span", class_="text-lg xl:text-2xl").text.strip()
        goalie_status = goalie_div.find("div", class_="flex flex-col font-bold text-red-600").text.strip()
        goalie_info.append({"name": goalie_name, "status": goalie_status})
        print(goalie_status)
        
    return goalie_info
    
# Main function
def main():
    # Define the number of retries
    max_retries = 5

    # Perform scraping with retries
    for _ in range(max_retries):
        try:
            goalie_info = scrape_website()
            break  # Break the loop if successful
        except Exception as e:
            print(f"Error: {e}")
            print("Retrying...")
            time.sleep(5)  # Wait for a few seconds before retrying

    # Print the scraped goalie information
    for goalie in goalie_info:
        print("Name:", goalie["name"])
        print("Status:", goalie["status"])
        print()

if __name__ == "__main__":
    main()
