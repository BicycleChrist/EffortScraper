"""Tooltip widget for venue/marker information on the globe"""
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGraphicsDropShadowEffect
from PyQt6.QtCore import Qt, QPoint, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPen, QPainterPath


class VenueTooltip(QWidget):
    """Floating tooltip for venue and game information with styled background"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Window setup - use Popup flag for better rendering
        self.setWindowFlags(
            Qt.WindowType.ToolTip |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Colors
        self.bg_color = QColor(15, 23, 42, 245)
        self.border_color = QColor(59, 130, 246, 200)
        self.accent_color = QColor(16, 185, 129)  # Green accent

        # Main container
        self.container = QWidget(self)
        self.container.setObjectName("tooltip_container")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.addWidget(self.container)

        # Content layout
        content_layout = QVBoxLayout(self.container)
        content_layout.setContentsMargins(14, 12, 14, 12)
        content_layout.setSpacing(6)

        # Header with icon and venue name
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        # Location icon
        self.icon_label = QLabel("📍")
        self.icon_label.setFont(QFont("Segoe UI Emoji", 12))
        self.icon_label.setStyleSheet("background: transparent;")
        header_layout.addWidget(self.icon_label)

        # Venue name (title)
        self.title_label = QLabel()
        self.title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.title_label.setStyleSheet("color: #10B981; background: transparent;")
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()

        content_layout.addLayout(header_layout)

        # Separator line
        self.separator = QWidget()
        self.separator.setFixedHeight(1)
        self.separator.setStyleSheet("background-color: rgba(100, 150, 200, 100);")
        content_layout.addWidget(self.separator)

        # Game info (matchup)
        self.matchup_label = QLabel()
        self.matchup_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.matchup_label.setStyleSheet("color: #F8FAFC; background: transparent;")
        self.matchup_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.matchup_label)

        # Date/time info
        self.datetime_label = QLabel()
        self.datetime_label.setFont(QFont("Segoe UI", 10))
        self.datetime_label.setStyleSheet("color: #94A3B8; background: transparent;")
        self.datetime_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.datetime_label)

        # Additional details
        self.details_label = QLabel()
        self.details_label.setFont(QFont("Segoe UI", 9))
        self.details_label.setStyleSheet("color: #64748B; background: transparent;")
        self.details_label.setWordWrap(True)
        content_layout.addWidget(self.details_label)

        # Auto-hide timer
        self.hide_timer = QTimer(self)
        self.hide_timer.timeout.connect(self.hide)
        self.hide_timer.setSingleShot(True)

        # Add drop shadow effect
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 100))
        self.setGraphicsEffect(shadow)

    def paintEvent(self, event):
        """Custom paint for rounded background with border"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Create rounded rect path
        path = QPainterPath()
        rect = self.rect().adjusted(4, 4, -4, -4)  # Account for shadow
        path.addRoundedRect(rect.x(), rect.y(), rect.width(), rect.height(), 10, 10)

        # Fill background
        painter.fillPath(path, QBrush(self.bg_color))

        # Draw border
        painter.setPen(QPen(self.border_color, 2))
        painter.drawPath(path)

        # Draw accent line at top
        accent_path = QPainterPath()
        accent_path.moveTo(rect.x() + 10, rect.y())
        accent_path.lineTo(rect.x() + rect.width() - 10, rect.y())
        painter.setPen(QPen(self.accent_color, 3))
        painter.drawPath(accent_path)

    def show_venue(self, venue_data: dict, screen_pos: QPoint, auto_hide_ms: int = 5000):
        """Show tooltip with venue information at screen position"""
        # Get venue/city name
        name = venue_data.get('name', venue_data.get('city_name', 'Unknown Venue'))
        self.title_label.setText(name)

        # Game info if present
        game_info = venue_data.get('game_info')
        if game_info:
            game = game_info.get('game')
            away = game_info.get('away_team')
            home = game_info.get('home_team')

            # Matchup
            if away and home:
                matchup_text = f"{away.abbreviation}  @  {home.abbreviation}"
                self.matchup_label.setText(matchup_text)
                self.matchup_label.setVisible(True)
            else:
                self.matchup_label.setVisible(False)

            # Date/time
            if game and hasattr(game, 'date'):
                datetime_text = game.date.strftime("%A, %b %d  •  %I:%M %p")
                self.datetime_label.setText(datetime_text)
                self.datetime_label.setVisible(True)
            else:
                self.datetime_label.setVisible(False)

            # League badge
            league = game_info.get('league', '')
            if league:
                self.icon_label.setText(self._get_league_icon(league))
        else:
            self.matchup_label.setVisible(False)
            self.datetime_label.setVisible(False)

        # Additional details
        details_lines = []

        # Live score (flashscore feed)
        live_line = venue_data.get('live_summary')
        if live_line:
            details_lines.append(f"🔴 {live_line}")

        # Capacity if available
        capacity = venue_data.get('capacity')
        if capacity:
            details_lines.append(f"Capacity: {capacity:,}")

        # Team info if no game
        team_id = venue_data.get('team_id')
        if team_id and not game_info:
            details_lines.append(f"Team: {team_id}")

        # Schedule-fatigue intel (list of pre-formatted lines)
        for line in venue_data.get('fatigue_summary', []):
            details_lines.append(f"⚡ {line}")

        self.details_label.setText('\n'.join(details_lines))
        self.details_label.setVisible(bool(details_lines))
        self.separator.setVisible(bool(game_info))

        # Position tooltip
        self.adjustSize()
        offset = QPoint(20, 20)
        new_pos = screen_pos + offset

        # Keep tooltip on screen
        # (could add screen boundary checking here)
        self.move(new_pos)

        self.show()
        self.raise_()

        # Auto-hide
        if auto_hide_ms > 0:
            self.hide_timer.start(auto_hide_ms)

    def _get_league_icon(self, league: str) -> str:
        """Get emoji icon for league"""
        icons = {
            'MLB': '⚾',
            'NBA': '🏀',
            'NHL': '🏒',
            'NFL': '🏈'
        }
        return icons.get(league.upper(), '📍')

    def show_marker(self, marker_data: dict, screen_pos: QPoint):
        """Show tooltip for a clicked marker"""
        self.show_venue(marker_data, screen_pos)

    def hide_tooltip(self):
        """Explicitly hide the tooltip"""
        self.hide_timer.stop()
        self.hide()
