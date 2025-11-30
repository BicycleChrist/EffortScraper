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
    """Filter for game links from various streaming sites"""
    # Totalsportek pattern: /game/team-vs-team/ID/
    if '/game/' in link and link.count('/') >= 4:
        segments = link.rstrip('/').split('/')
        if segments and segments[-1].isnumeric():
            return True

    # NFLbite/NBABite/MLBBite pattern: /Team-vs-Team/ID or /Team-vs-Team/ID/
    # Must have 'vs' and end with a numeric ID
    if 'vs' in link.lower() or 'Vs' in link:
        segments = link.rstrip('/').split('/')
        # Check if last segment is numeric
        if segments and segments[-1].isnumeric():
            # Make sure it's not a news article or other non-game link
            if '/news/' not in link and '/teams/' not in link:
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
    """Get stream links from individual game page (supports multiple site formats)"""
    try:
        response = requests.get(game_url, timeout=10, allow_redirects=True)
        if response.status_code != 200:
            print(f"Error: Received status code {response.status_code} from {game_url}")
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        stream_links = []

        # Method 1: Look for embedded stream table rows (nflbite, mlbbite, nbabite, etc.)
        table_rows = soup.find_all('tr', id='tr-round')
        if table_rows:
            for row in table_rows:
                # Find the hidden input with the stream URL
                hidden_input = row.find('input', {'type': 'hidden'})
                stream_url = hidden_input.get('value', '') if hidden_input else None

                # Find the provider name
                provider_td = row.find('td', class_='display-bg')
                if provider_td:
                    provider_name = provider_td.get_text(strip=True).replace('✓', '').strip()
                else:
                    provider_name = 'Unknown'

                if stream_url:
                    stream_links.append({
                        'provider': provider_name,
                        'url': stream_url
                    })

        # Method 2: If no table rows found, look for external site links (totalsportek style)
        if not stream_links:
            all_links = soup.find_all('a', href=True)
            stream_keywords = ['stream', 'watch', 'hd', 'link', 'bite', 'surge', 'hesgoal', 'sport']

            # Common sports/league names to exclude (these are navigation, not streams)
            league_names = ['football', 'soccer', 'basketball', 'baseball', 'hockey', 'tennis',
                           'premier league', 'champions league', 'europa league', 'la liga',
                           'nfl', 'nba', 'mlb', 'nhl', 'mls', 'serie a', 'bundesliga']

            for link in all_links:
                text = link.get_text().strip()
                href = link.get('href', '')

                if text and href and href.startswith('http'):
                    # Skip if it's just a league/sport name
                    if text.lower() in league_names:
                        continue

                    has_stream_keyword = any(keyword in text.lower() for keyword in stream_keywords)
                    is_short_caps_name = text.isupper() and 3 <= len(text) <= 30

                    if has_stream_keyword or is_short_caps_name:
                        is_social_or_other = any(exclude in href.lower() for exclude in ['facebook', 'twitter', 'instagram', 'javascript:', 'mailto:'])
                        is_internal_nav = 'totalsportek' in href and any(nav in href for nav in ['/soccerstreams', '/nflstreams', '/nbastreams', '/nhlstreams', '/mlbstreams', '/game/'])

                        if not is_internal_nav and not is_social_or_other:
                            stream_links.append({
                                'provider': text,
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
