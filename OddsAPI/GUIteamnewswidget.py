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

        # Pre-compile regex patterns for filtering - MAJOR PERFORMANCE OPTIMIZATION
        # Compiling these once at init instead of on every filter call saves 100-500ms
        self._compile_filter_patterns()

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
                    "https://www.rotowire.com/rss/news.php?sport=nfl",
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
                    "https://sports.yahoo.com/mlb/rss/",
                    # Bullpen/closer-role changes — Cloudflare-fronted (see
                    # the browser User-Agent set on the session below).
                    "https://closermonkey.com/feed/",
                    # Fast on transactions / IL moves / signings.
                    "https://www.mlbtraderumors.com/feed"
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


    def _compile_filter_patterns(self):
        """Pre-compile all regex patterns for fast filtering"""
        # Low-value patterns
        low_value_patterns = [
            r'\brank\b.*\btop\s*\d+\b', r'\bbest\b.*\brank\b', r'\btop\s*\d+\b.*\brank',
            r'\bpower\s*ranking', r'\bfantasy\s*football\b', r'\bfantasy\s*baseball\b',
            r'\bfantasy\s*basketball\b', r'\bfantasy\s*hockey\b', r'\bfantasy\s*impact\b',
            r'\bfantasy\s*outlook\b', r'\bfantasy\s*preview\b', r'\bstart\s*(\w+\s+)?sit\b',
            r'\bweek\s*\d+\s*start\b', r'\bweek\s*\d+\s*sit\b', r'\blineup\s*advice\b',
            r'\bpick\s*up\b.*\bwaiver\b', r'\bwaiver\s*wire\b', r'\bdraft\s*(\w+\s+)?pick\b',
            r'\bsleeper\s*pick\b', r'\bmock\s*draft\b', r'\brumor\s*roundup\b',
            r'\bquestions\s*about\b', r'\bwhat\s*if\b', r'\bshould\s*(\w+\s+)?trade\b',
            r'\bwill\s*(\w+\s+)?be\s*traded\b', r'\bfuture\s*with\b', r'\bwhere\s*will\b',
            r'\bwho\s*are\s*the\b', r'\bwho\s*has\s*the\b', r'\bwhich\s*team\b',
            r'\bprediction\b', r'\bpredict\b', r'\bexpectation\b', r'\blikely\s*to\b',
            r'\bcould\s*be\b', r'\bmight\s*be\b', r'\bpotential\s*(\w+\s+)?target\b',
            r'\bspeculation\b', r'\blatest\s*on\b', r'\bopinion\b', r'\banalysis\b',
            r'\btakeaway\b', r'\bbreakdown\b', r'\bgrade\b', r'\bgrading\b',
            r'\brated\b', r'\brating\b', r'\boverrated\b', r'\bunderrated\b',
            r'\bwinner\b.*\bloser\b', r'\bbiggest\s*(\w+\s+)?surprise\b',
            r'\bbest\s*(\w+\s+)?worst\b', r'\bmost\s*(\w+\s+)?least\b',
            r'\bevery\s*(\w+\s+)?team\b.*\brank\b', r'\ball\s*\d+\s*team\b',
            r'\bcount\s*down\b', r'\btier\s*list\b', r'\btiers\b',
            r'\bthings\s*to\s*know\b', r'\bwhat\s*to\s*watch\b', r'\bkey\s*storylines\b',
            r'\bbiggest\s*questions\b', r'\bmajor\s*questions\b', r'\bhelp\b.*\brank\b',
            r'\bscouts\s*help\b', r'\bcoaches\s*help\b', r'\bexecs\s*help\b', r'\bexperts\s*help\b'
        ]

        # Legitimate news patterns
        legitimate_patterns = [
            r'\binjur\w+\b', r'\bsign\w+\b', r'\btrade\w+\b', r'\brelease\w+\b',
            r'\bcut\b', r'\bwaivers\b', r'\bretire\w+\b', r'\bsuspend\w+\b',
            r'\bfin\w+\b', r'\bcontract\b', r'\bextension\b', r'\bdeal\b',
            r'\bmillion\b', r'\byear\b.*\b(deal|contract|extension)\b', r'\bagreement\b',
            r'\bactivate\w+\b', r'\bIR\b', r'\bIL\b', r'\bDL\b', r'\bPUP\b',
            r'\bNFI\b', r'\boptioned\b', r'\bclaimed\b', r'\bDFA\b', r'\boutright\b',
            r'\brecall\w+\b', r'\bdemoted\b', r'\bpromoted\b', r'\bstarting\b',
            r'\bbenched\b', r'\breturn\w+\b.*\b(from|injury|IL|IR|DL)\b', r'\bcleared\b',
            r'\bmedical\b', r'\bsurgery\b', r'\boperation\b', r'\brehab\b',
            r'\brecovery\b', r'\bhealth\b', r'\bdiagnosis\b',
            r'\btest\b.*\b(positive|negative|results)\b', r'\bprotocol\b',
            r'\bquestionable\b', r'\bdoubtful\b', r'\bout\b.*\b(week|month|season|game)\b',
            r'\bmiss\b.*\b(game|week|month|season)\b', r'\bexpected\s*to\s*miss\b',
            r'\bruled\s*out\b', r'\bgame\s*time\s*decision\b', r'\bprobable\b',
            r'\bday\s*to\s*day\b', r'\bweek\s*to\s*week\b', r'\bbreaking\b',
            r'\bbreaking\s*news\b', r'\bofficial\b', r'\bconfirm\w+\b',
            r'\bannounce\w+\b', r'\breport\w+\b', r'\bstatement\b',
            r'\bpress\s*release\b', r'\bpress\s*conference\b', r'\binterview\b',
            r'\bquote\w+\b', r'\bsay\w+\b', r'\bcomment\w+\b', r'\baddress\w+\b',
            r'\brespond\w+\b'
        ]

        # Compile all patterns once at initialization
        self.compiled_low_value = [re.compile(p, re.IGNORECASE) for p in low_value_patterns]
        self.compiled_legitimate = [re.compile(p, re.IGNORECASE) for p in legitimate_patterns]

        # --- Relevance scoring (ticker-worthiness) -----------------------
        # Unlike the keep/drop lists above, these drive a numeric score so
        # nothing is hard-dropped: low-value items stay in the news widget
        # (ranked to the bottom) but are flagged out of the ticker tape.
        #
        # Positive signal: concrete, actionable news — injuries, roster
        # moves, transactions, availability/lineup.
        hard_news_terms = [
            r'\binjur\w+', r'\bplaced on\b', r'\b(15|10|60)[\- ]day\b',
            r'\b(injured|disabled)\s+list\b', r'\bIL\b', r'\bDL\b', r'\bIR\b',
            r'\bactivat\w+', r'\breinstat\w+', r'\boption(ed|s)?\b',
            r'\brecall\w+', r'\bcall(ed)?\s*up\b', r'\bdesignated\b', r'\bDFA\b',
            r'\bclaim\w+', r'\bsign(s|ed|ing)?\b', r'\bre-?sign\w+',
            r'\btrade[ds]\b', r'\bacquir\w+', r'\breleas\w+', r'\bwaiv\w+',
            r'\bsuspend\w+', r'\bsuspension\b', r'\bsurger\w+', r'\bMRI\b',
            r'\bfractur\w+', r'\bstrain\w+', r'\bsprain\w+', r'\btorn?\b',
            r'\bconcussion\b', r'\bscratch\w+', r'\bbench(ed|es)?\b',
            r'\bruled out\b', r'\bday[\- ]to[\- ]day\b', r'\bout for\b',
            r'\bseason[\- ]ending\b', r'\bexits?\b', r'\bleft (the )?game\b',
            r'\bback in (the )?lineup\b', r'\bstarting\s+(pitcher|lineup)\b',
            r'\bpromot\w+', r'\bdemot\w+', r'\bfined?\b', r'\bretir(e|es|ed|ing)\b',
            r'\bextension\b', r'\bcontract\b', r'\bcall-?up\b',
        ]
        # Negative signal: fluff — speculation, opinion, listicles, fantasy
        # advice, rankings, "X things to know" filler.
        fluff_terms = [
            r'^\s*why\b', r'^\s*how\b', r'\bcould\b', r'\bshould\b',
            r'\bwould\b', r'\bmight\b',
            r'\bwill\s+\w+\s+(turn|bounce|rebound|figure|fix|save|win|improve)\b',
            r'\bthese\s+\d+\b',
            r'\b\d+\s+(players?|hitters?|pitchers?|guys|names|reasons|takeaways|things|sleepers?|targets?)\b',
            r'\btop\s+\d+\b', r'\brank(ing|ings)?\b', r'\bpower\s+rank',
            r'\btier', r'\bsleeper', r'\bwaiver', r'\bstart\s*/?\s*sit\b',
            r'\badd\s*/?\s*drop\b', r'\bstreamer', r'\bfantasy\b',
            r'\bdown the stretch\b', r'\brest[\- ]of[\- ]season\b', r'\bROS\b',
            r'\bhelp your\b', r'\bbold predict', r'\bpredict\w+',
            r'\bway[\- ]too[\- ]early\b', r'\bwhat if\b', r'\bcase for\b',
            r'\bcase against\b', r'\bthings to know\b', r'\bwhat to watch\b',
            r'\btakeaway', r'\bgrades?\b', r'\bwinners? and losers?\b',
            r'\boverreaction', r'\bbreakout candidate', r'\bbuy or sell\b',
            r'\bbuy[\- ]low\b', r'\bsell[\- ]high\b', r"\bhere'?s why\b",
            r"\bhere'?s what\b", r'\bwhat we learned\b', r'\byou need to know\b',
            r'\bbest and worst\b', r'\bmock draft\b',
        ]
        self.compiled_hard_news = [re.compile(p, re.IGNORECASE) for p in hard_news_terms]
        self.compiled_fluff = [re.compile(p, re.IGNORECASE) for p in fluff_terms]

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

        # Use pre-compiled patterns (compiled once at init)
        filtered_news = []
        filtered_count = 0

        for item in news_items:
            title = item.get('title', '')
            if not title:
                continue

            # Check if headline has legitimate news indicators
            is_legitimate = any(pattern.search(title) for pattern in self.compiled_legitimate)

            if is_legitimate:
                filtered_news.append(item)
                continue

            # Check if headline matches low-value patterns
            is_low_value = any(pattern.search(title) for pattern in self.compiled_low_value)

            if not is_low_value:
                filtered_news.append(item)
            else:
                filtered_count += 1
                if self.print_fetches:
                    print(f"[NewsWorker] Filtered out low-value headline: '{title[:80]}...'")

        print(f"[NewsWorker] Low-value filtering: {len(news_items)} → {len(filtered_news)} headlines ({filtered_count} filtered)")
        return filtered_news

    def _score_headline(self, title, description=""):
        """Numeric relevance score for one item. Positive = actionable news
        (injuries/roster/transactions); negative = fluff (speculation,
        listicles, fantasy advice). Title hits weigh more than body hits.
        Runs in the worker thread — keep it cheap (precompiled regex)."""
        t = title or ""
        d = description or ""
        score = 0
        for rx in self.compiled_hard_news:
            if rx.search(t):
                score += 3
            elif rx.search(d):
                score += 1
        for rx in self.compiled_fluff:
            if rx.search(t):
                score -= 3
            elif rx.search(d):
                score -= 1
        return score

    def dedupe_news(self, news_items):
        """Collapse the same story repeated across wire-sharing sources
        (ESPN/CBS/Yahoo/Fox), keeping the first occurrence. Normalizes the
        title (lowercase, strip punctuation + common 'Report:/Breaking:'
        prefixes) and dedupes on the first 60 chars. Worker-thread only."""
        if not news_items:
            return news_items
        seen = set()
        out = []
        for item in news_items:
            norm = re.sub(r'[^a-z0-9 ]', '', (item.get('title', '') or '').lower())
            norm = re.sub(r'^(report|breaking|update|official|sources?)\s+', '', norm)
            norm = re.sub(r'\s+', ' ', norm).strip()[:60]
            if not norm:
                out.append(item)
                continue
            if norm in seen:
                continue
            seen.add(norm)
            out.append(item)
        if self.print_fetches:
            print(f"[NewsWorker] Dedupe: {len(news_items)} → {len(out)} items")
        return out

    def score_and_rank(self, news_items):
        """Score every item, flag ticker-worthiness, and sort so the most
        actionable items lead and fluff sinks to the bottom. Nothing is
        dropped — the news widget still shows everything; only the ticker
        consults `ticker_worthy`. Worker-thread only (off the UI)."""
        if not news_items:
            return news_items
        for item in news_items:
            s = self._score_headline(item.get('title', ''),
                                     item.get('description', ''))
            item['relevance_score'] = s
            # Exclude net-negative (fluff) from the ticker, but always let
            # injury items ride even if phrased softly ("Why is X hurt?").
            item['ticker_worthy'] = (s >= 0) or item.get('is_injury_news', False)
        news_items.sort(key=lambda x: (x.get('relevance_score', 0), x['date']),
                        reverse=True)
        ticker_n = sum(1 for i in news_items if i.get('ticker_worthy'))
        print(f"[NewsWorker] Scored {len(news_items)} items; "
              f"{ticker_n} ticker-worthy, {len(news_items) - ticker_n} fluff")
        return news_items

    async def fetch_rss_feed_with_session(self, session, url):
        """Fetch and parse an RSS feed using provided session"""
        if self.print_fetches: print(f"fetching rss feed: {url}");
        try:
            # Fetch RSS feed with timeout and status checking
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as response:
                # Check response status
                if response.status != 200:
                    if self.print_fetches:
                        print(f"RSS feed {url} returned status {response.status}")
                    return []

                content = await response.text()

                # Parse feedparser in thread pool to avoid blocking the event loop
                # feedparser.parse() is synchronous and CPU-bound, so we run it in executor
                loop = asyncio.get_event_loop()
                feed = await loop.run_in_executor(None, feedparser.parse, content)

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
            # Set comprehensive timeouts to prevent any blocking
            timeout = aiohttp.ClientTimeout(
                total=5,      # Total timeout for entire request
                connect=2,    # Timeout for connection establishment
                sock_read=3   # Timeout for reading from socket
            )

            # Try to use async DNS resolver if available, otherwise fall back to threaded resolver
            # This prevents DNS lookups from blocking the event loop
            try:
                from aiohttp.resolver import AsyncResolver
                resolver = AsyncResolver()
            except (ImportError, RuntimeError):
                # aiodns not installed or not available, use default threaded resolver
                # ThreadedResolver runs DNS lookups in a thread pool (non-blocking)
                resolver = None  # Will use default ThreadedResolver

            # Use TCPConnector with async DNS resolver to prevent DNS blocking
            # This is critical - without async DNS, DNS lookups block the entire event loop!
            connector = aiohttp.TCPConnector(
                resolver=resolver,
                limit=50,
                limit_per_host=10,
                ttl_dns_cache=300,  # Cache DNS for 5 minutes
                force_close=False,   # Reuse connections
                enable_cleanup_closed=True
            )

            # Browser User-Agent: some feeds (e.g. CloserMonkey) sit behind
            # Cloudflare, which can challenge/403 aiohttp's default
            # "Python/aiohttp" UA depending on the client IP's reputation.
            # CloserMonkey currently serves 200 to the default UA from here,
            # but a browser UA is cheap insurance against Cloudflare flipping
            # to challenge mode. Harmless for the existing major-site feeds,
            # which already serve fine to browsers.
            headers = {
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                               "Gecko/20100101 Firefox/128.0"),
                "Accept": ("application/rss+xml, application/xml, text/xml, "
                           "application/atom+xml, */*"),
            }
            async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                             headers=headers) as session:
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
            
            # De-duplicate wire copy across sources, then score every item
            # for ticker-worthiness. Nothing is dropped: low-value/fluff
            # headlines stay in the list (shown at the bottom of the news
            # widget) but are flagged ticker_worthy=False so the ticker tape
            # can exclude them. All of this runs here in the worker thread,
            # off the UI — the consumers only read the flags.
            all_news = self.dedupe_news(all_news)
            all_news = self.score_and_rank(all_news)
            
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
        self.filtered_news_items = []  # Store filtered items for batched loading
        self.current_widget_index = 0  # Track current position in batch loading
        self.is_batch_loading = False  # Flag to track if batch loading is in progress
        self.setup_ui()
        self.setup_worker()

        # Auto-refresh timer (10 minutes). Start is offset by 60s in
        # refresh_news_offset() below so we don't tick at the exact same
        # second as ModernOddsWindow.status_timer (also 10min).
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_news)
        self.current_refresh_interval = 10  # minutes
        QTimer.singleShot(60 * 1000,
                          lambda: self.refresh_timer.start(10 * 60 * 1000))

        # Delay initial news fetch to avoid blocking during UI initialization
        # This prevents DNS/network blocking from affecting app startup
        QTimer.singleShot(3000, self.refresh_news)  # 5 second delay after UI is fully loaded

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
        self.refresh_interval.setValue(10)
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
        # Cancel any pending batch loading by setting index to end
        self.current_widget_index = len(self.filtered_news_items) if self.filtered_news_items else 0
        self.is_batch_loading = False  # Stop batch loading flag

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

        # Rank by relevance score first (so fluff/speculation sinks to the
        # bottom), then by date. The worker already sorted this way; re-sort
        # here as a safeguard since the injuries-only filter may have run.
        filtered_items.sort(
            key=lambda x: (x.get('relevance_score', 0), x['date']),
            reverse=True)

        if not filtered_items:
            message = "No injury news found" if self.show_injuries_only else "No news items found"
            self.status_label.setText(message)
            self.status_label.setVisible(True)
            return

        self.status_label.setVisible(False)

        # Store filtered items for lazy loading
        self.filtered_news_items = filtered_items
        self.current_widget_index = 0
        self.is_batch_loading = True  # Enable batch loading

        # Load widgets in batches to prevent UI blocking
        # Delay even the first batch to prevent any blocking during initial load
        QTimer.singleShot(10, self.load_news_batch)  # 10ms delay before first batch

    def load_news_batch(self):
        """Load a batch of news widgets to prevent UI blocking"""
        # Check if batch loading was cancelled
        if not self.is_batch_loading:
            return

        batch_size = 15  # Load 15 widgets at a time (reduced for smoother loading)
        end_index = min(self.current_widget_index + batch_size, len(self.filtered_news_items))

        # Add widgets for this batch
        for i in range(self.current_widget_index, end_index):
            # Double-check flag in case it changed during loop
            if not self.is_batch_loading:
                break

            item = self.filtered_news_items[i]
            news_widget = NewsArticleWidget(item)
            # Insert before the stretch (if it exists)
            if self.scroll_layout.count() > 0 and self.scroll_layout.itemAt(self.scroll_layout.count() - 1).spacerItem():
                self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, news_widget)
            else:
                self.scroll_layout.addWidget(news_widget)

        self.current_widget_index = end_index

        # If there are more items to load, schedule next batch
        if self.current_widget_index < len(self.filtered_news_items) and self.is_batch_loading:
            # Use QTimer to load next batch without blocking
            QTimer.singleShot(30, self.load_news_batch)  # 30ms delay between batches
        else:
            # All widgets loaded, add stretch at the end
            self.scroll_layout.addStretch()
            self.is_batch_loading = False  # Mark batch loading as complete

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
