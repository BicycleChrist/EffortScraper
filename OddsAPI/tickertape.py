from datetime import datetime
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QLinearGradient, QRadialGradient, QPainterPath, QFontDatabase
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import QRectF, QPointF
from livetape_scraper import scrape_espn_scores, scrape_all_sports
import threading
import platform

# Movie box office information that can be gathered as why the hell not (polymarket specific)
# Display boxscore data alongside game score data that gathered from livescore_scraper
class TickerTape(QWidget):
    """Advanced ESPN-style ticker with segmented sports and ultra-smooth scrolling"""

    def __init__(self, parent=None, transition_style="flip_card", news_widget=None):
        super().__init__(parent)
        self.news_widget = news_widget
        self.loading_attempted = False

        # Dwell-based crossfade display (replaces continuous scroll).
        # Each item is shown fully static for `dwell_ms`, then crossfades to the
        # next over `crossfade_ms`. No per-frame scroll => repaints happen only
        # during the brief fade, so a UI-thread block almost never coincides with
        # a paint, and when it does an opacity jump is imperceptible.
        self.dwell_ms = 8000          # how long each item stays fully readable
        self.crossfade_ms = 150       # fade duration between items
        self._text_opacity = 1.0      # 0..1 fade-in of the current item
        self._prev_text = ""          # outgoing text, drawn during the fade
        self._ticker_running = False
        self.is_paused = False
        # kept only so legacy reset_scroll_position()/resizeEvent() stay safe
        self._scroll_position = 0.0

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

        # Custom font loading with fallback
        #custom_font_name = "Z003-MediumItalic.otf"
        custom_font_name = "URWBookman-DemiItalic.otf"
        custom_font_path = f"custom_fonts/{custom_font_name}"

        print(f"TickerTape: Attempting to load custom font '{custom_font_name}' from '{custom_font_path}'")

        try:
            # this fuckaround is required to load a font from a file
            # font_id = QFontDatabase.addApplicationFont(f"custom_fonts/{custom_font_name}")
            # _fontstr = QFontDatabase.applicationFontFamilies(font_id)[0]
            # custom_font = QFont(_fontstr, 15)

            font_id = QFontDatabase.addApplicationFont(custom_font_path)
            if font_id != -1:
                font_families = QFontDatabase.applicationFontFamilies(font_id)
                if font_families:
                    _fontstr = font_families[0]
                    custom_font = QFont(_fontstr, 15)
                    print(f"TickerTape: ✓ Custom font '{_fontstr}' loaded successfully")
                else:
                    raise Exception("No font families found")
            else:
                raise Exception("Font file could not be loaded")
        except Exception as e:
            print(f"TickerTape: ✗ Custom font loading failed: {e}")
            print(f"TickerTape: → Using fallback font 'Arial'")
            print(f"TickerTape: → To use custom fonts, ensure '{custom_font_path}' exists and is accessible")
            custom_font = QFont("Arial", 15)

        self.sport_font = QFont("Roboto", 14, QFont.Weight.ExtraBold)
        # self.game_font = QFont("Roboto", 15, QFont.Weight.Bold)
        self.game_font = custom_font

        # Additional font settings for crisp, clean appearance
        self.sport_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self.game_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self.sport_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.game_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)

        # Dwell timer: single-shot, fires when the current item's read time is up
        self.dwell_timer = QTimer()
        self.dwell_timer.setSingleShot(True)
        self.dwell_timer.timeout.connect(self._begin_text_transition)

        # Crossfade animation drives text_opacity 0 -> 1 for the incoming item
        self.text_fade_anim = QPropertyAnimation(self, b"text_opacity")
        self.text_fade_anim.setDuration(self.crossfade_ms)
        self.text_fade_anim.setStartValue(0.0)
        self.text_fade_anim.setEndValue(1.0)
        self.text_fade_anim.finished.connect(self._on_text_fade_done)

        # Transition animation (left league badge flip — independent of text)
        self.transition_animation = QPropertyAnimation(self, b"transition_progress")
        self.transition_animation.setDuration(300)  # 300ms transition
        self.transition_animation.finished.connect(self.on_transition_finished)

        # Widget properties - increased height for larger fonts
        self.setMinimumHeight(55)
        self.setMaximumHeight(55)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

        # Initialize content
        self.update_current_text()

        # Cache text width (retained for legacy callers)
        self.cached_text_width = 0
        self.update_text_width()

        # Store previous sport info for transitions
        self.previous_sport_index = 0
        self._transition_progress = 0.0

        # Live data functionality
        self.data_loaded = False

        # Graphics caches - invalidated only when sport/size changes
        self._cached_gradients = None
        self._cached_sport_path = None
        self._cache_key = None

        # Load scores immediately, no delays
        QTimer.singleShot(500, self.load_live_scores)  # Load scores first

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

    @pyqtProperty(float)
    def text_opacity(self):
        return self._text_opacity

    @text_opacity.setter
    def text_opacity(self, value):
        self._text_opacity = value
        self.update()

    def reset_scroll_position(self):
        """Reset scroll position to start from the right edge"""
        self._scroll_position = self.width()

    def update_current_text(self):
        """Update the current text to display"""
        sports = list(self.sports_data.keys())
        if not sports:
            self.current_text = "Loading..."
            self.update_text_width()
            return

        # Ensure index is in bounds
        if self.current_sport_index >= len(sports):
            self.current_sport_index = 0

        sport = sports[self.current_sport_index]
        games = self.sports_data[sport]["games"]

        if games:
            game = games[self.current_game_index % len(games)]
            self.current_text = game  # Remove sport prefix since league segment shows it
        else:
            self.current_text = "No games available"

        # Update cached width when text changes
        self.update_text_width()
        # Invalidate graphics cache when sport changes
        self._cached_gradients = None
        self._cached_sport_path = None

    def update_text_width(self):
        """Cache the text width to avoid calculating it every frame"""
        font_metrics = QFontMetrics(self.game_font)
        self.cached_text_width = font_metrics.horizontalAdvance(self.current_text)

    def rotate_content(self):
        """Rotate through different sports and games with balanced cycling"""
        sports = list(self.sports_data.keys())
        if not sports:
            return

        # Ensure index is in bounds
        if self.current_sport_index >= len(sports):
            self.current_sport_index = 0

        sport = sports[self.current_sport_index]
        games = self.sports_data[sport]["games"]

        self.current_game_index += 1

        # Limit games per sport to ensure fair cycling between sports
        # Show more items per sport to properly cycle through content
        max_items_per_sport = 5

        if self.current_game_index >= min(len(games), max_items_per_sport):
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

    def start_ticker(self):
        """Begin the dwell/crossfade cycle (idempotent — safe to call repeatedly
        from the various data loaders)."""
        if self._ticker_running:
            return
        self._ticker_running = True
        self._text_opacity = 1.0
        self.dwell_timer.start(self.dwell_ms)

    def _begin_text_transition(self):
        """Dwell elapsed: advance to the next item and crossfade it in."""
        if self.is_paused or not self.current_text:
            # paused or nothing to show — re-arm and try again later
            self.dwell_timer.start(self.dwell_ms)
            return

        self._prev_text = self.current_text
        self.rotate_content()           # advance sport/game index
        self.update_current_text()      # ensure current_text is fresh now
                                        # (rotate_content defers it on sport change)
        self.text_fade_anim.stop()
        self.text_fade_anim.setDuration(self.crossfade_ms)
        self._text_opacity = 0.0
        self.text_fade_anim.start()

    def _on_text_fade_done(self):
        """Crossfade finished: settle on the new item and re-arm the dwell."""
        self._text_opacity = 1.0
        self._prev_text = ""
        self.update()
        if not self.is_paused:
            self.dwell_timer.start(self.dwell_ms)

    def enterEvent(self, event):
        """Pause cycling while hovered so a long item can be read in full."""
        self.is_paused = True
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Resume cycling when the cursor leaves."""
        self.is_paused = False
        if self._ticker_running and not self.dwell_timer.isActive() \
                and self.text_fade_anim.state() != QPropertyAnimation.State.Running:
            self.dwell_timer.start(self.dwell_ms)
        super().leaveEvent(event)

    def get_current_sport_info(self):
        """Get current sport color information"""
        sports = list(self.sports_data.keys())
        if not sports:
            # Return default sport info when no data available
            return {
                "color": QColor("#2C3E50"),
                "accent": QColor("#3498DB"),
                "icon": "🌐",
                "games": ["Loading..."]
            }
        # Ensure index is in bounds
        if self.current_sport_index >= len(sports):
            self.current_sport_index = 0
        sport = sports[self.current_sport_index]
        return self.sports_data[sport]

    def paintEvent(self, event):
        """Paint the ESPN-style ticker with smooth animations"""
        painter = QPainter(self)
        try:
            # Windows-specific performance optimization
            if platform.system() == "Windows":
                # Only use essential antialiasing on Windows
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            else:
                # Full antialiasing on Linux/Mac
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

            rect = self.rect()
            sport_info = self.get_current_sport_info()

            # Cache graphics objects when sport or size changes
            cache_key = (self.current_sport_index, rect.width(), rect.height())
            if self._cache_key != cache_key or self._cached_gradients is None:
                self._create_cached_graphics(rect, sport_info)
                self._cache_key = cache_key

            # Draw sophisticated background with sport-specific colors
            self.draw_background(painter, rect, sport_info)

            # Draw scrolling game content FIRST (underneath everything)
            self.draw_scrolling_content(painter, rect, sport_info)

            # Draw sport segment OVER the scrolling text (like an overlay)
            self.draw_sport_segment(painter, rect, sport_info)

            # Draw accent elements and effects on top
            self.draw_accent_effects(painter, rect, sport_info)
        except Exception as e:
            print(f"TickerTape paintEvent error: {e}")
        finally:
            painter.end()

    def _create_cached_graphics(self, rect, sport_info):
        """Create and cache gradients and paths"""
        self._cached_gradients = {
            'background': QLinearGradient(0, 0, rect.width(), 0),
            'glow': QRadialGradient(self.segment_width/2, rect.height()/2, self.segment_width),
            'top_accent': QLinearGradient(0, 0, rect.width(), 0),
            'bottom_accent': QLinearGradient(0, rect.height()-3, rect.width(), rect.height()-3)
        }

        # Background gradient
        bg = self._cached_gradients['background']
        bg.setColorAt(0, sport_info["color"])
        bg.setColorAt(0.3, sport_info["color"].darker(150))
        bg.setColorAt(0.6, QColor("#2C3E50"))
        bg.setColorAt(1, QColor("#34495E"))

        # Glow gradient
        glow = self._cached_gradients['glow']
        glow.setColorAt(0, QColor(sport_info["accent"].red(), sport_info["accent"].green(),
                                  sport_info["accent"].blue(), 30))
        glow.setColorAt(1, QColor(0, 0, 0, 0))

        # Top accent gradient
        top = self._cached_gradients['top_accent']
        top.setColorAt(0, sport_info["accent"])
        top.setColorAt(0.7, sport_info["accent"].darker(200))
        top.setColorAt(1, QColor(0, 0, 0, 0))

        # Bottom accent gradient
        bottom = self._cached_gradients['bottom_accent']
        bottom.setColorAt(0, sport_info["accent"])
        bottom.setColorAt(0.7, sport_info["accent"].darker(200))
        bottom.setColorAt(1, QColor(0, 0, 0, 0))

        # Sport segment path
        self._cached_sport_path = QPainterPath()
        self._cached_sport_path.moveTo(0, 0)
        self._cached_sport_path.lineTo(self.segment_width - 15, 0)
        self._cached_sport_path.lineTo(self.segment_width, rect.height())
        self._cached_sport_path.lineTo(0, rect.height())
        self._cached_sport_path.closeSubpath()

    def draw_background(self, painter, rect, sport_info):
        """Draw gradient background using cached gradients"""
        painter.fillRect(rect, self._cached_gradients['background'])
        painter.fillRect(0, 0, self.segment_width * 2, rect.height(), self._cached_gradients['glow'])

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
        """Draw normal sport segment using cached path"""
        painter.fillPath(self._cached_sport_path, sport_info["color"])

        # Sport segment border
        painter.setPen(QPen(sport_info["accent"], 2))
        painter.drawPath(self._cached_sport_path)

        # Draw sport icon and text
        self.draw_sport_text(painter, rect, sport_info)

    def draw_sport_text(self, painter, rect, sport_info):
        """Draw sport icon and text with shadow"""
        painter.setFont(self.sport_font)

        sports = list(self.sports_data.keys())
        if not sports or self.current_sport_index >= len(sports):
            sport_name = ""
        else:
            sport_name = sports[self.current_sport_index]
        icon = sport_info["icon"]

        # Center the text in the segment
        text_rect = QRectF(5, 0, self.segment_width - 20, rect.height())
        shadow_rect = QRectF(6, 1, self.segment_width - 20, rect.height())

        # Draw text shadow
        painter.setPen(QColor("#000000"))
        painter.drawText(shadow_rect, Qt.AlignmentFlag.AlignCenter, f"{icon} {sport_name}")

        # Draw main text
        painter.setPen(QColor("#3d9300"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, f"{icon} {sport_name}")

    def draw_flip_card_transition(self, painter, rect):
        """ESPN-style flip card transition"""
        painter.save()

        # Calculate flip angle (0 to 180 degrees)
        angle = self._transition_progress * 180

        # Get sport info for old and new sports
        sports = list(self.sports_data.keys())
        if not sports:
            painter.restore()
            return
        # Ensure indices are in bounds
        prev_idx = min(self.previous_sport_index, len(sports) - 1)
        curr_idx = min(self.current_sport_index, len(sports) - 1)
        old_sport_info = self.sports_data[sports[prev_idx]]
        new_sport_info = self.sports_data[sports[curr_idx]]

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
        if not sports:
            painter.restore()
            return
        # Ensure indices are in bounds
        prev_idx = min(self.previous_sport_index, len(sports) - 1)
        curr_idx = min(self.current_sport_index, len(sports) - 1)
        old_sport_info = self.sports_data[sports[prev_idx]]
        new_sport_info = self.sports_data[sports[curr_idx]]

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
        """Draw the current item statically, crossfading from the previous one.
        Text is left-aligned in the content area (right of the badge) and
        auto-shrunk to fit, so nothing has to move to be fully readable."""
        content_left = self.segment_width + 15
        content_rect = QRectF(content_left, 0,
                              max(0, rect.width() - content_left - 15), rect.height())

        painter.save()
        painter.setClipRect(content_rect)

        op = max(0.0, min(1.0, self._text_opacity))
        if op < 1.0 and self._prev_text:
            self._draw_centered_text(painter, content_rect, self._prev_text, 1.0 - op)
        self._draw_centered_text(painter, content_rect, self.current_text, op)

        painter.restore()

    def _fit_game_font(self, text, avail_w):
        """Shrink the game font until `text` fits within `avail_w` (down to 9pt)."""
        base = self.game_font.pointSize()
        if base <= 0:
            base = 15
        f = QFont(self.game_font)
        size = base
        while size > 9:
            f.setPointSize(size)
            if QFontMetrics(f).horizontalAdvance(text) <= avail_w:
                break
            size -= 1
        return f

    def _draw_centered_text(self, painter, rect, text, alpha):
        """Draw `text` vertically centered, left-aligned, at the given 0..1 alpha."""
        if not text or alpha <= 0.0:
            return
        a = int(255 * max(0.0, min(1.0, alpha)))
        painter.setFont(self._fit_game_font(text, rect.width()))

        flags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

        # shadow
        painter.setPen(QColor(0, 0, 0, int(a * 0.6)))
        painter.drawText(rect.translated(1, 1), flags, text)

        # main text
        painter.setPen(QColor(255, 255, 255, a))
        painter.drawText(rect, flags, text)

    def draw_accent_effects(self, painter, rect, sport_info):
        """Draw accent lines using cached gradients"""
        painter.fillRect(0, 0, rect.width(), 3, self._cached_gradients['top_accent'])
        painter.fillRect(0, rect.height()-3, rect.width(), 3, self._cached_gradients['bottom_accent'])


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
        if not self.news_widget:
            print("No news widget available")
            return []

        if not hasattr(self.news_widget, 'worker'):
            print("News widget has no worker")
            return []

        # Access the worker's news_items
        worker = self.news_widget.worker
        if not hasattr(worker, 'news_items'):
            print("Worker has no news_items attribute")
            return []

        if not worker.news_items:
            print(f"Worker news_items is empty: {worker.news_items}")
            return []

        print(f"Found {len(worker.news_items)} news items in worker")

        headlines = []
        news_items = worker.news_items

        # Get more headlines - prioritize by injury score first, then by date (newest first)
        # Keep only ticker-worthy items. The NewsWorker flags low-value /
        # fluff headlines (speculation, listicles, fantasy advice) as
        # ticker_worthy=False; they stay visible in the news widget but are
        # kept out of the ticker. Older payloads lack the flag -> treat as True.
        ticker_news = [x for x in news_items if x.get('ticker_worthy', True)]
        if not ticker_news:
            ticker_news = news_items
        sorted_news = sorted(ticker_news,
                           key=lambda x: (x.get('relevance_score', x.get('injury_score', 0)), x.get('date', datetime.min)),
                           reverse=True)

        # Process ALL available headlines for categorization
        all_headlines = []
        for item in sorted_news:  # Categorize all headlines first
            title = item.get('title', '')
            if not title:
                continue

            # Clean up "None:" prefix that comes from RSS feeds
            if title.startswith("None: "):
                title = title[6:]  # Remove "None: " (6 characters)

            all_headlines.append(title)

        # Skip duplicate filtering if we're during heavy operations to avoid UI blocking
        # Duplicate filtering will be done async when headlines are actually used
        # all_headlines = self.filter_duplicate_headlines(all_headlines)

        print(f"Total headlines available for categorization: {len(all_headlines)}")

        # Now categorize all headlines and select best ones
        categorized_headlines = {"NFL": [], "NBA": [], "MLB": [], "NHL": []}

        for headline in all_headlines:
            sport = self.categorize_headline_by_content(headline)
            if sport:
                categorized_headlines[sport].append(headline)

        # Select balanced representation from each sport (max 10 per sport)
        selected_headlines = []
        for sport, headlines_list in categorized_headlines.items():
            # Take up to 10 best headlines per sport (injury-scored items first)
            selected_from_sport = headlines_list[:10]
            selected_headlines.extend(selected_from_sport)
            print(f"Selected {len(selected_from_sport)} headlines from {sport} (out of {len(headlines_list)} available)")

        # If we have fewer than 40, fill with remaining high-injury-score headlines
        if len(selected_headlines) < 40:
            remaining_count = 40 - len(selected_headlines)
            print(f"Filling remaining {remaining_count} slots with uncategorized high-priority headlines")

            # Get uncategorized headlines
            uncategorized = []
            for headline in all_headlines:
                sport = self.categorize_headline_by_content(headline)
                if not sport:
                    uncategorized.append(headline)

            # Add uncategorized headlines to reach 40 total
            selected_headlines.extend(uncategorized[:remaining_count])

        print(f"Final selection: {len(selected_headlines)} headlines for ticker")
        return selected_headlines[:40]  # Ensure we don't exceed 40

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
        """Load live scores from all major sports in background thread (non-blocking)"""
        print("Loading live scores from all sports...")

        def fetch_scores_background():
            try:
                all_sports_scores = scrape_all_sports()

                if all_sports_scores:
                    # Store the data and signal main thread
                    self.pending_scores = all_sports_scores
                    # Use metaObject().invokeMethod for thread-safe Qt calls
                    QTimer.singleShot(0, self.process_pending_scores)
                    total_games = sum(len(sport_data['games']) for sport_data in all_sports_scores.values())
                    print(f"Loaded {total_games} games across {len(all_sports_scores)} sports")
                else:
                    print("No live games found in any sport")
                    # CRITICAL FIX: Load headlines even when no live scores exist
                    QTimer.singleShot(0, self.handle_no_live_scores)
            except Exception as e:
                print(f"Error loading scores: {e}")
                # CRITICAL FIX: Load headlines even when error occurs
                QTimer.singleShot(0, self.handle_no_live_scores)

        # Run in background thread
        threading.Thread(target=fetch_scores_background, daemon=True).start()

    def process_pending_scores(self):
        """Process scores on main thread"""
        if hasattr(self, 'pending_scores'):
            self.add_scores_to_ticker(self.pending_scores)
            delattr(self, 'pending_scores')

    def format_mlb_stats(self, top_players):
        """Format MLB stats for ticker display - both SPs, top hitters from each team, and saves"""
        if not top_players:
            return ""

        stats_parts = []

        # Both starting pitchers
        if top_players['away_team'].get('pitcher'):
            pitcher = top_players['away_team']['pitcher']
            stats_parts.append(f"{pitcher['name']}: {pitcher['stats']}")

        if top_players['home_team'].get('pitcher'):
            pitcher = top_players['home_team']['pitcher']
            stats_parts.append(f"{pitcher['name']}: {pitcher['stats']}")

        # Top hitter from each team (prioritize HRs and hits)
        for team_key in ['away_team', 'home_team']:
            hitters = top_players[team_key].get('hitters', [])
            if hitters:
                # Get top hitter (already sorted by weighted score: hits, RBI, HR)
                top_hitter = hitters[0]
                stats_parts.append(f"{top_hitter['name']}: {top_hitter['stats']}")

        # Add closer/save if available (usually only for winning team in final games)
        for team_key in ['away_team', 'home_team']:
            if top_players[team_key].get('closer'):
                closer = top_players[team_key]['closer']
                stats_parts.append(f"{closer['name']}: {closer['stats']}")

        return " | " + " • ".join(stats_parts) if stats_parts else ""

    def format_nba_stats(self, top_players):
        """Format NBA stats for ticker display - top scorers from both teams + outlier performances"""
        if not top_players:
            return ""

        stats_parts = []

        # Get top scorer from each team + check for outlier performances
        for team_key in ['away_team', 'home_team']:
            players = top_players[team_key].get('players', [])
            if not players:
                continue

            # Show top scorer from this team
            top_player = players[0]
            stats_parts.append(f"{top_player['name']}: {top_player['stats']}")

            # Also check other players on team for outliers (30+ pts, 15+ reb, 12+ ast)
            for player in players[1:]:
                pts = int(player['stats'].split(' PTS')[0].split()[-1] if 'PTS' in player['stats'] else '0')
                reb = int(player['stats'].split(' REB')[0].split(',')[-1].strip() if 'REB' in player['stats'] else '0')
                ast = int(player['stats'].split(' AST')[0].split(',')[-1].strip() if 'AST' in player['stats'] else '0')

                # If outlier performance, show it
                if pts >= 30 or reb >= 15 or ast >= 12:
                    stats_parts.append(f"{player['name']}: {player['stats']}")

        return " | " + " • ".join(stats_parts) if stats_parts else ""

    def format_nfl_stats(self, top_players):
        """Format NFL stats for ticker display - both QBs, top WR and RB from each team"""
        if not top_players:
            return ""

        stats_parts = []

        # Both QBs
        for team_key in ['away_team', 'home_team']:
            if top_players[team_key].get('qb'):
                qb = top_players[team_key]['qb']
                stats_parts.append(f"{qb['name']}: {qb['stats']}")

        # Top rusher from each team
        for team_key in ['away_team', 'home_team']:
            if top_players[team_key].get('rusher'):
                rusher = top_players[team_key]['rusher']
                stats_parts.append(f"{rusher['name']} (RB): {rusher['stats']}")

        # Top receiver from each team
        for team_key in ['away_team', 'home_team']:
            if top_players[team_key].get('receiver'):
                receiver = top_players[team_key]['receiver']
                stats_parts.append(f"{receiver['name']} (WR): {receiver['stats']}")

        return " | " + " • ".join(stats_parts) if stats_parts else ""

    def format_nhl_stats(self, top_players):
        """Format NHL stats for ticker display - both goalies and top scorers from both teams"""
        if not top_players:
            return ""

        stats_parts = []

        # Both goalies
        for team_key in ['away_team', 'home_team']:
            if top_players[team_key].get('goalie'):
                goalie = top_players[team_key]['goalie']
                stats_parts.append(f"{goalie['name']} (G): {goalie['stats']}")

        # Top scorers from each team (only if they scored goals)
        for team_key in ['away_team', 'home_team']:
            scorers = top_players[team_key].get('scorers', [])
            if scorers:
                # Only show top scorer if they actually scored (scorers list only contains players with goals > 0)
                top_scorer = scorers[0]
                stats_parts.append(f"{top_scorer['name']}: {top_scorer['stats']}")

        return " | " + " • ".join(stats_parts) if stats_parts else ""

    def add_scores_to_ticker(self, all_sports_scores):
        """Add live scores for all sports to ticker data (runs on main thread)"""
        if all_sports_scores:
            total_games = sum(len(sport_data['games']) for sport_data in all_sports_scores.values())
            print(f"Adding {total_games} scores from {len(all_sports_scores)} sports to ticker...")

            # Remove loading animation data first
            if "" in self.sports_data:
                del self.sports_data[""]

            # Sport configurations with colors
            sport_configs = {
                "MLB": {"color": QColor("#132448"), "accent": QColor("#BF0D3E")},
                "NBA": {"color": QColor("#C8102E"), "accent": QColor("#1D428A")},
                "NFL": {"color": QColor("#013369"), "accent": QColor("#D50A0A")},
                "NHL": {"color": QColor("#000000"), "accent": QColor("#F99923")}
            }

            # Create new sports data with live scores first
            new_sports_data = {}

            for sport_name, sport_data in all_sports_scores.items():
                games = sport_data['games']
                icon = sport_data['icon']
                config = sport_configs.get(sport_name, {"color": QColor("#2C3E50"), "accent": QColor("#3498DB")})

                # Convert scores to ticker format
                score_items = []
                for game in games:
                    away_team = game['away_team']
                    home_team = game['home_team']
                    away_runs = game['away_score']['runs']
                    home_runs = game['home_score']['runs']
                    status = game.get('status', 'Unknown')

                    # Get stats if available
                    stats_text = ""
                    if 'top_players' in game and game['top_players']:
                        if sport_name == 'MLB':
                            stats_text = self.format_mlb_stats(game['top_players'])
                        elif sport_name == 'NBA':
                            stats_text = self.format_nba_stats(game['top_players'])
                        elif sport_name == 'NFL':
                            stats_text = self.format_nfl_stats(game['top_players'])
                        elif sport_name == 'NHL':
                            stats_text = self.format_nhl_stats(game['top_players'])

                    # Format score display based on game status
                    if status == 'Final':
                        score_text = f"{away_team} {away_runs} - {home_team} {home_runs} | Final{stats_text}"
                    elif status == 'Pre-Game' or status == 'Unknown' or 'TBD' in status or away_runs == '-' or home_runs == '-':
                        # Pre-game: no scores yet or scores are placeholders
                        score_text = f"{away_team} vs {home_team} - Starting Soon"
                    else:
                        # Live game or finished game with status - display status with scores and stats
                        score_text = f"{away_team} {away_runs} - {home_team} {home_runs} | {status}{stats_text}"

                    score_items.append(score_text)

                new_sports_data[sport_name] = {
                    "color": config["color"],
                    "accent": config["accent"],
                    "icon": icon,
                    "games": score_items
                }

            # Add any existing non-score sports after (like PRED)
            for sport, data in self.sports_data.items():
                if sport not in new_sports_data and sport != "":
                    new_sports_data[sport] = data

            self.sports_data = new_sports_data

            # Reset to show first sport
            self.current_sport_index = 0
            self.current_game_index = 0

            # Stop loading animation and start ticker
            self.stop_loading_animation()
            self.start_ticker()
            self.data_loaded = True

            self.update_current_text()
            print(f"✓ Live scores added - {total_games} games across {len(all_sports_scores)} sports")
            print(f"Current sports: {list(self.sports_data.keys())}")
            print(f"Current text: {self.current_text[:50]}...")

            # Load headlines after scores are added to ensure proper cycling
            # Give news widget more time to load, then retry if needed
            QTimer.singleShot(5000, self.load_headlines_for_cycling)

    def handle_no_live_scores(self):
        """Handle case when no live scores exist - load headlines immediately"""
        print("No live scores found - loading headlines immediately...")

        # Remove loading animation
        if "" in self.sports_data:
            del self.sports_data[""]

        # Stop loading animation
        self.stop_loading_animation()

        # Start animation timer
        self.start_ticker()

        self.data_loaded = True

        # Load headlines with a short delay to allow news widget to initialize
        QTimer.singleShot(2000, self.load_headlines_for_cycling)

    def load_headlines_for_cycling(self, retry_count=0):
        """Load headlines from news widget for cycling between sports"""
        headlines = self.get_headlines_from_news_widget()
        if headlines:
            print(f"Loading {len(headlines)} headlines for sport cycling...")
            self.populate_ticker_with_headlines(headlines)
        else:
            print(f"No headlines available from news widget (attempt {retry_count + 1})")
            # Retry up to 3 times with longer delays
            if retry_count < 3:
                delay = 10000 + (retry_count * 5000)  # 10s, 15s, 20s
                print(f"Retrying in {delay/1000}s...")
                QTimer.singleShot(delay, lambda: self.load_headlines_for_cycling(retry_count + 1))

    def categorize_headline_by_content(self, headline):
        """Improved headline categorization using multiple signals and confidence scoring"""
        headline_lower = headline.lower()

        # Enhanced league-specific terminology with better signal quality
        league_signals = {
            "NBA": {
                "league_names": ["nba", "basketball"],  # 12 points each
                "unique_terms": ["three-pointer", "dunk", "rebound", "assist", "block", "steal", "buzzer beater",
                               "fast break", "alley-oop", "free throw", "technical foul", "flagrant", "and-one"],  # 8 points each
                "positions": ["PG", "SG", "SF", "PF", "C", "point guard", "shooting guard", "small forward",
                            "power forward", "center", "sixth man"],  # 6 points each
                "concepts": ["salary cap", "luxury tax", "lottery pick", "summer league", "g league", "all-star",
                           "finals mvp", "rookie of the year"],  # 6 points each
                "mega_stars": ["lebron", "curry", "durant", "giannis", "luka", "tatum", "jokic", "embiid",
                             "kawhi", "harden"],  # 10 points each
                "unique_cities": ["los angeles lakers", "golden state", "milwaukee", "phoenix suns"]  # 7 points each
            },
            "NFL": {
                "league_names": ["nfl", "football"],  # 12 points each
                "unique_terms": ["touchdown", "quarterback", "endzone", "snap count", "blitz", "sack", "interception",
                               "gridiron", "pigskin", "redzone", "fourth down", "field goal", "punt", "fumble"],  # 8 points each
                "positions": ["QB", "RB", "WR", "TE", "OL", "DL", "LB", "CB", "S", "quarterback", "running back",
                            "wide receiver", "linebacker", "defensive back", "tight end"],  # 6 points each
                "concepts": ["draft combine", "hard knocks", "pro bowl", "super bowl", "nfc", "afc", "wildcard"],  # 6 points each
                "mega_stars": ["mahomes", "brady", "rodgers", "josh allen", "jalen hurts", "lane johnson",
                             "lamar jackson", "joe burrow", "dak prescott", "russell wilson"],  # 10 points each
                "unique_cities": ["green bay", "kansas city", "buffalo", "tampa bay", "philadelphia", "pittsburgh"]  # 7 points each
            },
            "MLB": {
                "league_names": ["mlb", "baseball"],  # 12 points each
                "unique_terms": ["home run", "strikeout", "ERA", "RBI", "batting average", "bullpen", "closer",
                               "grand slam", "double play", "triple play", "balk", "wild pitch", "stolen base"],  # 8 points each
                "positions": ["pitcher", "catcher", "shortstop", "designated hitter", "DH", "closer", "setup man",
                            "first base", "second base", "third base", "outfielder"],  # 6 points each
                "concepts": ["spring training", "world series", "cy young", "mvp", "rookie of the year",
                           "all-star game", "trade deadline", "waiver wire"],  # 6 points each
                "mega_stars": ["ohtani", "judge", "trout", "soto", "betts", "tatis", "guerrero", "acuna",
                             "freeman", "machado"],  # 10 points each
                "unique_cities": ["yankee stadium", "fenway park", "dodger stadium", "wrigley field"]  # 7 points each
            },
            "NHL": {
                "league_names": ["nhl", "hockey"],  # 12 points each
                "unique_terms": ["goal", "assist", "power play", "penalty kill", "face-off", "icing", "offside",
                               "hat trick", "one-timer", "slap shot", "wrist shot", "breakaway", "shootout"],  # 8 points each
                "positions": ["goalie", "defenseman", "winger", "center", "left wing", "right wing", "d-man",
                            "netminder", "tender"],  # 6 points each
                "concepts": ["stanley cup", "overtime", "shootout", "playoffs", "draft lottery", "trade deadline",
                           "all-star game", "calder trophy", "hart trophy"],  # 6 points each
                "mega_stars": ["mcdavid", "ovechkin", "pastrnak", "kane", "crosby", "mackinnon", "draisaitl",
                             "matthews", "hedman", "vasilevskiy"],  # 10 points each
                "unique_cities": ["edmonton", "winnipeg", "calgary", "vancouver", "montreal", "ottawa", "toronto"]  # 7 points each
            }
        }

        best_league = None
        best_score = 0

        # Score each league using weighted signals
        for league, signals in league_signals.items():
            score = 0

            # League names (12 points each - high confidence)
            for term in signals["league_names"]:
                if term in headline_lower:
                    score += 12

            # Unique sport terminology (8 points each)
            for term in signals["unique_terms"]:
                if term in headline_lower:
                    score += 8

            # Position names (6 points each)
            for term in signals["positions"]:
                if term in headline_lower:
                    score += 6

            # Sport concepts (6 points each)
            for term in signals["concepts"]:
                if term in headline_lower:
                    score += 6

            # Mega stars (10 points each - high confidence)
            for star in signals["mega_stars"]:
                if star in headline_lower:
                    score += 10

            # Unique city identifiers (7 points each)
            for city in signals["unique_cities"]:
                if city in headline_lower:
                    score += 7

            # Team names from news widget (15 points each - highest weight)
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
                        score += 15

            if score > best_score:
                best_score = score
                best_league = league

        # Require higher confidence threshold to avoid misclassification
        return best_league if best_score >= 10 else None

    def filter_duplicate_headlines(self, headlines):
        """Filter out duplicate headlines using optimized approach to minimize UI blocking"""
        import difflib
        import re

        if not headlines:
            return headlines

        print(f"📰 Starting optimized duplicate filtering for {len(headlines)} headlines...")

        def extract_key_terms(headline):
            """Extract key terms from headline for comparison"""
            # Remove common words and extract meaningful terms
            stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'vs', 'beat', 'defeat', 'win', 'lose'}
            words = re.findall(r'\w+', headline.lower())
            return set(word for word in words if len(word) > 2 and word not in stop_words)

        # First pass: exact duplicates (very fast)
        seen_exact = set()
        unique_headlines = []
        for headline in headlines:
            if headline not in seen_exact:
                unique_headlines.append(headline)
                seen_exact.add(headline)

        if len(unique_headlines) < len(headlines):
            print(f"🔄 Removed {len(headlines) - len(unique_headlines)} exact duplicates")

        # For very large lists, use a more efficient approach
        if len(unique_headlines) > 200:
            print(f"📊 Using fast mode for {len(unique_headlines)} headlines")
            # Fast mode: Only check substring matches and first 3 key terms
            final_headlines = []
            seen_terms = set()

            for headline in unique_headlines:
                # Extract first 3 key terms as a signature
                terms = extract_key_terms(headline)
                if not terms:
                    final_headlines.append(headline)
                    continue

                # Create a signature from the most common terms
                signature = tuple(sorted(list(terms))[:3])

                if signature not in seen_terms:
                    final_headlines.append(headline)
                    seen_terms.add(signature)

            print(f"📰 Fast filtering complete: {len(unique_headlines)} → {len(final_headlines)} headlines")
            return final_headlines

        # Standard mode for smaller lists: fuzzy matching
        final_headlines = []
        seen_headlines = []

        for i, headline in enumerate(unique_headlines):
            if i % 25 == 0:  # Less frequent progress updates
                print(f"📊 Processing headline {i}/{len(unique_headlines)}")

            is_duplicate = False
            current_terms = extract_key_terms(headline)

            # Compare with existing headlines (limit comparisons to avoid O(n²) complexity)
            comparison_limit = min(50, len(seen_headlines))  # Only compare with last 50 headlines

            for existing in seen_headlines[-comparison_limit:]:
                # Calculate similarity ratio (more sensitive threshold)
                similarity = difflib.SequenceMatcher(None, headline.lower(), existing.lower()).ratio()

                # Also check for common key terms
                existing_terms = extract_key_terms(existing)
                if current_terms and existing_terms:
                    term_overlap = len(current_terms & existing_terms) / len(current_terms | existing_terms)
                else:
                    term_overlap = 0

                # Consider duplicates if high similarity OR high term overlap
                if similarity > 0.75 or term_overlap > 0.6:
                    is_duplicate = True
                    break

            if not is_duplicate:
                final_headlines.append(headline)
                seen_headlines.append(headline)

        print(f"📰 Duplicate filtering complete: {len(unique_headlines)} → {len(final_headlines)} headlines")
        return final_headlines

    def populate_ticker_with_headlines(self, headlines):
        """Populate ticker with headlines properly categorized by content"""
        print(f"🔄 Processing {len(headlines)} headlines synchronously on main thread...")

        # Skip filtering for now to isolate the issue
        # filtered_headlines = self.filter_duplicate_headlines(headlines)

        # Process directly on main thread
        self._continue_populate_ticker(headlines)

    def _continue_populate_ticker(self, headlines):
        """Continue ticker population after async duplicate filtering"""
        print(f"🎯 _continue_populate_ticker called with {len(headlines)} headlines")

        # League configurations
        league_configs = {
            "MLB": (QColor("#132448"), QColor("#BF0D3E"), "⚾"),
            "NBA": (QColor("#C8102E"), QColor("#1D428A"), "🏀"),
            "NFL": (QColor("#013369"), QColor("#D50A0A"), "🏈"),
            "NHL": (QColor("#000000"), QColor("#F99923"), "🏒"),
            "PRED": (QColor("#6A0DAD"), QColor("#FF6B35"), "🔮")  # Purple/Orange for prediction markets
        }

        # Categorize headlines by content
        categorized_headlines = {league: [] for league in league_configs.keys()}

        for headline in headlines:
            league = self.categorize_headline_by_content(headline)
            if league:  # Only add if we have a confident categorization
                categorized_headlines[league].append(headline)
                print(f"Categorized: '{headline[:50]}...' -> {league}")
            else:
                print(f"Skipped (low confidence): '{headline[:50]}...'")

        # Print categorization summary
        for league, headlines_list in categorized_headlines.items():
            print(f"{league}: {len(headlines_list)} headlines")

        # Build sports data, preserving existing live scores and maintaining order
        new_sports_data = {}

        # First, preserve ALL existing data in order (especially live scores)
        for existing_league, existing_data in self.sports_data.items():
            if existing_league != "":  # Skip loading animation
                new_sports_data[existing_league] = existing_data.copy()

        # Then merge in headlines for matching leagues OR add default news for empty leagues
        for league_name, (color, accent, icon) in league_configs.items():
            league_headlines = categorized_headlines[league_name]
            print(f"🔍 Processing {league_name}: {len(league_headlines)} headlines available")

            if league_name in new_sports_data:
                # Add headlines to existing league data (scores already there)
                existing_games = new_sports_data[league_name]["games"]
                if league_headlines:
                    new_sports_data[league_name]["games"] = existing_games + league_headlines
                    print(f"✅ Merged {len(league_headlines)} headlines with existing {league_name} data")
                else:
                    print(f"⚠️ No headlines to merge for existing {league_name}")
            else:
                # Only create new league if we have real headlines
                if league_headlines:
                    new_sports_data[league_name] = {
                        "color": color,
                        "accent": accent,
                        "icon": icon,
                        "games": league_headlines
                    }
                    print(f"✅ Created {league_name} with {len(league_headlines)} headlines")
                else:
                    print(f"❌ Skipping {league_name} - no headlines available")

        if new_sports_data:
            print(f"Before headline merge - current sports: {list(self.sports_data.keys())}")
            print(f"After headline merge - new sports: {list(new_sports_data.keys())}")

            # Update with merged data
            self.sports_data = new_sports_data
            print(f"✓ Sports data updated - now contains: {list(self.sports_data.keys())}")
            self.update_current_text()

            # Start animation timer if not already running
            self.start_ticker()

            total_headlines = sum(len(headlines) for headlines in categorized_headlines.values())
            print(f"✓ Headlines merged - {total_headlines} headlines across {len(new_sports_data)} leagues")
            print(f"Final sports order: {list(self.sports_data.keys())}")

            # Check if MLB still has scores
            if "MLB" in self.sports_data:
                mlb_games = self.sports_data["MLB"]["games"]
                score_count = sum(1 for game in mlb_games if " - " in game and ("Final" in game or "vs" in game or any(inning in game for inning in ["Top", "Bot", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th"])))
                print(f"MLB has {len(mlb_games)} total items, {score_count} appear to be scores")

    def add_prediction_markets(self, prediction_markets):
        """Add prediction market data to the ticker"""
        if not prediction_markets:
            print("No prediction markets to add")
            return

        print(f"Adding {len(prediction_markets)} prediction markets to ticker...")
        print(f"📊 Sports data before adding PRED: {list(self.sports_data.keys())}")

        # Create or update PRED section
        pred_config = {
            "color": QColor("#6A0DAD"),  # Purple
            "accent": QColor("#FF6B35"),  # Orange
            "icon": "🔮",
            "games": prediction_markets
        }

        # Add PRED section to existing sports data
        self.sports_data["PRED"] = pred_config

        # Update current text if needed
        self.update_current_text()

        # Start animation timer if not already running
        self.start_ticker()

        print(f"✓ Prediction markets added - PRED section with {len(prediction_markets)} markets")
        print(f"Updated sports order: {list(self.sports_data.keys())}")
