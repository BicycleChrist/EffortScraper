import asyncio
import aiohttp
import feedparser
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import feedparse_worker
from datetime import datetime
import calendar
import re
import sys
import webbrowser
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QThread,
    QAbstractListModel, QModelIndex, QSize, QRect, QEvent
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFrame, QSizePolicy, QToolButton, QSpinBox, QCheckBox,
    QLineEdit, QListView, QStyledItemDelegate, QStyle
)
from fpscraper import FantasyProsScraper

# Shared single-child process pool for feedparser parses. Parsing in a
# thread (the old run_in_executor(None, ...) path) held the GIL for the
# whole pure-Python parse and starved the qasync main loop — the stall
# watchdog kept catching feedparser/sgmllib as the lone busy thread while
# the main thread sat idle. A child process has its own GIL, so the parse
# costs the UI nothing; one worker is plenty for a handful of feeds per
# refresh. Created lazily so importers that never fetch news don't spawn
# a process. (see PERF_DIAGNOSTICS.md "ROUND 5")
_FEED_PARSE_POOL = None


def _feed_parse_pool():
    global _FEED_PARSE_POOL
    if _FEED_PARSE_POOL is None:
        _FEED_PARSE_POOL = ProcessPoolExecutor(max_workers=1)
    return _FEED_PARSE_POOL

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

        # --- Category classification (terminal-feed chips) ---------------
        # Each item gets a single category tag for the dense feed UI.
        # Precedence matters: an IL-move headline mentions both the injury
        # and the roster move — INJ wins.
        category_terms = [
            ("INJ", [
                r'\binjur\w+', r'\b(injured|disabled)\s+list\b',
                r'\b(7|10|15|60)[\- ]day\b', r'\bIL\b', r'\bIR\b', r'\bDL\b',
                r'\bsurger\w+', r'\bMRI\b', r'\bfractur\w+', r'\bstrain\w*\b',
                r'\bsprain\w*\b', r'\btorn\b', r'\bconcussion\b',
                r'\bruled out\b', r'\bday[\- ]to[\- ]day\b', r'\bout for\b',
                r'\bseason[\- ]ending\b', r'\bexits?\b', r'\bleft (the )?game\b',
                r'\bquestionable\b', r'\bdoubtful\b', r'\bgame[\- ]time decision\b',
                r'\brehab\b', r'\bhamstring\b', r'\bankle\b', r'\bknee\b',
                r'\boblique\b', r'\bshoulder\b', r'\belbow\b', r'\bwrist\b',
            ]),
            ("SUSP", [
                r'\bsuspend\w+', r'\bsuspension\b', r'\bfined?\b',
                r'\bbanned?\b', r'\bappeal\w*\b',
            ]),
            ("SIGN", [
                r'\bsign(s|ed|ing)?\b', r'\bre-?sign\w+', r'\bextension\b',
                r'\bcontract\b', r'\bdeal\b', r'\bagreement\b',
            ]),
            ("TXN", [
                r'\btrade[ds]?\b', r'\bacquir\w+', r'\breleas\w+', r'\bwaiv\w+',
                r'\bclaim\w+', r'\bDFA\b', r'\bdesignated\b', r'\boption(ed|s)?\b',
                r'\brecall\w+', r'\bcall(ed)?[\- ]?up\b', r'\bpromot\w+',
                r'\bdemot\w+', r'\bactivat\w+', r'\breinstat\w+', r'\bretir\w+',
            ]),
            ("LINEUP", [
                r'\bstarting\b', r'\blineup\b', r'\bbench(ed)?\b',
                r'\bscratch\w*\b', r'\bstarter\b', r'\bprobable\b',
            ]),
        ]
        self.compiled_categories = [
            (name, [re.compile(p, re.IGNORECASE) for p in pats])
            for name, pats in category_terms
        ]

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

    def classify_category(self, title, description=""):
        """Single category tag for the feed UI. Title hits beat body hits;
        within a field, list order (INJ > SUSP > SIGN > TXN > LINEUP) is the
        precedence. Worker-thread only — precompiled regex."""
        for name, patterns in self.compiled_categories:
            if any(rx.search(title) for rx in patterns):
                return name
        if description:
            for name, patterns in self.compiled_categories:
                if any(rx.search(description) for rx in patterns):
                    return name
        return ""

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
            item['category'] = self.classify_category(item.get('title', ''),
                                                      item.get('description', ''))
            # Exclude net-negative (fluff) from the ticker, but always let
            # injury items ride even if phrased softly ("Why is X hurt?").
            item['ticker_worthy'] = (s >= 0) or item.get('is_injury_news', False)
        news_items.sort(key=lambda x: (x.get('relevance_score', 0), x['date']),
                        reverse=True)
        ticker_n = sum(1 for i in news_items if i.get('ticker_worthy'))
        print(f"[NewsWorker] Scored {len(news_items)} items; "
              f"{ticker_n} ticker-worthy, {len(news_items) - ticker_n} fluff")
        return news_items

    async def fetch_rss_feed_with_session(self, session, url, league=""):
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

                # Parse out-of-process: feedparser.parse() is synchronous and
                # CPU-bound, and in a thread it competes for THIS process's
                # GIL with the UI loop. Falls back to the old in-thread parse
                # if the child process died (e.g. OOM-killed).
                loop = asyncio.get_event_loop()
                try:
                    feed = await loop.run_in_executor(
                        _feed_parse_pool(), feedparse_worker.parse_feed, content)
                except BrokenProcessPool:
                    global _FEED_PARSE_POOL
                    _FEED_PARSE_POOL = None
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
                    # published_parsed is a UTC struct_time — convert to
                    # local time. The old datetime(*pub_date[:6]) kept it as
                    # naive UTC, so articles displayed hours in the future.
                    date = datetime.fromtimestamp(calendar.timegm(pub_date))
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
                    'image_url': image_url,
                    'league': league
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

            # Add RSS feeds for all leagues, tagged with their league so each
            # item carries a 'league' key (drives the feed's league chips)
            for league in all_leagues:
                league_feeds = self.rss_urls.get(league, {})
                for url in league_feeds.get('general', []):
                    sources.append((league, url))

            # If we have a specific league_key set, also add team-specific feeds for that league
            if self.league_key:
                league_feeds = self.rss_urls.get(self.league_key, {})
                if league_feeds and self.team_name and self.team_name in league_feeds.get('teams', {}):
                    for url in league_feeds['teams'][self.team_name]:
                        sources.append((self.league_key, url))

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
                tasks = [self.fetch_rss_feed_with_session(session, url, league)
                         for league, url in sources]
                
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


# ---------------------------------------------------------------------------
# Terminal-style news feed UI
#
# Dense delegate-painted rows (QListView + QStyledItemDelegate) instead of
# one QFrame per article. The old card UI needed a 15-per-30ms batch loader
# plus show/hide deferral just to mask widget-construction cost; a delegate
# paints only the visible rows, so thousands of items scroll smoothly and
# all of that machinery is gone.
# ---------------------------------------------------------------------------

ACCENT = "#e55717"          # app accent (matches the old title orange)
ACCENT_BRIGHT = "#ff8a4a"

CATEGORY_COLORS = {
    # tag: (foreground, chip background)
    "INJ":    ("#ff5d5d", "#3a1518"),
    "SUSP":   ("#ff9e3d", "#3a2a12"),
    "SIGN":   ("#3fd68c", "#10301f"),
    "TXN":    ("#58a6ff", "#122a44"),
    "LINEUP": ("#d2a8ff", "#2a1f3d"),
}

LEAGUE_CHIPS = [
    ("all", "ALL"),
    ("baseball_mlb", "MLB"),
    ("basketball_nba", "NBA"),
    ("football_nfl", "NFL"),
    ("icehockey_nhl", "NHL"),
]


def _rel_age(dt):
    """Compact relative age: 'now', '4m', '2h', '3d'."""
    secs = (datetime.now() - dt).total_seconds()
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


NEWS_TERMINAL_QSS = """
QWidget#newsTerminal { background: #0b0f14; }
QWidget#newsHeader, QWidget#newsToolbar {
    background: #0e141c;
    border-bottom: 1px solid #1c2430;
}
QLabel#newsTitle {
    color: """ + ACCENT + """;
    font-family: monospace;
    font-size: 12px;
    font-weight: bold;
    letter-spacing: 2px;
}
QLabel#newsStatus, QLabel#newsToolbarLabel {
    color: #4d5866;
    font-family: monospace;
    font-size: 10px;
}
QPushButton#leagueChip, QPushButton#catChip, QPushButton#sortChip {
    background: #131a24;
    color: #7d8590;
    border: 1px solid #232c3a;
    border-radius: 3px;
    padding: 2px 8px;
    font-family: monospace;
    font-size: 10px;
    font-weight: bold;
}
QPushButton#leagueChip:hover, QPushButton#catChip:hover, QPushButton#sortChip:hover {
    border-color: #3a4656;
}
QPushButton#leagueChip:checked {
    background: #2a1812;
    color: """ + ACCENT_BRIGHT + """;
    border-color: """ + ACCENT + """;
}
QPushButton#sortChip:checked {
    background: #16202e;
    color: #58a6ff;
    border-color: #58a6ff;
}
QPushButton#catChip[cat="INJ"]:checked    { color: #ff5d5d; border-color: #ff5d5d; background: #1f1012; }
QPushButton#catChip[cat="SUSP"]:checked   { color: #ff9e3d; border-color: #ff9e3d; background: #201708; }
QPushButton#catChip[cat="SIGN"]:checked   { color: #3fd68c; border-color: #3fd68c; background: #0c2016; }
QPushButton#catChip[cat="TXN"]:checked    { color: #58a6ff; border-color: #58a6ff; background: #0d1c30; }
QPushButton#catChip[cat="LINEUP"]:checked { color: #d2a8ff; border-color: #d2a8ff; background: #1c1429; }
QLineEdit#newsSearch {
    background: #0b1018;
    color: #dbe4ee;
    border: 1px solid #232c3a;
    border-radius: 3px;
    padding: 2px 6px;
    font-family: monospace;
    font-size: 11px;
    selection-background-color: #2a3b55;
}
QLineEdit#newsSearch:focus { border-color: """ + ACCENT + """; }
QWidget#newsToolbar QComboBox, QWidget#newsToolbar QSpinBox {
    background: #131a24;
    color: #aab4c0;
    border: 1px solid #232c3a;
    border-radius: 3px;
    padding: 1px 4px;
    font-family: monospace;
    font-size: 10px;
}
QWidget#newsToolbar QComboBox QAbstractItemView {
    background: #131a24;
    color: #aab4c0;
    selection-background-color: #2a3b55;
}
QWidget#newsToolbar QCheckBox {
    color: #ff5d5d;
    font-family: monospace;
    font-size: 10px;
    font-weight: bold;
}
QToolButton#newsRefresh {
    background: #131a24;
    color: """ + ACCENT_BRIGHT + """;
    border: 1px solid #232c3a;
    border-radius: 3px;
    font-size: 13px;
    padding: 1px 6px;
}
QToolButton#newsRefresh:hover { border-color: """ + ACCENT + """; }
QListView#newsList {
    background: #0b0f14;
    border: none;
    outline: none;
}
QListView#newsList QScrollBar:vertical {
    background: #0b0f14;
    width: 8px;
    margin: 0;
}
QListView#newsList QScrollBar::handle:vertical {
    background: #232c3a;
    border-radius: 4px;
    min-height: 24px;
}
QListView#newsList QScrollBar::add-line:vertical,
QListView#newsList QScrollBar::sub-line:vertical { height: 0; }
"""


class NewsFeedModel(QAbstractListModel):
    """Flat list model over the worker's news-item dicts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.items[index.row()].get('title', '')
        return None

    def set_items(self, items):
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()


class NewsRowDelegate(QStyledItemDelegate):
    """Paints one dense terminal row per item:
        HH:MM │ SOURCE │ [CAT] │ headline …            ●NEW  4m
    The expanded row (owner.expanded_row) additionally paints the wrapped
    description plus an OPEN ARTICLE link. Single click toggles expansion,
    click on the link (or double-click anywhere) opens the article."""

    ROW_H = 24
    PAD = 8
    TIME_W = 46
    SRC_W = 64
    CAT_W = 58
    AGE_W = 44
    NEW_W = 40
    DESC_INDENT = 56
    LINK_H = 18
    LINK_W = 110

    SOURCE_ABBREVS = [
        ("rotowire", "ROTO"), ("espn", "ESPN"), ("cbs", "CBS"),
        ("yahoo", "YAHOO"), ("fox", "FOX"), ("mlb trade", "MLBTR"),
        ("mlbtraderumors", "MLBTR"), ("closer", "CLOSER"), ("nfl", "NFL"),
    ]

    def __init__(self, view, owner):
        super().__init__(view)
        self.view = view
        self.owner = owner
        mono = QFont()
        mono.setFamilies(["JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "Monospace"])
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(12)
        self.mono = mono
        self.mono_bold = QFont(mono)
        self.mono_bold.setBold(True)
        self.mono_small = QFont(mono)
        self.mono_small.setPixelSize(10)
        self.mono_small_bold = QFont(self.mono_small)
        self.mono_small_bold.setBold(True)
        self._src_cache = {}

    def _abbrev(self, source):
        cached = self._src_cache.get(source)
        if cached:
            return cached
        s = (source or "").lower()
        out = next((abbr for key, abbr in self.SOURCE_ABBREVS if key in s), None)
        if out is None:
            out = (source or "WIRE").split()[0][:6].upper()
        self._src_cache[source] = out
        return out

    def _link_rect(self, rect):
        return QRect(rect.left() + self.DESC_INDENT,
                     rect.bottom() - self.LINK_H - 4,
                     self.LINK_W, self.LINK_H)

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        if not item:
            return
        painter.save()
        r = option.rect
        row = index.row()
        expanded = (row == self.owner.expanded_row)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if expanded:
            painter.fillRect(r, QColor("#121a26"))
        elif hovered:
            painter.fillRect(r, QColor("#101722"))
        else:
            painter.fillRect(r, QColor("#0b0f14") if row % 2 == 0 else QColor("#0d1219"))

        if item.get('_is_new'):
            painter.fillRect(r.left(), r.top(), 2, self.ROW_H, QColor(ACCENT))

        y = r.top()
        x = r.left() + self.PAD

        painter.setFont(self.mono)
        painter.setPen(QColor("#6e7a8a"))
        painter.drawText(QRect(x, y, self.TIME_W, self.ROW_H),
                         int(Qt.AlignmentFlag.AlignVCenter),
                         item['date'].strftime('%H:%M'))
        x += self.TIME_W

        painter.setPen(QColor("#b8862d"))
        painter.drawText(QRect(x, y, self.SRC_W, self.ROW_H),
                         int(Qt.AlignmentFlag.AlignVCenter),
                         self._abbrev(item.get('source', '')))
        x += self.SRC_W

        cat = item.get('category', '')
        if cat:
            fg, bg = CATEGORY_COLORS.get(cat, ("#7d8590", "#161d28"))
            chip = QRect(x, y + (self.ROW_H - 16) // 2, self.CAT_W - 8, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(chip, 3, 3)
            painter.setPen(QColor(fg))
            painter.setFont(self.mono_small_bold)
            painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), cat)
        x += self.CAT_W

        right_w = self.AGE_W + (self.NEW_W if item.get('_is_new') else 0)
        head_rect = QRect(x, y, r.right() - right_w - x - 4, self.ROW_H)
        if item.get('relevance_score', 0) < 0:
            head_color = "#566070"   # fluff: dimmed but present
        elif cat == "INJ":
            head_color = "#ff8484"
        else:
            head_color = "#dbe4ee"
        head_font = self.mono_bold if cat == "INJ" else self.mono
        painter.setFont(head_font)
        painter.setPen(QColor(head_color))
        title = QFontMetrics(head_font).elidedText(
            item.get('title', ''), Qt.TextElideMode.ElideRight, head_rect.width())
        painter.drawText(head_rect, int(Qt.AlignmentFlag.AlignVCenter), title)

        age_rect = QRect(r.right() - self.AGE_W, y, self.AGE_W - self.PAD, self.ROW_H)
        painter.setFont(self.mono_small)
        painter.setPen(QColor("#4d5866"))
        painter.drawText(age_rect,
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                         _rel_age(item['date']))
        if item.get('_is_new'):
            painter.setFont(self.mono_small_bold)
            painter.setPen(QColor(ACCENT_BRIGHT))
            painter.drawText(QRect(age_rect.left() - self.NEW_W, y, self.NEW_W - 4, self.ROW_H),
                             int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                             "●NEW")

        if expanded:
            desc = (item.get('description') or '').strip() or "(no summary available)"
            painter.setFont(self.mono_small)
            painter.setPen(QColor("#9aa7b8"))
            desc_rect = QRect(r.left() + self.DESC_INDENT, y + self.ROW_H + 2,
                              r.width() - self.DESC_INDENT - 16,
                              r.height() - self.ROW_H - self.LINK_H - 10)
            painter.drawText(desc_rect, int(Qt.TextFlag.TextWordWrap), desc)

            link_rect = self._link_rect(r)
            painter.setFont(self.mono_small_bold)
            painter.setPen(QColor(ACCENT_BRIGHT))
            painter.drawText(link_rect, int(Qt.AlignmentFlag.AlignVCenter), "OPEN ARTICLE ↗")

            painter.setFont(self.mono_small)
            painter.setPen(QColor("#4d5866"))
            meta = f"{item['date'].strftime('%m/%d %H:%M')} · {item.get('source', '')}"
            painter.drawText(QRect(link_rect.right() + 12, link_rect.top(),
                                   r.right() - link_rect.right() - 24, self.LINK_H),
                             int(Qt.AlignmentFlag.AlignVCenter), meta)
        painter.restore()

    def sizeHint(self, option, index):
        if index.row() != self.owner.expanded_row:
            return QSize(0, self.ROW_H)
        item = index.data(Qt.ItemDataRole.UserRole) or {}
        desc = (item.get('description') or '').strip() or "(no summary available)"
        width = max(200, self.view.viewport().width() - self.DESC_INDENT - 16)
        br = QFontMetrics(self.mono_small).boundingRect(
            0, 0, width, 2000, int(Qt.TextFlag.TextWordWrap), desc)
        return QSize(0, self.ROW_H + br.height() + self.LINK_H + 12)

    def editorEvent(self, event, model, option, index):
        if (event.type() == QEvent.Type.MouseButtonDblClick
                and event.button() == Qt.MouseButton.LeftButton):
            self._open(index)
            return True
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            row = index.row()
            if row == self.owner.expanded_row:
                if self._link_rect(option.rect).contains(event.position().toPoint()):
                    self._open(index)
                    return True
                self.owner.set_expanded(-1)
            else:
                self.owner.set_expanded(row)
            return True
        return super().editorEvent(event, model, option, index)

    def _open(self, index):
        item = index.data(Qt.ItemDataRole.UserRole) or {}
        url = item.get('link')
        if url:
            webbrowser.open(url)


class TeamNewsWidget(QWidget):
    """Terminal-style news feed over the NewsWorker pipeline.

    Public surface consumed elsewhere (EffortOdds / tickertape) and kept
    stable: .worker (+ news_fetched signal, .news_items, .running),
    .worker_thread, .handle_league_change(), .get_teams_for_league(),
    .all_news_items."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.refresh_disabled = False
        self.current_league = None
        self.current_team = None
        self.show_injuries_only = False
        self.all_news_items = []
        self.expanded_row = -1
        self.active_league_chip = "all"
        self._seen_keys = set()       # first-seen tracking for ●NEW markers
        self._last_update = None
        self.setup_ui()
        self.setup_worker()

        # Auto-refresh timer (10 minutes). Start is offset by 60s so we
        # don't tick at the exact same second as ModernOddsWindow.status_timer
        # (also 10min).
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_news)
        self.current_refresh_interval = 10  # minutes
        QTimer.singleShot(60 * 1000,
                          lambda: self.refresh_timer.start(10 * 60 * 1000))

        # Delay initial news fetch to avoid blocking during UI initialization.
        # Set to 4.25s as part of startup staggering so the regex scoring
        # (score_and_rank) doesn't collide with the Novig dump's match-map
        # rebuild. Also prevents DNS/network blocking at startup.
        QTimer.singleShot(4250, self.refresh_news)

        # Keep relative ages ("4m") current; repaint is cheap and skipped
        # entirely while the panel is hidden.
        self.age_timer = QTimer(self)
        self.age_timer.timeout.connect(self._tick_ages)
        self.age_timer.start(30 * 1000)

    # ------------------------------------------------------------------ UI

    def setup_ui(self):
        self.setObjectName("newsTerminal")
        self.setStyleSheet(NEWS_TERMINAL_QSS)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        # -- Row 1: title, league chips, category chips, sort, status
        header = QWidget()
        header.setObjectName("newsHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(4)

        title_label = QLabel("NEWSWIRE")
        title_label.setObjectName("newsTitle")
        h.addWidget(title_label)
        h.addSpacing(10)

        self.league_chips = {}
        for key, label in LEAGUE_CHIPS:
            chip = QPushButton(label)
            chip.setObjectName("leagueChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _, k=key: self.on_league_chip(k))
            self.league_chips[key] = chip
            h.addWidget(chip)
        self.league_chips["all"].setChecked(True)
        h.addSpacing(12)

        self.category_chips = {}
        for cat in CATEGORY_COLORS:
            chip = QPushButton(cat)
            chip.setObjectName("catChip")
            chip.setProperty("cat", cat)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(f"Show only {cat} items (none checked = all)")
            chip.toggled.connect(self.apply_filters)
            self.category_chips[cat] = chip
            h.addWidget(chip)
        h.addSpacing(12)

        # Default order is relevance-ranked (fluff sinks); CHRONO flips to
        # strict newest-first like a wire feed.
        self.chrono_sort = QPushButton("CHRONO")
        self.chrono_sort.setObjectName("sortChip")
        self.chrono_sort.setCheckable(True)
        self.chrono_sort.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chrono_sort.setToolTip("Checked: strict newest-first. Unchecked: relevance-ranked, fluff last.")
        self.chrono_sort.toggled.connect(self.apply_filters)
        h.addWidget(self.chrono_sort)

        h.addStretch()
        self.status_label = QLabel("")
        self.status_label.setObjectName("newsStatus")
        h.addWidget(self.status_label)
        self.layout.addWidget(header)

        # -- Row 2: search, team, injuries-only, refresh controls
        toolbar = QWidget()
        toolbar.setObjectName("newsToolbar")
        t = QHBoxLayout(toolbar)
        t.setContentsMargins(8, 3, 8, 3)
        t.setSpacing(6)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("newsSearch")
        self.search_box.setPlaceholderText("⌕ filter headlines — player, team, keyword…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.apply_filters)
        t.addWidget(self.search_box, 1)

        self.injury_filter = QCheckBox("INJ ONLY")
        self.injury_filter.toggled.connect(self.toggle_injury_filter)
        t.addWidget(self.injury_filter)

        team_label = QLabel("TEAM")
        team_label.setObjectName("newsToolbarLabel")
        t.addWidget(team_label)
        self.team_filter = QComboBox()
        self.team_filter.addItem("All Teams")
        self.team_filter.currentTextChanged.connect(self.on_team_changed)
        t.addWidget(self.team_filter)

        refresh_label = QLabel("EVERY")
        refresh_label.setObjectName("newsToolbarLabel")
        t.addWidget(refresh_label)
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(0, 30)
        self.refresh_interval.setValue(10)
        self.refresh_interval.setSuffix(" min")
        self.refresh_interval.valueChanged.connect(self.update_refresh_interval)
        self.refresh_interval.setFixedWidth(70)
        t.addWidget(self.refresh_interval)

        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("newsRefresh")
        self.refresh_button.setText("⟳")
        self.refresh_button.setToolTip("Refresh News")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh_news)
        t.addWidget(self.refresh_button)
        self.layout.addWidget(toolbar)

        # -- Feed
        self.news_model = NewsFeedModel(self)
        self.list_view = QListView()
        self.list_view.setObjectName("newsList")
        self.list_view.setModel(self.news_model)
        self.delegate = NewsRowDelegate(self.list_view, self)
        self.list_view.setItemDelegate(self.delegate)
        self.list_view.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setUniformItemSizes(False)
        self.list_view.setMouseTracking(True)
        self.list_view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.layout.addWidget(self.list_view)

        self.status_label.setText("LOADING…")

    def setup_worker(self):
        """Set up the background worker for fetching news"""
        self.worker = NewsWorker()
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        self.worker.news_fetched.connect(self.on_news_fetched)
        self.worker.error_occurred.connect(self.show_error)
        self.worker_thread.start()

    # ------------------------------------------------------------ filtering

    def on_league_chip(self, key):
        """League chips behave as a radio group. The team dropdown follows
        the chip — not just the app-level league push from EffortOdds — so
        manually switching to NFL offers NFL teams; ALL offers every team."""
        self.active_league_chip = key
        for k, chip in self.league_chips.items():
            chip.setChecked(k == key)
        self.update_team_dropdown(key)
        self.apply_filters()

    def toggle_injury_filter(self, checked):
        self.show_injuries_only = checked
        self.apply_filters()

    def on_team_changed(self, team_name):
        if team_name == "All Teams":
            self.current_team = None
            self.worker.set_team(None)
        else:
            self.current_team = team_name
            self.worker.set_team(team_name)
        self.apply_filters()

    def apply_filters(self, *_):
        """Recompute the visible item list from all active filters and feed
        the model. Pure-Python list filtering over a few hundred dicts —
        cheap enough to run on every keystroke of the search box."""
        items = self.all_news_items or []
        total = len(items)

        if self.active_league_chip != "all":
            items = [i for i in items if i.get('league') == self.active_league_chip]

        active_cats = [c for c, chip in self.category_chips.items() if chip.isChecked()]
        if active_cats:
            items = [i for i in items if i.get('category') in active_cats]

        if self.show_injuries_only:
            items = [i for i in items if i.get('is_injury_news', False)]

        if self.current_team:
            team = self.current_team.lower()
            items = [i for i in items
                     if team in i.get('title', '').lower()
                     or team in i.get('description', '').lower()]

        query = self.search_box.text().strip().lower()
        if query:
            items = [i for i in items
                     if query in i.get('title', '').lower()
                     or query in i.get('description', '').lower()]

        if self.chrono_sort.isChecked():
            items = sorted(items, key=lambda x: x['date'], reverse=True)
        else:
            items = sorted(items,
                           key=lambda x: (x.get('relevance_score', 0), x['date']),
                           reverse=True)

        self.expanded_row = -1
        self.news_model.set_items(items)

        if not total:
            return  # initial state; status says LOADING…/FETCHING…
        upd = f" · UPD {self._last_update}" if self._last_update else ""
        self.status_label.setText(f"{len(items)}/{total}{upd}")

    def set_expanded(self, row):
        """Toggle the expanded (description + link) row in the feed."""
        old = self.expanded_row
        self.expanded_row = row
        for r in (old, row):
            if 0 <= r < self.news_model.rowCount():
                self.delegate.sizeHintChanged.emit(self.news_model.index(r))
        self.list_view.viewport().update()

    def _tick_ages(self):
        if self.isVisible():
            self.list_view.viewport().update()

    # ------------------------------------------------------------ data flow

    def refresh_news(self):
        """Refresh news data"""
        self.status_label.setText("FETCHING…")
        if hasattr(self.worker, 'news_items'):
            self.worker.news_items = None
        # Make sure the running flag is reset (in case it got stuck)
        self.worker.running = False
        QTimer.singleShot(0, self.worker.run_fetch)

    def on_news_fetched(self, news_items):
        """Handle a completed fetch: mark first-seen items for the ●NEW
        flag, store everything, re-apply the active filters."""
        keys = set()
        first_fetch = not self._seen_keys
        for item in news_items:
            key = (item.get('title') or '')[:80]
            keys.add(key)
            item['_is_new'] = (not first_fetch) and key not in self._seen_keys
        self._seen_keys = keys
        self.all_news_items = news_items
        self._last_update = datetime.now().strftime('%H:%M')
        self.apply_filters()

    def update_news_items(self, news_items):
        """Compatibility entry point: replace the item set and re-filter."""
        self.all_news_items = news_items
        self.apply_filters()

    def clear_news_items(self):
        self.expanded_row = -1
        self.news_model.set_items([])

    def show_error(self, error_message):
        self.status_label.setText(f"ERR: {error_message}")

    # -------------------------------------------------------- league / teams

    def set_league(self, league_key):
        """Set the current league (pushed from EffortOdds on app-level sport
        change): updates the worker and selects the matching league chip,
        which repopulates the team dropdown. User can re-select any chip."""
        self.current_league = league_key
        self.worker.set_league(league_key)
        self.on_league_chip(league_key if league_key in self.league_chips else "all")

    def update_team_dropdown(self, league_key):
        """Repopulate the team dropdown for one league, or for 'all' the
        union of every league's teams (sorted, so it stays scannable)."""
        if league_key == "all":
            teams = sorted({t for key, _ in LEAGUE_CHIPS if key != "all"
                            for t in self.get_teams_for_league(key)})
        else:
            teams = self.get_teams_for_league(league_key)
        self.team_filter.blockSignals(True)
        self.team_filter.clear()
        self.team_filter.addItem("All Teams")
        for team in teams:
            self.team_filter.addItem(team)
        self.team_filter.blockSignals(False)
        self.current_team = None
        self.worker.set_team(None)

    def get_teams_for_league(self, league_key):
        """Get list of teams for the specified league"""
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

    def handle_league_change(self, league_key):
        """Public method to update when the main app changes leagues"""
        if league_key in ["basketball_nba", "football_nfl", "baseball_mlb",
                          "icehockey_nhl", "soccer_usa_mls"]:
            self.set_league(league_key)

    # ----------------------------------------------------------------- misc

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
            interval_ms = new_interval * 60 * 1000
            self.refresh_disabled = False
            self.refresh_timer.stop()
            self.refresh_timer.start(interval_ms)
            print(f"News refresh interval updated to {new_interval} minutes")


# For testing the widget standalone
if __name__ == "__main__":
    app = QApplication(sys.argv)

    widget = TeamNewsWidget()
    widget.set_league("baseball_mlb")  # Set initial league
    widget.resize(1100, 700)
    widget.show()

    sys.exit(app.exec())
