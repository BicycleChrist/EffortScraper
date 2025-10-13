import requests
from bs4 import BeautifulSoup
import concurrent.futures
from urllib.parse import urlparse


def get_site(url):
    """Identify which site we're scraping"""
    start_name = url.removeprefix("https://")
    if "totalsportek" in start_name:
        return "totalsportek"
    else:
        return "unknown"


def filter_game_links(link):
    """Filter for totalsportek game links that match pattern: /game/team-vs-team/ID/"""
    if '/game/' in link and link.count('/') >= 4:
        segments = link.rstrip('/').split('/')
        if segments and segments[-1].isnumeric():
            return True
    return False

def get_href_links(url):
    """Get game page links from totalsportek main page"""
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            print(f"Error: Received status code {response.status_code} from {url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        links = soup.find_all('a', href=True)
        href_links = [link['href'] for link in links if filter_game_links(link['href'])]

        # Make sure URLs are absolute
        absolute_links = []
        for link in href_links:
            if link.startswith('http'):
                absolute_links.append(link)
            elif link.startswith('/'):
                parsed = urlparse(url)
                absolute_links.append(f"{parsed.scheme}://{parsed.netloc}{link}")
            else:
                base_url = url.rstrip('/')
                absolute_links.append(f"{base_url}/{link}")

        return absolute_links
    except Exception as e:
        print(f"Error fetching links from {url}: {e}")
        return []


def get_stream_links(game_url):
    """Get stream links from individual totalsportek game page"""
    try:
        response = requests.get(game_url, timeout=5)
        if response.status_code != 200:
            print(f"Error: Received status code {response.status_code} from {game_url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        stream_links = []

        # Find all data-row divs that contain stream links
        data_rows = soup.find_all('div', class_='data-row')

        for row in data_rows:
            # Find the anchor tag with the stream link
            link = row.find('a', href=True)
            if link:
                href = link['href']
                # Extract stream provider name if available
                provider_col = row.find('div', class_='col-md-3')
                provider_name = provider_col.get_text(strip=True) if provider_col else "Unknown"

                stream_links.append({
                    'provider': provider_name,
                    'url': href
                })

        return stream_links
    except Exception as e:
        print(f"Error fetching stream links from {game_url}: {e}")
        return []


def parse_url(url):
    """Parse main page and print game links"""
    print(f"\n{'='*80}")
    print(f"Game links from {url}:")
    print('='*80)
    href_links = get_href_links(url)
    for link in href_links:
        print(f"  {link}")
    print(f"\nTotal games found: {len(href_links)}")
    return href_links


def parse_game_page(game_url):
    """Parse individual game page and print stream links"""
    print(f"\n  Streams for {game_url}:")
    stream_links = get_stream_links(game_url)
    for stream in stream_links:
        if isinstance(stream, dict):
            print(f"    - {stream['provider']}: {stream['url']}")
        else:
            print(f"    - {stream}")
    return stream_links


def parse_urls_deep(urls, scrape_streams=True):
    """
    Two-stage scraping:
    1. Get all game links from main pages
    2. Get all stream links from each game page
    """
    all_game_links = []

    # Stage 1: Get game links from main pages
    print("\n" + "="*80)
    print("STAGE 1: Fetching game links from main pages")
    print("="*80)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(parse_url, url) for url in urls]
        for future in concurrent.futures.as_completed(futures):
            game_links = future.result()
            all_game_links.extend(game_links)

    if not scrape_streams:
        return all_game_links

    # Stage 2: Get stream links from each game page
    print("\n" + "="*80)
    print(f"STAGE 2: Fetching stream links from {len(all_game_links)} game pages")
    print("="*80)
    all_streams = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_game = {executor.submit(parse_game_page, game_url): game_url
                          for game_url in all_game_links}
        for future in concurrent.futures.as_completed(future_to_game):
            game_url = future_to_game[future]
            streams = future.result()
            all_streams[game_url] = streams

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"Total games found: {len(all_game_links)}")
    print(f"Total games with streams: {sum(1 for s in all_streams.values() if s)}")
    print(f"Total stream links: {sum(len(s) for s in all_streams.values())}")

    return all_streams


def parse_urls(urls):
    """Legacy function - just parse main pages"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=None) as executor:
        futures = [executor.submit(parse_url, url) for url in urls]
        for future in concurrent.futures.as_completed(futures):
            pass


if __name__ == "__main__":
    urls = [
        "https://today.totalsportek.army/",
    ]

    # Two-stage scraping: get game links, then stream links
    parse_urls_deep(urls, scrape_streams=True)
