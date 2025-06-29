from datetime import datetime
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QLinearGradient, QRadialGradient, QPainterPath
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import QRectF, QPointF
from livetape_scraper import scrape_espn_scores
import threading

#TODO: get live quarter/inning data along side the scores
class TickerTape(QWidget):
    """Advanced ESPN-style ticker with segmented sports and ultra-smooth scrolling"""
    
    def __init__(self, parent=None, transition_style="flip_card", news_widget=None):
        super().__init__(parent)
        self.news_widget = news_widget
        self.loading_attempted = False
        
        # Smooth scrolling properties - keep it simple!
        self._scroll_position = -0.5  
        self.scroll_speed = 2.0
        self.is_paused = False
        
        # Transition style: "flip_card" or "split_reveal"
        self.transition_style = transition_style
        self.is_transitioning = False
        self.transition_progress = 0.0  # 0.0 to 1.0
        
        # Loading animation state
        self.loading_animation_frame = 0
        self.loading_animation_timer = QTimer()
        self.loading_animation_timer.timeout.connect(self.update_loading_animation)
        self.loading_animation_timer.start(200)  # Update every 200ms
        
        # ESPN-style sport segments - start with animated loading display
        self.sports_data = {
            "": {  # Empty key = no text, just spinning globe
                "color": QColor("#2C3E50"),  # Dark blue-gray
                "accent": QColor("#3498DB"),  # Light blue
                "icon": self.get_loading_globe_icon(),
                "games": [self.get_loading_animation_text()]
            }
        }
        
        # Current display state
        self.current_sport_index = 0
        self.current_game_index = 0
        self.current_text = ""
        self.segment_width = 120  # Width of sport segment
        
        # Fonts - Professional broadcast-style fonts, bigger and more imposing
        self.sport_font = QFont("Arial Black", 14, QFont.Weight.ExtraBold)
        self.game_font = QFont("Arial", 13, QFont.Weight.Bold)
        
        # Fallback to Segoe UI if Arial not available
        if not self.sport_font.exactMatch():
            self.sport_font = QFont("Segoe UI", 14, QFont.Weight.ExtraBold)
            self.game_font = QFont("Segoe UI", 13, QFont.Weight.Bold)
        
        # Additional font settings for crisp, clean appearance
        self.sport_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self.game_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self.sport_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.game_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        
        # Animation timer - don't start until data is loaded
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self.advance_animation)
        
        # Transition animation
        self.transition_animation = QPropertyAnimation(self, b"transition_progress")
        self.transition_animation.setDuration(300)  # 300ms transition
        self.transition_animation.finished.connect(self.on_transition_finished)
        
        # Widget properties - increased height for larger fonts
        self.setMinimumHeight(55)
        self.setMaximumHeight(55)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        
        # Initialize content and start position
        self.update_current_text()
        self._scroll_position = self.width() if self.width() > 0 else 800
        
        # Cache text width to avoid recalculating every frame
        self.cached_text_width = 0
        self.update_text_width()
        
        # Store previous sport info for transitions
        self.previous_sport_index = 0
        self._transition_progress = 0.0
        
        # Live data functionality
        self.data_loaded = False
        
        # Load scores immediately, no delays
        QTimer.singleShot(500, self.load_live_scores)  # Load scores very fast
        
    @pyqtProperty(float)
    def scroll_position(self):
        return self._scroll_position
        
    @scroll_position.setter  
    def scroll_position(self, value):
        self._scroll_position = value
        self.update()
        
    @pyqtProperty(float)
    def transition_progress(self):
        return self._transition_progress
        
    @transition_progress.setter
    def transition_progress(self, value):
        self._transition_progress = value
        self.update()
        
    def reset_scroll_position(self):
        """Reset scroll position to start from the right edge"""
        self._scroll_position = self.width()
        
    def update_current_text(self):
        """Update the current text to display"""
        sports = list(self.sports_data.keys())
        sport = sports[self.current_sport_index]
        games = self.sports_data[sport]["games"]
        
        if games:
            game = games[self.current_game_index % len(games)]
            self.current_text = game  # Remove sport prefix since league segment shows it
        else:
            self.current_text = "No games available"
            
        # Update cached width when text changes
        self.update_text_width()
        
    def update_text_width(self):
        """Cache the text width to avoid calculating it every frame"""
        font_metrics = QFontMetrics(self.game_font)
        self.cached_text_width = font_metrics.horizontalAdvance(self.current_text)
            
    def rotate_content(self):
        """Rotate through different sports and games"""
        sports = list(self.sports_data.keys())
        sport = sports[self.current_sport_index]
        games = self.sports_data[sport]["games"]
        
        self.current_game_index += 1
        if self.current_game_index >= len(games):
            self.current_game_index = 0
            # Store previous sport index for transition
            self.previous_sport_index = self.current_sport_index
            self.current_sport_index = (self.current_sport_index + 1) % len(sports)
            
            # Start transition animation if sport changed
            self.start_transition()
        else:
            # Just update text for same sport
            self.update_current_text()
            
    def start_transition(self):
        """Start the sport segment transition animation"""
        if not self.is_transitioning:
            self.is_transitioning = True
            self.transition_animation.setStartValue(0.0)
            self.transition_animation.setEndValue(1.0)
            self.transition_animation.start()
            
    def on_transition_finished(self):
        """Called when transition animation completes"""
        self.is_transitioning = False
        self._transition_progress = 0.0
        self.update_current_text()
            
    def advance_animation(self):
        """Scroll and rotate content when text disappears under sport segment"""
        if not self.is_paused and self.current_text:
            self._scroll_position -= self.scroll_speed
            
            # When text disappears under sport segment, rotate to next content
            if self._scroll_position < -(self.cached_text_width + self.segment_width + 20):
                self.rotate_content()  # Move to next content
                self._scroll_position = self.width()  # Reset position
                
            self.update()
            
    def get_current_sport_info(self):
        """Get current sport color information"""
        sports = list(self.sports_data.keys())
        sport = sports[self.current_sport_index]
        return self.sports_data[sport]
        
    def paintEvent(self, event):
        """Paint the ESPN-style ticker with smooth animations"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        
        rect = self.rect()
        sport_info = self.get_current_sport_info()
        
        # Draw sophisticated background with sport-specific colors
        self.draw_background(painter, rect, sport_info)
        
        # Draw scrolling game content FIRST (underneath everything)
        self.draw_scrolling_content(painter, rect, sport_info)
        
        # Draw sport segment OVER the scrolling text (like an overlay)
        self.draw_sport_segment(painter, rect, sport_info)
        
        # Draw accent elements and effects on top
        self.draw_accent_effects(painter, rect, sport_info)
        
    def draw_background(self, painter, rect, sport_info):
        """Draw gradient background with sport colors"""
        gradient = QLinearGradient(0, 0, rect.width(), 0)
        
        # Sport color on the left fading to dark
        gradient.setColorAt(0, sport_info["color"])
        gradient.setColorAt(0.3, sport_info["color"].darker(150))
        gradient.setColorAt(0.6, QColor("#2C3E50"))
        gradient.setColorAt(1, QColor("#34495E"))
        
        painter.fillRect(rect, gradient)
        
        # Add subtle radial glow effect
        glow_gradient = QRadialGradient(self.segment_width/2, rect.height()/2, self.segment_width)
        glow_gradient.setColorAt(0, QColor(sport_info["accent"].red(), sport_info["accent"].green(), 
                                          sport_info["accent"].blue(), 30))
        glow_gradient.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillRect(0, 0, self.segment_width * 2, rect.height(), glow_gradient)
        
    def draw_sport_segment(self, painter, rect, sport_info):
        """Draw the sport category segment with transition animations"""
        if self.is_transitioning:
            if self.transition_style == "flip_card":
                self.draw_flip_card_transition(painter, rect)
            elif self.transition_style == "split_reveal":
                self.draw_split_reveal_transition(painter, rect)
        else:
            self.draw_normal_segment(painter, rect, sport_info)
            
    def draw_normal_segment(self, painter, rect, sport_info):
        """Draw normal sport segment without transitions"""
        # Create angular shape for sport segment
        sport_path = QPainterPath()
        sport_path.moveTo(0, 0)
        sport_path.lineTo(self.segment_width - 15, 0)
        sport_path.lineTo(self.segment_width, rect.height())
        sport_path.lineTo(0, rect.height())
        sport_path.closeSubpath()
        
        painter.fillPath(sport_path, sport_info["color"])
        
        # Sport segment border
        painter.setPen(QPen(sport_info["accent"], 2))
        painter.drawPath(sport_path)
        
        # Draw sport icon and text
        self.draw_sport_text(painter, rect, sport_info)
        
    def draw_sport_text(self, painter, rect, sport_info):
        """Draw sport icon and text with shadow"""
        painter.setFont(self.sport_font)
        
        sports = list(self.sports_data.keys())
        sport_name = sports[self.current_sport_index]
        icon = sport_info["icon"]
        
        # Center the text in the segment
        text_rect = QRectF(5, 0, self.segment_width - 20, rect.height())
        shadow_rect = QRectF(6, 1, self.segment_width - 20, rect.height())
        
        # Draw text shadow
        painter.setPen(QColor("#000000"))
        painter.drawText(shadow_rect, Qt.AlignmentFlag.AlignCenter, f"{icon} {sport_name}")
        
        # Draw main text
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, f"{icon} {sport_name}")
        
    def draw_flip_card_transition(self, painter, rect):
        """ESPN-style flip card transition"""
        painter.save()
        
        # Calculate flip angle (0 to 180 degrees)
        angle = self._transition_progress * 180
        
        # Get sport info for old and new sports
        sports = list(self.sports_data.keys())
        old_sport_info = self.sports_data[sports[self.previous_sport_index]]
        new_sport_info = self.sports_data[sports[self.current_sport_index]]
        
        # Transform for 3D flip effect
        center_x = self.segment_width / 2
        center_y = rect.height() / 2
        
        painter.translate(center_x, center_y)
        
        if angle <= 90:
            # First half: show old sport with scaling
            scale_x = abs(1.0 - (angle / 90.0))
            painter.scale(scale_x, 1.0)
            painter.translate(-center_x, -center_y)
            self.draw_normal_segment(painter, rect, old_sport_info)
        else:
            # Second half: show new sport with scaling
            scale_x = abs((angle - 90) / 90.0)
            painter.scale(scale_x, 1.0)
            painter.translate(-center_x, -center_y)
            self.draw_normal_segment(painter, rect, new_sport_info)
            
        painter.restore()
        
    def draw_split_reveal_transition(self, painter, rect):
        """Split reveal transition - segment splits and new one grows from center"""
        painter.save()
        
        sports = list(self.sports_data.keys())
        old_sport_info = self.sports_data[sports[self.previous_sport_index]]
        new_sport_info = self.sports_data[sports[self.current_sport_index]]
        
        progress = self._transition_progress
        
        if progress <= 0.5:
            # First half: split the old segment
            split_progress = progress * 2  # 0 to 1
            split_height = rect.height() * split_progress / 2
            
            # Draw top half of old segment
            painter.setClipRect(0, 0, self.segment_width, rect.height()/2 - split_height)
            self.draw_normal_segment(painter, rect, old_sport_info)
            
            # Draw bottom half of old segment
            painter.setClipRect(0, rect.height()/2 + split_height, self.segment_width, rect.height())
            self.draw_normal_segment(painter, rect, old_sport_info)
        else:
            # Second half: grow new segment from center
            grow_progress = (progress - 0.5) * 2  # 0 to 1
            grow_height = rect.height() * grow_progress
            
            center_y = rect.height() / 2
            clip_top = center_y - grow_height / 2
            
            painter.setClipRect(0, clip_top, self.segment_width, grow_height)
            self.draw_normal_segment(painter, rect, new_sport_info)
            
        painter.restore()
        
    def draw_scrolling_content(self, painter, rect, sport_info):
        """Simple scrolling text - no complications"""
        painter.setFont(self.game_font)
        
        # Just draw the text at the current scroll position
        y_baseline = rect.height() / 2 + 4
        
        # Draw text shadow
        painter.setPen(QColor("#000000"))
        painter.drawText(QPointF(self._scroll_position + 1, y_baseline + 1), self.current_text)
        
        # Draw main text
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QPointF(self._scroll_position, y_baseline), self.current_text)
        
    def draw_accent_effects(self, painter, rect, sport_info):
        """Draw accent lines and effects"""
        # Top accent line
        top_gradient = QLinearGradient(0, 0, rect.width(), 0)
        top_gradient.setColorAt(0, sport_info["accent"])
        top_gradient.setColorAt(0.7, sport_info["accent"].darker(200))
        top_gradient.setColorAt(1, QColor(0, 0, 0, 0))
        
        painter.fillRect(0, 0, rect.width(), 3, top_gradient)
        
        # Bottom accent line  
        bottom_gradient = QLinearGradient(0, rect.height()-3, rect.width(), rect.height()-3)
        bottom_gradient.setColorAt(0, sport_info["accent"])
        bottom_gradient.setColorAt(0.7, sport_info["accent"].darker(200))
        bottom_gradient.setColorAt(1, QColor(0, 0, 0, 0))
        
        painter.fillRect(0, rect.height()-3, rect.width(), 3, bottom_gradient)
            
        
    def update_sports_data(self, new_data):
        """Update the sports data with real odds information"""
        if new_data:
            self.sports_data.update(new_data)
            self.update_current_text()
            
    def resizeEvent(self, event):
        """Handle window resize to keep ticker responsive"""
        super().resizeEvent(event)
        # Reset position if we're off-screen after resize
        if self._scroll_position > self.width():
            self._scroll_position = self.width()
    
    
    
    def get_headlines_from_news_widget(self):
        """Get existing headlines from the NewsWidget that's already running"""
        if not self.news_widget or not hasattr(self.news_widget, 'worker'):
            return []
        
        # Access the worker's news_items
        worker = self.news_widget.worker
        if not hasattr(worker, 'news_items') or not worker.news_items:
            return []
        
        headlines = []
        news_items = worker.news_items
        
        # Get more headlines and prioritize injury news
        sorted_news = sorted(news_items, 
                           key=lambda x: (x.get('injury_score', 0), x.get('date', datetime.min)), 
                           reverse=True)
        
        # Take more headlines for cycling through leagues
        for item in sorted_news[:10]:  # Set headline number
            title = item.get('title', '')
            if not title:
                continue
            
            # Clean up "None:" prefix that comes from RSS feeds
            if title.startswith("None: "):
                title = title[6:]  # Remove "None: " (6 characters)
            
            # Add cleaned title
            headlines.append(title)
        
        return headlines
    
    def get_loading_animation_text(self):
        """Generate animated loading text"""
        # Create a sleek progress bar animation
        bar_length = 8
        filled_char = "■"
        empty_char = "□"
        
        # Create moving progress bar
        position = self.loading_animation_frame % (bar_length * 2)
        if position < bar_length:
            # Moving forward
            filled_count = position + 1
        else:
            # Moving backward
            filled_count = bar_length - (position - bar_length) - 1
        
        filled_count = max(0, min(bar_length, filled_count))
        empty_count = bar_length - filled_count
        
        progress_bar = filled_char * filled_count + empty_char * empty_count
        return f"Loading sports news {progress_bar}"
    
    def get_loading_globe_icon(self):
        """Generate spinning globe icon for sport segment"""
        # Spinning globe animation frames
        globe_frames = ["🌍", "🌎", "🌏", "🌎"]  # Earth rotating through different views
        current_frame = self.loading_animation_frame % len(globe_frames)
        return globe_frames[current_frame]
    
    def update_loading_animation(self):
        """Update the loading animation frame"""
        if "" in self.sports_data:  # Empty key for loading state
            self.loading_animation_frame += 1
            self.sports_data[""]["games"] = [self.get_loading_animation_text()]
            self.sports_data[""]["icon"] = self.get_loading_globe_icon()  # Update spinning globe
            self.update_current_text()
            self.update()  # Trigger repaint
    
    def stop_loading_animation(self):
        """Stop the loading animation when real data loads"""
        if hasattr(self, 'loading_animation_timer'):
            self.loading_animation_timer.stop()
    
    def on_news_ready(self, news_items):
        """Called when NewsWorker finishes fetching news - DISABLED"""
        pass  # No longer automatically load headlines
    
    def load_live_scores(self):
        """Load MLB scores in background thread (non-blocking)"""
        print("Loading live MLB scores...")
        
        def fetch_scores_background():
            try:
                scores = scrape_espn_scores()
                if scores:
                    # Convert scores to ticker format
                    score_items = []
                    for game in scores:
                        away_team = game['away_team']
                        home_team = game['home_team']
                        away_runs = game['away_score']['runs']
                        home_runs = game['home_score']['runs']
                        inning = game.get('inning', 'Unknown')
                        
                        # Format score display
                        if inning == 'Final':
                            score_text = f"{away_team} {away_runs}, {home_team} {home_runs} - Final"
                        elif inning == 'Pre-Game':
                            score_text = f"{away_team} vs {home_team} - Starting Soon"
                        else:
                            score_text = f"{away_team} {away_runs}, {home_team} {home_runs} - {inning}"
                        
                        score_items.append(score_text)
                    
                    # Store the data and signal main thread
                    self.pending_scores = score_items
                    # Use metaObject().invokeMethod for thread-safe Qt calls
                    QTimer.singleShot(0, self.process_pending_scores)
                    print(f"Loaded {len(score_items)} MLB games")
                else:
                    print("No MLB games found")
            except Exception as e:
                print(f"Error loading scores: {e}")
        
        # Run in background thread
        threading.Thread(target=fetch_scores_background, daemon=True).start()
    
    def process_pending_scores(self):
        """Process scores on main thread"""
        if hasattr(self, 'pending_scores'):
            self.add_scores_to_ticker(self.pending_scores)
            delattr(self, 'pending_scores')
    
    def add_scores_to_ticker(self, score_items):
        """Add live scores to ticker data (runs on main thread)"""
        if score_items:
            print(f"Adding {len(score_items)} scores to ticker...")
            
            # Remove loading animation data first
            if "" in self.sports_data:
                del self.sports_data[""]
            
            # Add MLB scores at the beginning
            mlb_config = {
                "color": QColor("#132448"),
                "accent": QColor("#BF0D3E"),
                "icon": "⚾",
                "games": score_items
            }
            
            # Always prioritize scores - create new sports data with MLB first
            new_sports_data = {"MLB": mlb_config}
            
            # Add any existing non-MLB sports after
            for sport, data in self.sports_data.items():
                if sport != "MLB" and sport != "":
                    new_sports_data[sport] = data
            
            self.sports_data = new_sports_data
            
            # Reset to show MLB scores first
            self.current_sport_index = 0
            self.current_game_index = 0
            
            # Stop loading animation and start ticker
            self.stop_loading_animation()
            if not self.animation_timer.isActive():
                self.animation_timer.start(16)
            self.data_loaded = True
            
            self.update_current_text()
            print(f"✓ Live scores added - MLB first with {len(score_items)} games")
            print(f"Current sports: {list(self.sports_data.keys())}")
            print(f"Current text: {self.current_text[:50]}...")

    def load_live_data(self):
        """Load headlines from existing NewsWidget - REMOVED, scores only"""
        pass  # No longer needed - just show live scores
    
    def categorize_headline_by_content(self, headline):
        """Advanced headline categorization using multiple signals"""
        headline_lower = headline.lower()
        
        # Advanced filtering patterns with weighted scoring
        league_signals = {
            "NBA": {
                "league_names": ["nba", "basketball"],  # 8 points each
                "terminology": ["three-pointer", "dunk", "playoffs", "draft", "trade deadline", "salary cap", "g league", "all-star"], # 6 points each
                "positions": ["point guard", "center", "forward", "guard"], # 4 points each
                "mega_stars": ["lebron", "curry", "durant", "giannis"], # 4 points each
                "cities": ["los angeles", "boston", "miami", "chicago", "new york"], # 4 points each
                "venues": ["madison square garden", "staples center", "td garden"] # 3 points each
            },
            "NFL": {
                "league_names": ["nfl", "football"],
                "terminology": ["touchdown", "quarterback", "draft pick", "free agency", "super bowl", "combine", "pro bowl"],
                "positions": ["quarterback", "running back", "wide receiver", "linebacker", "defensive back"],
                "mega_stars": ["mahomes", "brady", "rodgers", "josh allen"],
                "cities": ["green bay", "dallas", "kansas city", "buffalo", "tampa bay"],
                "venues": ["lambeau field", "arrowhead stadium", "gillette stadium"]
            },
            "MLB": {
                "league_names": ["mlb", "baseball"],
                "terminology": ["home run", "rbi", "batting average", "era", "world series", "spring training", "all-star game"],
                "positions": ["pitcher", "catcher", "shortstop", "outfielder", "first base"],
                "mega_stars": ["ohtani", "judge", "trout", "soto"],
                "cities": ["new york", "los angeles", "boston", "chicago", "houston"],
                "venues": ["yankee stadium", "fenway park", "dodger stadium"]
            },
            "NHL": {
                "league_names": ["nhl", "hockey"],
                "terminology": ["goal", "assist", "power play", "stanley cup", "playoffs", "trade deadline"],
                "positions": ["goalie", "defenseman", "winger", "center"],
                "mega_stars": ["mcdavid", "ovechkin", "pastrnak"],
                "cities": ["boston", "toronto", "montreal", "chicago", "detroit"],
                "venues": ["madison square garden", "td garden", "united center"]
            }
        }
        
        best_league = None
        best_score = 0
        
        # Score each league using multiple signals
        for league, signals in league_signals.items():
            score = 0
            
            # League names (8 points each)
            for term in signals["league_names"]:
                if term in headline_lower:
                    score += 8
            
            # Sport-specific terminology (6 points each)
            for term in signals["terminology"]:
                if term in headline_lower:
                    score += 6
            
            # Position names (4 points each)
            for term in signals["positions"]:
                if term in headline_lower:
                    score += 4
            
            # Mega stars only (4 points each)
            for star in signals["mega_stars"]:
                if star in headline_lower:
                    score += 4
            
            # City names (4 points each)
            for city in signals["cities"]:
                if city in headline_lower:
                    score += 4
            
            # Venues (3 points each)
            for venue in signals["venues"]:
                if venue in headline_lower:
                    score += 3
            
            # Team names from existing method (10 points each - highest weight)
            if self.news_widget and hasattr(self.news_widget, 'get_teams_for_league'):
                league_key = f"{league.lower().replace('nhl', 'icehockey_nhl').replace('nba', 'basketball_nba').replace('nfl', 'football_nfl').replace('mlb', 'baseball_mlb')}"
                if league == "NBA":
                    league_key = "basketball_nba"
                elif league == "NFL":
                    league_key = "football_nfl"
                elif league == "MLB":
                    league_key = "baseball_mlb"
                elif league == "NHL":
                    league_key = "icehockey_nhl"
                    
                teams = self.news_widget.get_teams_for_league(league_key)
                for team in teams:
                    if team.lower() in headline_lower:
                        score += 10
            
            if score > best_score:
                best_score = score
                best_league = league
        
        return best_league if best_score > 0 else "MLB"  # Default to MLB
    
    def populate_ticker_with_headlines(self, headlines):
        """Populate ticker with headlines properly categorized by content"""
        # League configurations
        league_configs = {
            "MLB": (QColor("#132448"), QColor("#BF0D3E"), "⚾"),
            "NBA": (QColor("#C8102E"), QColor("#1D428A"), "🏀"),
            "NFL": (QColor("#013369"), QColor("#D50A0A"), "🏈"),
            "NHL": (QColor("#000000"), QColor("#F99923"), "🏒")
        }
        
        # Categorize headlines by content
        categorized_headlines = {league: [] for league in league_configs.keys()}
        
        for headline in headlines:
            league = self.categorize_headline_by_content(headline)
            categorized_headlines[league].append(headline)
        
        # Build sports data, preserving existing live scores and maintaining order
        new_sports_data = {}
        
        # First, preserve ALL existing data in order (especially live scores)
        for existing_league, existing_data in self.sports_data.items():
            if existing_league != "":  # Skip loading animation
                new_sports_data[existing_league] = existing_data.copy()
        
        # Then merge in headlines for matching leagues
        for league_name, (color, accent, icon) in league_configs.items():
            league_headlines = categorized_headlines[league_name]
            
            if league_headlines:
                if league_name in new_sports_data:
                    # Add headlines to existing league data (scores already there)
                    existing_games = new_sports_data[league_name]["games"]
                    new_sports_data[league_name]["games"] = existing_games + league_headlines
                    print(f"Merged {len(league_headlines)} headlines with existing {league_name} data")
                else:
                    # Create new league with just headlines
                    new_sports_data[league_name] = {
                        "color": color,
                        "accent": accent,
                        "icon": icon,
                        "games": league_headlines
                    }
        
        if new_sports_data:
            print(f"Before headline merge - current sports: {list(self.sports_data.keys())}")
            print(f"After headline merge - new sports: {list(new_sports_data.keys())}")
            
            # Update with merged data
            self.sports_data = new_sports_data
            self.update_current_text()
            
            # Start animation timer if not already running
            if not self.animation_timer.isActive():
                self.animation_timer.start(16)  # 60fps smooth scrolling
            
            total_headlines = sum(len(headlines) for headlines in categorized_headlines.values())
            print(f"✓ Headlines merged - {total_headlines} headlines across {len(new_sports_data)} leagues")
            print(f"Final sports order: {list(self.sports_data.keys())}")
            
            # Check if MLB still has scores
            if "MLB" in self.sports_data:
                mlb_games = self.sports_data["MLB"]["games"]
                score_count = sum(1 for game in mlb_games if " - " in game and ("Final" in game or "vs" in game or any(inning in game for inning in ["Top", "Bot", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th"])))
                print(f"MLB has {len(mlb_games)} total items, {score_count} appear to be scores")
