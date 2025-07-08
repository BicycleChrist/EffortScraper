import asyncio
import aiohttp
import feedparser
from datetime import datetime
import re
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QScrollArea, QFrame, QSizePolicy, QToolButton, QSpinBox, QCheckBox
)
from fpscraper import FantasyProsScraper

#TODO: Manually scrape Fantasy pros as their Rss feed keeps returning errors despite being populated
# Below is an example "player-news-item" div
"""
<div class="player-news-item">
    <div class="clearfix">
        <div class="two columns player-news-image" style="padding: 8px 8px 0; background-color: #f7f7f7;">
            <a href="/nfl/players/trey-mcbride.php"><img src="https://images.fantasypros.com/images/players/nfl/22936/headshot/100x100.png" alt="Trey McBride" style="width: 100%;"></a><p style="font-size:11px; line-height:15px; margin-top:10px;"><a href="/nfl/players/trey-mcbride.php">» Rankings</a><br><a href="/nfl/stats/trey-mcbride.php">» Stats</a><br><a href="/nfl/news/trey-mcbride.php">» More News</a></p>        </div>
        <div class="ten columns">
            <div class="player-news-header clearfix"><div class="ten columns"><span style="font-size:16px; font-weight:bold;"><a href="/nfl/news/509904/trey-mcbride-signs-four-year-extension-with-cardinals-.php" target="_blank">Trey McBride signs four-year extension with Cardinals </a></span><br><p>Thu, Apr 3rd 6:33pm EDT<br>
By <a href="/news/correspondents/ari-koslow.php" target="_blank">Ari Koslow</a></p></div></div><p>The Cardinals signed TE Trey McBride to a four-year, $76 million extension.</p><p><b><em>Fantasy Impact:</em></b> The extension makes McBride the highest paid tight end in NFL history. It also includes $43 million in guaranteed money. McBride is coming off a career season and remains one of the top tight ends in fantasy football. </p><span class="pull-left"><p>Category: 
<a href="/nfl/transactions.php">Transactions</a></p></span>
<span class="pull-right fp-vote-container" data-itemid="n-509904"></span>        </div>
    </div>
</div>

"""


# not actually RSS feeds
fantasypros_links = {
    "NBA": "https://www.fantasypros.com/nba/player-news",
    "NFL": "https://www.fantasypros.com/nfl/player-news",
    "MLB": "https://www.fantasypros.com/mlb/injury-news",
    "NHL": "https://www.fantasypros.com/nhl/player-news",
}


class NewsWorker(QObject):
    """Background worker to fetch news without blocking the UI"""
    news_fetched = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, sources=None):
        super().__init__()
        self.sources = sources or []
        self.league_key = None
        self.team_name = None
        self.running = False
        self.news_items = None
        self.news_by_league = {}  # Store headlines by league for ticker tape
        self.print_fetches = False
        self.scraper = FantasyProsScraper()
        self.current_thread = None  # Track current fetch thread
        print(f"[NewsWorker] printing_fetches: {self.print_fetches}")
        
        self.rss_urls = {
            # NBA
            "basketball_nba": {
                "general": [
                    "https://www.rotowire.com/rss/news.php?sport=nba",
                    "https://www.espn.com/espn/rss/nba/news",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nba",
                    "https://sports.yahoo.com/nba/rss/",
                    "https://www.cbssports.com/rss/headlines/nba"
                ],
            },
            # NFL
            "football_nfl": {
                "general": [
                    "https://www.nfl.com/rss/rsslanding?searchString=home",
                    "https://www.rotowire.com/rss/news.php?sport=nfl,",
                    "https://www.espn.com/espn/rss/nfl/news",
                    "https://www.cbssports.com/rss/headlines/nfl",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nfl",
                    "https://sports.yahoo.com/nfl/rss/"
                ]
            },
            # MLB
            "baseball_mlb": {
                "general":[
                    "https://www.rotowire.com/rss/news.php?sport=mlb",
                    "https://www.espn.com/espn/rss/mlb/news",
                    "https://www.cbssports.com/rss/headlines/mlb/",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/mlb",
                    "https://sports.yahoo.com/mlb/rss/"
                ]
            },
            # NHL
            "icehockey_nhl": {
                "general": [
                    "https://www.rotowire.com/rss/news.php?sport=nhl",
                    "https://www.espn.com/espn/rss/nhl/news",
                    "https://www.cbssports.com/rss/headlines/nhl/injuries",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nhl",
                    "https://sports.yahoo.com/nhl/rss/"
                ]
            }
        }


    def set_league(self, league_key):
        """Set the current league to fetch news for"""
        self.league_key = league_key

    def set_team(self, team_name):
        """Set a specific team to filter news for"""
        self.team_name = team_name

    def prioritize_injury_news(self, news_items, threshold=2):
        """Prioritize news items about injuries"""
        injury_keywords = [
            'injury', 'injured', 'injuries', 'hurt', 'questionable', 'doubtful',
            'out', 'expected to miss', 'ruled out', 'status', 'return', 'recovering',
            'rehabilitation', 'surgery', 'health', 'hamstring', 'ankle', 'knee',
            'IL', 'injured list', 'disabled list', 'DNP', 'game-time decision',
            'hospital', 'recover', 'active', 'inactive'
        ]

        # Score each item based on injury relevance
        for item in news_items:
            # Skip if item already has an injury score
            if 'injury_score' in item:
                continue
                
            injury_score = 0
            title = item['title'].lower()
            desc = item['description'].lower()

            # Title mentions are more important
            for keyword in injury_keywords:
                if keyword in title:
                    injury_score += 3
                if keyword in desc:
                    injury_score += 1

            item['injury_score'] = injury_score
            
            # Tag items with high injury relevance
            if item['injury_score'] >= threshold:
                item['is_injury_news'] = True
            else:
                item['is_injury_news'] = False
        
        # Sort primarily by date (newest first), not injury score
        news_items.sort(key=lambda x: x['date'], reverse=True)
        return news_items

    def filter_low_value_headlines(self, news_items):
        """Filter out fantasy rankings, speculation, and low-value headlines"""
        if not news_items:
            return news_items
        
        import re
        
        # Patterns that indicate low-value content
        low_value_patterns = [
            # Fantasy/ranking content
            r'\brank\b.*\btop\s*\d+\b',  # "rank 2025's top 10"
            r'\bbest\b.*\brank\b',  # "best running backs? ... rank"
            r'\btop\s*\d+\b.*\brank',  # "top 10 ... rank"
            r'\bpower\s*ranking',  # "power ranking"
            r'\bfantasy\s*football\b',  # "fantasy football"
            r'\bfantasy\s*baseball\b',  # "fantasy baseball"
            r'\bfantasy\s*basketball\b',  # "fantasy basketball"
            r'\bfantasy\s*hockey\b',  # "fantasy hockey"
            r'\bfantasy\s*impact\b',  # "fantasy impact"
            r'\bfantasy\s*outlook\b',  # "fantasy outlook"
            r'\bfantasy\s*preview\b',  # "fantasy preview"
            r'\bstart\s*(\w+\s+)?sit\b',  # "start/sit", "start or sit"
            r'\bweek\s*\d+\s*start\b',  # "week 10 start"
            r'\bweek\s*\d+\s*sit\b',  # "week 10 sit"
            r'\blineup\s*advice\b',  # "lineup advice"
            r'\bpick\s*up\b.*\bwaiver\b',  # "pick up waiver"
            r'\bwaiver\s*wire\b',  # "waiver wire"
            r'\bdraft\s*(\w+\s+)?pick\b',  # "draft pick", "draft top pick"
            r'\bsleeper\s*pick\b',  # "sleeper pick"
            r'\bmock\s*draft\b',  # "mock draft"
            
            # Speculation/rumor content
            r'\brumor\s*roundup\b',  # "rumor roundup"
            r'\bquestions\s*about\b',  # "questions about"
            r'\bwhat\s*if\b',  # "what if"
            r'\bshould\s*(\w+\s+)?trade\b',  # "should trade", "should the team trade"
            r'\bwill\s*(\w+\s+)?be\s*traded\b',  # "will be traded"
            r'\bfuture\s*with\b',  # "future with"
            r'\bwhere\s*will\b',  # "where will"
            r'\bwho\s*are\s*the\b',  # "who are the"
            r'\bwho\s*has\s*the\b',  # "who has the"
            r'\bwhich\s*team\b',  # "which team"
            r'\bprediction\b',  # "prediction"
            r'\bpredict\b',  # "predict"
            r'\bexpectation\b',  # "expectation"
            r'\blikely\s*to\b',  # "likely to"
            r'\bcould\s*be\b',  # "could be"
            r'\bmight\s*be\b',  # "might be"
            r'\bpotential\s*(\w+\s+)?target\b',  # "potential target"
            r'\bspeculation\b',  # "speculation"
            r'\blatest\s*on\b',  # "latest on"
            
            # Content that's more opinion/analysis than news
            r'\bopinion\b',  # "opinion"
            r'\banalysis\b',  # "analysis"
            r'\btakeaway\b',  # "takeaway"
            r'\bbreakdown\b',  # "breakdown"
            r'\bgrade\b',  # "grade"
            r'\bgrading\b',  # "grading"
            r'\brated\b',  # "rated"
            r'\brating\b',  # "rating"
            r'\boverrated\b',  # "overrated"
            r'\bunderrated\b',  # "underrated"
            r'\bwinner\b.*\bloser\b',  # "winner and loser"
            r'\bbiggest\s*(\w+\s+)?surprise\b',  # "biggest surprise"
            r'\bbest\s*(\w+\s+)?worst\b',  # "best and worst"
            r'\bmost\s*(\w+\s+)?least\b',  # "most and least"
            
            # List/countdown content
            r'\bevery\s*(\w+\s+)?team\b.*\brank\b',  # "every team ranked"
            r'\ball\s*\d+\s*team\b',  # "all 32 teams"
            r'\bcount\s*down\b',  # "count down"
            r'\btier\s*list\b',  # "tier list"
            r'\btiers\b',  # "tiers"
            
            # Generic "content" headlines
            r'\bthings\s*to\s*know\b',  # "things to know"
            r'\bwhat\s*to\s*watch\b',  # "what to watch"
            r'\bkey\s*storylines\b',  # "key storylines"
            r'\bbiggest\s*questions\b',  # "biggest questions"
            r'\bmajor\s*questions\b',  # "major questions"
            
            # Help/advice content
            r'\bhelp\b.*\brank\b',  # "help rank"
            r'\bscouts\s*help\b',  # "scouts help"
            r'\bcoaches\s*help\b',  # "coaches help"
            r'\bexecs\s*help\b',  # "execs help"
            r'\bexperts\s*help\b',  # "experts help"
        ]
        
        # Compile patterns for efficiency
        compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in low_value_patterns]
        
        # Positive patterns that indicate legitimate news (override filters)
        legitimate_patterns = [
            r'\binjur\w+\b',  # "injury", "injured", "injuries"
            r'\bsign\w+\b',  # "signed", "signing"
            r'\btrade\w+\b',  # "traded", "trading" (when not speculation)
            r'\brelease\w+\b',  # "released", "releasing"
            r'\bcut\b',  # "cut"
            r'\bwaivers\b',  # "waivers"
            r'\bretire\w+\b',  # "retired", "retiring"
            r'\bsuspend\w+\b',  # "suspended", "suspending"
            r'\bfin\w+\b',  # "fined", "fining"
            r'\bcontract\b',  # "contract"
            r'\bextension\b',  # "extension"
            r'\bdeal\b',  # "deal"
            r'\bmillion\b',  # "$X million"
            r'\byear\b.*\b(deal|contract|extension)\b',  # "4-year deal"
            r'\bagreement\b',  # "agreement"
            r'\bactivate\w+\b',  # "activated", "activating"
            r'\bIR\b',  # "IR" (injured reserve)
            r'\bIL\b',  # "IL" (injured list)
            r'\bDL\b',  # "DL" (disabled list)
            r'\bPUP\b',  # "PUP" (physically unable to perform)
            r'\bNFI\b',  # "NFI" (non-football injury)
            r'\boptioned\b',  # "optioned"
            r'\bclaimed\b',  # "claimed"
            r'\bDFA\b',  # "DFA" (designated for assignment)
            r'\boutright\b',  # "outright"
            r'\brecall\w+\b',  # "recalled", "recalling"
            r'\bdemoted\b',  # "demoted"
            r'\bpromoted\b',  # "promoted"
            r'\bstarting\b',  # "starting"
            r'\bbenched\b',  # "benched"
            r'\breturn\w+\b.*\b(from|injury|IL|IR|DL)\b',  # "returns from injury"
            r'\bcleared\b',  # "cleared"
            r'\bmedical\b',  # "medical"
            r'\bsurgery\b',  # "surgery"
            r'\boperation\b',  # "operation"
            r'\brehab\b',  # "rehab"
            r'\brecovery\b',  # "recovery"
            r'\bhealth\b',  # "health"
            r'\bdiagnosis\b',  # "diagnosis"
            r'\btest\b.*\b(positive|negative|results)\b',  # "test positive"
            r'\bprotocol\b',  # "protocol"
            r'\bquestionable\b',  # "questionable"
            r'\bdoubtful\b',  # "doubtful"
            r'\bout\b.*\b(week|month|season|game)\b',  # "out for week"
            r'\bmiss\b.*\b(game|week|month|season)\b',  # "miss game"
            r'\bexpected\s*to\s*miss\b',  # "expected to miss"
            r'\bruled\s*out\b',  # "ruled out"
            r'\bgame\s*time\s*decision\b',  # "game time decision"
            r'\bprobable\b',  # "probable"
            r'\bday\s*to\s*day\b',  # "day to day"
            r'\bweek\s*to\s*week\b',  # "week to week"
            r'\bbreaking\b',  # "breaking"
            r'\bbreaking\s*news\b',  # "breaking news"
            r'\bofficial\b',  # "official"
            r'\bconfirm\w+\b',  # "confirmed", "confirming"
            r'\bannounce\w+\b',  # "announced", "announcing"
            r'\breport\w+\b',  # "reported", "reporting"
            r'\bstatement\b',  # "statement"
            r'\bpress\s*release\b',  # "press release"
            r'\bpress\s*conference\b',  # "press conference"
            r'\binterview\b',  # "interview"
            r'\bquote\w+\b',  # "quoted", "quoting"
            r'\bsay\w+\b',  # "says", "said"
            r'\bcomment\w+\b',  # "commented", "commenting"
            r'\baddress\w+\b',  # "addressed", "addressing"
            r'\brespond\w+\b',  # "responded", "responding"
        ]
        
        compiled_legitimate = [re.compile(pattern, re.IGNORECASE) for pattern in legitimate_patterns]
        
        filtered_news = []
        filtered_count = 0
        
        for item in news_items:
            title = item.get('title', '')
            if not title:
                continue
                
            # Check if headline has legitimate news indicators
            is_legitimate = any(pattern.search(title) for pattern in compiled_legitimate)
            
            if is_legitimate:
                filtered_news.append(item)
                continue
                
            # Check if headline matches low-value patterns
            is_low_value = any(pattern.search(title) for pattern in compiled_patterns)
            
            if not is_low_value:
                filtered_news.append(item)
            else:
                filtered_count += 1
                if self.print_fetches:
                    print(f"[NewsWorker] Filtered out low-value headline: '{title[:80]}...'")
        
        print(f"[NewsWorker] Low-value filtering: {len(news_items)} → {len(filtered_news)} headlines ({filtered_count} filtered)")
        return filtered_news

    async def fetch_rss_feed_with_session(self, session, url):
        """Fetch and parse an RSS feed using provided session"""
        if self.print_fetches: print(f"fetching rss feed: {url}");
        try:
            async with session.get(url) as response:
                content = await response.text()
                feed = feedparser.parse(content)
            
            # Check if feed parsed correctly
            if hasattr(feed, 'bozo_exception') and feed.bozo_exception:
                if self.print_fetches: print(f"Warning parsing RSS feed {url}: {feed.bozo_exception}");
            
            fetched_news_items = []
            if self.print_fetches: print(f"parsing {len(feed.entries)} entries");
            
            for entry in feed.entries:  # Limit to 10 items per feed
                # Extract date (handling various formats)
                pub_date = entry.get('published_parsed', None)
                if pub_date:
                    date = datetime(*pub_date[:6])
                else:
                    # Default to current time if no date available
                    date = datetime.now()

                # Extract image if available
                image_url = None
                if 'media_content' in entry:
                    for media in entry.media_content:
                        if 'url' in media:
                            image_url = media['url']
                            break
                elif 'links' in entry:
                    for link in entry.links:
                        if link.get('type', '').startswith('image/'):
                            image_url = link.get('href')
                            break

                # Clean and limit title/description length
                title = entry.title[:120] if hasattr(entry, 'title') else "No Title"
                description = entry.get('summary', '')

                # Remove HTML tags from description
                description = re.sub(r'<[^>]+>', '', description)
                description = description[:200] + '...' if len(description) > 200 else description

                fetched_news_items.append({
                    'title': title,
                    'description': description,
                    'link': entry.link,
                    'date': date,
                    'source': feed.feed.title if hasattr(feed, 'feed') and hasattr(feed.feed, 'title') else url.split('/')[2],
                    'image_url': image_url
                })
            
            if self.print_fetches: print(f"items parsed: {len(fetched_news_items)}");
            return fetched_news_items
        except Exception as e:
            print(f"Error fetching RSS feed {url}: {str(e)}")
            return []
            
    def fetch_fantasypros_news(self):
        """Fetch news from FantasyPros website using HTML scraping"""
        try:
            # Use the appropriate method based on league
            if self.league_key == "basketball_nba":
                return self.scraper.scrape_nba_news()
            elif self.league_key == "football_nfl":
                return self.scraper.scrape_nfl_news()
            elif self.league_key == "baseball_mlb":
                return self.scraper.scrape_mlb_news()
            elif self.league_key == "icehockey_nhl":
                return self.scraper.scrape_nhl_news()
            else:
                return []
        except Exception as e:
            print(f"Error fetching FantasyPros news: {str(e)}")
            return []

    async def fetch_news(self):
        """Fetch news from all configured sources with connection pooling"""
        try:
            
            
            # Fetch news for all 4 sports leagues for ticker tape
            all_leagues = ["basketball_nba", "football_nfl", "baseball_mlb", "icehockey_nhl"]
            sources = []
            
            # Add RSS feeds for all leagues
            for league in all_leagues:
                league_feeds = self.rss_urls.get(league, {})
                if league_feeds:
                    sources.extend(league_feeds.get('general', []))

            # If we have a specific league_key set, also add team-specific feeds for that league
            if self.league_key:
                league_feeds = self.rss_urls.get(self.league_key, {})
                if league_feeds and self.team_name and self.team_name in league_feeds.get('teams', {}):
                    sources.extend(league_feeds['teams'][self.team_name])

            # Use single session for all requests - much faster!
            timeout = aiohttp.ClientTimeout(total=2)  # 2 second timeout
            connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)  # Connection pooling
            
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                # Fetch from all RSS sources in parallel
                tasks = [self.fetch_rss_feed_with_session(session, url) for url in sources]
                
                # Add FantasyPros scraping (no longer async)
                # Skip FantasyPros for now to avoid blocking - can add back later if needed

                # Execute all tasks with faster concurrency
                results = await asyncio.gather(*tasks, return_exceptions=True)

            # Combine and sort all news items (handle exceptions)
            all_news = []
            for result in results:
                if isinstance(result, Exception):
                    print(f"News fetch error: {result}")
                    continue
                if isinstance(result, list):
                    all_news.extend(result)

            # Filter by team name if specified
            if self.team_name:
                team_keywords = [self.team_name.lower()]
                # Add common variations/abbreviations
                if len(self.team_name) > 3:
                    team_keywords.append(self.team_name[-3:].lower())  # Last 3 chars as possible abbreviation

                # Filter news items containing team name in title or description
                filtered_news = []
                for item in all_news:
                    title_lower = item['title'].lower()
                    desc_lower = item['description'].lower()
                    if any(keyword in title_lower or keyword in desc_lower for keyword in team_keywords):
                        filtered_news.append(item)

                all_news = filtered_news

            # Apply injury news prioritization (but keep date sorting)
            all_news = self.prioritize_injury_news(all_news)
            
            # Apply comprehensive headline filtering for ticker tape
            all_news = self.filter_low_value_headlines(all_news)
            
            self.news_items = all_news
            
            
            # Emit the signal with results
            self.news_fetched.emit(all_news)
        finally:
            # Make sure to reset the running flag when done
            self.running = False

    def run_fetch(self):
        """Start the fetch operation in a background thread"""
        if self.running: 
            print(f"fetch already in progress!")
            return
            
        # Reset news_items to None to allow fresh fetching
        self.news_items = None
        self.running = True
        
        # Use a simple thread to run the async fetch without blocking the UI
        import threading
        def run_async_fetch():
            try:
                asyncio.run(self.fetch_news())
            except Exception as e:
                print(f"Error in background fetch: {e}")
        
        thread = threading.Thread(target=run_async_fetch, daemon=True)
        self.current_thread = thread
        thread.start()


class NewsArticleWidget(QFrame):
    """Widget to display a single news article"""

    def __init__(self, news_item, parent=None):
        super().__init__(parent)
        self.news_item = news_item
        
        # Set up frame style
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        
        # Set standard styling with smaller margins
        if news_item.get('is_injury_news', False):
            # Highlight injury news with a different background
            self.setStyleSheet("""
                NewsArticleWidget {
                    background-color: #fdf4f4;
                    border-radius: 6px;
                    border: 1px solid #f1c0c0;
                    margin: 4px;  /* Reduced margin */
                }
                QLabel {
                    color: #212529;
                }
            """)
        else:
            self.setStyleSheet("""
                NewsArticleWidget {
                    background-color: #f8f9fa;
                    border-radius: 6px;
                    border: 1px solid #e9ecef;
                    margin: 4px;  /* Reduced margin */
                }
                QLabel {
                    color: #212529;
                }
            """)
        
        # Create layout with smaller margins
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0) 
        layout.setSpacing(0)  
        
        # Add an "INJURY UPDATE" indicator if relevant, with smaller font
        if news_item.get('is_injury_news', False):
            injury_label = QLabel("⚠️ INJURY UPDATE")
            injury_label.setStyleSheet("color: #d9534f; font-weight: bold; font-size: 11px;")  # Smaller font
            layout.addWidget(injury_label)
        
        # Create header with source and date
        header_layout = QHBoxLayout()
        
        source_label = QLabel(news_item['source'])
        source_label.setStyleSheet("font-weight: bold; color: #495057; font-size: 11px;")  # Smaller font
        header_layout.addWidget(source_label)
        
        header_layout.addStretch()
        
        # Format date with smaller font
        date_str = news_item['date'].strftime('%m/%d/%Y %H:%M')
        date_label = QLabel(date_str)
        date_label.setStyleSheet("color: #6c757d; font-size: 9px;")  # Smaller font
        header_layout.addWidget(date_label)
        
        layout.addLayout(header_layout)
        
        # Create title with smaller font
        title_label = QLabel(news_item['title'])
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-weight: bold; font-size: 12px;")  # Smaller font
        layout.addWidget(title_label)
        
        # Skip the image section to save space
        # Only add description if it's injury news or particularly important
        if news_item.get('is_injury_news', False) or news_item.get('injury_score', 0) > 1:
            desc_label = QLabel(news_item['description'])
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #495057; font-size: 11px;")  # Smaller font
            layout.addWidget(desc_label)
        
        # Create "Read More" button with smaller size
        read_more_button = QPushButton("Read Article")  # Shortened text
        read_more_button.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: white;
                border: none;
                padding: 3px 8px;  /* Smaller padding */
                border-radius: 3px;
                font-size: 10px;  /* Smaller font */
            }
            QPushButton:hover {
                background-color: #0069d9;
            }
        """)
        read_more_button.clicked.connect(self.open_article)
        layout.addWidget(read_more_button, 0, Qt.AlignmentFlag.AlignRight)
        
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def open_article(self):
        """Open the article URL in the default browser"""
        url = self.news_item.get('link', '')
        if url:
            import webbrowser
            webbrowser.open(url)


class TeamNewsWidget(QWidget):
    """Widget for displaying team news and updates"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.refresh_disabled = False
        
        self.current_league = None
        self.current_team = None
        self.show_injuries_only = False
        self.all_news_items = []  # Store all news items for filtering
        self.setup_ui()
        self.setup_worker()
        
        # Add auto-refresh timer (5 minutes by default)
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_news)
        self.refresh_timer.start(10 * 60 * 1000)  # 5 minutes in milliseconds
        self.current_refresh_interval = 5  # Store current interval in minutes
        
        # Trigger initial news fetch immediately - RSS feeds should be first priority
        QTimer.singleShot(100, self.refresh_news)  # Minimal delay to ensure UI is ready

    def setup_ui(self):
        """Set up the UI components"""
        self.layout = QVBoxLayout(self)
        # Remove all margins to eliminate the spacing completely
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)  # Minimal spacing between elements
        
        # Title and controls - move them into the same line as content begins
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(5, 0, 5, 0)  # Small horizontal margins only
        
        title_label = QLabel("Team News & Injury Updates")
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #e55717;")
        header_layout.addWidget(title_label)
        
        # Add padding space between title and refresh controls
        header_layout.addSpacing(20)
        
        # Add refresh interval spinner
        refresh_layout = QHBoxLayout()
        refresh_layout.setSpacing(2)
        refresh_layout.addWidget(QLabel("Refresh Freq"))
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(0, 30)
        self.refresh_interval.setValue(25)
        self.refresh_interval.setSuffix(" min")
        self.refresh_interval.valueChanged.connect(self.update_refresh_interval)
        self.refresh_interval.setFixedWidth(70)
        self.refresh_interval.setStyleSheet("font-size: 10px;")
        refresh_layout.addWidget(self.refresh_interval)
        header_layout.addLayout(refresh_layout)
        
        # Add injury filter checkbox
        self.injury_filter = QCheckBox("Injuries Only")
        self.injury_filter.setStyleSheet("""
            QCheckBox {
                font-size: 10px;
                color: #d9534f;
                padding: 4px
                font-weight: bold;
            }
        """)
        self.injury_filter.toggled.connect(self.toggle_injury_filter)
        header_layout.addWidget(self.injury_filter)
        
        header_layout.addStretch()
        
        # Team filter dropdown
        self.team_filter = QComboBox()
        self.team_filter.addItem("All Teams")
        self.team_filter.currentTextChanged.connect(self.on_team_changed)
        header_layout.addWidget(QLabel("Team:"))
        header_layout.addWidget(self.team_filter)
        
        # Refresh button
        self.refresh_button = QToolButton()
        self.refresh_button.setText("⟳")
        self.refresh_button.setToolTip("Refresh News")
        self.refresh_button.clicked.connect(self.refresh_news)
        self.refresh_button.setStyleSheet("""
            QToolButton {
                background-color: #28a745;
                color: white;
                font-size: 14px;
                padding: 2px;
                border-radius: 3px;
            }
            QToolButton:hover {
                background-color: #218838;
            }
        """)
        header_layout.addWidget(self.refresh_button)
        
        self.layout.addLayout(header_layout)
        
        # Status label - make it more compact
        self.status_label = QLabel("Loading news...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #6c757d; font-style: italic; font-size: 11px;")
        self.status_label.setMaximumHeight(20)  # Limit height
        self.layout.addWidget(self.status_label)
        
        # Scroll area for news items - remove any frame or border
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setContentsMargins(0, 0, 0, 1)
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_layout.setSpacing(4)  # Minimal spacing
        self.scroll_layout.setContentsMargins(5, 0, 5, 0)  # Small horizontal margins only
        
        self.scroll_area.setWidget(self.scroll_content)
        self.layout.addWidget(self.scroll_area)

    def setup_worker(self):
        """Set up the background worker for fetching news"""
        self.worker = NewsWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        # Connect signals
        self.worker.news_fetched.connect(self.on_news_fetched)
        self.worker.error_occurred.connect(self.show_error)

        # Start the thread
        self.worker_thread.start()

    def set_league(self, league_key):
        """Set the current league and update the team dropdown"""
        self.current_league = league_key
        self.worker.set_league(league_key)

        # Update team dropdown with teams for this league
        self.update_team_dropdown(league_key)

        # Apply filter to existing news items instead of fetching new ones
        if hasattr(self.worker, 'news_items') and self.worker.news_items:
            self.update_news_items(self.worker.news_items)

    def update_team_dropdown(self, league_key):
        """Update the team dropdown with teams for the current league"""
        self.team_filter.clear()
        self.team_filter.addItem("All Teams")

        # Add teams for the selected league
        teams = self.get_teams_for_league(league_key)
        for team in teams:
            self.team_filter.addItem(team)

    def get_teams_for_league(self, league_key):
        """Get list of teams for the specified league"""
        # You can expand this with a more complete list for each league
        teams = {
            "basketball_nba": [
                "Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers",
                "Mavericks", "Nuggets", "Pistons", "Warriors", "Rockets", "Pacers",
                "Clippers", "Lakers", "Grizzlies", "Heat", "Bucks", "Timberwolves",
                "Pelicans", "Knicks", "Thunder", "Magic", "76ers", "Suns",
                "Trail Blazers", "Kings", "Spurs", "Raptors", "Jazz", "Wizards"
            ],
            "football_nfl": [
                "Cardinals", "Falcons", "Ravens", "Bills", "Panthers", "Bears",
                "Bengals", "Browns", "Cowboys", "Broncos", "Lions", "Packers",
                "Texans", "Colts", "Jaguars", "Chiefs", "Raiders", "Chargers",
                "Rams", "Dolphins", "Vikings", "Patriots", "Saints", "Giants",
                "Jets", "Eagles", "Steelers", "49ers", "Seahawks", "Buccaneers",
                "Titans", "Commanders"
            ],
            "baseball_mlb": [
                "Diamondbacks", "Braves", "Orioles", "Red Sox", "Cubs", "White Sox",
                "Reds", "Guardians", "Rockies", "Tigers", "Astros", "Royals",
                "Angels", "Dodgers", "Marlins", "Brewers", "Twins", "Mets",
                "Yankees", "Athletics", "Phillies", "Pirates", "Padres", "Giants",
                "Mariners", "Cardinals", "Rays", "Rangers", "Blue Jays", "Nationals"
            ],
            "icehockey_nhl": [
                "Ducks", "Bruins", "Sabres", "Flames", "Hurricanes", "Blackhawks",
                "Avalanche", "Blue Jackets", "Stars", "Red Wings", "Oilers", "Panthers",
                "Kings", "Wild", "Canadiens", "Predators", "Devils", "Islanders",
                "Rangers", "Senators", "Flyers", "Penguins", "Sharks", "Kraken",
                "Blues", "Lightning", "Maple Leafs", "Canucks", "Golden Knights", "Capitals"
            ],
            "soccer_usa_mls": [
                "Atlanta United", "Austin FC", "Charlotte FC", "Chicago Fire",
                "Colorado Rapids", "Columbus Crew", "D.C. United", "FC Cincinnati",
                "FC Dallas", "Houston Dynamo", "Inter Miami", "LA Galaxy",
                "LAFC", "Minnesota United", "Nashville SC", "New England Revolution",
                "New York City FC", "New York Red Bulls", "Orlando City", "Philadelphia Union",
                "Portland Timbers", "Real Salt Lake", "San Jose Earthquakes",
                "Seattle Sounders", "Sporting Kansas City", "St. Louis City", "Toronto FC", "Vancouver Whitecaps"
            ]
        }

        return teams.get(league_key, [])

    def on_team_changed(self, team_name):
        """Handle team selection change"""
        if team_name == "All Teams":
            self.current_team = None
            self.worker.set_team(None)
        else:
            self.current_team = team_name
            self.worker.set_team(team_name)

        # Apply filter to existing news items instead of refreshing
        if hasattr(self.worker, 'news_items') and self.worker.news_items:
            self.update_news_items(self.worker.news_items)

    def toggle_injury_filter(self, checked):
        """Toggle showing only injury news"""
        self.show_injuries_only = checked
        
        # Apply filter to existing news items
        if self.all_news_items:
            self.update_news_items(self.all_news_items)
            
        # Update status to reflect filter status
        if checked and not self.all_news_items:
            self.status_label.setText("No injury news found")
            self.status_label.setVisible(True)

    def refresh_news(self):
        """Refresh news data"""
        self.status_label.setText("Loading news...")
        self.status_label.setVisible(True)

        # Clear current news items
        self.clear_news_items()
        
        # Force the worker to reset its cached news_items
        if hasattr(self.worker, 'news_items'):
            self.worker.news_items = None
        
        # Make sure the running flag is reset (in case it got stuck)
        self.worker.running = False

        # Start the worker using QTimer to avoid blocking
        QTimer.singleShot(0, self.worker.run_fetch)

    def clear_news_items(self):
        """Clear all news items from the display"""
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def on_news_fetched(self, news_items):
        """Handle when news is fetched - store all items first"""
        # Store all news items before filtering
        self.all_news_items = news_items
        self.update_news_items(news_items)

    def update_news_items(self, news_items):
        """Update the display with news items, applying filters as needed"""
        self.clear_news_items()
        
        # Apply injury filter if enabled
        filtered_items = news_items
        if self.show_injuries_only:
            filtered_items = [item for item in news_items if item.get('is_injury_news', False)]
        
        # Sort by date (newest first) - this is now redundant as we sort in prioritize_injury_news
        # But keeping it here as a safeguard
        filtered_items.sort(key=lambda x: x['date'], reverse=True)

        if not filtered_items:
            message = "No injury news found" if self.show_injuries_only else "No news items found"
            self.status_label.setText(message)
            self.status_label.setVisible(True)
            return

        self.status_label.setVisible(False)

        # Add news items to the scroll area
        for item in filtered_items:
            news_widget = NewsArticleWidget(item)
            self.scroll_layout.addWidget(news_widget)

        # Add a stretch at the end to push items to the top
        self.scroll_layout.addStretch()

    def show_error(self, error_message):
        """Display an error message"""
        self.status_label.setText(f"Error: {error_message}")
        self.status_label.setVisible(True)
        
    def update_refresh_interval(self):
        """Update the timer interval when spinbox value changes"""
        new_interval = self.refresh_interval.value()
        if new_interval <= 0:
            print("[TeamNewsWidget] disabling refresh")
            self.current_refresh_interval = 0
            self.refresh_disabled = True
            self.refresh_timer.stop()
        elif new_interval != self.current_refresh_interval:
            self.current_refresh_interval = new_interval
            interval_ms = new_interval * 60 * 1000  # Convert minutes to milliseconds
            self.refresh_disabled = False
            self.refresh_timer.stop()
            self.refresh_timer.start(interval_ms)
            print(f"News refresh interval updated to {new_interval} minutes")

    def handle_league_change(self, league_key):
        """Public method to update when the main app changes leagues"""
        if league_key in ["basketball_nba", "football_nfl", "baseball_mlb",
                         "icehockey_nhl", "soccer_usa_mls"]:
            self.set_league(league_key)


# For testing the widget standalone
if __name__ == "__main__":
    app = QApplication(sys.argv)

    widget = TeamNewsWidget()
    widget.set_league("basketball_nba")  # Set initial league
    widget.show()

    sys.exit(app.exec())
