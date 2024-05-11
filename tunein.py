import requests
from bs4 import BeautifulSoup
import threading

# TODO: Ignore links that don't lead to a stream, output is far greater than needed
# I.E https://buffstreams.ai/soccer or https://the.streameast.app/team/anaheim-ducks

# add methods for new sites in here
def GetSiteMainContent(site, soup:BeautifulSoup):
    if site == "buffstreams":
        main_content = soup.find('main', class_="container contentContain")
        return main_content.extract()
    elif site == "streameast": # hoping that HTML is the same across mirrors
        site_wrapper = soup.find('div', class_="site-wrapper")
        main_content = site_wrapper.find('main')
        return main_content.extract()
    else:
        print("unrecognized site")
        return soup
    return soup


def GetSite(url):
    start_name = url.removeprefix("https://")
    if start_name.startswith("buffstreams"): return "buffstreams"
    elif start_name.startswith("the.streameast"): return "streameast"
    else: return "unknown site"

def SiteFilterHref(site, link):
    lastsegment = link.split('/')[-1]
    if not lastsegment.isnumeric(): return False
    if site == "streameast":
        if '/team/' in link: return False
    return True

def get_href_links(url):
    try:
        # TODO: check status code
        response = requests.get(url, timeout=5)
        soup = BeautifulSoup(response.content, 'html.parser')
        site = GetSite(url)
        main_content = GetSiteMainContent(site, soup)
        links = main_content.find_all('a', href=True)
        href_links = [link['href'] for link in links if SiteFilterHref(site, link['href'])]
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
        "https://the.streameast.app/mlbstreams17",
        "https://the.streameast.app/nbastreams64",
        "https://the.streameast.app/nflstreams3",
        "https://buffstreams.ai/mlb",
        "https://buffstreams.ai/nba",
        "https://buffstreams.ai/nhl"
    ]
    parse_urls(urls)
