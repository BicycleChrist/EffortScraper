import sys
import math
import time
import threading
import sqlite3
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import traceback

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QGridLayout, QProgressBar, QSizePolicy, QLineEdit, QPushButton, QMenu, QListWidgetItem, QListWidget,
    QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPointF, QRect
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QLinearGradient,
    QRadialGradient, QPolygonF, QPainterPath, QAction
)

from tennis_abstract_scraper import TennisAbstractScraper, PlayerBio, TacticsData
from tennis_h2h_scraper import TennisScraper, PlayerRanking

#TODO: Display Serve data,fix I-Hard surface dissappearing in career surface widget display

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
    """Compact surface performance visualization - FRESH IMPLEMENTATION"""
    
    def __init__(self):
        super().__init__()
        self.surface_stats = SurfaceStats()
        self.setFixedSize(140, 85)
        self.setToolTip("Surface Performance")
        self.is_populated = False
        print(f"NEW CompactSurfaceWidget initialized - Size: 180x85")
        
    def update_stats_from_yearly_data(self, yearly_stats: dict):
        """Update surface statistics from H2H yearly stats - SINGLE UPDATE ONLY"""
        if self.is_populated:
            print("WARNING: CompactSurfaceWidget already populated, ignoring update")
            return
            
        print(f"CompactSurfaceWidget.update_stats_from_yearly_data called")
        print(f"Available years: {list(yearly_stats.keys())}")
        
        # Get last 3 years of data, or use career totals if insufficient
        current_year = 2025
        last_3_years = [str(current_year), str(current_year-1), str(current_year-2)]
        
        available_years = [year for year in last_3_years if year in yearly_stats]
        print(f"Available years in last 3: {available_years}")
        
        if len(available_years) >= 3:
            # Use last 3 years data
            print("Using last 3 years of data")
            data_source = "Last 3 Years"
            years_to_use = available_years
        else:
            # Use career totals
            print("Insufficient recent data, using career totals")
            data_source = "Career Totals"
            if "Year Total" in yearly_stats:
                years_to_use = ["Year Total"]
            else:
                print("ERROR: No career totals available")
                return
                
        # Aggregate surface stats from selected years
        stats = SurfaceStats()
        
        for year in years_to_use:
            year_data = yearly_stats[year]
            print(f"Processing year {year}: {year_data}")
            
            for surface_key, record in year_data.items():
                if not record or record == "--" or record == "0-0":
                    continue
                    
                if '-' not in record:
                    continue
                    
                try:
                    wins_str, losses_str = record.split('-')
                    wins = int(wins_str.strip())
                    losses = int(losses_str.strip())
                    
                    surface_lower = surface_key.lower()
                    
                    if surface_lower == 'hard':
                        stats.hard_wins += wins
                        stats.hard_total += (wins + losses)
                        print(f"  HARD: +{wins}/{wins+losses} -> Total: {stats.hard_wins}/{stats.hard_total}")
                    elif surface_lower == 'clay':
                        stats.clay_wins += wins
                        stats.clay_total += (wins + losses)
                        print(f"  CLAY: +{wins}/{wins+losses} -> Total: {stats.clay_wins}/{stats.clay_total}")
                    elif surface_lower == 'grass':
                        stats.grass_wins += wins
                        stats.grass_total += (wins + losses)
                        print(f"  GRASS: +{wins}/{wins+losses} -> Total: {stats.grass_wins}/{stats.grass_total}")
                    elif surface_lower == 'i.hard':
                        stats.indoor_wins += wins
                        stats.indoor_total += (wins + losses)
                        print(f"  I.HARD: +{wins}/{wins+losses} -> Total: {stats.indoor_wins}/{stats.indoor_total}")
                        
                except (ValueError, AttributeError) as e:
                    print(f"  ERROR parsing {surface_key}: {record} - {e}")
                    continue
                    
        print(f"FINAL STATS ({data_source}):")
        print(f"  Hard: {stats.hard_wins}/{stats.hard_total} ({stats.get_percentage('hard'):.1f}%)")
        print(f"  Clay: {stats.clay_wins}/{stats.clay_total} ({stats.get_percentage('clay'):.1f}%)")
        print(f"  Grass: {stats.grass_wins}/{stats.grass_total} ({stats.get_percentage('grass'):.1f}%)")
        print(f"  Indoor: {stats.indoor_wins}/{stats.indoor_total} ({stats.get_percentage('indoor'):.1f}%)")
        
        self.surface_stats = stats
        self.is_populated = True
        print("CompactSurfaceWidget populated and locked")
        self.update()
        
    def paintEvent(self, event):
        """Custom paint for surface performance"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.SURFACE))
        
        # Career label at the top
        painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        painter.setFont(QFont("Arial", 7))
        painter.drawText(2, 2, 156, 10, Qt.AlignmentFlag.AlignCenter, "Trailing 3y Surface W%")
        
        # Surface data - ALL 4 SURFACES ALWAYS
        surfaces = [
            ('Hard', TennisTheme.HARD_COURT, self.surface_stats.get_percentage('hard')),
            ('Clay', TennisTheme.CLAY_COURT, self.surface_stats.get_percentage('clay')),
            ('Grass', TennisTheme.GRASS_COURT, self.surface_stats.get_percentage('grass')),
            ('Indoor', TennisTheme.INDOOR_COURT, self.surface_stats.get_percentage('indoor'))
        ]
        
        print(f"PAINTING 4 surfaces:")
        for i, (name, color, percentage) in enumerate(surfaces):
            print(f"  {i}: {name} = {percentage:.1f}%")
        
        # Draw compact bars
        bar_width = 28
        bar_height = 45
        spacing = 3
        start_x = 8
        start_y = 25
        
        for i, (name, color, percentage) in enumerate(surfaces):
            x = start_x + i * (bar_width + spacing)
            print(f"  Drawing {name} at x={x} (width={bar_width})")
            
            # Background bar
            painter.setPen(QPen(QColor("#2A3441"), 1))
            painter.setBrush(QBrush(QColor("#2A3441")))
            painter.drawRect(x, start_y, bar_width, bar_height)
            
            # Performance bar
            fill_height = max(1, int((percentage / 100) * bar_height)) if percentage > 0 else 0
            if fill_height > 0:
                painter.setBrush(QBrush(QColor(color)))
                painter.drawRect(x, start_y + bar_height - fill_height, bar_width, fill_height)
            
            # Surface label
            painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            painter.setFont(QFont("Arial", 7))
            label = "I.Hard" if name == "Indoor" else name[:1]
            painter.drawText(x, start_y - 5, bar_width, 12, Qt.AlignmentFlag.AlignCenter, label)
            
            # Percentage
            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            painter.setFont(QFont("Arial", 7, QFont.Weight.Bold))
            percentage_text = f"{percentage:.0f}%"
            painter.drawText(x, start_y + bar_height + 5, bar_width, 10, Qt.AlignmentFlag.AlignCenter, percentage_text)

class HistoricalSurfaceTableWidget(QWidget):
    """Historical surface performance table widget matching Tennis Tonic style"""
    
    def __init__(self):
        super().__init__()
        self.player1_yearly_stats = {}
        self.player2_yearly_stats = {}
        self.player1_name = ""
        self.player2_name = ""
        self.setFixedSize(600, 240)  # Reduced width for more compact layout
        self.setStyleSheet(f"""
            HistoricalSurfaceTableWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 8px;
            }}
        """)
        
    def update_player_stats(self, player_name: str, yearly_stats: dict, player_num: int):
        """Update yearly surface statistics for a specific player"""
        if player_num == 1:
            self.player1_yearly_stats = yearly_stats
            self.player1_name = player_name
        else:
            self.player2_yearly_stats = yearly_stats
            self.player2_name = player_name
        self.update()
        
    def paintEvent(self, event):
        """Custom paint for historical surface table"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))
        
        # Draw two side-by-side tables with no spacing
        self.draw_player_table(painter, self.player1_name, self.player1_yearly_stats, 0, 0, 300, TennisTheme.PRIMARY)
        self.draw_player_table(painter, self.player2_name, self.player2_yearly_stats, 300, 0, 300, TennisTheme.ACCENT)
        
    def draw_player_table(self, painter, player_name, yearly_stats, x_offset, y_offset, width, accent_color):
        """Draw historical surface table for one player"""
        # Header background
        header_color = QColor("#2A3441") 
        painter.fillRect(x_offset, y_offset, width, 30, header_color)
        
        # Player name in header
        painter.setPen(QColor(accent_color))
        painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        painter.drawText(x_offset + 10, y_offset + 20, player_name if player_name else "Select Player")
        
        # Table headers
        y = y_offset + 30
        header_height = 25
        painter.fillRect(x_offset, y, width, header_height, QColor("#1A1F2E"))
        
        painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        
        # Column headers
        headers = ["Year", "Sum", "Hard", "Clay", "I.hard", "Grass"]
        col_widths = [45, 50, 50, 45, 50, 50]
        x_pos = x_offset + 5
        
        for header, col_width in zip(headers, col_widths):
            painter.drawText(x_pos, y + 17, header)
            x_pos += col_width
            
        # Draw data rows
        if yearly_stats:
            # Sort years in descending order
            sorted_years = sorted([year for year in yearly_stats.keys() if year != "Year Total"], reverse=True)
            
            row_y = y + header_height
            row_height = 22
            
            # Data rows
            for i, year in enumerate(sorted_years[:8]):  # Show last 8 years
                if i % 2 == 0:
                    painter.fillRect(x_offset, row_y, width, row_height, QColor("#252B3A"))
                else:
                    painter.fillRect(x_offset, row_y, width, row_height, QColor("#1E242F"))
                
                year_data = yearly_stats[year]
                
                painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
                painter.setFont(QFont("Arial", 9))
                
                # Year
                painter.drawText(x_offset + 5, row_y + 15, year)
                
                # Data columns
                data_values = [
                    year_data.get('Sum.', '0-0'),
                    year_data.get('Hard', '0-0'),  
                    year_data.get('Clay', '0-0'),
                    year_data.get('I.hard', '0-0'),
                    year_data.get('Grass', '0-0')
                ]
                
                x_pos = x_offset + 50
                for value, col_width in zip(data_values, col_widths[1:]):
                    # Color code based on performance
                    if '-' in value and value != '0-0':
                        wins, losses = value.split('-')
                        try:
                            win_pct = int(wins) / (int(wins) + int(losses)) if int(wins) + int(losses) > 0 else 0
                            if win_pct >= 0.7:
                                painter.setPen(QColor("#4CAF50"))  # Green for good performance
                            elif win_pct >= 0.5:
                                painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))  # Normal
                            else:
                                painter.setPen(QColor("#FF6B6B"))  # Red for poor performance
                        except:
                            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
                    else:
                        painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                    
                    painter.drawText(x_pos, row_y + 15, value)
                    x_pos += col_width
                
                row_y += row_height
            
            # Total row (if available)
            if "Year Total" in yearly_stats:
                # Separator line
                painter.setPen(QColor(accent_color))
                painter.drawLine(x_offset, row_y, x_offset + width, row_y)
                row_y += 2
                
                # Total row background
                painter.fillRect(x_offset, row_y, width, row_height, QColor("#2A3441"))
                
                total_data = yearly_stats["Year Total"]
                
                painter.setPen(QColor(accent_color))
                painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
                painter.drawText(x_offset + 5, row_y + 15, "Total")
                
                # Total data
                total_values = [
                    total_data.get('Sum.', '0-0'),
                    total_data.get('Hard', '0-0'),
                    total_data.get('Clay', '0-0'), 
                    total_data.get('I.hard', '0-0'),
                    total_data.get('Grass', '0-0')
                ]
                
                x_pos = x_offset + 50
                for value, col_width in zip(total_values, col_widths[1:]):
                    painter.drawText(x_pos, row_y + 15, value)
                    x_pos += col_width

class CompactRankingChart(QWidget):
    """Compact ranking evolution chart with dual player overlay"""
    
    def __init__(self, db_path: str = "tennis_rankings.db"):
        super().__init__()
        self.db_path = db_path
        self.player1_data = []
        self.player2_data = []
        self.player1_name = ""
        self.player2_name = ""
        self.player1_points = []  # Store point coordinates for hover detection
        self.player2_points = []
        self.hover_point = None  # Current hovered point info
        self.setMinimumSize(900, 220)  # Match surface table width
        self.setMouseTracking(True)  # Enable mouse tracking for hover
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
        
    def load_player_rankings(self, player_name: str) -> List[Tuple[str, int, int, int]]:
        """Load last 52 weeks of ranking data for player with points and rank changes"""
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
            
            # Get ranking data with points and rank changes for the player within the last 52 weeks
            cursor.execute('''
                SELECT ranking_date, rank, points, rank_change
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
        
        # Draw legend at the top first
        self._draw_legend(painter)
        
        # Chart area - adjusted margins for legend at top and X-axis labels at bottom
        left_margin = 35  # More space for Y-axis labels
        top_margin = 40   # Space for legend at top
        bottom_margin = 35  # Space for X-axis date labels
        right_margin = 20
        chart_rect = self.rect().adjusted(left_margin, top_margin, -right_margin, -bottom_margin)
        
        if not self.player1_data and not self.player2_data:
            # No data message
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 10))
            painter.drawText(chart_rect, Qt.AlignmentFlag.AlignCenter, "Select players to view rankings")
            return
        
        # Calculate scale
        all_ranks = []
        if self.player1_data:
            all_ranks.extend([rank for _, rank, _, _ in self.player1_data])
        if self.player2_data:
            all_ranks.extend([rank for _, rank, _, _ in self.player2_data])
            
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
        
        # Draw X-axis date labels
        self._draw_x_axis_dates(painter, chart_rect)
        
    def _draw_player_line(self, painter, data, chart_rect, min_rank, max_rank, color, player_name):
        """Draw ranking line for a player with data points"""
        if len(data) < 2:
            return
            
        painter.setPen(QPen(QColor(color), 3))
        
        points = []
        point_data = []  # Store data for hover detection
        
        for i, (date, rank, points_val, rank_change) in enumerate(data):
            x = chart_rect.left() + (chart_rect.width() * i / max(len(data) - 1, 1))
            # Correct Y axis: better rank (#1) at top, worse rank (#100) at bottom
            y_ratio = (rank - min_rank) / (max_rank - min_rank)
            y = chart_rect.top() + (chart_rect.height() * y_ratio)
            point = QPointF(x, y)
            points.append(point)
            point_data.append((date, rank, points_val, rank_change, point))
        
        # Store points for hover detection
        if player_name == self.player1_name:
            self.player1_points = point_data
        else:
            self.player2_points = point_data
        
        # Draw line with glow effect
        painter.setPen(QPen(QColor(color + "80"), 6))  # Semi-transparent thicker line
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])
            
        painter.setPen(QPen(QColor(color), 3))  # Main line
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])
        
        # Draw data point dots
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(QPen(QColor(color), 2))
        for point in points:
            painter.drawEllipse(int(point.x() - 3), int(point.y() - 3), 6, 6)
        
        # Highlight current rank point with larger dot
        if points:
            painter.setBrush(QBrush(QColor(color)))
            painter.setPen(QPen(QColor(color), 2))
            last_point = points[-1]
            painter.drawEllipse(int(last_point.x() - 4), int(last_point.y() - 4), 8, 8)
    
    def _draw_legend(self, painter):
        """Draw compact legend at the top"""
        if not self.player1_name and not self.player2_name:
            return
            
        legend_y = 20  # Position at top
        x_pos = 35
        
        painter.setFont(QFont("Arial", 9))
        
        if self.player1_name:
            # Player 1 legend
            painter.setPen(QPen(QColor(TennisTheme.PRIMARY), 3))
            painter.drawLine(x_pos, legend_y, x_pos + 15, legend_y)
            painter.setPen(QColor(TennisTheme.PRIMARY))
            painter.drawText(x_pos + 20, legend_y - 6, 100, 12, Qt.AlignmentFlag.AlignLeft, 
                           self.player1_name[:12] + ("..." if len(self.player1_name) > 12 else ""))
            x_pos += 140
            
        if self.player2_name:
            # Player 2 legend
            painter.setPen(QPen(QColor(TennisTheme.ACCENT), 3))
            painter.drawLine(x_pos, legend_y, x_pos + 15, legend_y)
            painter.setPen(QColor(TennisTheme.ACCENT))
            painter.drawText(x_pos + 20, legend_y - 6, 100, 12, Qt.AlignmentFlag.AlignLeft,
                           self.player2_name[:12] + ("..." if len(self.player2_name) > 12 else ""))
    
    def _draw_x_axis_dates(self, painter, chart_rect):
        """Draw X-axis date labels"""
        # Use the longer dataset to determine date positions
        data_to_use = self.player1_data if len(self.player1_data) >= len(self.player2_data) else self.player2_data
        
        if not data_to_use:
            return
            
        painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        painter.setFont(QFont("Arial", 8))
        
        # Show dates at regular intervals
        num_labels = min(6, len(data_to_use))  # Show max 6 date labels
        step = max(1, len(data_to_use) // num_labels)
        
        for i in range(0, len(data_to_use), step):
            if i < len(data_to_use):
                date, _, _, _ = data_to_use[i]
                x = chart_rect.left() + (chart_rect.width() * i / max(len(data_to_use) - 1, 1))
                
                # Format date (show month/year)
                try:
                    from datetime import datetime
                    date_obj = datetime.strptime(date, '%Y-%m-%d')
                    formatted_date = date_obj.strftime('%m/%y')
                except:
                    formatted_date = date[-5:]  # Fallback to last 5 chars
                
                painter.drawText(int(x - 15), chart_rect.bottom() + 20, 30, 12, 
                               Qt.AlignmentFlag.AlignCenter, formatted_date)
    
    def mouseMoveEvent(self, event):
        """Handle mouse movement for hover tooltips"""
        mouse_pos = event.pos()
        hover_found = False
        
        # Check player 1 points
        for date, rank, points_val, rank_change, point in self.player1_points:
            if self._is_point_hovered(mouse_pos, point):
                self.hover_point = {
                    'date': date,
                    'rank': rank, 
                    'points': points_val,
                    'rank_change': rank_change,
                    'player': self.player1_name,
                    'color': TennisTheme.PRIMARY,
                    'pos': point
                }
                hover_found = True
                break
        
        # Check player 2 points if no player 1 hover found
        if not hover_found:
            for date, rank, points_val, rank_change, point in self.player2_points:
                if self._is_point_hovered(mouse_pos, point):
                    self.hover_point = {
                        'date': date,
                        'rank': rank,
                        'points': points_val, 
                        'rank_change': rank_change,
                        'player': self.player2_name,
                        'color': TennisTheme.ACCENT,
                        'pos': point
                    }
                    hover_found = True
                    break
        
        if not hover_found:
            self.hover_point = None
            
        self.update()  # Trigger repaint to show/hide tooltip
    
    def _is_point_hovered(self, mouse_pos, point):
        """Check if mouse is hovering over a data point"""
        hover_radius = 8  # Hover detection radius
        distance = ((mouse_pos.x() - point.x()) ** 2 + (mouse_pos.y() - point.y()) ** 2) ** 0.5
        return distance <= hover_radius
    
    def paintEvent(self, event):
        """Custom paint for ranking chart with hover tooltips"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Fill background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))
        
        # Draw legend at the top first
        self._draw_legend(painter)
        
        # Chart area - adjusted margins for legend at top and X-axis labels at bottom
        left_margin = 35  # More space for Y-axis labels
        top_margin = 40   # Space for legend at top
        bottom_margin = 35  # Space for X-axis date labels
        right_margin = 20
        chart_rect = self.rect().adjusted(left_margin, top_margin, -right_margin, -bottom_margin)
        
        if not self.player1_data and not self.player2_data:
            # No data message
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 10))
            painter.drawText(chart_rect, Qt.AlignmentFlag.AlignCenter, "Select players to view rankings")
            return
        
        # Calculate scale
        all_ranks = []
        if self.player1_data:
            all_ranks.extend([rank for _, rank, _, _ in self.player1_data])
        if self.player2_data:
            all_ranks.extend([rank for _, rank, _, _ in self.player2_data])
            
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
        
        # Draw X-axis date labels
        self._draw_x_axis_dates(painter, chart_rect)
        
        # Draw hover tooltip if there's a hovered point
        if self.hover_point:
            self._draw_tooltip(painter)
    
    def _draw_tooltip(self, painter):
        """Draw hover tooltip for data points"""
        if not self.hover_point:
            return
            
        # Tooltip content
        date = self.hover_point['date']
        rank = self.hover_point['rank']
        points = self.hover_point['points']
        rank_change = self.hover_point['rank_change']
        player = self.hover_point['player']
        color = self.hover_point['color']
        pos = self.hover_point['pos']
        
        # Format date
        try:
            from datetime import datetime
            date_obj = datetime.strptime(date, '%Y-%m-%d')
            formatted_date = date_obj.strftime('%B %d, %Y')
        except:
            formatted_date = date
        
        # Format rank change (handle None values)
        if rank_change is None:
            change_text = "━ N/A"
            change_color = TennisTheme.TEXT_SECONDARY
        elif rank_change > 0:
            change_text = f"▲ {rank_change}"
            change_color = "#4CAF50"  # Green for improvement
        elif rank_change < 0:
            change_text = f"▼ {abs(rank_change)}"
            change_color = "#FF6B6B"  # Red for decline
        else:
            change_text = "━ 0"
            change_color = TennisTheme.TEXT_SECONDARY
        
        # Tooltip text
        points_text = f"{points:,}" if points is not None else "N/A"
        tooltip_lines = [
            f"{player}",
            f"{formatted_date}",
            f"Rank: #{rank}",
            f"Points: {points_text}",
            f"Change: {change_text}"
        ]
        
        # Calculate tooltip size
        painter.setFont(QFont("Arial", 9))
        max_width = 0
        line_height = 14
        for line in tooltip_lines:
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(line)
            max_width = max(max_width, text_width)
        
        tooltip_width = max_width + 20
        tooltip_height = len(tooltip_lines) * line_height + 10
        
        # Position tooltip near the point but within widget bounds
        tooltip_x = int(pos.x() + 15)
        tooltip_y = int(pos.y() - tooltip_height - 10)
        
        # Adjust if tooltip goes off-screen
        if tooltip_x + tooltip_width > self.width():
            tooltip_x = int(pos.x() - tooltip_width - 15)
        if tooltip_y < 0:
            tooltip_y = int(pos.y() + 15)
        
        # Draw tooltip background
        tooltip_rect = QRect(tooltip_x, tooltip_y, tooltip_width, tooltip_height)
        painter.setPen(QPen(QColor(color), 2))
        painter.setBrush(QBrush(QColor("#2A3441")))
        painter.drawRoundedRect(tooltip_rect, 5, 5)
        
        # Draw tooltip text
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        painter.drawText(tooltip_x + 10, tooltip_y + 15, tooltip_lines[0])  # Player name
        
        painter.setFont(QFont("Arial", 9))
        for i, line in enumerate(tooltip_lines[1:], 1):
            y_pos = tooltip_y + 15 + (i * line_height)
            if i == 4:  # Rank change line
                painter.setPen(QColor(change_color))
            else:
                painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            painter.drawText(tooltip_x + 10, y_pos, line)

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
        self.selected_index_p1 = -1  # Currently selected suggestion index for player 1
        self.selected_index_p2 = -1  # Currently selected suggestion index for player 2
        self.current_suggestions_p1 = []  # Current suggestions for player 1
        self.current_suggestions_p2 = []  # Current suggestions for player 2
        self.suggestion_buttons_p1 = []  # Button references for player 1
        self.suggestion_buttons_p2 = []  # Button references for player 2
        self.load_players()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)  # Compact margins
        layout.setSpacing(2)  # Tighter spacing
        
        # Set minimum size for the widget but allow it to expand
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        # Main search frame - much more compact
        search_frame = QFrame()
        search_frame.setFixedHeight(70)  # Keep search bar area fixed
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(8, 6, 8, 6)  # Much smaller margins
        search_layout.setSpacing(14)  # Reduced spacing

        # Player 1 section
        p1_layout = QVBoxLayout()
        p1_layout.setSpacing(3)  # Very tight spacing
        p1_label = QLabel("Player 1:")
        p1_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))  # Smaller font
        p1_label.setStyleSheet(f"color: #00D4AA; font-weight: bold; margin: 0px;")

        self.player1_input = QLineEdit()
        self.player1_input.setPlaceholderText("Search first player...")
        self.player1_input.textChanged.connect(lambda text: self.filter_players(text, 1))
        self.player1_input.keyPressEvent = lambda event: self.handle_key_press(event, 1)
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
        self.player2_input.keyPressEvent = lambda event: self.handle_key_press(event, 2)
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

        # Results areas (embedded) - allow dynamic expansion
        self.results1_frame = QFrame()
        self.results1_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.results1_frame.setMaximumHeight(320)  # Limit max height to prevent excessive expansion
        self.results1_layout = QVBoxLayout(self.results1_frame)
        self.results1_layout.setContentsMargins(8, 8, 8, 8)  # Add some margins for better appearance
        self.results1_layout.setSpacing(6)  # Even more spacing for better readability
        self.results1_frame.setStyleSheet(f"""
            QFrame {{
                background: {TennisTheme.SURFACE};
                border: 1px solid #2A3441;
                border-radius: 6px;
                margin-top: 4px;
            }}
        """)
        self.results1_frame.hide()

        self.results2_frame = QFrame()
        self.results2_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.results2_frame.setMaximumHeight(320)  # Limit max height to prevent excessive expansion
        self.results2_layout = QVBoxLayout(self.results2_frame)
        self.results2_layout.setContentsMargins(8, 8, 8, 8)  # Add some margins for better appearance
        self.results2_layout.setSpacing(6)  # Even more spacing for better readability
        self.results2_frame.setStyleSheet(f"""
            QFrame {{
                background: {TennisTheme.SURFACE};
                border: 1px solid #2A3441;
                border-radius: 6px;
                margin-top: 4px;
            }}
        """)
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
                self.selected_index_p1 = -1
                self.current_suggestions_p1 = []
            else:
                self.results2_frame.hide()
                self.selected_index_p2 = -1
                self.current_suggestions_p2 = []
            return

        filtered = [(name, rank) for name, rank in self.players 
                   if text.lower() in name.lower()][:8]

        # Store current suggestions and reset selection
        if player_num == 1:
            self.current_suggestions_p1 = filtered
            self.selected_index_p1 = -1
        else:
            self.current_suggestions_p2 = filtered
            self.selected_index_p2 = -1

        self.update_results(filtered, player_num)

    def update_results(self, filtered_players, player_num):
        """Update dropdown results with better styling"""
        if player_num == 1:
            frame = self.results1_frame
            layout = self.results1_layout
            button_list = self.suggestion_buttons_p1
        else:
            frame = self.results2_frame
            layout = self.results2_layout
            button_list = self.suggestion_buttons_p2

        # Clear previous
        for i in reversed(range(layout.count())):
            layout.itemAt(i).widget().setParent(None)
        button_list.clear()

        if not filtered_players:
            frame.hide()
            return

        for i, (name, rank) in enumerate(filtered_players):
            btn = QPushButton(f"#{rank} {name}")
            btn.clicked.connect(lambda checked, n=name, p=player_num: self.select_player(n, p))
            btn.setFixedHeight(38)  # Even larger height for better visibility
            btn.setMinimumWidth(320)  # Ensure minimum width for full names
            btn.setStyleSheet(self.get_button_style(False))  # Normal style initially
            layout.addWidget(btn)
            button_list.append(btn)

        frame.show()

    def get_button_style(self, is_selected):
        """Get button style based on selection state"""
        if is_selected:
            return """
                QPushButton {
                    text-align: left;
                    padding: 8px 12px;
                    background: #00D4AA;
                    border: 2px solid #00D4AA;
                    color: white;
                    font-size: 13px;
                    border-radius: 6px;
                    margin: 3px 0px;
                    font-weight: bold;
                }
            """
        else:
            return """
                QPushButton {
                    text-align: left;
                    padding: 8px 12px;
                    background: #252B3A;
                    border: 1px solid #2A3441;
                    color: white;
                    font-size: 13px;
                    border-radius: 6px;
                    margin: 3px 0px;
                }
                QPushButton:hover {
                    background: #00D4AA;
                    color: white;
                }
            """

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

    def handle_key_press(self, event, player_num):
        """Handle arrow key navigation and Enter key selection"""
        key = event.key()
        
        if player_num == 1:
            suggestions = self.current_suggestions_p1
            selected_index = self.selected_index_p1
            button_list = self.suggestion_buttons_p1
            frame = self.results1_frame
        else:
            suggestions = self.current_suggestions_p2
            selected_index = self.selected_index_p2
            button_list = self.suggestion_buttons_p2
            frame = self.results2_frame
        
        if not suggestions or not frame.isVisible():
            # Let the line edit handle normal text input
            QLineEdit.keyPressEvent(self.player1_input if player_num == 1 else self.player2_input, event)
            return
        
        if key == Qt.Key.Key_Down:
            # Move selection down
            new_index = min(selected_index + 1, len(suggestions) - 1)
            self.update_selection(player_num, new_index)
            event.accept()
        elif key == Qt.Key.Key_Up:
            # Move selection up
            new_index = max(selected_index - 1, 0) if selected_index > -1 else len(suggestions) - 1
            self.update_selection(player_num, new_index)
            event.accept()
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            # Select current highlighted item
            if 0 <= selected_index < len(suggestions):
                name, rank = suggestions[selected_index]
                self.select_player(name, player_num)
            event.accept()
        elif key == Qt.Key.Key_Escape:
            # Hide suggestions
            frame.hide()
            if player_num == 1:
                self.selected_index_p1 = -1
            else:
                self.selected_index_p2 = -1
            event.accept()
        else:
            # Let the line edit handle normal text input
            QLineEdit.keyPressEvent(self.player1_input if player_num == 1 else self.player2_input, event)

    def update_selection(self, player_num, new_index):
        """Update visual selection of suggestion buttons"""
        if player_num == 1:
            old_index = self.selected_index_p1
            self.selected_index_p1 = new_index
            button_list = self.suggestion_buttons_p1
        else:
            old_index = self.selected_index_p2
            self.selected_index_p2 = new_index
            button_list = self.suggestion_buttons_p2
        
        # Update button styles
        for i, btn in enumerate(button_list):
            if i == new_index:
                btn.setStyleSheet(self.get_button_style(True))
            else:
                btn.setStyleSheet(self.get_button_style(False))




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
    tacticsDataLoaded = pyqtSignal(str, list)  # player_name, tactics_data
    surfaceStatsLoaded = pyqtSignal(str, object, int)  # player_name, SurfaceStats, player_num
    yearlyStatsLoaded = pyqtSignal(str, dict, int)  # player_name, yearly_stats_dict, player_num
    rawPlayerDataLoaded = pyqtSignal(str, object, int)  # player_name, raw_player_data, player_num
    
    def __init__(self, player_color: str = TennisTheme.PRIMARY, player_num: int = 1):
        super().__init__()
        self.player_color = player_color
        self.player_num = player_num
        self.player_name = ""
        self.player_bio: Optional[PlayerBio] = None
        self.current_ranking: Optional[PlayerRanking] = None
        self.surface_stats = SurfaceStats()
        
        # Scrapers
        self.abstract_scraper = TennisAbstractScraper(headless=True)
        self.h2h_scraper = TennisScraper()
        
        # Database path for rankings
        self.db_path = "tennis_rankings.db"
        
        self.setup_ui()
        
    def get_player_rankings_from_db(self, player_name: str) -> Tuple[Optional[int], Optional[int]]:
        """Get current and peak rankings from the database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get current ranking (most recent entry)
            cursor.execute("""
                SELECT rank FROM rankings 
                WHERE player_name = ? 
                ORDER BY ranking_date DESC 
                LIMIT 1
            """, (player_name,))
            current_result = cursor.fetchone()
            current_rank = current_result[0] if current_result else None
            
            # Get peak ranking (minimum rank value)
            cursor.execute("""
                SELECT MIN(rank) FROM rankings 
                WHERE player_name = ?
            """, (player_name,))
            peak_result = cursor.fetchone()
            peak_rank = peak_result[0] if peak_result else None
            
            conn.close()
            return current_rank, peak_rank
            
        except Exception as e:
            print(f"Error getting rankings from database for {player_name}: {e}")
            return None, None
        
    def setup_ui(self):
        """Setup the compact player profile UI"""
        self.setFixedSize(420, 200)  # Even wider to prevent bio text from pushing widgets
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
        
        # Header with player name and recent form
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)
        
        self.name_label = QLabel("Select Player")
        self.name_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.name_label.setStyleSheet(f"color: {self.player_color}; margin-bottom: 4px;")
        
        # Recent form display (moved from bottom)
        self.form_display = QLabel("🔴🔴🔴🔴🔴")
        self.form_display.setFont(QFont("Arial", 12))
        self.form_display.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        header_layout.addWidget(self.name_label)
        header_layout.addStretch()
        header_layout.addWidget(self.form_display)
        
        header_widget = QWidget()
        header_widget.setLayout(header_layout)
        
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
        
        # Center: Ranking display aligned with Country label
        center_layout = QVBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(4)
        
        self.ranking_widget = RankingGraphWidget()
        center_layout.addWidget(self.ranking_widget)
        center_layout.addStretch()  # Push ranking widget to top
        
        center_widget = QWidget()
        center_widget.setLayout(center_layout)
        
        # Right side: Surface performance aligned with Country label
        right_side_layout = QVBoxLayout()
        right_side_layout.setContentsMargins(0, 0, 0, 0)
        right_side_layout.setSpacing(4)
        
        # Surface performance widget positioned at top to align with Country label
        self.surface_widget = CompactSurfaceWidget()
        right_side_layout.addWidget(self.surface_widget)
        right_side_layout.addStretch()  # Push surface widget to top
        
        right_side_widget = QWidget()
        right_side_widget.setLayout(right_side_layout)
        
        # Assemble content
        content_layout.addWidget(bio_widget, 1)
        content_layout.addWidget(center_widget, 0)
        content_layout.addWidget(right_side_widget, 0)
        
        # Assemble main layout
        layout.addWidget(header_widget)
        layout.addWidget(content_widget, 1)
        
    def set_player(self, player_name: str):
        """Set the player and load their data"""
        if player_name == self.player_name:
            return
            
        self.player_name = player_name
        self.name_label.setText(player_name)
        
        # Load data asynchronously
        self.load_player_data()
         
        
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
                    
                    # Parse and abbreviate plays info
                    plays_text = player_data.player_bio.plays
                    abbreviated_plays = self.abbreviate_plays(plays_text)
                    self.plays_label.setText(f"Plays: {abbreviated_plays}")
                    
                    # Parse ELO rating
                    try:
                        elo_rating = int(player_data.player_bio.elo_rating) if player_data.player_bio.elo_rating else None
                        self.elo_label.setText(f"ELO: {elo_rating}" if elo_rating else "ELO: --")
                    except:
                        self.elo_label.setText("ELO: --")
                    
                    # Get rankings from database instead of scraping
                    try:
                        current_rank_db, peak_rank_db = self.get_player_rankings_from_db(self.player_name)
                        
                        # Use database rankings if available, otherwise fallback to scraped data
                        current_rank = current_rank_db if current_rank_db is not None else (int(player_data.player_bio.current_rank) if player_data.player_bio.current_rank.isdigit() else None)
                        peak_rank = peak_rank_db if peak_rank_db is not None else (int(player_data.player_bio.peak_rank) if player_data.player_bio.peak_rank.isdigit() else None)
                        
                        self.ranking_widget.update_ranking(current_rank, peak_rank, elo_rating)
                    except:
                        pass
                    
                        
                    # Emit tactics data if available
                    if hasattr(player_data, 'tactics') and player_data.tactics:
                        self.tacticsDataLoaded.emit(self.player_name, player_data.tactics)
                    
                    # Emit raw player data for stats widget
                    if hasattr(player_data, '__dict__'):
                        self.rawPlayerDataLoaded.emit(self.player_name, player_data.__dict__, self.player_num)
                
                # Get rankings from database for cases where Abstract data wasn't available
                if not self.player_bio:
                    try:
                        current_rank_db, peak_rank_db = self.get_player_rankings_from_db(self.player_name)
                        if current_rank_db is not None:
                            self.ranking_widget.update_ranking(current_rank_db, peak_rank_db)
                    except:
                        pass
                
                # Get recent form and surface data from H2H scraper by doing a dummy comparison
                try:
                    # Create a dummy comparison to get player stats with recent form and surface data
                    dummy_player = "Carlos Alcaraz"  # Use a common player as dummy
                    if self.player_name.lower() == dummy_player.lower():
                        dummy_player = "Jannik Sinner"  # Use different dummy if same player
                    
                    h2h_data = self.h2h_scraper.scrape_h2h_comprehensive_sync(self.player_name, dummy_player)
                    if h2h_data:
                        # Check which player matches our target player by name similarity
                        target_player_data = None
                        
                        # Check if player1 name matches our target player
                        if (h2h_data.player1.name and 
                            self.player_name.lower() in h2h_data.player1.name.lower() or
                            h2h_data.player1.name.lower() in self.player_name.lower()):
                            target_player_data = h2h_data.player1
                        # Check if player2 name matches our target player  
                        elif (h2h_data.player2.name and 
                              self.player_name.lower() in h2h_data.player2.name.lower() or
                              h2h_data.player2.name.lower() in self.player_name.lower()):
                            target_player_data = h2h_data.player2
                        
                        # Use the recent form from the correct player
                        if target_player_data and target_player_data.recent_form:
                            self.update_form(target_player_data.recent_form)
                        else:
                            # Fallback: try player1 first, then player2
                            if h2h_data.player1.recent_form:
                                self.update_form(h2h_data.player1.recent_form)
                            elif h2h_data.player2.recent_form:
                                self.update_form(h2h_data.player2.recent_form)
                                
                        # Update surface widget with yearly stats data - SINGLE UPDATE ONLY
                        if target_player_data and target_player_data.yearly_stats:
                            self.surface_widget.update_stats_from_yearly_data(target_player_data.yearly_stats)
                            
                            # Also emit yearly stats for historical table
                            self.yearlyStatsLoaded.emit(self.player_name, target_player_data.yearly_stats, self.player_num)
                            
                except Exception as e:
                    print(f"Error getting H2H data for {self.player_name}: {e}")
                    # Fallback to placeholder if H2H scraper fails
                    self.update_form(['L', 'L', 'L', 'L', 'L'])
                        
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
        
    def abbreviate_plays(self, plays_text: str) -> str:
        """Abbreviate plays information to standard tennis format"""
        if not plays_text:
            return "--"
            
        # Convert to lowercase for easier matching
        plays_lower = plays_text.lower()
        
        # Determine handedness
        if "right" in plays_lower:
            handedness = "RH"
        elif "left" in plays_lower:
            handedness = "LH"
        else:
            handedness = "?"
            
        # Determine backhand style
        if "two" in plays_lower or "2" in plays_lower:
            backhand = "2HBH"
        elif "one" in plays_lower or "1" in plays_lower:
            backhand = "1HBH"
        else:
            backhand = "?"
            
        return f"{handedness}/{backhand}"
        
    def extract_surface_stats(self, career_splits: List) -> SurfaceStats:
        """Extract surface statistics from career splits data"""
        stats = SurfaceStats()
        
        # Debug: Print what splits we're getting
        print(f"DEBUG: Extracting surface stats from {len(career_splits)} splits:")
        for split in career_splits:
            print(f"  Split: '{split.split}' - Wins: {split.wins}, Matches: {split.matches}")
        
        for split in career_splits:
            split_name = split.split.lower()
            
            # Parse wins-losses from matches
            try:
                wins = int(split.wins) if split.wins.isdigit() else 0
                total = int(split.matches) if split.matches.isdigit() else 0
                
                if 'hard' in split_name or 'outdoor hard' in split_name:
                    stats.hard_wins += wins
                    stats.hard_total += total
                    print(f"  -> Added to HARD: {wins}/{total}")
                elif 'clay' in split_name:
                    stats.clay_wins += wins
                    stats.clay_total += total
                    print(f"  -> Added to CLAY: {wins}/{total}")
                elif 'grass' in split_name:
                    stats.grass_wins += wins
                    stats.grass_total += total
                    print(f"  -> Added to GRASS: {wins}/{total}")
                elif 'indoor' in split_name or 'carpet' in split_name:
                    stats.indoor_wins += wins
                    stats.indoor_total += total
                    print(f"  -> Added to INDOOR: {wins}/{total}")
                else:
                    print(f"  -> UNMATCHED: '{split_name}'")
                    
            except (ValueError, AttributeError):
                continue
                
        print(f"Final stats: Hard: {stats.hard_wins}/{stats.hard_total} ({stats.get_percentage('hard'):.1f}%)")
        print(f"             Clay: {stats.clay_wins}/{stats.clay_total} ({stats.get_percentage('clay'):.1f}%)")
        print(f"             Grass: {stats.grass_wins}/{stats.grass_total} ({stats.get_percentage('grass'):.1f}%)")
        print(f"             Indoor: {stats.indoor_wins}/{stats.indoor_total} ({stats.get_percentage('indoor'):.1f}%)")
        
        return stats
    
    def extract_surface_stats_from_h2h(self, yearly_stats: Dict[str, Dict[str, str]]) -> SurfaceStats:
        """Extract surface statistics from H2H scraper yearly stats data"""
        stats = SurfaceStats()
        
        # Debug: Print what yearly stats we're getting
        print(f"DEBUG: Extracting surface stats from H2H yearly stats:")
        for year, surfaces in yearly_stats.items():
            print(f"  Year {year}: {surfaces}")
        
        # Use the most recent year (2025)
        current_year = "2025"
        if current_year in yearly_stats:
            surfaces = yearly_stats[current_year]
            
            for surface_name, record in surfaces.items():
                if not record or record == "--":
                    continue
                    
                # Parse records like "7-0", "11-2", "0-0" 
                try:
                    if '-' in record:
                        wins_str, losses_str = record.split('-')
                        wins = int(wins_str.strip())
                        losses = int(losses_str.strip())
                        total = wins + losses
                        
                        surface_lower = surface_name.lower()
                        
                        # Handle I.hard/indoor specifically first to prevent it from matching 'hard'
                        if surface_lower == 'i.hard' or 'indoor' in surface_lower:
                            stats.indoor_wins += wins
                            stats.indoor_total += total
                            print(f"  -> Added to INDOOR: {wins}/{total} from '{surface_name}: {record}'")
                        elif 'hard' in surface_lower and 'i.hard' not in surface_lower:
                            stats.hard_wins += wins
                            stats.hard_total += total
                            print(f"  -> Added to HARD: {wins}/{total} from '{surface_name}: {record}'")
                        elif 'clay' in surface_lower:
                            stats.clay_wins += wins
                            stats.clay_total += total
                            print(f"  -> Added to CLAY: {wins}/{total} from '{surface_name}: {record}'")
                        elif 'grass' in surface_lower:
                            stats.grass_wins += wins
                            stats.grass_total += total
                            print(f"  -> Added to GRASS: {wins}/{total} from '{surface_name}: {record}'")
                        else:
                            print(f"  -> UNMATCHED: '{surface_name}: {record}'")
                            
                except (ValueError, AttributeError) as e:
                    print(f"  -> ERROR parsing '{surface_name}: {record}': {e}")
                    continue
        else:
            print(f"  -> No data found for current year {current_year}")
            
        print(f"H2H Final stats: Hard: {stats.hard_wins}/{stats.hard_total} ({stats.get_percentage('hard'):.1f}%)")
        print(f"                 Clay: {stats.clay_wins}/{stats.clay_total} ({stats.get_percentage('clay'):.1f}%)")
        print(f"                 Grass: {stats.grass_wins}/{stats.grass_total} ({stats.get_percentage('grass'):.1f}%)")
        print(f"                 Indoor: {stats.indoor_wins}/{stats.indoor_total} ({stats.get_percentage('indoor'):.1f}%)")
        
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
            
        # Take the first 5 matches from TennisTonic (they display in chronological order)
        # The rightmost position shows the most recent match
        first_5_matches = recent_form[:5] if len(recent_form) >= 5 else recent_form
        
        form_display = ""
        for result in first_5_matches:
            if result.upper() == 'W':
                form_display += "🟢"
            elif result.upper() == 'L':
                form_display += "🔴"
            else:
                form_display += "⚫"  # Unknown/no data
                
        self.form_display.setText(form_display)





class TacticsTableWidget(QWidget):
    """Dual-table widget displaying tactics data for both players"""
    
    def __init__(self):
        super().__init__()
        self.player1_tactics = []
        self.player2_tactics = []
        self.player1_name = ""
        self.player2_name = ""
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the tactics table UI"""
        self.setFixedSize(950, 450)  # Reduced width to prevent extending off screen
        self.setStyleSheet(f"""
            TacticsTableWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 12px;
            }}
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(0)  # No spacing between tables
        
        # Title
        title_label = QLabel("Player Tactics Comparison")
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        title_label.setStyleSheet(f"color: {TennisTheme.TEXT_PRIMARY}; margin-bottom: 4px;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Create stacked tables for each player
        self.player1_table = self.create_player_tactics_table(self.player1_name, TennisTheme.PRIMARY)
        self.player2_table = self.create_player_tactics_table(self.player2_name, TennisTheme.ACCENT)
        
        layout.addWidget(title_label)
        layout.addWidget(self.player1_table)
        layout.addWidget(self.player2_table)
        
    def create_player_tactics_table(self, player_name: str, color: str) -> QTableWidget:
        """Create a tactics table for one player"""
        # Create table directly without container
        table = QTableWidget()
        
        # Define all tactics columns
        headers = [
            "Match", "Result", "SnV Freq", "SnV W%", "Net Freq", "Net W%",
            "FH Wnr%", "DTL Wnr%", "IO Wnr%", "BH Wnr%", "DTL Wnr%", 
            "Drop Freq", "Drop W%", "Rally Agg", "Return Agg"
        ]
        
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setFixedSize(900, 180)  # Reduced width to fit within container
        
        # Style table with player color
        table.setStyleSheet(f"""
            QTableWidget {{
                background: {TennisTheme.SURFACE};
                border: 2px solid {color};
                gridline-color: {TennisTheme.TEXT_MUTED};
                color: {TennisTheme.TEXT_PRIMARY};
                font-size: 9px;
            }}
            QHeaderView::section {{
                background: {color};
                color: white;
                font-weight: bold;
                padding: 4px;
                border: 1px solid {TennisTheme.TEXT_MUTED};
                font-size: 9px;
            }}
            QTableWidget::item {{
                padding: 2px 4px;
                border-bottom: 1px solid {TennisTheme.TEXT_MUTED};
            }}
            QScrollBar:vertical {{
                background: {TennisTheme.SURFACE};
                width: 12px;
                border: 1px solid {TennisTheme.TEXT_MUTED};
            }}
            QScrollBar::handle:vertical {{
                background: {color};
                border-radius: 6px;
            }}
        """)
        
        # Configure table
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        
        return table
        
    def update_tactics_data(self, player_num: int, tactics_data: List[TacticsData], player_name: str):
        """Update tactics data for a player"""
        if player_num == 1:
            self.player1_tactics = tactics_data
            self.player1_name = player_name
            self.populate_player_table(self.player1_table, tactics_data)
        else:
            self.player2_tactics = tactics_data
            self.player2_name = player_name
            self.populate_player_table(self.player2_table, tactics_data)
            
    def populate_player_table(self, table: QTableWidget, tactics_data: List[TacticsData]):
        """Populate individual player table with tactics data"""
        if not tactics_data:
            table.setRowCount(1)
            table.setItem(0, 0, QTableWidgetItem("No data available"))
            return
            
        # Filter out career totals and get individual matches
        match_data = [data for data in tactics_data if "Career" not in data.match]
        
        # Sort by most recent first
        match_data.sort(key=lambda x: x.match, reverse=True)
        
        table.setRowCount(len(match_data))
        
        for row, data in enumerate(match_data):
            # Populate all columns
            table.setItem(row, 0, QTableWidgetItem(data.match))
            table.setItem(row, 1, QTableWidgetItem(data.result))
            table.setItem(row, 2, QTableWidgetItem(data.snv_freq))
            table.setItem(row, 3, QTableWidgetItem(data.snv_w_pct))
            table.setItem(row, 4, QTableWidgetItem(data.net_freq))
            table.setItem(row, 5, QTableWidgetItem(data.net_w_pct))
            table.setItem(row, 6, QTableWidgetItem(data.fh_wnr_pct))
            table.setItem(row, 7, QTableWidgetItem(data.dtl_wnr_pct))
            table.setItem(row, 8, QTableWidgetItem(data.io_wnr_pct))
            table.setItem(row, 9, QTableWidgetItem(data.bh_wnr_pct))
            table.setItem(row, 10, QTableWidgetItem(data.dtl_wnr_pct_bh))
            table.setItem(row, 11, QTableWidgetItem(data.drop_freq))
            table.setItem(row, 12, QTableWidgetItem(data.drop_wnr_pct))
            table.setItem(row, 13, QTableWidgetItem(data.rally_agg))
            table.setItem(row, 14, QTableWidgetItem(data.return_agg))


class CompactStatsWidget(QWidget):
    """Compact stats widget showing Tour-Level vs Challenger stats with individual toggles"""
    
    def __init__(self):
        super().__init__()
        self.player1_data = {}
        self.player2_data = {}
        self.player1_name = ""
        self.player2_name = ""
        self.player1_show_tour = True  # Individual toggle for player 1
        self.player2_show_tour = True  # Individual toggle for player 2
        self.stats_mode = "current_year"  # current_year, career, last52
        self.current_split_type = "Hard"  # Current split type for career/last52 modes
        self.available_splits = []  # Will be populated when data is loaded
        self.setFixedSize(280, 280)  # Taller for additional toggle button
        self.setStyleSheet(f"""
            CompactStatsWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 8px;
            }}
        """)
        
        # Create toggle buttons for each player
        self.setup_toggle_buttons()
        
    def setup_toggle_buttons(self):
        """Create individual toggle buttons for each player"""
        # Mode toggle button (cycles through current_year, career, last52) - left side
        self.mode_toggle = QPushButton("2025")
        self.mode_toggle.setFixedSize(60, 20)
        self.mode_toggle.setStyleSheet(f"""
            QPushButton {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: none;
                border-radius: 10px;
                font-size: 9px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {TennisTheme.SECONDARY};
            }}
        """)
        self.mode_toggle.clicked.connect(self.toggle_stats_mode)
        self.mode_toggle.setParent(self)
        self.mode_toggle.move(85, 25)  # Left side of center
        
        # Split type toggle button (only active for career/last52 modes) - right side
        self.split_toggle = QPushButton("Hard")
        self.split_toggle.setFixedSize(70, 20)
        self.split_toggle.setStyleSheet(f"""
            QPushButton {{
                background: {TennisTheme.TEXT_MUTED};
                color: {TennisTheme.TEXT_PRIMARY};
                border: none;
                border-radius: 10px;
                font-size: 8px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {TennisTheme.SECONDARY};
            }}
            QPushButton:disabled {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_MUTED};
            }}
        """)
        self.split_toggle.clicked.connect(self.toggle_split_type)
        self.split_toggle.setParent(self)
        self.split_toggle.move(150, 25)  # Right side of center
        self.split_toggle.setEnabled(False)  # Initially disabled
        
        # Player 1 toggle button - positioned inline with player 1 name
        self.player1_toggle = QPushButton("Tour")
        self.player1_toggle.setFixedSize(35, 14)
        self.player1_toggle.setStyleSheet(f"""
            QPushButton {{
                background: {TennisTheme.PRIMARY};
                color: {TennisTheme.TEXT_PRIMARY};
                border: none;
                border-radius: 7px;
                font-size: 7px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {TennisTheme.SECONDARY};
            }}
            QPushButton:disabled {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_MUTED};
            }}
        """)
        self.player1_toggle.clicked.connect(lambda: self.toggle_player_stats(1))
        self.player1_toggle.setParent(self)
        self.player1_toggle.move(100, 65)  # Inline with player 1 name (adjusted for new y position)
        
        # Player 2 toggle button - positioned inline with player 2 name
        self.player2_toggle = QPushButton("Tour")
        self.player2_toggle.setFixedSize(35, 14)
        self.player2_toggle.setStyleSheet(f"""
            QPushButton {{
                background: {TennisTheme.ACCENT};
                color: {TennisTheme.TEXT_PRIMARY};
                border: none;
                border-radius: 7px;
                font-size: 7px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {TennisTheme.SECONDARY};
            }}
            QPushButton:disabled {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_MUTED};
            }}
        """)
        self.player2_toggle.clicked.connect(lambda: self.toggle_player_stats(2))
        self.player2_toggle.setParent(self)
        self.player2_toggle.move(240, 65)  # Inline with player 2 name (adjusted for new y position)
        
    def toggle_player_stats(self, player_num: int):
        """Toggle between Tour-Level and Challenger for specific player"""
        if player_num == 1:
            self.player1_show_tour = not self.player1_show_tour
            self.player1_toggle.setText("Tour" if self.player1_show_tour else "Chall")
        else:
            self.player2_show_tour = not self.player2_show_tour
            self.player2_toggle.setText("Tour" if self.player2_show_tour else "Chall")
        self.update()
        
    def toggle_stats_mode(self):
        """Toggle between current year, career, and last 52 week stats"""
        if self.stats_mode == "current_year":
            self.stats_mode = "career"
            self.mode_toggle.setText("Career")
            self.split_toggle.setEnabled(True)  # Enable split toggle for career mode
            self.player1_toggle.setEnabled(False)  # Disable tour/challenger for splits
            self.player2_toggle.setEnabled(False)
            self.update_available_splits()
        elif self.stats_mode == "career":
            self.stats_mode = "last52"
            self.mode_toggle.setText("Last52")
            self.split_toggle.setEnabled(True)  # Enable split toggle for last52 mode
            self.player1_toggle.setEnabled(False)  # Disable tour/challenger for splits
            self.player2_toggle.setEnabled(False)
            self.update_available_splits()
        else:
            self.stats_mode = "current_year"
            self.mode_toggle.setText("2025")
            self.split_toggle.setEnabled(False)  # Disable split toggle for current year mode
            self.player1_toggle.setEnabled(True)  # Enable tour/challenger for season stats
            self.player2_toggle.setEnabled(True)
        self.update()
        
    def toggle_split_type(self):
        """Toggle between different split types for career/last52 modes"""
        if not self.available_splits:
            return
            
        current_index = self.available_splits.index(self.current_split_type) if self.current_split_type in self.available_splits else 0
        next_index = (current_index + 1) % len(self.available_splits)
        self.current_split_type = self.available_splits[next_index]
        
        # Update button text (abbreviate long names)
        button_text = self.current_split_type
        if len(button_text) > 8:
            abbreviations = {
                "Grand Slams": "GS",
                "Masters": "M1000",
                "Other Tours": "Other",
                "Best of 5": "Bo5",
                "Best of 3": "Bo3",
                "Semi-finals": "SF",
                "Quarter-finals": "QF",
                "vs Righties": "vsR",
                "vs Lefties": "vsL",
                "vs Top 10": "vsT10"
            }
            button_text = abbreviations.get(button_text, button_text[:8])
        
        self.split_toggle.setText(button_text)
        self.update()
        
    def update_available_splits(self):
        """Update available splits based on current data"""
        splits_key = "career_splits" if self.stats_mode == "career" else "last52_splits"
        
        # Gather all unique splits from both players
        all_splits = set()
        
        for player_data in [self.player1_data, self.player2_data]:
            if player_data and splits_key in player_data:
                for split_data in player_data[splits_key]:
                    if hasattr(split_data, 'split'):
                        all_splits.add(split_data.split)
        
        # Convert to sorted list, prioritizing common splits
        priority_splits = ["Hard", "Clay", "Grass", "Grand Slams", "Masters", "vs Lefties", "vs Top 10"]
        self.available_splits = [s for s in priority_splits if s in all_splits]
        self.available_splits.extend(sorted([s for s in all_splits if s not in priority_splits]))
        
        # Set default split type if current one is not available
        if self.current_split_type not in self.available_splits and self.available_splits:
            self.current_split_type = self.available_splits[0]
            
        # Update button text
        if self.available_splits:
            button_text = self.current_split_type
            if len(button_text) > 8:
                abbreviations = {
                    "Grand Slams": "GS",
                    "Masters": "M1000",
                    "Other Tours": "Other",
                    "Best of 5": "Bo5",
                    "Best of 3": "Bo3",
                    "Semi-finals": "SF",
                    "Quarter-finals": "QF",
                    "vs Righties": "vsR",
                    "vs Lefties": "vsL",
                    "vs Top 10": "vsT10"
                }
                button_text = abbreviations.get(button_text, button_text[:8])
            self.split_toggle.setText(button_text)
        
    def update_player_data(self, player_name: str, player_data: dict, player_num: int):
        """Update player data from tennis abstract"""
        if player_num == 1:
            self.player1_data = player_data
            self.player1_name = player_name
        else:
            self.player2_data = player_data
            self.player2_name = player_name
        
        # Update available splits when new data is loaded
        if self.stats_mode in ["career", "last52"]:
            self.update_available_splits()
        
        self.update()
        
    def paintEvent(self, event):
        """Custom paint for stats display"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))
        
        # Title based on current mode
        mode_titles = {
            "current_year": "2025 Stats",
            "career": "Career Stats", 
            "last52": "Last 52 Weeks"
        }
        title = mode_titles.get(self.stats_mode, "Stats")
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        painter.drawText(5, 5, 270, 20, Qt.AlignmentFlag.AlignCenter, title)
        
        # Draw player stats tables with individual toggles (5px spacing from top toggles)
        self.draw_player_stats(painter, self.player1_name, self.player1_data, 0, 50, 140, TennisTheme.PRIMARY, self.player1_show_tour)
        self.draw_player_stats(painter, self.player2_name, self.player2_data, 140, 50, 140, TennisTheme.ACCENT, self.player2_show_tour)
        
    def draw_player_stats(self, painter, player_name, player_data, x_offset, y_offset, width, accent_color, show_tour_level):
        """Draw stats for one player"""
        # Player name header  
        painter.setPen(QColor(accent_color))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(x_offset + 5, y_offset + 15, player_name[:15] + ("..." if len(player_name) > 15 else ""))
        
        # Get data based on current mode
        if not player_data:
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(x_offset + 5, y_offset + 40, "No data")
            return
            
        current_data = None
        
        if self.stats_mode == "current_year":
            # Get current year stats
            current_year = "2025"
            stats_key = "tour_seasons" if show_tour_level else "challenger_seasons"
            
            if stats_key not in player_data:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                painter.drawText(x_offset + 5, y_offset + 40, "No data")
                return
                
            # Find current year data
            for season in player_data[stats_key]:
                if hasattr(season, 'year') and season.year == current_year:
                    current_data = season
                    break
                    
        elif self.stats_mode == "career":
            # Get career splits data for specific split type (ignores tour/challenger)
            if "career_splits" not in player_data:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                painter.drawText(x_offset + 5, y_offset + 40, "No career data")
                return
                
            # Find the specific split type
            for split_data in player_data["career_splits"]:
                if hasattr(split_data, 'split') and split_data.split == self.current_split_type:
                    current_data = split_data
                    break
                    
        elif self.stats_mode == "last52":
            # Get last 52 weeks splits data for specific split type (ignores tour/challenger)
            if "last52_splits" not in player_data:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                painter.drawText(x_offset + 5, y_offset + 40, "No last52 data")
                return
                
            # Find the specific split type
            for split_data in player_data["last52_splits"]:
                if hasattr(split_data, 'split') and split_data.split == self.current_split_type:
                    current_data = split_data
                    break
                    
        if not current_data:
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(x_offset + 5, y_offset + 40, "No data")
            return
            
        # Draw stats
        stats_y = y_offset + 35
        painter.setFont(QFont("Arial", 10))
        
        # Stats to display
        stats_items = [
            ("Set%", getattr(current_data, "set_percentage", "0%")),
            ("Game%", getattr(current_data, "game_percentage", "0%")),
            ("Hld%", getattr(current_data, "hold_percentage", "0%")),
            ("Brk%", getattr(current_data, "break_percentage", "0%")),
            ("SPW%", getattr(current_data, "service_points_won", "0%")),
            ("DF%", getattr(current_data, "double_fault_rate", "0%")),
            ("RPW%", getattr(current_data, "return_points_won", "0%")),
            ("DR", getattr(current_data, "dominance_ratio", "0.0"))
        ]
        
        for i, (label, value) in enumerate(stats_items):
            y_pos = stats_y + (i * 20)
            
            # Label
            painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            painter.drawText(x_offset + 5, y_pos, 45, 20, Qt.AlignmentFlag.AlignLeft, label)
            
            # Value with color coding
            if label == "DR":
                # Color code dominance ratio
                try:
                    dr_value = float(value)
                    if dr_value >= 1.0:
                        painter.setPen(QColor("#4CAF50"))  # Green for good
                    elif dr_value >= 0.9:
                        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))  # Normal
                    else:
                        painter.setPen(QColor("#FF6B6B"))  # Red for poor
                except:
                    painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            else:
                # Color code percentages
                try:
                    pct_value = float(value.replace('%', ''))
                    if label in ["Set%", "Game%", "Hld%", "Brk%", "SPW%", "RPW%"]:
                        if pct_value >= 50:
                            painter.setPen(QColor("#4CAF50"))  # Green for good
                        elif pct_value >= 40:
                            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))  # Normal
                        else:
                            painter.setPen(QColor("#FF6B6B"))  # Red for poor
                    elif label == "DF%":
                        # Double fault rate - lower is better
                        if pct_value <= 2.0:
                            painter.setPen(QColor("#4CAF50"))  # Green for low DF%
                        elif pct_value <= 4.0:
                            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))  # Normal
                        else:
                            painter.setPen(QColor("#FF6B6B"))  # Red for high DF%
                    else:
                        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
                except:
                    painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            
            painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
            painter.drawText(x_offset + 55, y_pos, 80, 20, Qt.AlignmentFlag.AlignRight, value)
            painter.setFont(QFont("Arial", 10))


class CompactTennisComparisonWidget(QWidget):
    """Main container combining search and player profile widgets"""
    
    def __init__(self):
        super().__init__()
        self.h2h_scraper = TennisScraper()
        self.current_player1 = ""
        self.current_player2 = ""
        self.setup_ui()
        self.setup_connections()
        
    def setup_ui(self):
        """Setup the complete comparison interface"""
        self.setWindowTitle("Compact Tennis Player Comparison")
        self.setGeometry(100, 100, 1900, 800)  # Optimized size for stacked tables
        self.setMinimumSize(1400, 600)  # Set minimum size for proper functionality
        self.setStyleSheet(f"background: {TennisTheme.BACKGROUND};")
        
        # Main layout: Grid layout for better organization
        main_layout = QGridLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)
        
        # Top-left area: Player comparison widgets
        top_left_widget = QWidget()
        top_left_widget.setFixedWidth(900)  # Wider to accommodate both 420px player widgets
        top_left_widget.setMinimumHeight(420)  # Reduced minimum height to allow more compact layout
        top_left_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        top_left_layout = QVBoxLayout(top_left_widget)
        top_left_layout.setContentsMargins(0, 0, 0, 0)
        top_left_layout.setSpacing(8)  # Reduced spacing from 12 to 8
        
        # Search widget
        self.search_widget = CompactPlayerSearchWidget()
        
        # Player profile widgets
        profiles_layout = QHBoxLayout()
        profiles_layout.setSpacing(15)
        
        self.player1_widget = PlayerProfileWidget(TennisTheme.PRIMARY, player_num=1)
        self.player2_widget = PlayerProfileWidget(TennisTheme.ACCENT, player_num=2)
        
        profiles_layout.addWidget(self.player1_widget)
        profiles_layout.addWidget(self.player2_widget)
        
        # Rankings chart widget (compact size)
        self.ranking_chart = CompactRankingChart()
        self.ranking_chart.setFixedSize(900, 220)  # Fixed size matching surface table
        
        # Historical surface performance table
        self.surface_table_widget = HistoricalSurfaceTableWidget()
        
        # Create horizontal layout for surface table and stats widget
        surface_stats_layout = QHBoxLayout()
        surface_stats_layout.setSpacing(20)  # Spacing between surface table and stats widget
        surface_stats_layout.addWidget(self.surface_table_widget)
        
        # Compact stats widget
        self.stats_widget = CompactStatsWidget()
        surface_stats_layout.addWidget(self.stats_widget)
        
        # Add widgets to top-left layout
        top_left_layout.addWidget(self.search_widget)
        top_left_layout.addLayout(profiles_layout)
        top_left_layout.addLayout(surface_stats_layout)
        top_left_layout.addStretch()  # Push everything to top
        
        # Place top-left widget in grid position (0, 0)
        main_layout.addWidget(top_left_widget, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        
        # Top-right area: Tactics comparison tables
        self.tactics_widget = TacticsTableWidget()
        main_layout.addWidget(self.tactics_widget, 0, 1, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        
        # Bottom area: Rankings chart
        main_layout.addWidget(self.ranking_chart, 1, 0, 1, 2, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        
    def setup_connections(self):
        """Connect search signals to player widgets"""
        self.search_widget.player1Selected.connect(self.on_player1_selected)
        self.search_widget.player2Selected.connect(self.on_player2_selected)
        
        # Connect tactics data signals
        self.player1_widget.tacticsDataLoaded.connect(self.on_player1_tactics_loaded)
        self.player2_widget.tacticsDataLoaded.connect(self.on_player2_tactics_loaded)
        
        # Connect surface stats signals
        self.player1_widget.surfaceStatsLoaded.connect(self.on_surface_stats_loaded)
        self.player2_widget.surfaceStatsLoaded.connect(self.on_surface_stats_loaded)
        
        # Connect yearly stats signals for historical table
        self.player1_widget.yearlyStatsLoaded.connect(self.on_yearly_stats_loaded)
        self.player2_widget.yearlyStatsLoaded.connect(self.on_yearly_stats_loaded)
        
        # Connect raw player data signals for stats widget
        self.player1_widget.rawPlayerDataLoaded.connect(self.on_raw_player_data_loaded)
        self.player2_widget.rawPlayerDataLoaded.connect(self.on_raw_player_data_loaded)
        
    def on_player1_selected(self, player_name: str):
        """Handle player 1 selection"""
        self.current_player1 = player_name
        self.player1_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 1)
        self.update_status()
        self.check_and_load_h2h()
        
    def on_player2_selected(self, player_name: str):
        """Handle player 2 selection"""
        self.current_player2 = player_name
        self.player2_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 2)
        self.update_status()
        self.check_and_load_h2h()
        
    def on_player1_tactics_loaded(self, player_name: str, tactics_data: list):
        """Handle player 1 tactics data loaded"""
        self.tactics_widget.update_tactics_data(1, tactics_data, player_name)
        
    def on_player2_tactics_loaded(self, player_name: str, tactics_data: list):
        """Handle player 2 tactics data loaded"""
        self.tactics_widget.update_tactics_data(2, tactics_data, player_name)
        
    def on_surface_stats_loaded(self, player_name: str, surface_stats, player_num: int):
        """Handle surface statistics loaded for either player"""
        # This is kept for compatibility but the historical table uses yearly stats instead
        pass
        
    def on_yearly_stats_loaded(self, player_name: str, yearly_stats: dict, player_num: int):
        """Handle yearly surface statistics loaded for either player"""
        self.surface_table_widget.update_player_stats(player_name, yearly_stats, player_num)
        
    def on_raw_player_data_loaded(self, player_name: str, player_data: dict, player_num: int):
        """Handle raw player data loaded for stats widget"""
        self.stats_widget.update_player_data(player_name, player_data, player_num)
        
    def update_status(self):
        """Update status label"""
        if self.current_player1 and self.current_player2:
            pass  # Status is handled by H2H widget now
        elif self.current_player1:
            pass  # Individual status if needed
        elif self.current_player2:
            pass  # Individual status if needed
        else:
            pass  # Default status if needed
            
    def check_and_load_h2h(self):
        """Load H2H data if both players are selected"""
        if self.current_player1 and self.current_player2:
            # Load H2H data in background thread
            def load_h2h():
                try:
                    h2h_data = self.h2h_scraper.scrape_h2h_comprehensive_sync(
                        self.current_player1, self.current_player2
                    )
                    # Could add H2H display here in the future
                    print(f"H2H data loaded for {self.current_player1} vs {self.current_player2}")
                except Exception as e:
                    print(f"Error loading comparison data: {e}")
            
            # Run in background thread
            import threading
            thread = threading.Thread(target=load_h2h, daemon=True)
            thread.start()
        else:
            # No action needed when not both players selected
            pass
        

# Test application
if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Create main comparison widget
    window = CompactTennisComparisonWidget()
    window.show()
    
    sys.exit(app.exec())
