import asyncio
import aiohttp
import feedparser
from datetime import datetime, timedelta
import re
from bs4 import BeautifulSoup
import time
import sys
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QColor, QIcon, QFont, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QScrollArea, QFrame, QSizePolicy, QToolButton
)

#TODO: Add in a news refresh timer...


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
        self.rss_urls = {
            # NBA
            "basketball_nba": {
                "general": [
                    "https://www.nba.com/rss/nba",
                    "https://www.rotowire.com/rss/news.php?sport=nba",
                    "https://www.espn.com/espn/rss/nba/news",
                    "https://www.fantasypros.com/nba/player-news.php",
                    "https://www.insidehoops.com/blog/?feed=rss2",
                    "https://sports.yahoo.com/nba/rss.xml",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nba"
                ],
        
            },
            # NFL
            "football_nfl": {
                "general": [
                    "https://www.nfl.com/rss/rsslanding?searchString=home",
                    "https://www.rotowire.com/rss/news.php?sport=nfl,",
                    "https://www.espn.com/espn/rss/nfl/news",
                    "https://www.fantasypros.com/nfl/news/injury.php?format=rss",                    
                    "https://www.cbssports.com/rss/headlines/nfl/injuries",
                    "https://sports.yahoo.com/nfl/rss.xml",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nfl"
                ]
            },
            # MLB
            "baseball_mlb": {
                "general": [
                    "https://www.mlb.com/feeds/news/rss.xml",
                    "https://www.rotowire.com/rss/news.php?sport=mlb",
                    "https://www.espn.com/espn/rss/mlb/news",
                    "https://www.fantasypros.com/mlb/news/injury.php?format=rss",
                    "https://www.cbssports.com/rss/headlines/mlb/injuries",
                    "https://sports.yahoo.com/mlb/rss.xml",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/mlb"
                ]
            },
            # NHL
            "icehockey_nhl": {
                "general": [
                    "http://www.nhl.com/rss/news.xml",
                    "https://www.rotowire.com/rss/news.php?sport=nhl",
                    "https://www.espn.com/espn/rss/nhl/news",
                    "https://www.fantasypros.com/nhl/news/injury.php?format=rss",
                    "https://www.cbssports.com/rss/headlines/nhl/injuries",
                    "https://sports.yahoo.com/nhl/rss.xml",
                    "https://api.foxsports.com/v2/content/optimized-rss?partnerKey=MB0Wehpmuj2lUhuRhQaafhBjAJqaPU244mlTDK1i&size=30&tags=fs/nba"
                ]
            }
        }


    def set_league(self, league_key):
        """Set the current league to fetch news for"""
        self.league_key = league_key

    def set_team(self, team_name):
        """Set a specific team to filter news for"""
        self.team_name = team_name

    def prioritize_injury_news(self, news_items):
        """Prioritize news items about injuries"""
        injury_keywords = [
            'injury', 'injured', 'injuries', 'hurt', 'questionable', 'doubtful',
            'out', 'expected to miss', 'ruled out', 'status', 'return', 'recovering',
            'rehabilitation', 'surgery', 'health', 'hamstring', 'ankle', 'knee',
            'IL', 'injured list', 'disabled list', 'DNP', 'game-time decision'
        ]

        # Score each item based on injury relevance
        for item in news_items:
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

        # Sort by injury score first, then by date
        news_items.sort(key=lambda x: (-x['injury_score'], x['date']), reverse=True)

        # Tag items with high injury relevance
        for item in news_items:
            if item['injury_score'] > 2:
                item['is_injury_news'] = True
            else:
                item['is_injury_news'] = False

        return news_items

    async def fetch_rss_feed(self, url):
        """Fetch and parse an RSS feed"""
        try:
            feed = feedparser.parse(url)
            news_items = []

            for entry in feed.entries[:10]:  # Limit to 10 items per feed
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

                news_items.append({
                    'title': title,
                    'description': description,
                    'link': entry.link,
                    'date': date,
                    'source': feed.feed.title if hasattr(feed, 'feed') and hasattr(feed.feed, 'title') else url.split('/')[2],
                    'image_url': image_url
                })

            return news_items
        except Exception as e:
            print(f"Error fetching RSS feed {url}: {str(e)}")
            return []


    async def fetch_news(self):
        """Fetch news from all configured sources"""
        if not self.league_key:
            self.error_occurred.emit("No league selected")
            return

        sources = []

        # Add RSS feeds for the current league
        league_feeds = self.rss_urls.get(self.league_key, {})
        if league_feeds:
            # Add general feeds for the league
            sources.extend(league_feeds.get('general', []))

            # Add team-specific feeds if a team is selected
            if self.team_name and self.team_name in league_feeds.get('teams', {}):
                sources.extend(league_feeds['teams'][self.team_name])

        # Fetch from all RSS sources in parallel
        tasks = [self.fetch_rss_feed(url) for url in sources]


        # Execute all tasks
        results = await asyncio.gather(*tasks)

        # Combine and sort all news items
        all_news = []
        for result in results:
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

        # Apply injury news prioritization
        all_news = self.prioritize_injury_news(all_news)

        # Limit to most recent 20 items
        all_news = all_news[:20]

        # Emit the signal with results
        self.news_fetched.emit(all_news)

    def run_fetch(self):
        """Start the fetch operation in a separate thread"""
        self.running = True
        asyncio.run(self.fetch_news())


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
        self.current_league = None
        self.current_team = None
        self.setup_ui()
        self.setup_worker()

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
        self.worker.news_fetched.connect(self.update_news_items)
        self.worker.error_occurred.connect(self.show_error)

        # Start the thread
        self.worker_thread.start()

    def set_league(self, league_key):
        """Set the current league and update the team dropdown"""
        self.current_league = league_key
        self.worker.set_league(league_key)

        # Update team dropdown with teams for this league
        self.update_team_dropdown(league_key)

        # Refresh news with the new league
        self.refresh_news()

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

        # Refresh news with the new team filter
        self.refresh_news()

    def refresh_news(self):
        """Refresh news data"""
        self.status_label.setText("Loading news...")
        self.status_label.setVisible(True)

        # Clear current news items
        self.clear_news_items()

        # Start the worker in its thread
        QTimer.singleShot(0, self.worker.run_fetch)

    def clear_news_items(self):
        """Clear all news items from the display"""
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def update_news_items(self, news_items):
        """Update the display with new news items"""
        self.clear_news_items()

        if not news_items:
            self.status_label.setText("No news items found")
            self.status_label.setVisible(True)
            return

        self.status_label.setVisible(False)

        # Add news items to the scroll area
        for item in news_items:
            news_widget = NewsArticleWidget(item)
            self.scroll_layout.addWidget(news_widget)

        # Add a stretch at the end to push items to the top
        self.scroll_layout.addStretch()

    def show_error(self, error_message):
        """Display an error message"""
        self.status_label.setText(f"Error: {error_message}")
        self.status_label.setVisible(True)

    def handle_league_change(self, league_key):
        """Public method to update when the main app changes leagues"""
        if league_key in ["basketball_nba", "football_nfl", "baseball_mlb",
                         "icehockey_nhl", "soccer_usa_mls"]:
            self.set_league(league_key)


# For testing the widget standalone
if __name__ == "__main__":
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    widget = TeamNewsWidget()
    widget.set_league("basketball_nba")  # Set initial league
    widget.show()

    sys.exit(app.exec())
