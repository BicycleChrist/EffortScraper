import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

class FantasyProsScraper:
    """Class to scrape news from FantasyPros website"""

    def __init__(self):
        self.base_url = "https://www.fantasypros.com"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

    def scrape_nfl_news(self):
        """Scrape NFL news from FantasyPros"""
        url = f"{self.base_url}/nfl/player-news.php"
        return self._scrape_news(url, "NFL")

    def scrape_nba_news(self):
        """Scrape NBA news from FantasyPros"""
        url = f"{self.base_url}/nba/player-news.php"
        return self._scrape_news(url, "NBA")

    def scrape_mlb_news(self):
        """Scrape MLB news from FantasyPros"""
        url = f"{self.base_url}/mlb/player-news.php"
        return self._scrape_news(url, "MLB")

    def scrape_nhl_news(self):
        """Scrape NHL news from FantasyPros"""
        url = f"{self.base_url}/nhl/player-news.php"
        return self._scrape_news(url, "NHL")

    def _parse_date(self, date_str):
        """Parse date string from FantasyPros format"""
        # Example format: "Thu, Apr 17th 12:34am EDT"
        try:
            # Extract components
            day_str, time_str = date_str.rsplit(' ', 1)[0].rsplit(',', 1)

            # Parse time
            day_str = day_str.strip()
            time_str = time_str.strip()

            # Convert month abbreviation to number
            month_map = {
                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
            }

            # Extract parts
            parts = time_str.split()
            month = month_map[parts[0]]

            # Extract day, removing ordinal suffix (1st, 2nd, 3rd, etc.)
            day = int(re.sub(r'(\d+)(st|nd|rd|th)', r'\1', parts[1]))

            # Parse time with AM/PM
            time_parts = parts[2].lower()
            hour, minute = map(int, time_parts[:-2].split(':'))

            # Adjust for PM
            if 'pm' in time_parts and hour < 12:
                hour += 12
            # Adjust for 12 AM
            if 'am' in time_parts and hour == 12:
                hour = 0

            # Use current year (in a production environment, you might want to handle year boundaries better)
            year = datetime.now().year

            return datetime(year, month, day, hour, minute)
        except Exception as e:
            print(f"Error parsing date '{date_str}': {str(e)}")
            return datetime.now()  # Fallback to current time

    def _scrape_news(self, url, source):
        """Scrape news from a given URL"""
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            news_items = []

            # Find all news items
            for item in soup.select('.player-news-item'):
                try:
                    # Extract player info
                    player_link = item.select_one('.player-news-image a')
                    player_name = player_link.get('alt') if player_link else "Unknown Player"
                    player_img = item.select_one('.player-news-image img')
                    image_url = player_img.get('src') if player_img else None

                    # Extract article info
                    title_elem = item.select_one('.player-news-header a')
                    title = title_elem.text.strip() if title_elem else "No Title"
                    link = self.base_url + title_elem.get('href') if title_elem and title_elem.get('href').startswith('/') else title_elem.get('href') if title_elem else ""

                    # Extract date
                    date_elem = item.select_one('.player-news-header p')
                    date_str = date_elem.contents[0].strip() if date_elem else ""
                    date = self._parse_date(date_str)

                    # Extract description
                    desc_elem = item.select('.ten.columns p')
                    description = desc_elem[0].text.strip() if len(desc_elem) > 0 else ""

                    # Extract fantasy impact
                    fantasy_impact = ""
                    impact_elem = item.select_one('p:contains("Fantasy Impact")')
                    if impact_elem:
                        fantasy_impact = impact_elem.text.replace('Fantasy Impact:', '').strip()

                    # Extract category
                    category = ""
                    category_elem = item.select_one('.pull-left p')
                    if category_elem:
                        category_text = category_elem.text.strip()
                        if "Category:" in category_text:
                            category = category_text.replace("Category:", "").strip()

                    # Check if it's injury news
                    is_injury_news = ('injury' in title.lower() or
                                     'injury' in description.lower() or
                                     category.lower() == 'injury updates')

                    # Calculate injury score for sorting
                    injury_score = 0
                    injury_keywords = [
                        'injury', 'injured', 'injuries', 'hurt', 'questionable', 'doubtful',
                        'out', 'expected to miss', 'ruled out', 'status', 'return', 'recovering',
                        'rehabilitation', 'surgery', 'health', 'hamstring', 'ankle', 'knee',
                        'IL', 'injured list', 'disabled list', 'DNP', 'game-time decision',
                        'hospital', 'recover', 'active', 'inactive'
                    ]

                    # Title mentions are more important
                    for keyword in injury_keywords:
                        if keyword in title.lower():
                            injury_score += 3
                        if keyword in description.lower():
                            injury_score += 1
                        if keyword in fantasy_impact.lower():
                            injury_score += 2

                    # Create news item
                    news_item = {
                        'title': f"{player_name}: {title}",
                        'description': description + (f"\n\nFantasy Impact: {fantasy_impact}" if fantasy_impact else ""),
                        'link': link,
                        'date': date,
                        'source': f"FantasyPros {source}",
                        'image_url': image_url,
                        'is_injury_news': is_injury_news or injury_score >= 2,
                        'injury_score': injury_score
                    }

                    news_items.append(news_item)
                except Exception as e:
                    print(f"Error processing news item: {str(e)}")
                    continue

            return news_items
        except Exception as e:
            print(f"Error scraping {url}: {str(e)}")
            return []


# Integration with NewsWorker class
def fetch_fantasypros(league_key):
    """Fetch news from FantasyPros based on league key"""
    scraper = FantasyProsScraper()

    if league_key == "basketball_nba":
        return scraper.scrape_nba_news()
    elif league_key == "football_nfl":
        return scraper.scrape_nfl_news()
    elif league_key == "baseball_mlb":
        return scraper.scrape_mlb_news()
    elif league_key == "icehockey_nhl":
        return scraper.scrape_nhl_news()
    else:
        return []


if __name__ == "__main__":
    result = fetch_fantasypros('baseball_mlb')
    print(result)
