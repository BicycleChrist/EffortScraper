import requests
from bs4 import BeautifulSoup
import threading

# TODO: Ignore links that don't lead to a stream, output is far greater than needed
# I.E https://buffstreams.ai/soccer or https://the.streameast.app/team/anaheim-ducks


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


def parse_url(url):
    print(f"Links from {url}:")
    href_links = get_href_links(url)
    for link in href_links:
        print(link)
    print()


def parse_urls(urls):
    threads = []
    for url in urls:
        thread = threading.Thread(target=parse_url, args=(url,))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


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
