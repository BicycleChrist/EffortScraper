import requests
from bs4 import BeautifulSoup

def get_href_links(url):
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        links = soup.find_all('a', href=True)
        href_links = [link['href'] for link in links]
        return href_links
    except Exception as e:
        print(f"Error fetching links from {url}: {e}")
        return []

def parse_urls(urls):
    for url in urls:
        print(f"Links from {url}:")
        href_links = get_href_links(url)
        for link in href_links:
            print(link)
        print()

if __name__ == "__main__":
    urls = [
        "https://the.streameast.app/nhlstreams3",
        #"https://the.streameast.app/mlbstreams17",
        #"https://the.streameast.app/nbastreams64",
        #"https://the.streameast.app/nflstreams3",
        "https://buffstreams.ai/mlb",
        "https://buffstreams.ai/nba",
        "https://buffstreams.ai/nhl"
    ]
    parse_urls(urls)

