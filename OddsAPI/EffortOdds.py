import pathlib
from datetime import datetime, timezone
import aiohttp
import json
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QRect, QRectF, QPointF, pyqtProperty, QThread
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QIcon, QFont, QFontMetrics, QLinearGradient, QRadialGradient, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QHBoxLayout, QFrame, QSizePolicy, QGridLayout, QSplitter
)
from PropQuery import PropClient
from OddsAPIQuery import league_query, odds_query, scores_query, get_game_status
from Creds import SUPER_KEY
from marketKeys import *
from EffortOddsPropsWindow import PropsWindow
import pandas as pd
from GUItuneinwidget import TuneInWidget
from GUIteamnewswidget import TeamNewsWidget
from GUIbestlineswidget import *
from HistoricalOddsClient import *
from TTwindow import TableTennisGUI
from effortcalculator import OddsConverterWidget
import feedparser
import traceback
  # Use SUPER_KEY since ODDS_API_KEY is commented out
#TODO: MMA (Mixed Marital Arts) Markets ouput is nuked, gotta investigate that one
#TODO: Auto update cuts off last line and errors-out due to progress-bar apparently no longer existing.
# League market configurations
#TODO: Toggeling news widget on, off, then on again causes sizing issues
#TODO: Add in accsessibility for calculator 



MAJOR_PROP_LEAGUES = {
    "basketball_nba": NBA_MARKETS,
    "baseball_mlb": MLB_MARKETS,
    "icehockey_nhl": NHL_MARKETS,
    "football_nfl": NFL_MARKETS,
    "aussierules_afl": AFL_MARKETS,
    "soccer_usa_mls": SOCCER_MARKETS
}

REGULAR_MARKETS = {"spreads"} # "h2h", "spreads", "totals"

class ColoredTableItem(QTableWidgetItem):
    """Custom table item that stores game ID for color coordination"""
    def __init__(self, text, game_id):
        super().__init__(text)
        self.game_id = game_id


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
        
        # Schedule live data loading after UI is ready
        QTimer.singleShot(5000, self.load_live_data)
        
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
        
    def enterEvent(self, event):
        """Pause animations on hover"""
        self.is_paused = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
    def leaveEvent(self, event):
        """Resume animations"""
        self.is_paused = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        
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
        for item in sorted_news[:20]:  # Increased from 4 to 20
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
        """Called when NewsWorker finishes fetching news"""
        if self.data_loaded:
            return
            
        print("NewsWorker finished - loading ticker headlines...")
        headlines = self.get_headlines_from_news_widget()
        
        if headlines:
            self.populate_ticker_with_headlines(headlines)
    
    def load_live_data(self):
        """Load headlines from existing NewsWidget"""
        if self.data_loaded or self.loading_attempted:
            return
        
        self.loading_attempted = True
        print("Loading ticker tape news headlines...")
        
        # Try to get headlines immediately
        headlines = self.get_headlines_from_news_widget()
        
        if headlines:
            self.populate_ticker_with_headlines(headlines)
        else:
            # If no headlines yet, wait for NewsWorker signal
            print("Waiting for NewsWorker to finish...")
    
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
        
        # Build sports data only for leagues that have headlines
        new_sports_data = {}
        for league_name, (color, accent, icon) in league_configs.items():
            league_headlines = categorized_headlines[league_name]
            if league_headlines:
                new_sports_data[league_name] = {
                    "color": color,
                    "accent": accent,
                    "icon": icon,
                    "games": league_headlines
                }
        
        if new_sports_data:
            # Stop loading animation
            self.stop_loading_animation()
            
            # Replace loading state with real data
            self.sports_data = new_sports_data
            self.update_current_text()
            
            # Start animation timer now that data is loaded
            if not self.animation_timer.isActive():
                self.animation_timer.start(16)  # 60fps smooth scrolling
            
            total_headlines = sum(len(headlines) for headlines in categorized_headlines.values())
            print(f"Updated ticker with {total_headlines} headlines across {len(new_sports_data)} leagues")
            self.data_loaded = True




class LeagueTabData:
    """Manages data and display state for each league tab"""
    def __init__(self, league_name, sport_key):
        self.league_name = league_name
        self.sport_key = sport_key
        self.num_rows = 0
        self.num_cols = 0
        self.table_rows = []
        self.table_data = {}
        self.game_colors = {}
        self.current_color_index = 0
        self.bookmakers = []
        self.previous_data = {}
        self.game_status = {}
        self.color_palette = [
            QColor(232, 240, 254),  # Sky Blue
            QColor(240, 247, 255),  # Ice Blue
            QColor(230, 255, 230),  # Mint
            QColor(240, 255, 240),  # Honeydew
            QColor(255, 240, 240),  # Misty Rose
            QColor(255, 245, 245),  # Lavender Blush
            QColor(245, 240, 255),  # Lavender
            QColor(240, 230, 255),  # Periwinkle
            QColor(255, 255, 240),  # Ivory
            QColor(255, 250, 240),  # Floral White
            QColor(240, 255, 255),  # Azure
            QColor(245, 255, 250),  # Mint Cream
            QColor(255, 245, 230),  # Peach
            QColor(245, 245, 245),  # White Smoke
            QColor(240, 248, 255),  # Alice Blue
            QColor(248, 248, 255),  # Ghost White
            QColor(255, 248, 220),  # Cornsilk
            QColor(240, 255, 244),  # Soft Mint
            QColor(255, 240, 245),  # Pink Snow
            QColor(245, 240, 240),  # Soft Pink
        ]
        self.table_widget = None
        self.last_update_time = None
        
    def create_table_widget(self):
        """Create and configure a new table widget for this league"""
        self.table_widget = QTableWidget()
        self.table_widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table_widget.updateGeometry()
        # Style the header
        header = self.table_widget.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #2C3E50;
                color: white;
                padding: 4px;
                border: 1px solid #34495E;
            }
        """)
        return self.table_widget
    
    def get_game_color(self, game_id):
        """Get or assign a color for a specific game"""
        if game_id not in self.game_colors:
            self.game_colors[game_id] = self.color_palette[self.current_color_index]
            self.current_color_index = (self.current_color_index + 1) % len(self.color_palette)
        return self.game_colors[game_id]
    
    def to_dataframe(self):
        """Convert the current table data to a pandas DataFrame"""
        # Create a DataFrame with rows as indices and bookmakers as columns
        df = pd.DataFrame(index=self.table_rows, columns=self.bookmakers)
        
        # Fill the DataFrame with current odds values
        for row_label in self.table_rows:
            row_data = self.table_data.get(row_label, {})
            for bm in self.bookmakers:
                df.at[row_label, bm] = row_data.get(bm, "")
        
        # Add game_id and is_header columns for reference
        df['game_id'] = [self.table_data.get(row, {}).get('game_id', '') for row in self.table_rows]
        df['is_header'] = [self.table_data.get(row, {}).get('is_header', False) for row in self.table_rows]
        
        return df
    
    def update_from_dataframe(self, df):
        """Update table data from a pandas DataFrame"""
        # Update bookmakers list if needed
        bm_columns = [col for col in df.columns if col not in ['game_id', 'is_header']]
        for bm in bm_columns:
            if bm not in self.bookmakers:
                self.bookmakers.append(bm)
        
        # Update rows
        self.table_rows = df.index.tolist()
        
        # Update table data
        for row_label in self.table_rows:
            if row_label not in self.table_data:
                self.table_data[row_label] = {}
            
            # Set game_id and is_header
            self.table_data[row_label]['game_id'] = df.at[row_label, 'game_id']
            self.table_data[row_label]['is_header'] = df.at[row_label, 'is_header']
            
            # Set bookmaker data
            for bm in bm_columns:
                if pd.notna(df.at[row_label, bm]) and df.at[row_label, bm] != "":
                    self.table_data[row_label][bm] = df.at[row_label, bm]

    
    
    
        



#TODO: Make this work, spinbox not allowing for interval selection for update interval 
class UpdateProgressIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.progress = 0
        
    def setProgress(self, value):
        self.progress = value
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw background circle
        painter.setPen(QPen(QColor("#e9ecef"), 2))
        painter.drawEllipse(2, 2, 16, 16)
        
        # Draw progress arc
        painter.setPen(QPen(QColor("#007bff"), 2))
        angle = int(-self.progress * 360)
        painter.drawArc(2, 2, 16, 16, 90 * 16, angle * 16)




class DataManager(QObject):
    """Manages data fetching and processing for odds data"""
    games_updated = pyqtSignal(list)
    odds_updated = pyqtSignal(dict, str)  # Added league_name parameter

    def __init__(self):
        super().__init__()
        self.sport_key = None
        self.prop_client = None
        self.league_map = {}

    async def fetch_leagues(self, session=None):
        """Fetch available leagues from the API"""
        return await league_query(session)

    async def fetch_odds(self, sport, region, markets, odds_format, date_format, session):
        """Fetch odds for a specific sport, region, and markets"""
        return await odds_query(sport, region, markets, odds_format, date_format, session)


class ModernOddsWindow(QMainWindow):
    """Main window for displaying and managing odds data"""
    BUTTON_STYLE = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 1px solid #0056b3;
            padding: 5px 10px;
            margin-right: 5px;
            border-radius: 4px;
        }
        QPushButton:checked {
            background-color: #28a745;  /* Green */
            color: white;
            border: 1px solid #218838;
        }
        QPushButton:disabled {
            background-color: #e9ecef;
            color: #6c757d;
            border-color: #dee2e6;
        }
    """
    
    fetch_odds_button_style = """
    QPushButton {
        background-color: #dc9437;  /* Orange */
        color: white; /* text color */
        border: 1px solid #0056b3;
        padding: 5px 10px;
        margin-right: 5px;
        border-radius: 4px;
        width: 200px;
        font-size: 24px;
    }"""
    
    props_button_style = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 3px solid #0056b3;
            border-radius: 4px;
            width: 72px;
            height: 36px;
            font-size: 12px;
        }
        QPushButton:disabled {
            background-color: #909090;
        }
    """
    
    def __init__(self):
        super().__init__()
        self.region_selector = None
        self.timer = QTimer()
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.selected_markets = {"spreads"}  # Initialize with default market
        self.init_ui()
        self.connect_signals()
        self.selected_region = "us" # default region should always be a string
        
        self.icon_frame = 0
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_game_statuses)
        self.status_timer.start(600000) # 10 minutes - makes a request to fetch scores
    
    def UpdateIcon(self):
        framesdir = pathlib.Path(__file__).parent / "appicon_frames"
        next_icon = framesdir / f"frame{str(self.icon_frame).zfill(3)}.png"
        self.setWindowIcon(QIcon(str(next_icon)))
        self.icon_frame = ((self.icon_frame + 1) % 200)
        #print(next_icon)
    

    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)
        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.layout = QVBoxLayout(main_widget)
        
        # --------- TOP SECTION ---------
        # League selection dropdown
        league_layout = QHBoxLayout()
        league_layout.addWidget(QLabel("Select League:"))
        self.league_selector = QComboBox()
        league_layout.addWidget(self.league_selector)
        
        # Add streaming toggle button to the right of league selector
        league_layout.addStretch(1)  # Push the button to the right
        
        # Create toggle buttons
        self.stream_toggle_button = QPushButton("Show Streaming Links ▼")
        self.stream_toggle_button.setCheckable(True)
        self.stream_toggle_button.setChecked(False)
        self.stream_toggle_button.clicked.connect(self.toggle_streaming_links)
        self.stream_toggle_button.setFixedWidth(150)
        self.stream_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        # Create news toggle button
        self.news_toggle_button = QPushButton("Show Injury News ▼")
        self.news_toggle_button.setCheckable(True)
        self.news_toggle_button.setChecked(False)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        self.news_toggle_button.setFixedWidth(150)
        self.news_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        # Create historical odds toggle button
        self.history_toggle_button = QPushButton("Show Historical Odds ▼")
        self.history_toggle_button.setCheckable(True)
        self.history_toggle_button.setChecked(False)
        self.history_toggle_button.clicked.connect(self.toggle_historical_odds)
        self.history_toggle_button.setFixedWidth(150)
        self.history_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)
        
        
        # Create a dedicated container for the right side elements
        right_side_container = QWidget()
        right_side_layout = QVBoxLayout(right_side_container)
        right_side_layout.setContentsMargins(0, 0, 0, 0)  # No margins
        right_side_layout.setSpacing(2)  # Minimal spacing between buttons and widget
        
        # Create a horizontal layout for the buttons
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.stream_toggle_button)
        buttons_layout.addWidget(self.news_toggle_button)
        buttons_layout.addWidget(self.history_toggle_button)  # Add historical odds toggle button
        buttons_layout.setSpacing(4)  # Small spacing between buttons
        
        
        # ---- Calculator button ----
        self.calc_button = QPushButton("Calculator   🧮🎲")# ⚙
        self.calc_button.setFixedWidth(150)
        self.calc_button.setCheckable(False)
        self.calc_button.clicked.connect(self.handle_calc_button)
        self.calc_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:hover {
                background-color: #34495E;
            }
        """)
        buttons_layout.addWidget(self.calc_button)
        
        
        
        # Add the buttons layout to the right side layout
        right_side_layout.addLayout(buttons_layout)
        
        # Add the right side container to the league layout
        league_layout.addWidget(right_side_container, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        
        self.layout.addLayout(league_layout)
        
        # --------- REGION AND TICKER SECTION ---------
        # Create horizontal layout for region selection and ticker tape
        region_ticker_layout = QHBoxLayout()
        
        # Left side: Region selection
        region_section = QWidget()
        region_section_layout = QVBoxLayout(region_section)
        region_section_layout.setContentsMargins(0, 0, 0, 0)
        
        region_label = QLabel("Select Region:")
        region_section_layout.addWidget(region_label)
        
        region_container = QWidget()
        region_layout = QGridLayout(region_container)
        region_layout.setSpacing(10)
        
        self.region_checkboxes = {}
        regions = ['us', 'us2', 'eu', 'au', 'uk', 'global']
        
        # Create checkboxes in a 3x2 grid (3 columns, 2 rows)
        for i, region in enumerate(regions):
            checkbox = QCheckBox(region)
            if region == 'us':
                checkbox.setChecked(True)
            checkbox.stateChanged.connect(lambda state, r=region: self.handle_region_change(r))
            self.region_checkboxes[region] = checkbox
            region_layout.addWidget(checkbox, i // 3, i % 3)
        
        region_container.setFixedHeight(58)
        region_container.setFixedWidth(200)
        region_section_layout.addWidget(region_container)
        region_section.setFixedWidth(220)
        
        region_ticker_layout.addWidget(region_section)
        
        # Add vertical separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        separator.setStyleSheet("""
            QFrame {
                color: #34495E;
                background-color: #34495E;
                border: none;
            }
        """)
        separator.setFixedWidth(2)
        region_ticker_layout.addWidget(separator)
        
        # Add some spacing after separator
        region_ticker_layout.addSpacing(15)
        
        # Right side: Ticker tape
        ticker_section = QWidget()
        ticker_section_layout = QVBoxLayout(ticker_section)
        ticker_section_layout.setContentsMargins(0, 0, 0, 0)
        
        
        # Choose transition style: "flip_card" or "split_reveal"
        self.ticker_tape = TickerTape(transition_style="flip_card")
        ticker_section_layout.addWidget(self.ticker_tape)
        
        region_ticker_layout.addWidget(ticker_section, 1)  # Give ticker section stretch
        
        self.layout.addLayout(region_ticker_layout)
        
        # --------- MARKET AND STREAMING SECTION ---------
        # This is where the key change happens - we put the streaming widget in the same row as the market buttons
        
        # Create a horizontal layout for markets and streaming
        market_streaming_layout = QHBoxLayout()
        
        # Left side container for regular markets
        left_container = QWidget()
        left_layout = QHBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create regular market buttons
        self.market_buttons = {}
        for market in ["h2h", "spreads", "totals"]:
            btn = QPushButton(market.capitalize())
            btn.setCheckable(True)
            btn.setChecked(market in self.selected_markets)
            btn.setObjectName(f"market_{market}")
            self.market_buttons[market] = btn
            btn.setStyleSheet(self.BUTTON_STYLE)
            left_layout.addWidget(btn)
        
        # Add fetch button
        self.fetch_odds_button = QPushButton("Fetch Odds 🎰")
        self.fetch_odds_button.setStyleSheet(self.fetch_odds_button_style)
        left_layout.addWidget(self.fetch_odds_button)
        
        # Add props button
        self.props_button = QPushButton("Props ➣➣")
        self.props_button.setObjectName("market_props")
        self.props_button.setEnabled(False)
        self.props_button.setStyleSheet(self.props_button_style)
        self.market_buttons["props"] = self.props_button
        left_layout.addWidget(self.props_button)
        
        # Add Table Tennis Button
        self.tt_button = QPushButton("TT🏓")
        self.tt_button.setObjectName("market_tt")
        self.tt_button.setStyleSheet(self.props_button_style)
        left_layout.addWidget(self.tt_button)
        
        # Add props availability label
        self.props_availability_label = QLabel("No Props available for this league")
        self.props_availability_label.setStyleSheet("color: #6c757d; font-style: italic;")
        self.props_availability_label.setVisible(False)
        left_layout.addWidget(self.props_availability_label)
        
        # Add the left container to the market_streaming_layout
        market_streaming_layout.addWidget(left_container)
        
        # Add a stretch to push everything to the left and right
        market_streaming_layout.addStretch(1)
        
        # Create and configure the TuneInWidget
        self.tune_in_widget = TuneInWidget()
        self.tune_in_widget.setVisible(False)  # Hidden by default
        self.tune_in_widget.setFixedWidth(650)  # Fixed width to make it compact
        market_streaming_layout.addWidget(self.tune_in_widget)
        
        # Add the market_streaming_layout to the main layout
        self.layout.addLayout(market_streaming_layout)
        
        # --------- PROGRESS BAR ---------
        # Progress bar
        self.progress = QProgressBar()
        self.layout.addWidget(self.progress)
        
        # --------- ODDS SECTION ---------
        # Tab widget for different leagues
        self.tab_widget = QTabWidget()
        self.layout.addWidget(QLabel("Odds:"))
        
        # Create the team news widget first
        self.team_news_widget = TeamNewsWidget()
        self.team_news_widget.setVisible(False)  # Hidden by default
        self.team_news_widget.setFixedWidth(650)  # Fixed width
        
        # Set the news widget reference for the ticker tape
        self.ticker_tape.news_widget = self.team_news_widget
        
        # Connect to NewsWorker signal to avoid polling
        if hasattr(self.team_news_widget, 'worker'):
            self.team_news_widget.worker.news_fetched.connect(self.ticker_tape.on_news_ready)
        
        # Create news container and add the team news widget
        self.news_container = QWidget()
        self.news_container.setFixedWidth(650)  # Match the width of the widget
        news_container_layout = QVBoxLayout(self.news_container)
        news_container_layout.setContentsMargins(0, 0, 0, 0)  # Zero margins
        news_container_layout.setSpacing(0)  # Zero spacing
        news_container_layout.addWidget(self.team_news_widget)
        
        # Set initial state for the news container
        self.news_container.setVisible(False)  # Hide container initially
        
        # Create the best lines container
        self.best_lines_container = QWidget()
        best_lines_layout = QVBoxLayout(self.best_lines_container)
        best_lines_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create header layout with label and buttons
        best_lines_header_layout = QHBoxLayout()
        
        # Add the header label
        best_lines_header = QLabel("Best Lines ⮟")
        best_lines_header.setStyleSheet("font-weight: bold; font-size: 14px; color: #7bd419")
        best_lines_header_layout.addWidget(best_lines_header)
        
        # Add spacer to push buttons to the right
        best_lines_header_layout.addStretch(1)
        
        # Add refresh button for splits data
        self.splits_refresh_button = QPushButton("↻")
        self.splits_refresh_button.setToolTip("Refresh Betting Splits Data")
        self.splits_refresh_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 2px 4px;
                border-radius: 3px;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #34495E;
            }
        """)
        self.splits_refresh_button.setFixedWidth(25)
        self.splits_refresh_button.clicked.connect(self.refresh_splits_data)
        best_lines_header_layout.addWidget(self.splits_refresh_button)
        
        # Create the best lines widget
        self.best_lines_widget = BestLinesWidget()
        
        def UpdateBestLinesHeader():
            best_lines_header.setText("Splits ⮟" if self.best_lines_widget.show_splits else "Best Lines ⮟")
        
        # Add the toggle button from the BestLinesWidget
        best_lines_header_layout.addWidget(self.best_lines_widget.toggle_button)
        self.best_lines_widget.toggle_button.clicked.connect(UpdateBestLinesHeader)
        
        # Add the header layout and the widget to the container
        best_lines_layout.addLayout(best_lines_header_layout)
        best_lines_layout.addWidget(self.best_lines_widget)
        
        # Create the historical odds container
        self.historical_odds_container = QWidget()
        historical_odds_layout = QVBoxLayout(self.historical_odds_container)
        historical_odds_layout.setContentsMargins(0, 0, 0, 0)
        self.historical_odds_container.setFixedWidth(750)
        
        # Create the historical odds widget
        self.historical_odds_widget = HistoricalOddsWidget(SUPER_KEY, 10)
        historical_odds_layout.addWidget(self.historical_odds_widget)
        
        # Initially hide the historical container (important!)
        self.historical_odds_container.setVisible(False)
        self.historical_odds_widget.setVisible(False)
        
        # Create a container for the bottom right section (best lines + historical odds)
        right_bottom_container = QWidget()
        right_bottom_layout = QHBoxLayout(right_bottom_container)
        right_bottom_layout.setContentsMargins(0, 0, 0, 0)
        right_bottom_layout.addWidget(self.best_lines_container)
        right_bottom_layout.addWidget(self.historical_odds_container)
        
        # First, create a horizontal splitter for the bottom section
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Add the news container to the left side of horizontal splitter
        self.horizontal_splitter.addWidget(self.news_container)
        
        # Add the right bottom container to the right side of horizontal splitter  
        self.horizontal_splitter.addWidget(right_bottom_container)
        
        # Set initial sizes for horizontal splitter
        self.horizontal_splitter.setSizes([325, 325])  # Equal width initially
        
        # Now create the vertical splitter with tab widget on top and horizontal splitter on bottom
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.addWidget(self.tab_widget)
        self.vertical_splitter.addWidget(self.horizontal_splitter)
        
        # Set initial sizes for vertical splitter to show more of the tab widget
        self.vertical_splitter.setSizes([400, 200])
        
        # Add the vertical splitter to the main layout
        self.layout.addWidget(self.vertical_splitter, 1)  # The 1 gives it stretch
        
        # --------- AUTO-UPDATE CONTROLS ---------
        # Auto-update controls
        update_controls_layout = QHBoxLayout()
        
        self.auto_update_check = QCheckBox("Auto-Update Odds")
        update_controls_layout.addWidget(self.auto_update_check)
        
        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(5)
        self.update_interval.setEnabled(True)
        update_controls_layout.addWidget(QLabel("Update Interval:"))
        update_controls_layout.addWidget(self.update_interval)
        
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.status_frame_layout = QHBoxLayout(self.status_frame)
        
        self.last_update_label = QLabel("Last Update: Never")
        self.last_update_label.setStyleSheet("color: #6c757d;")
        
        self.update_status = QLabel("")
        self.update_status.setStyleSheet("""
            QLabel {
                padding: 5px;
                border-radius: 3px;
                background-color: #f8f9fa;
            }
        """)
        
        # Progress indicator
        self.progress_indicator = UpdateProgressIndicator()
        self.status_frame_layout.addWidget(self.progress_indicator)
        self.status_frame_layout.addWidget(self.last_update_label)
        self.status_frame_layout.addWidget(self.update_status)
        update_controls_layout.addWidget(self.status_frame)
        update_controls_layout.addStretch()
        
        self.layout.addLayout(update_controls_layout)


    def update_market_selection(self):
        """Update selected markets based on button states"""
        self.selected_markets = {market for market, btn in self.market_buttons.items() 
                               if btn.isChecked() and btn.isEnabled()}
        print("Selected markets:", self.selected_markets)

    def handle_league_change(self):
        """Handle league selection changes"""
        
        # Ensure self.props_button exists before proceeding
        if not hasattr(self, "props_button"):
            print("Warning: props_button does not exist yet. Skipping handle_league_change.")
            return
        
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)
    
        has_props = sport_key in MAJOR_PROP_MARKETS  # Check if this league has props
        self.props_button.setEnabled(has_props)
        self.props_availability_label.setVisible(not has_props)
        
        if hasattr(self, 'team_news_widget'):
            self.team_news_widget.handle_league_change(sport_key)
            
        # Update splits data when league changes
        if hasattr(self, 'best_lines_widget'):
            self.best_lines_widget.set_sport(sport_key)
        return
      
    def handle_region_change(self, region):
        """Handle region selection changes"""
        if region == 'global' and self.region_checkboxes['global'].isChecked():
            # Check all other regions when global is selected
            for r, cb in self.region_checkboxes.items():
                if r != 'global':
                    cb.setChecked(True)
            self.selected_region = "us,us2,eu,au,uk"  # Ensure it's a string, not a set
        else:
            # If global is unchecked, uncheck it when selecting individual regions
            if region != 'global':
                self.region_checkboxes['global'].setChecked(False)
            
            # Get all selected regions except 'global'
            selected = [r for r, cb in self.region_checkboxes.items() 
                       if cb.isChecked() and r != 'global']
            
            # Join selected regions with commas or default to "us"
            self.selected_region = ",".join(selected) if selected else "us"  # Ensure string format
        
        print(f"Selected regions: {self.selected_region}")
        return
    
    def get_valid_markets(self, sport_key):
        """Get valid markets for the selected sport"""
        valid_markets = REGULAR_MARKETS.copy()
        if sport_key in MAJOR_PROP_LEAGUES:
            valid_markets.update(MAJOR_PROP_LEAGUES[sport_key].keys())
        return valid_markets
    
    def handle_props_button(self):
        """Handle Props button click to open PropsWindow."""
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)
        
        print(f"Props button clicked. League: {selected_league}, Sport Key: {sport_key}")  # Debug print
        
        if sport_key in MAJOR_PROP_MARKETS:  # Ensure props exist for this league
            print("Props are available for this league. Creating PropsWindow...")  # Debug print
            
            # If there's an existing window, properly destroy it
            if hasattr(self, "props_window") and self.props_window is not None:
                try:
                    self.props_window.close()
                    self.props_window.deleteLater()  # Schedule for Qt deletion
                    self.props_window = None
                except Exception as e:
                    print(f"Error cleaning up old props window: {e}")
            
            # Create a completely new instance with no reference to old data
            self.props_window = PropsWindow(sport_key, selected_league)
            self.props_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # Qt will delete the widget when closed
            self.props_window.show()
        else:
            print("No props available for this league.")  # Debug print

    # overriding inherited method for custom keybinds
    def keyPressEvent(self, a0):
        self.clearFocus()
        # “1” → Props (already existing)
        if a0.key() == Qt.Key.Key_1:
            if not self.props_button.isEnabled():
                return
            self.handle_props_button()
            return

        # “2” → Calculator
        if a0.key() == Qt.Key.Key_2:
            self.handle_calc_button()
            return
        
        if a0.key() == Qt.Key.Key_R:
            print("refreshing")
            self.status_timer.setSingleShot(True)
            self.status_timer.setInterval(0)
            return
        
        super().keyPressEvent(a0)

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.league_selector.currentTextChanged.connect(self.handle_league_change)
        for btn in self.market_buttons.values():
            btn.toggled.connect(self.update_market_selection)
        self.fetch_odds_button.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.update_interval.valueChanged.connect(self.update_timer_interval)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)
        self.timer.timeout.connect(self.refresh_data)
        self.props_button.clicked.connect(self.handle_props_button)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        self.tt_button.clicked.connect(self.handle_tt_button)
        


    def RestartTimer(self):
        interval_ms = self.update_interval.value() * 60 * 1000
        self.timer.start(interval_ms)
        return interval_ms
        
    def update_region(self):
        selected_region = self.region_selector.currentText()
        print(f"Selected bookmaker region: {selected_region}")
        # Modify your odds fetching logic based on selected region here

    def update_status_text(self):
        """Update the status text showing time until next update"""
        if not self.auto_update_check.isChecked():
            self.update_status.setText("")
            self.progress_indicator.setProgress(0)
            return
    
        remaining_time = self.timer.remainingTime() // 1000  # Convert to milliseconds to seconds
        total_time = self.update_interval.value() * 60  # Total time in seconds
        progress = 1 - (remaining_time / total_time)  # Calculate progress (0 to 1)
        
        minutes = remaining_time // 60
        seconds = remaining_time % 60
        
        # Color coding based on remaining time
        if remaining_time < 60:  # Less than 1 minute
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
        elif remaining_time < 180:  # Less than 3 minutes
            self.update_status.setStyleSheet("background-color: #ffc107; color: #000;")
        else:
            self.update_status.setStyleSheet("background-color: #28a745; color: white;")
        
        self.update_status.setText(f"Next update in: {minutes}m {seconds}s")
        self.progress_indicator.setProgress(progress)
    
    
    
    
    async def initialize(self):
        """Initialize the window with league data"""
        await self.populate_leagues()
        self.leagues_loaded = True

    async def populate_leagues(self, default_league="MLB"):
        """Fetch and populate leagues in the dropdown"""
        # Create a single session for the league fetch
        async with aiohttp.ClientSession() as session:
            leagues = await self.data_manager.fetch_leagues(session)
            
        print("Fetched leagues:", leagues)
        self.league_selector.clear()
        self.data_manager.league_map.clear()
    
        for sport_category, league_list in leagues.items():
            for league in league_list:
                self.league_selector.addItem(league['title'])
                self.data_manager.league_map[league['title']] = league['key']
        
        self.league_selector.setCurrentText(default_league)
        
        # Call handle_league_change after populating
        if self.league_selector.count() > 0:
            self.handle_league_change()

    def handle_tab_change(self, index):
        """Handle tab switching events, properly sync best lines data and main table data"""
        if index >= 0:
            self.current_league = self.tab_widget.tabText(index)
            # Extract the league name without the market info
            if "(" in self.current_league:
                base_league = self.current_league.split(" (")[0]
                # Optionally update league selector to match the tab
                self.league_selector.setCurrentText(base_league)
            
            # UPDATE BEST LINES FOR CURRENT TAB
            if hasattr(self, 'best_lines_widget') and self.current_league in self.league_tabs:
                tab_data = self.league_tabs[self.current_league]
                if hasattr(tab_data, 'consolidated_odds_data'):
                    self.best_lines_widget.update_display(tab_data.consolidated_odds_data)

    def create_league_tab(self, league_name, sport_key, selected_markets=None):
        """Create a new tab for a league with specific markets"""
        # Create a unique tab identifier based on league name and selected markets
        markets_str = "+".join(sorted(selected_markets)) if selected_markets else "default"
        tab_id = f"{league_name} ({markets_str})"
        
        if tab_id not in self.league_tabs:
            tab_data = LeagueTabData(league_name, sport_key)
            table_widget = tab_data.create_table_widget()
            
            # Connect selection signal for the new table
            table_widget.itemSelectionChanged.connect(self.on_market_selection_changed)
            
            self.tab_widget.addTab(table_widget, tab_id)
            self.league_tabs[tab_id] = tab_data
            self.current_league = tab_id
            self.tab_widget.setCurrentIndex(self.tab_widget.count() - 1)
        return self.league_tabs[tab_id]


    # Proper formatting for 3-way markets (likely redundant, but im tilt)
    def format_market_label(self, market_key, outcome):
        """Format the market label based on market type and outcome"""
        if market_key == 'h2h':
            return f"Moneyline: {outcome['name']}"
        elif market_key == 'h2h_3way':
            return f"3-Way Moneyline: {outcome['name']}"
        elif market_key == 'spreads':
            return f"Spread: {outcome['name']} {outcome.get('point', '')}"
        elif market_key == 'totals':
            return f"Total {outcome['name']} {outcome.get('point', '')}"
        return f"{market_key}: {outcome['name']}"



    def format_price(self, outcome):
        """Format the price display including point if available"""
        price = str(outcome.get('price', ''))
        if 'point' in outcome:
            price += f" ({outcome['point']})"
        return price

    def display_odds(self, odds: dict, league_name: str):
        """Update odds display efficiently by only processing changed data"""
        if not odds or 'bookmakers' not in odds:
            return
        
        tab_data = self.league_tabs.get(league_name)
        if not tab_data:
            return
        
        game_id = odds.get('id', 'unknown_game')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')
        
        # Collect bookmaker names
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in tab_data.bookmakers:
                tab_data.bookmakers.append(bm_title)
        
        # Add a header row for the game if it doesn't exist
        game_header = f"Game: {home_team} vs {away_team}"
        if game_header not in tab_data.table_rows:
            tab_data.table_rows.append(game_header)
            tab_data.table_data[game_header] = {'is_header': True, 'game_id': game_id}
            tab_data.num_rows += 1
        
        # Process markets and track changes
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                    
                    # Add new row if needed
                    if unique_label not in tab_data.table_rows:
                        tab_data.table_rows.append(unique_label)
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                        tab_data.num_rows += 1
                    
                    # Update price only if changed
                    price = self.format_price(outcome)
                    if unique_label not in tab_data.table_data:
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                    tab_data.table_data[unique_label][bm_title] = price
        
        self.update_table_display(tab_data)




    def update_table_display(self, tab_data: LeagueTabData):
        """Update table display with improved price change highlighting and live status"""
        table = tab_data.table_widget
        current_rows = table.rowCount()
        current_cols = table.columnCount()
        
        # Update table structure if needed
        expected_cols = len(tab_data.bookmakers) + 1
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
        
        expected_rows = len(tab_data.table_rows)
        if current_rows != expected_rows:
            table.setRowCount(expected_rows)
        
        needs_resize = False
        
        for row_idx, row_label in enumerate(tab_data.table_rows):
            row_data = tab_data.table_data[row_label]
            game_id = row_data['game_id']
            
            # Check if this is a live game for special coloring
            status_info = row_data.get('status_info', {})
            is_live = status_info.get('is_live', False)
            
            # Choose color based on live status
            if is_live:
                color = QColor(220, 53, 69, 120)  # Semi-transparent red for live games
            else:
                color = tab_data.get_game_color(game_id)  # Normal game colors
            
            # Create or update row header if needed
            header_item = table.item(row_idx, 0)
            if not header_item:
                header_item = ColoredTableItem(row_label, game_id)
                table.setItem(row_idx, 0, header_item)
                needs_resize = True
            
            # Apply header styling with live game highlighting
            if row_data.get('is_header'):
                font = QFont()
                font.setBold(True)
                header_item.setFont(font)
                header_item.setBackground(color)
                
                # Use white text for live games for better contrast, black for others
                if is_live:
                    header_item.setForeground(QColor('white'))
                else:
                    header_item.setForeground(QColor('black'))
            else:
                market_color = QColor(color)
                market_color.setAlpha(230)
                header_item.setBackground(market_color)
                header_item.setForeground(QColor('black'))
            
            # Update bookmaker columns
            for col_idx, bm in enumerate(tab_data.bookmakers, 1):
                current_value = row_data.get(bm, "")
                previous_value = tab_data.previous_data.get((row_label, bm))
                
                item = table.item(row_idx, col_idx)
                if not item:
                    item = ColoredTableItem(current_value, game_id)
                    table.setItem(row_idx, col_idx, item)
                    needs_resize = True
                
                # Only update if value has changed
                if current_value != previous_value and previous_value is not None:
                    item.setText(current_value)
                    
                    # Parse the odds values for comparison
                    try:
                        current_odds = float(current_value.split()[0])
                        previous_odds = float(previous_value.split()[0])
                        
                        # Better odds (higher value) = green, worse odds = red
                        if current_odds > previous_odds:
                            highlight_color = QColor(0, 200, 0, 180)  # Semi-transparent green
                        else:
                            highlight_color = QColor(200, 0, 0, 180)  # Semi-transparent red
                        
                        item.setBackground(highlight_color)
                        item.setForeground(QColor('black'))  # Keep text black for readability
                        
                        # Reset background after 5 seconds
                        market_color = QColor(color)
                        market_color.setAlpha(230)
                        QTimer.singleShot(5000, lambda i=item, c=market_color: (
                            i.setBackground(c),
                            i.setForeground(QColor('black'))
                        ))
                    except (ValueError, IndexError):
                        # If we can't parse the odds, just update without highlighting
                        item.setText(current_value)
                    
                    tab_data.previous_data[(row_label, bm)] = current_value
                    needs_resize = True
                elif current_value != previous_value:
                    # First time seeing this value
                    item.setText(current_value)
                    tab_data.previous_data[(row_label, bm)] = current_value
                
                # Maintain consistent background and text color when not highlighted
                if not row_data.get('is_header') and item.background().color().alpha() != 180:  # Don't override highlight
                    market_color = QColor(color)
                    market_color.setAlpha(230)
                    item.setBackground(market_color)
                    item.setForeground(QColor('black'))
        
        # Only resize if needed
        if needs_resize:
            table.resizeColumnsToContents()
            table.resizeRowsToContents()
    
    
    def toggle_auto_update(self):
        """Enable or disable auto-update functionality"""
        if self.auto_update_check.isChecked():
           self.update_interval.setEnabled(True)
           interval_ms = self.RestartTimer()
           print(f"next update in: {int(interval_ms/1000)} seconds")
        else:
            self.timer.stop()
            print("timer stopped")
        # self.update_status_text() # crashes

    @qasync.asyncSlot()
    async def refresh_splits_data(self):
        """The actual async refresh method"""
        self.splits_refresh_button.setEnabled(False)
        self.splits_refresh_button.setText("⟳")
        
        try:
            result = await self.best_lines_widget.refresh_splits_data()
            if result:
                # Show success indicator briefly  
                self.splits_refresh_button.setText("✓")
                QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))
            else:
                # Show error indicator briefly
                self.splits_refresh_button.setText("✗")
                QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))
        except Exception as e:
            print(f"Error refreshing splits data: {e}")
            
            traceback.print_exc()
            self.splits_refresh_button.setText("✗") 
            QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))
        
        self.splits_refresh_button.setEnabled(True)

    
    
    
      
    
    def update_timer_interval(self):
        """Update the timer interval when spinbox value changes"""
        if self.auto_update_check.isChecked():
            interval_ms = self.update_interval.value() * 60 * 1000
            self.timer.start(interval_ms)
            print(f"timer interval updated: {interval_ms}")
        # self.update_status_text() # crashes
    
    def toggle_streaming_links(self):
        """Toggle visibility of the streaming links widget"""
        visible = self.stream_toggle_button.isChecked()
        self.tune_in_widget.setVisible(visible)
        
        # Update button text
        if visible:
            self.stream_toggle_button.setText("Hide Streaming Links ▲")
        else:
            self.stream_toggle_button.setText("Show Streaming Links ▼")
    
    
    def toggle_news_feed(self):
        """Toggle visibility of the news feed widget with optimized spacing"""
        visible = self.news_toggle_button.isChecked()
        
        # Make the widget visible first (needed for proper layout calculations)
        self.news_container.setVisible(visible)
        self.team_news_widget.setVisible(visible)
        
        # Update button text and adjust container size
        if visible:
            self.news_toggle_button.setText("Hide Injury News ▲")
            
            # Calculate exact height for 3 articles
            article_height = 85
            container_height = (article_height * 3)
            self.news_container.setMinimumHeight(container_height)
            
            # KEY FIX: Set negative top margin on progress bar to pull it upward
            prog_margins = self.progress.contentsMargins()
            prog_margins.setTop(-100)  # Adjust this value as needed
            self.progress.setContentsMargins(prog_margins)
            
            # Set minimal spacing in main layout
            self.layout.setSpacing(0)
        else:
            self.news_toggle_button.setText("Show Injury News ▼")
            
            # Reset progress bar margins to normal
            prog_margins = self.progress.contentsMargins()
            prog_margins.setTop(0)
            self.progress.setContentsMargins(prog_margins)
            
            # Reset layout spacing
            self.layout.setSpacing(0)
        
        # Force update to apply changes
        QTimer.singleShot(10, self.update)
        
        
    def toggle_historical_odds(self):
        """Toggle visibility of the historical odds widget"""
        visible = self.history_toggle_button.isChecked()
        
        # Make the historical odds container visible/invisible based on toggle state
        self.historical_odds_container.setVisible(visible)
        
        # Update button text
        if visible:
            self.history_toggle_button.setText("Hide Historical Odds ▲")
        else:
            self.history_toggle_button.setText("Show Historical Odds ▼")
            
            # If we're hiding, cancel any running data loads
            if hasattr(self.historical_odds_widget, '_load_task') and self.historical_odds_widget._load_task:
                try:
                    self.historical_odds_widget._load_task.cancel()
                except:
                    pass
        
        # Force update to apply changes
        # Make sure both are visible
        self.historical_odds_container.setVisible(visible)
        self.historical_odds_widget.setVisible(visible)
        
        # Force a layout update
        self.historical_odds_container.updateGeometry()
        self.historical_odds_widget.updateGeometry()
        QTimer.singleShot(10, self.update)
    
    
    def update_best_lines_display(self):
        """Update the best lines widget with the latest consolidated raw API data."""
        if not self.best_lines_widget:
            print("Best lines widget not initialized. Skipping update.")
            return
        
        # Use the consolidated raw data if available
        if hasattr(self, 'consolidated_odds_data') and self.consolidated_odds_data:
            self.best_lines = self.best_lines_widget.update_display(self.consolidated_odds_data)
        else:
            print("No consolidated odds data available for best lines calculation.")

    
    def on_market_selection_changed(self):
        """Handle market selection in the odds table"""
        table = self.tab_widget.currentWidget()
        if not table or not isinstance(table, QTableWidget):
            return
        
        current_row = table.currentRow()
        if current_row < 0:
            return
        
        # Get event and market info from the selected row
        header_item = table.item(current_row, 0)
        if not header_item:
            return
        
        row_label = header_item.text()
        market_type = ""
        
        # Skip header rows
        if "Game:" in row_label:
            return
        
        # Try to determine market type from the row label
        if "Moneyline" in row_label:
            market_type = "h2h"
        elif "Spread" in row_label:
            market_type = "spreads"
        elif "Total" in row_label:
            market_type = "totals"
        else:
            # If we can't determine, don't update
            return
        
        # Find the game ID from the item
        if not hasattr(header_item, 'game_id'): return;
        game_id = header_item.game_id
        
        # Get league and sport info
        league_name = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(league_name)
        
        game_text = header_item.text().split('|', maxsplit=1)[0].strip()
        (home_team, away_team) = [text.strip() for text in game_text.split(' vs ', maxsplit=1)]
        
        # Only update the historical odds widget if it's visible
        if self.historical_odds_container.isVisible() and hasattr(self, 'historical_odds_widget'):
            self.historical_odds_widget.set_market(sport_key, game_id, market_type, home_team, away_team)
            
        
        
    def handle_tt_button(self):
        """Handle Table Tennis button click to open TableTennisGUI."""
        print("Table Tennis button clicked.")
        
        # If there's an existing window, properly destroy it
        if hasattr(self, "tt_window") and self.tt_window is not None:
            try:
                self.tt_window.close()
                self.tt_window.deleteLater()  # Schedule for Qt deletion
                self.tt_window = None
            except Exception as e:
                print(f"Error cleaning up old TT window: {e}")
        
        # Create a completely new instance
        self.tt_window = TableTennisGUI()
        self.tt_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # Qt will delete the widget when closed
        self.tt_window.show()    




    @qasync.asyncSlot() 
    # This function might just be too fucking much 
    async def refresh_data(self):
        """Fetch and update odds data asynchronously using pandas for comparison"""
        if not self.leagues_loaded:
            print("Leagues not loaded yet. Please wait.")
            return
    
        self.progress.setValue(0)
        self.fetch_odds_button.setEnabled(False)
        
        # Update last update time
        current_time = datetime.now().strftime("%H:%M:%S")
        self.last_update_label.setText(f"Last Update: {current_time}")
        
        # Update status during refresh
        self.update_status.setStyleSheet("background-color: #007bff; color: white;")
        self.update_status.setText("Updating odds...")
        
        try:
            selected_league = self.league_selector.currentText()
            sport_key = self.data_manager.league_map.get(selected_league)
            if not sport_key:
                print(f"No valid sport key found for the selected league: {selected_league}")
                self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
                self.update_status.setText("Error: Invalid league selection")
                return
    
            # Create a unique tab based on league and currently selected markets
            current_markets = self.selected_markets.copy()
            
            # Create or get existing tab (now with markets parameter)
            tab_data = self.create_league_tab(selected_league, sport_key, current_markets)
            
            # Store current table state in a DataFrame before updating
            current_df = None
            if tab_data.table_rows:
                current_df = tab_data.to_dataframe()
            
            self.data_manager.sport_key = sport_key
            self.data_manager.prop_client = PropClient(sport_key)
    
            # Create a single aiohttp session for all API calls
            async with aiohttp.ClientSession() as session:
                # Fetch scores data for live status detection - now uses session
                scores_data = await scores_query(sport_key, session=session)
        
                # Create a new DataFrame for the updated data
                new_table_rows = []
                new_table_data = {}
                
                games = await self.data_manager.prop_client.get_games(session)
                print(f"Fetched games response: {games}")
        
                if not isinstance(games, list):
                    print(f"Unexpected response from get_games(): {games}")
                    self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
                    self.update_status.setText("Error: Invalid response format")
                    return
        
                # Process odds data for each game
                total_games = len(games)
                bookmakers_seen = set()
                game_odds_data = {}
                
                # Create a consolidated odds data structure for the best lines widget
                consolidated_odds_data = {
                    'bookmakers': []
                }
                bookmakers_map = {}  # To track and merge bookmakers across games
                
                for index, game in enumerate(games):
                    game_id = game.get('id', '')
                    if not game_id:
                        print("No game ID found in the response.")
                        continue
        
                    # Update progress based on game processing
                    progress_value = int((index + 1) / total_games * 100)
                    self.progress.setValue(progress_value)
        
                    # Reuse the same session for prop client calls
                    available_markets = current_markets.copy()  # Use the current_markets variable
                    selected_region = self.selected_region
                    odds = await self.data_manager.prop_client.get_event_odds(
                        session, 
                        game_id, 
                        available_markets, 
                        region=selected_region
                    )
                    
                    if odds is None:
                        print("you're out of credits - no odds returned (probably)")
                        return
                    
                    game_odds_data[game_id] = odds
                    
                    # Extract key info from this game
                    home_team = odds.get('home_team', 'Unknown')
                    away_team = odds.get('away_team', 'Unknown')
                    
                    # Get game status using the new function
                    status_text, is_live, scores_text = get_game_status(odds, scores_data)
                    
                    # Store status info in tab_data
                    if not hasattr(tab_data, 'game_status'):
                        tab_data.game_status = {}
                    tab_data.game_status[game_id] = {
                        'text': status_text,
                        'is_live': is_live,
                        'scores_text': scores_text
                    }
                    
                    # Create game header with status and scores
                    game_header = f"Game: {home_team} vs {away_team} [{status_text}]"
                    if scores_text:
                        game_header += f" - {scores_text}"
                    
                    new_table_rows.append(game_header)
                    new_table_data[game_header] = {
                        'is_header': True, 
                        'game_id': game_id,
                        'status_info': tab_data.game_status[game_id]
                    }
                    
                    # Process all bookmakers and markets
                    for bm in odds.get('bookmakers', []):
                        bm_title = bm['title']
                        bookmakers_seen.add(bm_title)
                        
                        # Add to consolidated data for best lines widget
                        if bm_title not in bookmakers_map:
                            bookmakers_map[bm_title] = {
                                'title': bm_title,
                                'markets': []
                            }
                            consolidated_odds_data['bookmakers'].append(bookmakers_map[bm_title])
                        
                        for market in bm.get('markets', []):
                            market_key = market['key']
                            
                            # Add game_id to each outcome for reference (needed for best lines)
                            for outcome in market.get('outcomes', []):
                                outcome['game_id'] = game_id
                            
                            # Add to consolidated data
                            bookmakers_map[bm_title]['markets'].append(market)
                            
                            for outcome in market.get('outcomes', []):
                                # Create the row label
                                unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                                
                                # Add row if not already present
                                if unique_label not in new_table_rows:
                                    new_table_rows.append(unique_label)
                                    new_table_data[unique_label] = {'game_id': game_id}
                                    
                                # Update price
                                price = self.format_price(outcome)
                                new_table_data[unique_label][bm_title] = price
                
                # Store the consolidated data for the best lines widget
                self.consolidated_odds_data = consolidated_odds_data
                
                tab_data.consolidated_odds_data = consolidated_odds_data  # Update bestlines when tab switched
                # Create a new DataFrame from the collected data
                new_df = pd.DataFrame(index=new_table_rows, columns=list(bookmakers_seen))
                
                # Fill the new DataFrame
                for row_label in new_table_rows:
                    row_data = new_table_data.get(row_label, {})
                    for bm in bookmakers_seen:
                        new_df.at[row_label, bm] = row_data.get(bm, "")
                        
                # Add game_id and is_header columns
                new_df['game_id'] = [new_table_data.get(row, {}).get('game_id', '') for row in new_table_rows]
                new_df['is_header'] = [new_table_data.get(row, {}).get('is_header', False) for row in new_table_rows]
                
                # Identify changes between current and new data
                changes = {}
                if current_df is not None:
                    # Look for changed odds
                    for row in new_df.index:
                        if row in current_df.index:
                            for bm in bookmakers_seen:
                                if bm in current_df.columns:
                                    old_val = current_df.at[row, bm]
                                    new_val = new_df.at[row, bm]
                                    if old_val != new_val and old_val != "" and new_val != "":
                                        if row not in changes:
                                            changes[row] = {}
                                        changes[row][bm] = (old_val, new_val)
                
                # Update tab_data with the new data
                tab_data.bookmakers = list(bookmakers_seen)
                tab_data.table_rows = new_table_rows
                tab_data.table_data = new_table_data
                
                # Update the table display with highlighting changes
                self.update_table_with_changes(tab_data, changes)
                
                # Print a debug message before updating best lines widget
                print("About to update best lines widget with consolidated data")
                print(f"Number of bookmakers in consolidated data: {len(consolidated_odds_data['bookmakers'])}")
                
                # Update best lines widget with the consolidated data
                if hasattr(self, 'best_lines_widget') and self.best_lines_widget:
                    print("Updating best lines widget...")
                    try:
                        self.best_lines = self.best_lines_widget.update_display(consolidated_odds_data)
                        print("Best lines widget updated successfully")
                    except Exception as e:
                        print(f"Error updating best lines widget: {e}")
                        
                        traceback.print_exc()
                else:
                    print("Best lines widget not available")
                
                self.progress.setValue(100)
                
                # Reset status text if auto-update is enabled
                if self.auto_update_check.isChecked():
                    self.update_status_text()
                    self.RestartTimer()
                else:
                    self.update_status.setStyleSheet("background-color: #28a745; color: white;")
                    self.update_status.setText("Update complete")
                    
                # Log the number of changed lines
                if changes:
                    total_changes = sum(len(changes_dict) for changes_dict in changes.values())
                    print(f"Total lines changed: {total_changes}")
                    
        except aiohttp.ClientError as e:
            print(f"Network error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("Network error occurred")
        except Exception as e:
            print(f"Error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("An error occurred")
            
            traceback.print_exc()
        finally:
            self.fetch_odds_button.setEnabled(True)
            # Reset progress bar
            QTimer.singleShot(2000, lambda: self.progress.setValue(0))
            # Reset error messages after a delay
            if not self.auto_update_check.isChecked():
                QTimer.singleShot(5000, lambda: self.update_status.setText(""))
    
    
    # These two functions are for live game indication
    # This class if getting massive oh no
    def _update_game_statuses(self):
        """Wrapper for async status updates"""
        asyncio.create_task(self.update_game_statuses())

    async def update_game_statuses(self):
        """Update game statuses periodically"""
        try:
            # Create a single session for all status updates
            async with aiohttp.ClientSession() as session:
                for tab_id, tab_data in self.league_tabs.items():
                    if not tab_data.game_status:
                        continue
                    
                    # Fetch fresh scores data - only retrieve live/upcoming games
                    scores_data = await scores_query(tab_data.sport_key, days_from=None, session=session)
                    status_changed = False
                    print("-1 credit")
                    
                    # Update each game's status
                    for game_id, current_status in tab_data.game_status.items():
                        matching_games = [score_game for score_game in scores_data if score_game.get('id') == game_id]
                        if len(matching_games) == 0: continue;
                        game_data = matching_games[0]
                        
                        old_status = current_status.get('text', '')
                        old_scores = current_status.get('scores_text', '')
                        
                        # Create game data for status check
                        #game_data = {'id': game_id, 'commence_time': ''}  # commence_time will be ignored with scores_data
                        #status_text, is_live, scores_text = get_game_status(game_data, scores_data)
                        
                        # always do time-based calc because most leagues won't return scores for live games
                        time_diff = (datetime.now(timezone.utc) - datetime.fromisoformat(game_data["commence_time"])).total_seconds()
                        
                        if time_diff < -1800:  # More than 30 min before
                            status_text, is_live, scores_text = "Pre-Game", False, "";
                        elif time_diff < 0:  # Less than 30 min before
                            status_text, is_live, scores_text = "Starting Soon", False, "";
                        elif time_diff < 14400:  # Less than 4 hours after (likely live)
                            status_text, is_live, scores_text = "🔴LIVE", True, "";
                        else:  # More than 4 hours after (likely finished)
                            status_text, is_live, scores_text = "Finished", False, "";
                        
                        if status_text != old_status or scores_text != old_scores:
                            tab_data.game_status[game_id] = {
                                'text': status_text,
                                'is_live': is_live,
                                'scores_text': scores_text
                            }
                            status_changed = True
                    
                    # Update table if statuses changed
                    if status_changed:
                        # Update row labels with new status
                        for i, row_label in enumerate(tab_data.table_rows):
                            if 'Game:' in row_label and '[' in row_label:
                                row_data = tab_data.table_data[row_label]
                                game_id = row_data.get('game_id')
                                if game_id in tab_data.game_status:
                                    # Rebuild header with new status
                                    teams_part = row_label.split('[')[0].strip()
                                    status_info = tab_data.game_status[game_id]
                                    
                                    new_row_label = f"{teams_part} [{status_info['text']}]"
                                    if status_info.get('scores_text'):
                                        new_row_label += f" - {status_info['scores_text']}"
                                    
                                    # Update the data structures
                                    tab_data.table_rows[i] = new_row_label
                                    tab_data.table_data[new_row_label] = tab_data.table_data.pop(row_label)
                                    tab_data.table_data[new_row_label]['status_info'] = status_info
                        
                        self.update_table_display(tab_data)
                        
        except Exception as e:
            print(f"Error updating game statuses: {e}")
    
    def update_table_with_changes(self, tab_data, changes):
         """Update the table with efficient display of odds changes"""
         table = tab_data.table_widget
         
         # Count the total number of changed cells
         total_changes = sum(len(changes_dict) for changes_dict in changes.values())
         
         # Update the changes counter label
         if hasattr(self, 'changes_counter_label'):
             if total_changes > 0:
                 self.changes_counter_label.setText(f"Lines Changed: {total_changes}")
                 self.changes_counter_label.setStyleSheet("color: #dc3545; font-weight: bold;")
                 
                 # Reset the color after 5 seconds
                 QTimer.singleShot(5000, lambda: self.changes_counter_label.setStyleSheet("color: #6c757d;"))
             else:
                 self.changes_counter_label.setText("No Changes")
         
         # Update table structure if needed
         expected_cols = len(tab_data.bookmakers) + 1
         if table.columnCount() != expected_cols:
             table.setColumnCount(expected_cols)
             table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
         
         expected_rows = len(tab_data.table_rows)
         if table.rowCount() != expected_rows:
             table.setRowCount(expected_rows)
         
         # Update all rows
         for row_idx, row_label in enumerate(tab_data.table_rows):
             row_data = tab_data.table_data[row_label]
             game_id = row_data['game_id']
             color = tab_data.get_game_color(game_id)
             
             # Create or update row header
             header_item = table.item(row_idx, 0)
             if not header_item:
                 header_item = ColoredTableItem(row_label, game_id)
                 table.setItem(row_idx, 0, header_item)
             else:
                 header_item.setText(row_label)
             
             # Apply header styling
             if row_data.get('is_header'):
                 font = QFont()
                 font.setBold(True)
                 header_item.setFont(font)
                 header_item.setBackground(color)
                 header_item.setForeground(QColor('black'))
             else:
                 market_color = QColor(color)
                 market_color.setAlpha(230)
                 header_item.setBackground(market_color)
                 header_item.setForeground(QColor('black'))
             
             # Update bookmaker columns
             for col_idx, bm in enumerate(tab_data.bookmakers, 1):
                 current_value = row_data.get(bm, "")
                 
                 item = table.item(row_idx, col_idx)
                 if not item:
                     item = ColoredTableItem(current_value, game_id)
                     table.setItem(row_idx, col_idx, item)
                 else:
                     item.setText(current_value)
                 
                 # Check if this cell has changed
                 if row_label in changes and bm in changes[row_label]:
                     old_value, new_value = changes[row_label][bm]
                     
                     # Parse the odds values for comparison
                     try:
                         current_odds = float(new_value.split()[0])
                         previous_odds = float(old_value.split()[0])
                         
                         # Better odds (higher value) = green, worse odds = red
                         if current_odds > previous_odds:
                             highlight_color = QColor(0, 200, 0, 180)  # Semi-transparent green
                         else:
                             highlight_color = QColor(200, 0, 0, 180)  # Semi-transparent red
                         
                         item.setBackground(highlight_color)
                         item.setForeground(QColor('black'))  # Keep text black for readability
                         
                         # Reset background after 5 seconds
                         market_color = QColor(color)
                         market_color.setAlpha(230)
                         QTimer.singleShot(5000, lambda i=item, c=market_color: (
                             i.setBackground(c),
                             i.setForeground(QColor('black'))
                         ))
                     except (ValueError, IndexError):
                         # If we can't parse the odds, just update without highlighting
                         pass
                 else:
                     # No change, maintain consistent background and text color
                     if not row_data.get('is_header'):
                         market_color = QColor(color)
                         market_color.setAlpha(230)
                         item.setBackground(market_color)
                         item.setForeground(QColor('black'))
         
         # Resize the table
         table.resizeColumnsToContents()
         table.resizeRowsToContents()
    
    def handle_calc_button(self):
        """Show the odds-converter/calculator."""
        # If it’s already open, close & re-open to reset state
        if hasattr(self, "calc_window") and self.calc_window:
            try:
                self.calc_window.close()
            except:
                pass
        self.calc_window = OddsConverterWidget()
        self.calc_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.calc_window.show()
    


    
async def main():
    app = QApplication([])
    
    window = ModernOddsWindow()
    await window.initialize()
    window.show()
    
    app.dumpObjectTree()
    app.dumpObjectInfo()
    
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop) # not necessary?
    loop.run_forever() # Run event loop
    return 


if __name__ == "__main__":
    
    asyncio.run(main(), debug=True)
