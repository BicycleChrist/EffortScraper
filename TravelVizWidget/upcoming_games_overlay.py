"""
Collapsible Upcoming Games Overlay Widget
Displays upcoming games in a toggleable overlay on the globe view
"""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QListWidget, QSpinBox, QPushButton, QFrame)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve, QPoint
from PyQt6.QtGui import QFont
from datetime import datetime, timedelta


class UpcomingGamesOverlay(QWidget):
    """Collapsible overlay showing upcoming games, positioned over globe area"""

    # Signals
    visibilityChanged = pyqtSignal(bool)  # Emitted when visibility changes
    daysFilterChanged = pyqtSignal(int)  # Emitted when days filter changes

    def __init__(self, parent=None):
        super().__init__(parent)

        self.is_expanded = True
        self.animation = None
        self.collapsed_width = 30  # Just enough for the arrow button
        self.expanded_width = 280

        self.setup_ui()
        self.setup_animations()

        # Start collapsed by default
        # Use setMinimumWidth/setMaximumWidth instead of setFixedWidth for animation compatibility
        self.setMinimumWidth(self.collapsed_width)
        self.setMaximumWidth(self.collapsed_width)
        self.is_expanded = False
        self.content_frame.setVisible(False)

        # Cache for games data
        self.cached_games = []

    def setup_ui(self):
        """Setup the overlay UI"""
        # Main layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Content frame (the part that slides in/out)
        self.content_frame = QFrame()
        self.content_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 20, 35, 240);
                border: 1px solid #374151;
                border-left: none;
                border-top-right-radius: 8px;
                border-bottom-right-radius: 8px;
            }
        """)

        content_layout = QVBoxLayout(self.content_frame)
        content_layout.setContentsMargins(8, 6, 8, 6)
        content_layout.setSpacing(5)  # Increased from 3 to give more room

        # Header - title only
        title_label = QLabel("UPCOMING GAMES")
        title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #10B981; border: none;")
        content_layout.addWidget(title_label)

        # Days filter buttons - compact preset options
        days_layout = QHBoxLayout()
        days_layout.setSpacing(3)
        days_layout.setContentsMargins(0, 0, 0, 4)  # Add bottom margin to prevent clipping

        days_label = QLabel("Show:")
        days_label.setFont(QFont("Segoe UI", 7))
        days_label.setStyleSheet("color: #9CA3AF; border: none;")
        days_layout.addWidget(days_label)

        # Create button group for days
        self.current_days = 14  # Default
        self.days_buttons = []

        button_style = """
            QPushButton {
                background-color: rgba(55, 65, 81, 180);
                border: 1px solid #4B5563;
                border-radius: 3px;
                color: #9CA3AF;
                padding: 2px 6px;
                font-size: 7pt;
                min-width: 22px;
            }
            QPushButton:hover {
                background-color: rgba(75, 85, 99, 200);
                color: #D1D5DB;
            }
            QPushButton:checked {
                background-color: #10B981;
                border-color: #059669;
                color: white;
                font-weight: bold;
            }
        """

        # Add buttons including "All" option (365 days to show full season)
        for days, label in [(7, "7d"), (14, "14d"), (30, "30d"), (365, "All")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(days == 14)  # 14 days default
            btn.setFixedHeight(18)
            btn.setStyleSheet(button_style)
            btn.clicked.connect(lambda checked, d=days: self.set_days_filter(d))
            days_layout.addWidget(btn)
            self.days_buttons.append((days, btn))

        days_layout.addStretch()
        content_layout.addLayout(days_layout)

        # Games list - optimized spacing
        self.games_list = QListWidget()
        self.games_list.setFont(QFont("Segoe UI", 8))  # Slightly larger for readability
        self.games_list.setStyleSheet("""
            QListWidget {
                background-color: rgba(31, 41, 55, 200);
                border: 1px solid #374151;
                border-radius: 4px;
                color: #E5E7EB;
                padding: 2px;
            }
            QListWidget::item {
                padding: 3px 6px;
                border-radius: 2px;
                margin: 1px 0px;
                min-height: 18px;
            }
            QListWidget::item:hover {
                background-color: rgba(59, 130, 246, 100);
            }
            QListWidget::item:selected {
                background-color: #2563EB;
                color: #FFFFFF;
            }
        """)
        content_layout.addWidget(self.games_list)

        layout.addWidget(self.content_frame)

        # Toggle button (arrow) - always visible
        self.toggle_btn = QPushButton("◀")
        self.toggle_btn.setFixedSize(28, 80)
        self.toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(31, 41, 55, 240);
                border: 1px solid #374151;
                border-left: none;
                border-top-right-radius: 8px;
                border-bottom-right-radius: 8px;
                color: #10B981;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(41, 51, 65, 240);
                color: #34D399;
            }
            QPushButton:pressed {
                background-color: rgba(21, 31, 45, 240);
            }
        """)
        self.toggle_btn.clicked.connect(self.toggle_visibility)
        layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # Set overall styling
        self.setStyleSheet("""
            QWidget {
                background-color: transparent;
            }
        """)

    def setup_animations(self):
        """Setup smooth expand/collapse animations"""
        # Use minimumWidth instead of maximumWidth for better control
        self.animation = QPropertyAnimation(self, b"minimumWidth")
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def toggle_visibility(self):
        """Toggle between expanded and collapsed states"""
        if self.is_expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self):
        """Expand the overlay to show games"""
        if self.is_expanded:
            return

        self.is_expanded = True
        self.content_frame.setVisible(True)
        self.toggle_btn.setText("◀")

        # Populate from cache when expanding
        self._populate_from_cache()

        # Animate widget width - also update maximumWidth to allow expansion
        self.setMaximumWidth(self.expanded_width)
        self.animation.setStartValue(self.collapsed_width)
        self.animation.setEndValue(self.expanded_width)
        self.animation.start()

        self.visibilityChanged.emit(True)

    def collapse(self):
        """Collapse the overlay to just show the arrow"""
        if not self.is_expanded:
            return

        self.is_expanded = False
        self.toggle_btn.setText("▶")

        # Animate widget width
        self.animation.setStartValue(self.expanded_width)
        self.animation.setEndValue(self.collapsed_width)
        self.animation.finished.connect(self._on_collapse_finished)
        self.animation.start()

        self.visibilityChanged.emit(False)

    def _on_collapse_finished(self):
        """Hide content frame after collapse animation completes"""
        if not self.is_expanded:
            self.content_frame.setVisible(False)
            # Also update maximumWidth to keep it collapsed
            self.setMaximumWidth(self.collapsed_width)
        try:
            self.animation.finished.disconnect(self._on_collapse_finished)
        except:
            pass

    def _populate_from_cache(self):
        """Populate the list from cached data"""
        print(f"🔄 Overlay: Populating list with {len(self.cached_games)} cached games")
        self.games_list.clear()
        if not self.cached_games:
            self.games_list.addItem("No upcoming games found")
            print("   ⚠️ No games in cache")
        else:
            for game in self.cached_games:
                self.games_list.addItem(game)
            print(f"   ✅ Added {len(self.cached_games)} games to list")

    def update_games(self, games_list):
        """Update the games list with new data - just cache it"""
        # Store in cache
        self.cached_games = games_list

        # Debug output
        print(f"🎮 Overlay: Received {len(games_list)} games to display")
        if games_list:
            print(f"   First game: {games_list[0]}")

        # If currently expanded, update the display immediately
        if self.is_expanded:
            self._populate_from_cache()

    def set_days_filter(self, days: int):
        """Set the days filter and update button states"""
        if self.current_days == days:
            return  # No change needed

        self.current_days = days

        # Update button states
        for btn_days, btn in self.days_buttons:
            btn.setChecked(btn_days == days)

        # Emit signal to trigger update in main window
        print(f"🔘 Days filter changed to: {days}")
        self.daysFilterChanged.emit(days)

    def set_days_ahead(self, days: int):
        """Set the days ahead value - for external API compatibility"""
        self.set_days_filter(days)

    def get_days_ahead(self) -> int:
        """Get current days ahead value"""
        return self.current_days
