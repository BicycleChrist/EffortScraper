
import sys
import math
import threading
import sqlite3
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QGridLayout, QProgressBar, QSizePolicy, QLineEdit, QPushButton, QMenu, QListWidgetItem, QListWidget
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPointF
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QLinearGradient,
    QRadialGradient, QPolygonF, QPainterPath, QAction
)

from tennis_abstract_scraper import TennisAbstractScraper, PlayerBio
from tennis_h2h_scraper import TennisScraper, PlayerRanking

#TODO: Format search bar better, avoid focus being stolen by suggestion menu.
class TennisTheme:
    """Tennis theme colors"""
    BACKGROUND = "#0A0E1A"
    SURFACE = "#1A1F2E"
    CARD_BACKGROUND = "#252B3A"
    PRIMARY = "#00D4AA"
    SECONDARY = "#FFD700"
    ACCENT = "#FF6B6B"
    
    # Surface Colors
    HARD_COURT = "#1976D2"  # Blue for hard courts
    CLAY_COURT = "#D84315"
    GRASS_COURT = "#388E3C"
    INDOOR_COURT = "#9C27B0"  # Purple for indoor
    
    # Text Colors
    TEXT_PRIMARY = "#FFFFFF"
    TEXT_SECONDARY = "#B0BEC5"
    TEXT_MUTED = "#78909C"

@dataclass
class SurfaceStats:
    """Surface performance statistics"""
    hard_wins: int = 0
    hard_total: int = 0
    clay_wins: int = 0
    clay_total: int = 0
    grass_wins: int = 0
    grass_total: int = 0
    indoor_wins: int = 0
    indoor_total: int = 0
    
    def get_percentage(self, surface: str) -> float:
        """Get win percentage for a surface"""
        if surface.lower() == 'hard':
            return (self.hard_wins / self.hard_total * 100) if self.hard_total > 0 else 0
        elif surface.lower() == 'clay':
            return (self.clay_wins / self.clay_total * 100) if self.clay_total > 0 else 0
        elif surface.lower() == 'grass':
            return (self.grass_wins / self.grass_total * 100) if self.grass_total > 0 else 0
        elif surface.lower() == 'indoor':
            return (self.indoor_wins / self.indoor_total * 100) if self.indoor_total > 0 else 0
        return 0

class CompactSurfaceWidget(QWidget):
    """Compact surface performance visualization"""
    
    def __init__(self):
        super().__init__()
        self.surface_stats = SurfaceStats()
        self.setFixedSize(130, 85)  # Reduced size to remove unused space
        self.setToolTip("Surface Performance")
        
    def update_stats(self, stats: SurfaceStats):
        """Update surface statistics"""
        self.surface_stats = stats
        self.update()
        
    def paintEvent(self, event):
        """Custom paint for surface performance"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.SURFACE))
        
        # Surface data
        surfaces = [
            ('Hard', TennisTheme.HARD_COURT, self.surface_stats.get_percentage('hard')),
            ('Clay', TennisTheme.CLAY_COURT, self.surface_stats.get_percentage('clay')),
            ('Grass', TennisTheme.GRASS_COURT, self.surface_stats.get_percentage('grass')),
            ('Indoor', TennisTheme.INDOOR_COURT, self.surface_stats.get_percentage('indoor'))
        ]
        
        # Draw compact bars
        bar_width = 26
        bar_height = 45
        spacing = 4
        start_x = 5
        start_y = 25
        
        for i, (name, color, percentage) in enumerate(surfaces):
            x = start_x + i * (bar_width + spacing)
            
            # Background bar
            painter.setPen(QPen(QColor("#2A3441"), 1))
            painter.setBrush(QBrush(QColor("#2A3441")))
            painter.drawRect(x, start_y, bar_width, bar_height)
            
            # Performance bar
            fill_height = int((percentage / 100) * bar_height)
            painter.setBrush(QBrush(QColor(color)))
            painter.drawRect(x, start_y + bar_height - fill_height, bar_width, fill_height)
            
            # Surface label
            painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            painter.setFont(QFont("Arial", 8))
            painter.drawText(x, start_y - 5, bar_width, 12, Qt.AlignmentFlag.AlignCenter, name[:1])
            
            # Percentage
            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            painter.setFont(QFont("Arial", 7, QFont.Weight.Bold))
            percentage_text = f"{percentage:.0f}%" if percentage > 0 else "0%"
            painter.drawText(x, start_y + bar_height + 5, bar_width, 10, Qt.AlignmentFlag.AlignCenter, percentage_text)

class CompactRankingChart(QWidget):
    """Compact ranking evolution chart with dual player overlay"""
    
    def __init__(self, db_path: str = "tennis_rankings.db"):
        super().__init__()
        self.db_path = db_path
        self.player1_data = []
        self.player2_data = []
        self.player1_name = ""
        self.player2_name = ""
        self.setMinimumSize(600, 220)  # Much wider for better horizontal space
        self.setStyleSheet(f"""
            CompactRankingChart {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 12px;
            }}
        """)
        
    def add_player(self, player_name: str, player_num: int):
        """Add player ranking data to chart"""
        ranking_data = self.load_player_rankings(player_name)
        
        if player_num == 1:
            self.player1_data = ranking_data
            self.player1_name = player_name
        else:
            self.player2_data = ranking_data
            self.player2_name = player_name
            
        self.update()
        
    def load_player_rankings(self, player_name: str) -> List[Tuple[str, int]]:
        """Load last 52 weeks of ranking data for player from most recent date available"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # First, get the most recent date in the database
            cursor.execute('SELECT MAX(ranking_date) FROM rankings')
            latest_date = cursor.fetchone()[0]
            
            if not latest_date:
                conn.close()
                return []
            
            # Convert to datetime to calculate 52 weeks back
            from datetime import datetime, timedelta
            latest_datetime = datetime.strptime(latest_date, '%Y-%m-%d')
            earliest_datetime = latest_datetime - timedelta(weeks=52)
            earliest_date = earliest_datetime.strftime('%Y-%m-%d')
            
            # Get ranking data for the player within the last 52 weeks
            cursor.execute('''
                SELECT ranking_date, rank
                FROM rankings 
                WHERE player_name = ? 
                AND ranking_date >= ?
                AND ranking_date <= ?
                ORDER BY ranking_date ASC
            ''', (player_name, earliest_date, latest_date))
            
            data = cursor.fetchall()
            conn.close()
            
            return data
            
        except Exception as e:
            print(f"Error loading rankings for {player_name}: {e}")
            return []
    
    def paintEvent(self, event):
        """Custom paint for ranking chart"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Fill background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))
        
        # Chart area - more space, no title, larger left margin for Y-axis labels
        left_margin = 35  # More space for Y-axis labels
        other_margin = 20
        chart_rect = self.rect().adjusted(left_margin, other_margin, -other_margin, -other_margin - 20)
        
        if not self.player1_data and not self.player2_data:
            # No data message
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 10))
            painter.drawText(chart_rect, Qt.AlignmentFlag.AlignCenter, "Select players to view rankings")
            return
        
        # Calculate scale
        all_ranks = []
        if self.player1_data:
            all_ranks.extend([rank for _, rank in self.player1_data])
        if self.player2_data:
            all_ranks.extend([rank for _, rank in self.player2_data])
            
        if not all_ranks:
            return
            
        min_rank = min(all_ranks)
        max_rank = max(all_ranks)
        rank_range = max_rank - min_rank
        
        # Add padding to range
        padding = max(1, rank_range * 0.1)
        min_rank = max(1, min_rank - padding)
        max_rank = max_rank + padding
        
        # Draw grid lines
        painter.setPen(QPen(QColor("#2A3441"), 1))
        grid_lines = 4
        for i in range(grid_lines + 1):
            y = chart_rect.top() + (chart_rect.height() * i / grid_lines)
            painter.drawLine(chart_rect.left(), int(y), chart_rect.right(), int(y))
        
        # Draw Player 1
        if self.player1_data:
            self._draw_player_line(painter, self.player1_data, chart_rect, min_rank, max_rank, 
                                 TennisTheme.PRIMARY, self.player1_name)
        
        # Draw Player 2  
        if self.player2_data:
            self._draw_player_line(painter, self.player2_data, chart_rect, min_rank, max_rank,
                                 TennisTheme.ACCENT, self.player2_name)
        
        # Draw Y-axis labels (rankings) - better positioning
        painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        painter.setFont(QFont("Arial", 10))
        for i in range(grid_lines + 1):
            rank = min_rank + (max_rank - min_rank) * (i / grid_lines)
            y = chart_rect.top() + (chart_rect.height() * i / grid_lines)
            painter.drawText(5, int(y - 7), 30, 14, Qt.AlignmentFlag.AlignCenter, f"#{int(rank)}")
        
        # Legend
        self._draw_legend(painter)
        
    def _draw_player_line(self, painter, data, chart_rect, min_rank, max_rank, color, player_name):
        """Draw ranking line for a player"""
        if len(data) < 2:
            return
            
        painter.setPen(QPen(QColor(color), 3))
        
        points = []
        for i, (date, rank) in enumerate(data):
            x = chart_rect.left() + (chart_rect.width() * i / max(len(data) - 1, 1))
            # Correct Y axis: better rank (#1) at top, worse rank (#100) at bottom
            y_ratio = (rank - min_rank) / (max_rank - min_rank)
            y = chart_rect.top() + (chart_rect.height() * y_ratio)
            points.append(QPointF(x, y))
        
        # Draw line with glow effect
        painter.setPen(QPen(QColor(color + "80"), 6))  # Semi-transparent thicker line
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])
            
        painter.setPen(QPen(QColor(color), 3))  # Main line
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])
        
        # Draw current rank point
        if points:
            painter.setBrush(QBrush(QColor(color)))
            painter.setPen(QPen(QColor(color), 2))
            last_point = points[-1]
            painter.drawEllipse(int(last_point.x() - 4), int(last_point.y() - 4), 8, 8)
    
    def _draw_legend(self, painter):
        """Draw compact legend"""
        if not self.player1_name and not self.player2_name:
            return
            
        legend_y = self.height() - 15
        x_pos = 25
        
        painter.setFont(QFont("Arial", 9))
        
        if self.player1_name:
            # Player 1 legend
            painter.setPen(QPen(QColor(TennisTheme.PRIMARY), 3))
            painter.drawLine(x_pos, legend_y, x_pos + 15, legend_y)
            painter.setPen(QColor(TennisTheme.PRIMARY))
            painter.drawText(x_pos + 20, legend_y - 6, 100, 12, Qt.AlignmentFlag.AlignLeft, 
                           self.player1_name[:12] + ("..." if len(self.player1_name) > 12 else ""))
            x_pos += 120
            
        if self.player2_name:
            # Player 2 legend
            painter.setPen(QPen(QColor(TennisTheme.ACCENT), 3))
            painter.drawLine(x_pos, legend_y, x_pos + 15, legend_y)
            painter.setPen(QColor(TennisTheme.ACCENT))
            painter.drawText(x_pos + 20, legend_y - 6, 100, 12, Qt.AlignmentFlag.AlignLeft,
                           self.player2_name[:12] + ("..." if len(self.player2_name) > 12 else ""))

class CompactPlayerSearchWidget(QWidget):
    """Dual-player search using embedded results frames - no popups, no focus issues."""

    player1Selected = pyqtSignal(str)
    player2Selected = pyqtSignal(str)
    comparisonRequested = pyqtSignal(str, str)

    def __init__(self, db_path: str = "tennis_rankings.db"):
        super().__init__()
        self.db_path = db_path
        self.players = []
        self.current_player1 = ""
        self.current_player2 = ""
        self.load_players()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)  # Much more compact margins
        layout.setSpacing(2)  # Tighter spacing

        # Main search frame - much more compact
        search_frame = QFrame()
        search_frame.setFixedHeight(60)  # Reduced from 80
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(8, 6, 8, 6)  # Much smaller margins
        search_layout.setSpacing(12)  # Reduced spacing

        # Player 1 section
        p1_layout = QVBoxLayout()
        p1_layout.setSpacing(3)  # Very tight spacing
        p1_label = QLabel("Player 1:")
        p1_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))  # Smaller font
        p1_label.setStyleSheet(f"color: #00D4AA; font-weight: bold; margin: 0px;")

        self.player1_input = QLineEdit()
        self.player1_input.setPlaceholderText("Search first player...")
        self.player1_input.textChanged.connect(lambda text: self.filter_players(text, 1))
        self.player1_input.setFixedHeight(28)  # Much smaller height
        self.player1_input.setStyleSheet("""
            QLineEdit {
                background: #1A1F2E;
                border: 2px solid #2A3441;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
                color: white;
            }
            QLineEdit:focus {
                border: 2px solid #00D4AA;
            }
        """)

        p1_layout.addWidget(p1_label)
        p1_layout.addWidget(self.player1_input)

        # VS separator - more compact
        vs_label = QLabel("VS")
        vs_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))  # Smaller font
        vs_label.setStyleSheet(f"color: #FFD700; font-weight: bold;")
        vs_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vs_label.setFixedWidth(30)  # Narrower

        # Player 2 section
        p2_layout = QVBoxLayout()
        p2_layout.setSpacing(3)  # Very tight spacing
        p2_label = QLabel("Player 2:")
        p2_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))  # Smaller font
        p2_label.setStyleSheet(f"color: #FF6B6B; font-weight: bold; margin: 0px;")

        self.player2_input = QLineEdit()
        self.player2_input.setPlaceholderText("Search second player...")
        self.player2_input.textChanged.connect(lambda text: self.filter_players(text, 2))
        self.player2_input.setFixedHeight(28)  # Much smaller height
        self.player2_input.setStyleSheet("""
            QLineEdit {
                background: #1A1F2E;
                border: 2px solid #2A3441;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
                color: white;
            }
            QLineEdit:focus {
                border: 2px solid #FF6B6B;
            }
        """)

        p2_layout.addWidget(p2_label)
        p2_layout.addWidget(self.player2_input)

        # Add to search layout (no analyze button)
        search_layout.addLayout(p1_layout, 1)
        search_layout.addWidget(vs_label)
        search_layout.addLayout(p2_layout, 1)

        # Results areas (embedded, no popups) - more compact
        self.results1_frame = QFrame()
        self.results1_layout = QVBoxLayout(self.results1_frame)
        self.results1_layout.setContentsMargins(0, 0, 0, 0)
        self.results1_layout.setSpacing(1)  # Minimal spacing
        self.results1_frame.hide()

        self.results2_frame = QFrame()
        self.results2_layout = QVBoxLayout(self.results2_frame)
        self.results2_layout.setContentsMargins(0, 0, 0, 0)
        self.results2_layout.setSpacing(1)  # Minimal spacing
        self.results2_frame.hide()

        layout.addWidget(search_frame)
        layout.addWidget(self.results1_frame)
        layout.addWidget(self.results2_frame)

    def load_players(self):
        """Load players from database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT player_name, MIN(rank) as best_rank
                FROM rankings 
                GROUP BY player_name 
                ORDER BY best_rank
            ''')
            self.players = [(name, rank) for name, rank in cursor.fetchall()]
            conn.close()
        except Exception as e:
            print(f"Error loading players: {e}")

    def filter_players(self, text, player_num):
        """Filter and show player suggestions"""
        if not text:
            if player_num == 1:
                self.results1_frame.hide()
            else:
                self.results2_frame.hide()
            return

        filtered = [(name, rank) for name, rank in self.players 
                   if text.lower() in name.lower()][:8]

        self.update_results(filtered, player_num)

    def update_results(self, filtered_players, player_num):
        """Update dropdown results with better styling"""
        if player_num == 1:
            frame = self.results1_frame
            layout = self.results1_layout
        else:
            frame = self.results2_frame
            layout = self.results2_layout

        # Clear previous
        for i in reversed(range(layout.count())):
            layout.itemAt(i).widget().setParent(None)

        if not filtered_players:
            frame.hide()
            return

        for name, rank in filtered_players:
            btn = QPushButton(f"#{rank} {name}")
            btn.clicked.connect(lambda checked, n=name, p=player_num: self.select_player(n, p))
            btn.setFixedHeight(22)  # Much smaller height
            btn.setStyleSheet(f"""
                QPushButton {{
                    text-align: left;
                    padding: 3px 8px;
                    background: #252B3A;
                    border: 1px solid #2A3441;
                    color: white;
                    font-size: 11px;
                    border-radius: 3px;
                    margin: 1px;
                }}
                QPushButton:hover {{
                    background: #00D4AA;
                    color: white;
                }}
            """)
            layout.addWidget(btn)

        frame.show()

    def select_player(self, name, player_num):
        """Select player and emit signal"""
        if player_num == 1:
            self.player1_input.setText(name)
            self.results1_frame.hide()
            self.current_player1 = name
        else:
            self.player2_input.setText(name)
            self.results2_frame.hide()
            self.current_player2 = name

        if player_num == 1:
            self.player1Selected.emit(name)
        else:
            self.player2Selected.emit(name)




class RankingGraphWidget(QWidget):
    """Compact ranking visualization with current and peak"""
    
    def __init__(self):
        super().__init__()
        self.current_rank = None
        self.peak_rank = None
        self.elo_rating = None
        self.setFixedSize(100, 80)
        
    def update_ranking(self, current_rank: Optional[int], peak_rank: Optional[int], elo_rating: Optional[int] = None):
        """Update ranking information"""
        self.current_rank = current_rank
        self.peak_rank = peak_rank
        self.elo_rating = elo_rating
        self.update()
        
    def paintEvent(self, event):
        """Custom paint for ranking display"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.SURFACE))
        
        # Current rank (main display)
        if self.current_rank:
            painter.setPen(QColor(TennisTheme.PRIMARY))
            painter.setFont(QFont("Arial", 20, QFont.Weight.Bold))
            rank_text = f"#{self.current_rank}"
            painter.drawText(5, 5, 90, 35, Qt.AlignmentFlag.AlignCenter, rank_text)
            
            # Peak rank (smaller)
            if self.peak_rank:
                painter.setPen(QColor(TennisTheme.SECONDARY))
                painter.setFont(QFont("Arial", 10))
                peak_text = f"Peak: #{self.peak_rank}"
                painter.drawText(5, 40, 90, 15, Qt.AlignmentFlag.AlignCenter, peak_text)
                
            # ELO rating (bottom)
            if self.elo_rating:
                painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
                painter.setFont(QFont("Arial", 8))
                elo_text = f"ELO: {self.elo_rating}"
                painter.drawText(5, 60, 90, 15, Qt.AlignmentFlag.AlignCenter, elo_text)
        else:
            # No ranking data
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 12))
            painter.drawText(5, 5, 90, 75, Qt.AlignmentFlag.AlignCenter, "Unranked")





class PlayerProfileWidget(QWidget):
    """Compact player profile container widget"""
    
    # Signals
    dataUpdated = pyqtSignal(str)  # player_name
    
    def __init__(self, player_color: str = TennisTheme.PRIMARY):
        super().__init__()
        self.player_color = player_color
        self.player_name = ""
        self.player_bio: Optional[PlayerBio] = None
        self.current_ranking: Optional[PlayerRanking] = None
        self.surface_stats = SurfaceStats()
        
        # Scrapers
        self.abstract_scraper = TennisAbstractScraper(headless=True)
        self.h2h_scraper = TennisScraper()
        
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the compact player profile UI"""
        self.setFixedSize(350, 200)
        self.setStyleSheet(f"""
            PlayerProfileWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {self.player_color};
                border-radius: 12px;
            }}
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        
        # Header with player name
        self.name_label = QLabel("Select Player")
        self.name_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.name_label.setStyleSheet(f"color: {self.player_color}; margin-bottom: 4px;")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Main content area
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        
        # Left side: Bio information
        bio_widget = QWidget()
        bio_layout = QVBoxLayout(bio_widget)
        bio_layout.setContentsMargins(0, 0, 0, 0)
        bio_layout.setSpacing(4)
        
        # Bio labels
        self.country_label = QLabel("Country: --")
        self.age_label = QLabel("Age: --")
        self.plays_label = QLabel("Plays: --")
        self.elo_label = QLabel("ELO: --")
        
        for label in [self.country_label, self.age_label, self.plays_label, self.elo_label]:
            label.setFont(QFont("Arial", 10))
            label.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY};")
            bio_layout.addWidget(label)
            
        bio_layout.addStretch()
        
        # Center: Ranking display
        self.ranking_widget = RankingGraphWidget()
        
        # Right: Surface performance
        self.surface_widget = CompactSurfaceWidget()
        
        # Assemble content
        content_layout.addWidget(bio_widget, 1)
        content_layout.addWidget(self.ranking_widget)
        content_layout.addWidget(self.surface_widget)
        
        # Recent form at bottom
        form_layout = QHBoxLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        
        form_title = QLabel("Recent Form:")
        form_title.setFont(QFont("Arial", 10))
        form_title.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY};")
        
        self.form_display = QLabel("🔴🔴🔴🔴🔴")
        self.form_display.setFont(QFont("Arial", 10))
        
        form_layout.addWidget(form_title)
        form_layout.addWidget(self.form_display)
        form_layout.addStretch()
        
        # Assemble main layout
        layout.addWidget(self.name_label)
        layout.addWidget(content_widget, 1)
        layout.addLayout(form_layout)
        
    def set_player(self, player_name: str):
        """Set the player and load their data"""
        if player_name == self.player_name:
            return
            
        self.player_name = player_name
        self.name_label.setText(player_name)
        
        # Reset display
        self.reset_display()
        
        # Load data asynchronously
        self.load_player_data()
        
    def reset_display(self):
        """Reset all displays to default state"""
        self.country_label.setText("Country: Loading...")
        self.age_label.setText("Age: Loading...")
        self.plays_label.setText("Plays: Loading...")
        self.elo_label.setText("ELO: Loading...")
        self.ranking_widget.update_ranking(None, None)
        self.surface_widget.update_stats(SurfaceStats())
        self.form_display.setText("Loading...")
        
    def load_player_data(self):
        """Load player data from multiple sources"""
        def background_load():
            try:
                # Load Tennis Abstract data
                formatted_name = self.player_name.replace(' ', '')
                url = f"https://www.tennisabstract.com/cgi-bin/player.cgi?p={formatted_name}"
                
                player_data = self.abstract_scraper._scrape_player_page(url)
                
                if player_data and player_data.player_bio:
                    self.player_bio = player_data.player_bio
                    
                    # Update bio info
                    self.country_label.setText(f"Country: {player_data.player_bio.country}")
                    self.age_label.setText(f"Age: {player_data.player_bio.age}")
                    self.plays_label.setText(f"Plays: {player_data.player_bio.plays}")
                    
                    # Parse ELO rating
                    try:
                        elo_rating = int(player_data.player_bio.elo_rating) if player_data.player_bio.elo_rating else None
                        self.elo_label.setText(f"ELO: {elo_rating}" if elo_rating else "ELO: --")
                    except:
                        self.elo_label.setText("ELO: --")
                    
                    # Parse ranking info
                    try:
                        current_rank = int(player_data.player_bio.current_rank) if player_data.player_bio.current_rank.isdigit() else None
                        peak_rank = int(player_data.player_bio.peak_rank) if player_data.player_bio.peak_rank.isdigit() else None
                        self.ranking_widget.update_ranking(current_rank, peak_rank, elo_rating)
                    except:
                        pass
                    
                    # Extract surface stats from career splits
                    if hasattr(player_data, 'career_splits') and player_data.career_splits:
                        surface_stats = self.extract_surface_stats(player_data.career_splits)
                        self.surface_widget.update_stats(surface_stats)
                    
                    # Extract recent form from recent results
                    if hasattr(player_data, 'recent_results') and player_data.recent_results:
                        recent_form = []
                        for result in player_data.recent_results[-8:]:  # Last 8 matches
                            # Analyze score to determine win/loss
                            if hasattr(result, 'score') and result.score:
                                score = result.score.strip()
                                # If score has sets like "6-4 6-2", analyze first player perspective
                                # Tennis Abstract shows results from player's perspective
                                if self.is_winning_score(score):
                                    recent_form.append('W')
                                else:
                                    recent_form.append('L')
                            else:
                                recent_form.append('?')  # Unknown result
                        self.update_form(recent_form)
                    
                # Load ATP ranking data
                rankings = self.h2h_scraper.get_atp_rankings_sync(top_n=1000)
                player_ranking = self.h2h_scraper.find_player_ranking(self.player_name, rankings)
                
                if player_ranking:
                    self.current_ranking = player_ranking
                    # Update with ATP data if Abstract data wasn't available
                    if not self.player_bio:
                        self.ranking_widget.update_ranking(player_ranking.rank, None)
                        
                # Emit signal that data is updated
                self.dataUpdated.emit(self.player_name)
                
            except Exception as e:
                print(f"Error loading player data for {self.player_name}: {e}")
                self.country_label.setText("Country: Error")
                self.age_label.setText("Age: Error")
                self.plays_label.setText("Plays: Error")
                self.elo_label.setText("ELO: Error")
                
        # Run in background thread
        thread = threading.Thread(target=background_load, daemon=True)
        thread.start()
        
    def extract_surface_stats(self, career_splits: List) -> SurfaceStats:
        """Extract surface statistics from career splits data"""
        stats = SurfaceStats()
        
        for split in career_splits:
            split_name = split.split.lower()
            
            # Parse wins-losses from matches
            try:
                wins = int(split.wins) if split.wins.isdigit() else 0
                total = int(split.matches) if split.matches.isdigit() else 0
                
                if 'hard' in split_name or 'outdoor hard' in split_name:
                    stats.hard_wins += wins
                    stats.hard_total += total
                elif 'clay' in split_name:
                    stats.clay_wins += wins
                    stats.clay_total += total
                elif 'grass' in split_name:
                    stats.grass_wins += wins
                    stats.grass_total += total
                elif 'indoor' in split_name or 'carpet' in split_name:
                    stats.indoor_wins += wins
                    stats.indoor_total += total
                    
            except (ValueError, AttributeError):
                continue
                
        return stats
    
    def is_winning_score(self, score: str) -> bool:
        """Analyze tennis score to determine if it's a win from player's perspective"""
        try:
            # Handle common score formats from Tennis Abstract
            if not score or score == "--":
                return False
                
            # Remove common suffixes
            score = score.replace(" (ret.)", "").replace(" (wo)", "").strip()
            
            # Split by spaces to get sets
            sets = score.split()
            if not sets:
                return False
                
            player_sets = 0
            opponent_sets = 0
            
            for set_score in sets:
                if '-' in set_score:
                    try:
                        # Handle formats like "6-4", "7-6(3)", etc.
                        parts = set_score.split('-')
                        if len(parts) == 2:
                            # Remove tiebreak info like "(3)"
                            player_games = int(parts[0].split('(')[0])
                            opponent_games = int(parts[1].split('(')[0])
                            
                            if player_games > opponent_games:
                                player_sets += 1
                            elif opponent_games > player_games:
                                opponent_sets += 1
                    except ValueError:
                        continue
                        
            # Player wins if they won more sets
            return player_sets > opponent_sets
            
        except Exception:
            return False
        
    def update_form(self, recent_form: List[str]):
        """Update recent form display"""
        if not recent_form:
            self.form_display.setText("No data")
            return
            
        form_display = ""
        for result in recent_form[-5:]:  # Last 5 matches
            if result.upper() == 'W':
                form_display += "🟢"
            elif result.upper() == 'L':
                form_display += "🔴"
            else:
                form_display += "⚫"  # Unknown/no data
                
        self.form_display.setText(form_display)

class CompactTennisComparisonWidget(QWidget):
    """Main container combining search and player profile widgets"""
    
    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.setup_connections()
        
    def setup_ui(self):
        """Setup the complete comparison interface"""
        self.setWindowTitle("Compact Tennis Player Comparison")
        self.setGeometry(100, 100, 750, 470)  # More height for larger ranking chart
        self.setStyleSheet(f"background: {TennisTheme.BACKGROUND};")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # Search widget
        self.search_widget = CompactPlayerSearchWidget()
        
        # Player profile widgets
        profiles_layout = QHBoxLayout()
        profiles_layout.setSpacing(15)
        
        self.player1_widget = PlayerProfileWidget(TennisTheme.PRIMARY)
        self.player2_widget = PlayerProfileWidget(TennisTheme.ACCENT)
        
        profiles_layout.addWidget(self.player1_widget)
        profiles_layout.addWidget(self.player2_widget)
        
        # Rankings chart widget
        self.ranking_chart = CompactRankingChart()
        
        # Bottom section with ranking chart taking full width
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(0)
        bottom_layout.addWidget(self.ranking_chart)
        
        # Status label
        self.status_label = QLabel("Select two players to compare")
        self.status_label.setFont(QFont("Arial", 11))
        self.status_label.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY}; margin-top: 4px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        layout.addWidget(self.search_widget)
        layout.addLayout(profiles_layout)
        layout.addLayout(bottom_layout)
        layout.addWidget(self.status_label)
        
    def setup_connections(self):
        """Connect search signals to player widgets"""
        self.search_widget.player1Selected.connect(self.on_player1_selected)
        self.search_widget.player2Selected.connect(self.on_player2_selected)
        
    def on_player1_selected(self, player_name: str):
        """Handle player 1 selection"""
        self.player1_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 1)
        self.update_status()
        
    def on_player2_selected(self, player_name: str):
        """Handle player 2 selection"""
        self.player2_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 2)
        self.update_status()
        
    def update_status(self):
        """Update status label"""
        p1_name = self.player1_widget.player_name
        p2_name = self.player2_widget.player_name
        
        if p1_name and p2_name:
            self.status_label.setText(f"Comparing: {p1_name} vs {p2_name}")
        elif p1_name:
            self.status_label.setText(f"Player 1: {p1_name} | Select Player 2")
        elif p2_name:
            self.status_label.setText(f"Player 2: {p2_name} | Select Player 1")
        else:
            self.status_label.setText("Select two players to compare")

# Test application
if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Create main comparison widget
    window = CompactTennisComparisonWidget()
    window.show()
    
    sys.exit(app.exec())
