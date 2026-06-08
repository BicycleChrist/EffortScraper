import requests
from bs4 import BeautifulSoup
import concurrent.futures
from urllib.parse import urlparse, urljoin
import re

# Set True to dump every scraped <a> link to stdout. Off by default: the
# per-link print loop in get_href_links was ~30 GIL-held, synchronized
# stdout writes per scrape, which stalled the main-thread ticker (~364ms).
DEBUG = False

# Prefer lxml
try:
    BeautifulSoup("<a></a>", "lxml")
    _BS_PARSER = "lxml"
except Exception:
    _BS_PARSER = "html.parser"


def get_site(url):
    """Identify which site we're scraping"""
    start_name = url.removeprefix("https://")


def filter_game_links(link):
    """
    Filter for likely game/event links.

    istreameast structure:
      /sport-or-league/team-one-vs-team-two/42249792
      e.g. /nhl-playoffs/vegas-golden-knights-colorado-avalanche/42249792

    We require a hyphenated slug segment immediately before the numeric ID
    so that bare version paths like /v52 are never matched.
    """

    if not link:
        return False

    link_lower = link.lower()

    # Skip junk/social/navigation links
    banned = [
        'twitter',
        'facebook',
        'instagram',
        'discord',
        'telegram',
        'javascript:',
        'mailto:',
        '#'
    ]

    if any(x in link_lower for x in banned):
        return False

    # Match URLs where a hyphenated slug precedes the trailing numeric ID.
    # Pattern: /some-hyphenated-slug/42249792  (optional trailing slash)
    # This excludes plain /v52 or /42249792 with no slug segment before it.
    if re.search(r'/[a-z0-9]+(?:-[a-z0-9]+)+/\d+/?$', link_lower):
        return True

    return False


def get_href_links(url):
    """Get game page links from main page"""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            print(f"Error: Received status code {response.status_code} from {url}")
            return []

        soup = BeautifulSoup(response.content, _BS_PARSER)

        links = soup.find_all('a', href=True)

        if DEBUG: print(f"\nTotal raw links found: {len(links)}")

        href_links = []

        for a in links:
            href = a.get('href', '')

            result = filter_game_links(href)

            if DEBUG:
                text = a.get_text(strip=True)
                print(f"[{result}] TEXT={text!r} HREF={href!r}")

            if result:
                href_links.append(href)

        # Convert all links to absolute URLs safely
        absolute_links = [urljoin(url, href) for href in href_links]

        # Remove duplicates while preserving order
        absolute_links = list(dict.fromkeys(absolute_links))

        return absolute_links

    except Exception as e:
        print(f"Error fetching links from {url}: {e}")
        return []


def get_stream_links(game_url):
    """
    Get stream links from an individual game page.

    istreameast has exactly one stream per event — the game page itself
    is the stream destination.  We extract a human-readable label from
    the URL slug rather than scraping the page, which avoids picking up
    navigation links (NCAAB, BOXING, etc.) as fake stream entries.
    """
    try:
        # Derive a tidy sport/league label from the URL path.
        # e.g. /nhl-playoffs/vegas-golden-knights-colorado-avalanche/42249792
        #       parts[-3] = "nhl-playoffs"  -> "Nhl Playoffs"
        parts = game_url.rstrip('/').split('/')
        if len(parts) >= 3 and parts[-1].isdigit():
            sport_slug = parts[-3]
            provider_label = sport_slug.replace('-', ' ').title()
        else:
            provider_label = 'Stream'

        return [{'provider': provider_label, 'url': game_url}]

    except Exception as e:
        print(f"Error building stream link for {game_url}: {e}")
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

    # Stage 1: Get game links
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

    # Stage 2: Get stream links
    print("\n" + "="*80)
    print(f"STAGE 2: Fetching stream links from {len(all_game_links)} game pages")
    print("="*80)

    all_streams = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:

        future_to_game = {
            executor.submit(parse_game_page, game_url): game_url
            for game_url in all_game_links
        }

        for future in concurrent.futures.as_completed(future_to_game):

            game_url = future_to_game[future]

            streams = future.result()

            all_streams[game_url] = streams

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)

    print(f"Total games found: {len(all_game_links)}")

    print(
        f"Total games with streams: "
        f"{sum(1 for s in all_streams.values() if s)}"
    )

    print(
        f"Total stream links: "
        f"{sum(len(s) for s in all_streams.values())}"
    )

    return all_streams


def parse_urls(urls):
    """Legacy function - just parse main pages"""

    with concurrent.futures.ThreadPoolExecutor(max_workers=None) as executor:

        futures = [executor.submit(parse_url, url) for url in urls]

        for future in concurrent.futures.as_completed(futures):
            pass


if __name__ == "__main__":

    urls = [
        "https://istreameast.app/v52",
    ]

    # Two-stage scraping
    parse_urls_deep(urls, scrape_streams=True)
