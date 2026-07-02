import sys
import re
import threading
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QGridLayout, QSizePolicy, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QScrollArea, QTabWidget
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRect, QPoint
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QIntValidator
)
from tennis_abstract_scraper import TennisAbstractScraper, PlayerBio, TacticsData
from tennis_h2h_scraper import TennisScraper, PlayerRanking
import tennis_sim
import tennis_context


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
        current_year = datetime.now().year
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
        self.setFixedSize(600, 280)  # Height increased to match stats widget
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
            row_height = 20  # Slightly reduced for better space utilization

            # Data rows - show more years with increased height
            for i, year in enumerate(sorted_years[:10]):  # Show last 10 years
                if i % 2 == 0:
                    painter.fillRect(x_offset, row_y, width, row_height, QColor("#252B3A"))
                else:
                    painter.fillRect(x_offset, row_y, width, row_height, QColor("#1E242F"))

                year_data = yearly_stats[year]

                painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
                painter.setFont(QFont("Arial", 9))

                # Year
                painter.drawText(x_offset + 5, row_y + 14, year)

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

                    painter.drawText(x_pos, row_y + 14, value)
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
                    painter.drawText(x_pos, row_y + 14, value)
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

        # Draw tooltip if hovering over a point
        self._draw_tooltip(painter)

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
        # NOTE: the results frames are deliberately NOT added to the layout.
        # They float as overlay children of the top-level window (positioned in
        # _position_overlay) so showing suggestions never resizes/pushes the
        # surrounding widgets. They are reparented to the window on first show.
        self._overlay_reparented = False

    def load_players(self):
        """Load players from database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT r1.player_name, r1.rank, r1.tour
                FROM rankings r1
                INNER JOIN (
                    SELECT player_name, MAX(ranking_date) as latest_date
                    FROM rankings
                    GROUP BY player_name
                ) r2 ON r1.player_name = r2.player_name AND r1.ranking_date = r2.latest_date
                ORDER BY r1.rank
            ''')
            self.players = [(name, rank, tour) for name, rank, tour in cursor.fetchall()]
            conn.close()
        except sqlite3.OperationalError:
            # Pre-migration DB without a tour column (fresh ATP-only setup).
            try:
                cursor.execute('''
                    SELECT r1.player_name, r1.rank
                    FROM rankings r1
                    INNER JOIN (
                        SELECT player_name, MAX(ranking_date) as latest_date
                        FROM rankings
                        GROUP BY player_name
                    ) r2 ON r1.player_name = r2.player_name AND r1.ranking_date = r2.latest_date
                    ORDER BY r1.rank
                ''')
                self.players = [(name, rank, "ATP") for name, rank in cursor.fetchall()]
                conn.close()
            except Exception as e:
                print(f"Error loading players: {e}")
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

        filtered = [(name, rank, tour) for name, rank, tour in self.players
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
            input_widget = self.player1_input
        else:
            frame = self.results2_frame
            layout = self.results2_layout
            button_list = self.suggestion_buttons_p2
            input_widget = self.player2_input

        # Clear previous
        for i in reversed(range(layout.count())):
            layout.itemAt(i).widget().setParent(None)
        button_list.clear()

        if not filtered_players:
            frame.hide()
            return

        for i, (name, rank, tour) in enumerate(filtered_players):
            tag = "  ·  WTA" if tour == "WTA" else ""
            btn = QPushButton(f"#{rank} {name}{tag}")
            btn.clicked.connect(lambda checked, n=name, p=player_num: self.select_player(n, p))
            btn.setFixedHeight(38)  # larger height for better visibility
            btn.setMinimumWidth(320)  # Ensure minimum width for full names
            btn.setStyleSheet(self.get_button_style(False))  # Normal style initially
            layout.addWidget(btn)
            button_list.append(btn)

        self._position_overlay(frame, input_widget)

    def _position_overlay(self, frame, input_widget):
        """Float the results frame over the window, just under its input box.

        The frame is a child of the top-level window (not in any layout), so it
        overlays the dashboard instead of expanding the search widget and
        pushing the other panels down.

        Height is computed deterministically from the button count rather than
        from sizeHint(): once the frame has been shown the layout's cached size
        hint goes stale on re-layout and would collapse the frame to a sliver.
        """
        win = self.window()
        if frame.parentWidget() is not win:
            frame.setParent(win)

        lay = frame.layout()
        n = lay.count()
        m = lay.contentsMargins()
        # 38px fixed button height + ~6px CSS margin per row + inter-row spacing.
        row_h = 38 + 6
        height = m.top() + m.bottom() + n * row_h + max(0, n - 1) * lay.spacing()
        height = min(height, 320)

        pt = input_widget.mapTo(win, QPoint(0, input_widget.height() + 2))
        frame.setGeometry(pt.x(), pt.y(), input_widget.width(), height)
        frame.raise_()
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
                name = suggestions[selected_index][0]
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
        self.peak_date = None
        self.setFixedSize(100, 80)

    def update_ranking(self, current_rank: Optional[int], peak_rank: Optional[int],
                       elo_rating: Optional[int] = None, peak_date: Optional[str] = None):
        """Update ranking information"""
        self.current_rank = current_rank
        self.peak_rank = peak_rank
        self.elo_rating = elo_rating
        self.peak_date = peak_date
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
            painter.setFont(QFont("Arial", 19, QFont.Weight.Bold))
            rank_text = f"#{self.current_rank}"
            painter.drawText(5, 2, 90, 30, Qt.AlignmentFlag.AlignCenter, rank_text)

            # Peak rank (smaller)
            if self.peak_rank:
                painter.setPen(QColor(TennisTheme.SECONDARY))
                painter.setFont(QFont("Arial", 10))
                peak_text = f"Peak: #{self.peak_rank}"
                painter.drawText(5, 31, 90, 14, Qt.AlignmentFlag.AlignCenter, peak_text)

                # Peak date (when first achieved)
                if self.peak_date:
                    painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                    painter.setFont(QFont("Arial", 7))
                    painter.drawText(5, 44, 90, 12, Qt.AlignmentFlag.AlignCenter,
                                     str(self.peak_date))

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
    surfaceStatsLoaded = pyqtSignal(str, object, int)  # player_name, SurfaceStats, player_num
    yearlyStatsLoaded = pyqtSignal(str, dict, int)  # player_name, yearly_stats_dict, player_num
    rawPlayerDataLoaded = pyqtSignal(str, object, int)  # player_name, raw_player_data, player_num
    recentResultsLoaded = pyqtSignal(str, list, int)  # player_name, recent_results_list, player_num
    historicalMatchesLoaded = pyqtSignal(str, list, int)  # player_name, historical_matches_list, player_num

    def __init__(self, player_color: str = TennisTheme.PRIMARY, player_num: int = 1):
        super().__init__()
        self.player_color = player_color
        self.player_num = player_num
        self.player_name = ""
        self.player_bio: Optional[PlayerBio] = None
        self.current_ranking: Optional[PlayerRanking] = None
        self.surface_stats = SurfaceStats()

        # Scrapers
        self.h2h_scraper = TennisScraper()

        # Database path for rankings
        self.db_path = "tennis_rankings.db"

        self.setup_ui()

    def get_player_rankings_from_db(self, player_name: str) -> Tuple[Optional[int], Optional[int], bool]:
        """Get current and peak rankings from the database, and whether current rank is recent"""
        try:

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Get current ranking (most recent entry) with date
            cursor.execute("""
                SELECT rank, ranking_date FROM rankings
                WHERE player_name = ?
                ORDER BY ranking_date DESC
                LIMIT 1
            """, (player_name,))
            current_result = cursor.fetchone()

            current_rank = None
            is_current = False

            if current_result:
                current_rank = current_result[0]
                ranking_date_str = current_result[1]

                # Check if ranking is recent (within last 3 months)
                try:
                    ranking_date = datetime.strptime(ranking_date_str, '%Y-%m-%d')
                    three_months_ago = datetime.now() - timedelta(days=90)
                    is_current = ranking_date >= three_months_ago
                except:
                    # If date parsing fails, consider it not current
                    is_current = False

            # Get peak ranking (minimum rank value)
            cursor.execute("""
                SELECT MIN(rank) FROM rankings
                WHERE player_name = ?
            """, (player_name,))
            peak_result = cursor.fetchone()
            peak_rank = peak_result[0] if peak_result else None

            conn.close()
            return current_rank, peak_rank, is_current

        except Exception as e:
            print(f"Error getting rankings from database for {player_name}: {e}")
            return None, None, False

    def get_peak_ranking_date(self, player_name: str, peak_rank: Optional[int]) -> Optional[str]:
        """Return a 'MMM YYYY' label for when the player first reached peak_rank."""
        if not peak_rank:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MIN(ranking_date) FROM rankings
                WHERE player_name = ? AND rank = ?
            """, (player_name, peak_rank))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                try:
                    return datetime.strptime(row[0], '%Y-%m-%d').strftime("%b %Y")
                except ValueError:
                    return row[0]
            return None

        except Exception as e:
            print(f"Error getting peak ranking date for {player_name}: {e}")
            return None

    def setup_ui(self):
        """Setup the compact player profile UI"""
        self.setFixedSize(420, 152)  # Trimmed height: content only needs ~135px
        self.setStyleSheet(f"""
            PlayerProfileWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {self.player_color};
                border-radius: 12px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 4)  # Reduced bottom margin to decrease spacing
        layout.setSpacing(8)

        # Header with player name and recent form
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        self.name_label = QLabel("Select Player")
        self.name_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        self.name_label.setStyleSheet(f"color: {self.player_color}; margin-bottom: 4px;")

        # Recent form display (moved from bottom)
        self.form_display = QLabel("")
        self.form_display.setFont(QFont("Arial", 12))
        self.form_display.setAlignment(Qt.AlignmentFlag.AlignRight)

        header_layout.addWidget(self.name_label)
        header_layout.addStretch()
        # header_layout.addWidget(self.form_display)

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
                # Load Tennis Abstract data - create new scraper instance for each use
                formatted_name = self.player_name.replace(' ', '')
                url = f"https://www.tennisabstract.com/cgi-bin/player.cgi?p={formatted_name}"

                abstract_scraper = TennisAbstractScraper(headless=True)
                try:
                    player_data = abstract_scraper._scrape_player_page(url)
                finally:
                    # Always close the scraper after use
                    abstract_scraper.close()
                    print(f"Closed scraper for {self.player_name}")

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

                    # Get rankings from database with recency check
                    try:
                        current_rank_db, peak_rank_db, is_current = self.get_player_rankings_from_db(self.player_name)

                        # Use database current ranking only if it's recent (within 3 months)
                        # Otherwise fallback to Tennis Abstract scraped ranking
                        if is_current and current_rank_db is not None:
                            current_rank = current_rank_db
                        else:
                            # Use Tennis Abstract current_rank as fallback for stale DB data
                            current_rank = int(player_data.player_bio.current_rank) if player_data.player_bio.current_rank.isdigit() else None

                        # Always use database peak rank if available (historical data is always valid)
                        peak_rank = peak_rank_db if peak_rank_db is not None else (int(player_data.player_bio.peak_rank) if player_data.player_bio.peak_rank.isdigit() else None)

                        # Peak date only meaningful when peak came from the DB.
                        peak_date = self.get_peak_ranking_date(self.player_name, peak_rank_db) if peak_rank_db is not None else None
                        self.ranking_widget.update_ranking(current_rank, peak_rank, elo_rating, peak_date)
                    except:
                        pass



                    # Emit raw player data for stats widget
                    if hasattr(player_data, '__dict__'):
                        # Convert dataclass to dict properly, including nested lists
                        try:
                            player_dict = asdict(player_data)
                            self.rawPlayerDataLoaded.emit(self.player_name, player_dict, self.player_num)
                        except Exception as e:
                            print(f"Error converting player data to dict: {e}")
                            # Fallback to __dict__ if asdict fails
                            self.rawPlayerDataLoaded.emit(self.player_name, player_data.__dict__, self.player_num)

                    # Emit recent results for form/momentum widget
                    if hasattr(player_data, 'recent_results') and player_data.recent_results:
                        self.recentResultsLoaded.emit(self.player_name, player_data.recent_results, self.player_num)

                    # Emit historical matches for enhanced momentum analysis
                    if hasattr(player_data, 'historical_matches') and player_data.historical_matches:
                        self.historicalMatchesLoaded.emit(self.player_name, player_data.historical_matches, self.player_num)

                # Get rankings from database for cases where Abstract data wasn't available
                if not self.player_bio:
                    try:
                        current_rank_db, peak_rank_db, is_current = self.get_player_rankings_from_db(self.player_name)
                        # Only use database ranking if it's current, otherwise show no ranking
                        if is_current and current_rank_db is not None:
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

        # Use the most recent season present in the data (falling back to the
        # calendar year if the keys are non-numeric).
        numeric_years = [y for y in yearly_stats if str(y).isdigit() and len(str(y)) == 4]
        current_year = max(numeric_years) if numeric_years else str(datetime.now().year)
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
            self.form_display.setText("")
            return

        # Take the first 5 matches from TennisTonic (they display in chronological order)
        # The rightmost position shows the most recent match
        first_5_matches = recent_form[:5] if len(recent_form) >= 5 else recent_form

        form_display = ""
        for result in first_5_matches:
            if result.upper() == 'W':
                form_display += ""
            elif result.upper() == 'L':
                form_display += ""
            else:
                form_display += ""  # Unknown/no data

        self.form_display.setText(form_display)







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
        self.selected_year = str(datetime.now().year)  # Year shown in season mode
        self.available_years = []  # Populated from loaded player season data
        self.setFixedSize(280, 288)  # Taller for the pill row above player names
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
        self.mode_toggle = QPushButton("Season")
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

        # Split/year toggle button - right side. In season mode it cycles the
        # displayed year; in career/last52 modes it cycles the split type.
        self.split_toggle = QPushButton(self.selected_year)
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
        self.player1_toggle.move(5, 46)  # Above player 1's name, left-aligned

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
        self.player2_toggle.move(240, 46)  # Above player 2's name, right-aligned

    def toggle_player_stats(self, player_num: int):
        """Toggle between Tour-Level and Challenger for specific player"""
        if player_num == 1:
            self.player1_show_tour = not self.player1_show_tour
            self.player1_toggle.setText("Tour" if self.player1_show_tour else "Chall")
        else:
            self.player2_show_tour = not self.player2_show_tour
            self.player2_toggle.setText("Tour" if self.player2_show_tour else "Chall")

        # Trigger repaint to show updated data
        self.update()

    def toggle_stats_mode(self):
        """Toggle between current year, career, and last 52 week stats"""
        if self.stats_mode == "current_year":
            self.stats_mode = "career"
            self.mode_toggle.setText("Career")
            self.split_toggle.setEnabled(True)  # Enable split toggle for career mode
            self.player1_toggle.setEnabled(True)  # Enable tour/challenger for splits (now we have both)
            self.player2_toggle.setEnabled(True)
            self.update_available_splits()
        elif self.stats_mode == "career":
            self.stats_mode = "last52"
            self.mode_toggle.setText("Last52")
            self.split_toggle.setEnabled(True)  # Enable split toggle for last52 mode
            self.player1_toggle.setEnabled(True)  # Enable tour/challenger for splits (now we have both)
            self.player2_toggle.setEnabled(True)
            self.update_available_splits()
        else:
            self.stats_mode = "current_year"
            self.mode_toggle.setText("Season")
            # In season mode the right-hand button cycles the displayed year.
            self.split_toggle.setText(self.selected_year)
            self.split_toggle.setEnabled(len(self.available_years) > 1)
            self.player1_toggle.setEnabled(True)  # Enable tour/challenger for season stats
            self.player2_toggle.setEnabled(True)
        self.update()

    def toggle_split_type(self):
        """Cycle the season year (season mode) or split type (career/last52)."""
        if self.stats_mode == "current_year":
            if len(self.available_years) < 2:
                return
            try:
                idx = self.available_years.index(self.selected_year)
            except ValueError:
                idx = -1
            self.selected_year = self.available_years[(idx + 1) % len(self.available_years)]
            self.split_toggle.setText(self.selected_year)
            self.update()
            return
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
        # Get splits from all players, combining both tour and challenger data
        tour_key = "career_splits" if self.stats_mode == "career" else "last52_splits"
        chall_key = "career_splits_chall" if self.stats_mode == "career" else "last52_splits_chall"

        # Gather all unique splits from both players and both levels
        all_splits = set()

        for player_data in [self.player1_data, self.player2_data]:
            if player_data:
                # Check tour-level splits (if available)
                if tour_key in player_data and player_data[tour_key]:
                    for split_data in player_data[tour_key]:
                        if isinstance(split_data, dict) and 'split' in split_data:
                            all_splits.add(split_data['split'])

                # Check challenger-level splits (if available)
                if chall_key in player_data and player_data[chall_key]:
                    for split_data in player_data[chall_key]:
                        if isinstance(split_data, dict) and 'split' in split_data:
                            all_splits.add(split_data['split'])

        # Convert to sorted list, prioritizing common splits
        priority_splits = ["Hard", "Clay", "Grass", "Grand Slams", "Masters", "vs Lefties", "vs Top 10"]
        self.available_splits = [s for s in priority_splits if s in all_splits]
        self.available_splits.extend(sorted([s for s in all_splits if s not in priority_splits]))

        # Set default split type if current one is not available
        if self.current_split_type not in self.available_splits and self.available_splits:
            self.current_split_type = self.available_splits[0]

        # Update button text and enable/disable based on available data
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

            # Enable button if we have multiple split types to cycle through
            if self.stats_mode in ["career", "last52"]:
                self.split_toggle.setEnabled(len(self.available_splits) > 1)
        else:
            self.split_toggle.setEnabled(False)

    def update_available_years(self):
        """Collect the season years present in either player's loaded data."""
        years = set()
        for player_data in [self.player1_data, self.player2_data]:
            if not player_data:
                continue
            for key in ("tour_seasons", "challenger_seasons"):
                for season in player_data.get(key) or []:
                    if isinstance(season, dict):
                        y = str(season.get('year', ''))
                        if y.isdigit() and len(y) == 4:
                            years.add(y)
        self.available_years = sorted(years, reverse=True)
        # Default to the most recent season with data if the selected year
        # (initially the calendar year) has none.
        if self.available_years and self.selected_year not in self.available_years:
            self.selected_year = self.available_years[0]
        if self.stats_mode == "current_year":
            self.split_toggle.setText(self.selected_year)
            self.split_toggle.setEnabled(len(self.available_years) > 1)

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
        self.update_available_years()

        self.update()

    def paintEvent(self, event):
        """Custom paint for stats display"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))

        # Title based on current mode
        mode_titles = {
            "current_year": f"{self.selected_year} Stats",
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
        # Player name header (drawn below the Tour/Chall pill row at y=46-60)
        painter.setPen(QColor(accent_color))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(x_offset + 5, y_offset + 23, player_name[:15] + ("..." if len(player_name) > 15 else ""))

        # Get data based on current mode
        if not player_data:
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(x_offset + 5, y_offset + 48,"No data")
            return

        current_data = None

        if self.stats_mode == "current_year":
            # Get selected-year stats - only use the requested level (no fallback)
            current_year = self.selected_year
            stats_key = "tour_seasons" if show_tour_level else "challenger_seasons"

            if stats_key not in player_data or not player_data[stats_key]:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                level_text = "tour" if show_tour_level else "challenger"
                painter.drawText(x_offset + 5, y_offset + 48,f"No {level_text} data")
                return

            # Find current year data
            for season in player_data[stats_key]:
                if isinstance(season, dict) and season.get('year') == current_year:
                    current_data = season
                    break

        elif self.stats_mode == "career":
            # Get career splits data - only use the requested level (no fallback)
            splits_key = "career_splits" if show_tour_level else "career_splits_chall"

            if splits_key not in player_data or not player_data[splits_key]:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                level_text = "tour" if show_tour_level else "challenger"
                painter.drawText(x_offset + 5, y_offset + 48,f"No {level_text} career data")
                return

            # Find the specific split type
            for split_data in player_data[splits_key]:
                if isinstance(split_data, dict) and split_data.get('split') == self.current_split_type:
                    current_data = split_data
                    break

        elif self.stats_mode == "last52":
            # Get last 52 weeks splits data - only use the requested level (no fallback)
            splits_key = "last52_splits" if show_tour_level else "last52_splits_chall"

            if splits_key not in player_data or not player_data[splits_key]:
                painter.setPen(QColor(TennisTheme.TEXT_MUTED))
                painter.setFont(QFont("Arial", 9))
                level_text = "tour" if show_tour_level else "challenger"
                painter.drawText(x_offset + 5, y_offset + 48,f"No {level_text} last52 data")
                return

            # Find the specific split type
            for split_data in player_data[splits_key]:
                if isinstance(split_data, dict) and split_data.get('split') == self.current_split_type:
                    current_data = split_data
                    break

        if not current_data:
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(x_offset + 5, y_offset + 48,"No data")
            return

        # Draw stats
        stats_y = y_offset + 43
        painter.setFont(QFont("Arial", 10))

        # Stats to display - handle both dict and object data
        def get_stat_value(data, key, default="0%"):
            if isinstance(data, dict):
                return data.get(key, default)
            else:
                return getattr(data, key, default)

        stats_items = [
            ("Set%", get_stat_value(current_data, "set_percentage", "0%")),
            ("Game%", get_stat_value(current_data, "game_percentage", "0%")),
            ("Hld%", get_stat_value(current_data, "hold_percentage", "0%")),
            ("Brk%", get_stat_value(current_data, "break_percentage", "0%")),
            ("SPW%", get_stat_value(current_data, "service_points_won", "0%")),
            ("1st%", get_stat_value(current_data, "first_serve_in", "0%")),
            ("2nd%", get_stat_value(current_data, "second_serve_won", "0%")),
            ("DF%", get_stat_value(current_data, "double_fault_rate", "0%")),
            ("RPW%", get_stat_value(current_data, "return_points_won", "0%")),
            ("DR", get_stat_value(current_data, "dominance_ratio", "0.0"))
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
                    if label in ["Set%", "Game%", "Hld%", "Brk%", "SPW%", "1st%", "2nd%", "RPW%"]:
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


class RecentFormMomentumWidget(QWidget):
    """Recent form and momentum widget with rolling averages graph"""

    def __init__(self):
        super().__init__()
        self.player1_recent_results = []
        self.player2_recent_results = []
        self.player1_historical_data = []
        self.player2_historical_data = []
        self.player1_name = ""
        self.player2_name = ""
        self.current_metric = "first_serve_in"  # Default metric
        self.current_surface = "All"  # Default surface filter
        self.current_match_count = 10  # Default rolling average window
        self.current_level = "All levels"  # Competition-level filter
        # Fixed height, but expand width to use the available right-hand space.
        self.setMinimumSize(950, 450)
        self.setMaximumHeight(450)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            RecentFormMomentumWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 12px;
            }}
        """)

        # Create dropdown controls
        self.create_controls()

    def create_controls(self):
        """Create metric and surface filter dropdown controls"""
        # Metric selector
        self.metric_combo = QComboBox(self)
        self.metric_combo.addItems([
            "1st Serve %", "Dominance Ratio", "Ace %", "Double Fault %",
            "1st Serve Won %", "2nd Serve Won %", "Break Points Saved %"
        ])
        self.metric_combo.setStyleSheet(f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                padding: 4px;
                min-width: 120px;
            }}
        """)
        self.metric_combo.currentTextChanged.connect(self.on_metric_changed)
        self.metric_combo.setGeometry(72, 45, 128, 25)

        # Surface filter
        self.surface_combo = QComboBox(self)
        self.surface_combo.addItems(["All", "Hard", "Clay", "Grass", "Carpet"])
        self.surface_combo.setStyleSheet(f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                padding: 4px;
                min-width: 80px;
            }}
        """)
        self.surface_combo.currentTextChanged.connect(self.on_surface_changed)
        self.surface_combo.setGeometry(258, 45, 80, 25)

        # Match count selection dropdown
        self.match_count_combo = QComboBox(self)
        self.match_count_combo.addItems(["10", "25", "50", "All Career"])
        self.match_count_combo.setStyleSheet(f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                padding: 4px;
                min-width: 70px;
            }}
        """)
        self.match_count_combo.currentTextChanged.connect(self.on_match_count_changed)
        self.match_count_combo.setGeometry(382, 45, 78, 25)

        # Competition-level filter (tour vs challenger vs ITF)
        self.level_combo = QComboBox(self)
        self.level_combo.addItems(["All levels", "Tour", "Challenger", "ITF"])
        self.level_combo.setStyleSheet(f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                padding: 4px;
                min-width: 95px;
            }}
        """)
        self.level_combo.currentTextChanged.connect(self.on_level_changed)
        self.level_combo.setGeometry(515, 45, 100, 25)

    def on_metric_changed(self, text):
        """Handle metric selection change"""
        metric_mapping = {
            "1st Serve %": "first_serve_in",
            "Dominance Ratio": "dominance_ratio",
            "Ace %": "ace_rate",
            "Double Fault %": "double_fault_rate",
            "1st Serve Won %": "first_serve_won",
            "2nd Serve Won %": "second_serve_won",
            "Break Points Saved %": "break_points_saved"
        }
        self.current_metric = metric_mapping.get(text, "first_serve_in")
        self.update()

    def on_surface_changed(self, text):
        """Handle surface filter change"""
        self.current_surface = text
        self.update()

    def on_match_count_changed(self, text):
        """Handle match count selection change"""
        if text == "All Career":
            self.current_match_count = -1  # Use all available matches
        else:
            self.current_match_count = int(text)
        self.update()

    def on_level_changed(self, text):
        """Handle competition-level filter change (Tour vs Challenger/ITF)."""
        self.current_level = text
        self.update()

    @staticmethod
    def _level_class(code):
        """Classify a Tennis Abstract level code as 'Tour', 'Challenger' or 'ITF'.

        TA level codes: G=Grand Slam, M=Masters/1000, A=ATP tour, F=Tour Finals,
        D=Davis Cup, O=Olympics, P=team event -> Tour; C=Challenger; everything
        else (S=Satellite/futures, 15/25=ITF $15k/$25k) -> ITF.
        """
        c = (code or '').strip()
        if c in ('G', 'M', 'A', 'F', 'D', 'O', 'P'):
            return 'Tour'
        if c == 'C':
            return 'Challenger'
        return 'ITF'

    def filter_by_level(self, matches):
        """Filter a match list by the current competition-level selection."""
        if self.current_level == "All levels":
            return matches
        want = self.current_level  # 'Tour' or 'Ch/ITF'
        out = []
        for m in matches:
            code = getattr(m, 'level', '')
            # Untagged matches (no level info) are only kept under "All levels".
            if code and self._level_class(code) == want:
                out.append(m)
        return out

    def filter_by_surface(self, recent_results):
        """Filter results by selected surface"""
        if self.current_surface == "All":
            return recent_results
        return [result for result in recent_results if result.surface == self.current_surface]

    def update_player_results(self, player_name: str, recent_results: list, player_num: int):
        """Update recent results for a player"""
        # Filter out matches with no underlying data
        filtered_results = self.filter_valid_matches(recent_results)

        if player_num == 1:
            self.player1_recent_results = filtered_results[:15]  # Last 15 valid matches
            self.player1_name = player_name
        else:
            self.player2_recent_results = filtered_results[:15]  # Last 15 valid matches
            self.player2_name = player_name
        self._tag_recent_levels(player_num)
        self.update()

    def _tag_recent_levels(self, player_num):
        """Stamp each recent-result match with its competition level by joining
        on date against the historical log (recent results carry no level)."""
        if player_num == 1:
            recent, hist = self.player1_recent_results, self.player1_historical_data
        else:
            recent, hist = self.player2_recent_results, self.player2_historical_data
        if not recent or not hist:
            return
        level_by_date = {}
        for h in hist:
            d = str(getattr(h, 'date', '') or '')
            lv = getattr(h, 'level', '')
            if d and lv and d not in level_by_date:
                level_by_date[d] = lv
        for m in recent:
            if getattr(m, 'level', ''):
                continue
            dt = self._match_date(m)
            if dt:
                lv = level_by_date.get(dt.strftime("%Y%m%d"))
                if lv:
                    try:
                        m.level = lv
                    except AttributeError:
                        pass

    def update_player_historical_data(self, player_name: str, historical_matches: list, player_num: int):
        """Update historical matches for enhanced analysis"""
        # Convert HistoricalMatchData to MatchResult format for compatibility
        converted_matches = []
        for match in historical_matches:
            if hasattr(match, 'date') and match.date:
                # Create MatchResult-compatible object from HistoricalMatchData
                # Handle missing fields gracefully
                converted_match = type('MatchResult', (), {
                    'date': getattr(match, 'date', ''),
                    'tournament': getattr(match, 'tournament', ''),
                    'surface': getattr(match, 'surface', ''),
                    'round': getattr(match, 'round', ''),
                    'opponent': getattr(match, 'opponent', ''),
                    'result': getattr(match, 'result', ''),  # 'Win'/'Loss' (needed for W/L)
                    'level': getattr(match, 'level', ''),    # tour vs challenger/ITF
                    'score': getattr(match, 'score', ''),
                    'dominance_ratio': getattr(match, 'dominance_ratio', ''),
                    'ace_rate': getattr(match, 'ace_rate', ''),
                    'double_fault_rate': getattr(match, 'double_fault_rate', ''),
                    'first_serve_in': getattr(match, 'first_serve_in', ''),
                    'first_serve_won': getattr(match, 'first_serve_won', ''),
                    'second_serve_won': getattr(match, 'second_serve_won', ''),
                    'break_points_saved': getattr(match, 'break_points_saved', ''),
                    'match_time': getattr(match, 'match_time', '')
                })()
                converted_matches.append(converted_match)

        # Store the full career log (bounded for safety) so "All Career" really
        # spans the whole career; the dropdown limits how much is actually used.
        if player_num == 1:
            self.player1_historical_data = converted_matches[:1500]
            if len(self.player1_recent_results) < 10:
                self.player1_recent_results = converted_matches[:15]
        else:
            self.player2_historical_data = converted_matches[:1500]
            if len(self.player2_recent_results) < 10:
                self.player2_recent_results = converted_matches[:15]
        self._tag_recent_levels(player_num)
        self.update()

    def filter_valid_matches(self, recent_results: list) -> list:
        """Filter out matches that have no underlying statistical data"""
        valid_matches = []

        for match in recent_results:
            # Check if match has meaningful data
            if self.has_valid_match_data(match):
                valid_matches.append(match)

        return valid_matches

    def has_valid_match_data(self, match_result) -> bool:
        """Check if a match result has valid underlying data"""
        # Check for presence of key statistical fields
        required_fields = ['opponent', 'score']
        optional_stats = ['first_serve_in', 'ace_rate', 'double_fault_rate', 'dominance_ratio']

        # Must have basic match info
        for field in required_fields:
            if not hasattr(match_result, field) or not getattr(match_result, field):
                return False

        # Check if score is meaningful (not empty or placeholder)
        score = getattr(match_result, 'score', '')
        if not score or score.strip() == '' or score.strip() == '--':
            return False

        # Check if opponent field has meaningful data
        opponent = getattr(match_result, 'opponent', '')
        if not opponent or opponent.strip() == '' or opponent.strip() == '--':
            return False

        # At least one statistical field should have data
        has_stats = False
        for stat in optional_stats:
            if hasattr(match_result, stat):
                value = getattr(match_result, stat)
                if value and value.strip() != '' and value.strip() != '--':
                    has_stats = True
                    break

        return has_stats

    def paintEvent(self, event):
        """Custom paint for recent form and momentum display"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))

        # Title
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        title_rect = QRect(0, 10, self.width(), 30)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, "Recent Form & Momentum")

        # Draw labels for controls
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(15, 60, "Metric:")
        painter.drawText(208, 60, "Surface:")
        painter.drawText(348, 60, "Last:")
        painter.drawText(470, 60, "Level:")

        # Account for controls at top (70px height for controls)
        controls_height = 75
        content_y = controls_height
        content_height = self.height() - controls_height - 10

        # Split into two main areas: Form summary (left) and Rolling averages graph (right)
        form_area_width = 300
        graph_area_width = self.width() - form_area_width - 20

        # Draw form summary area
        self.draw_form_summary(painter, 10, content_y, form_area_width, content_height)

        # Draw rolling averages graph area
        self.draw_rolling_averages_graph(painter, form_area_width + 20, content_y, graph_area_width, content_height)

    def get_form_dataset(self, player_num):
        """Surface- and count-filtered match list for a player's form summary.

        Applies the Surface and Matches dropdowns consistently and pulls from the
        deeper historical log when the requested window exceeds the recent list.
        """
        if player_num == 1:
            recent = self.player1_recent_results or []
            hist = self.player1_historical_data or []
        else:
            recent = self.player2_recent_results or []
            hist = self.player2_historical_data or []

        count = self.current_match_count          # -1 means "All Career"
        need = (10 ** 9) if count < 0 else count
        base = recent
        # Use the historical log when we need a bigger window than recent covers.
        if hist and len(hist) > len(base) and need > len(base):
            base = hist

        filtered = self.filter_by_surface(base)
        filtered = self.filter_by_level(filtered)
        if count > 0:
            filtered = filtered[:count]
        return filtered

    def draw_form_summary(self, painter, x, y, width, height):
        """Draw recent form summary for both players"""
        ds1 = self.get_form_dataset(1)
        ds2 = self.get_form_dataset(2)

        # Player 1 section
        painter.setPen(QColor(TennisTheme.PRIMARY))
        painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        player1_rect = QRect(x, y, width, 25)
        painter.drawText(player1_rect, Qt.AlignmentFlag.AlignLeft, self.player1_name or "Player 1")
        self.draw_player_form(painter, x, y + 30, width, 150, ds1, TennisTheme.PRIMARY, self.player1_name)

        # Separator line
        painter.setPen(QColor(TennisTheme.SURFACE))
        painter.drawLine(x, y + 200, x + width, y + 200)

        # Player 2 section
        painter.setPen(QColor(TennisTheme.ACCENT))
        painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        player2_rect = QRect(x, y + 220, width, 25)
        painter.drawText(player2_rect, Qt.AlignmentFlag.AlignLeft, self.player2_name or "Player 2")
        self.draw_player_form(painter, x, y + 250, width, 150, ds2, TennisTheme.ACCENT, self.player2_name)

    def draw_player_form(self, painter, x, y, width, height, dataset, color, player_name):
        """Draw form indicators for one player from a pre-filtered dataset."""
        if not dataset:
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 10))
            filt = []
            if self.current_surface != "All":
                filt.append(self.current_surface)
            if self.current_level != "All levels":
                filt.append(self.current_level)
            note = (f"No {' / '.join(filt)} matches" if filt
                    else "No recent results available")
            painter.drawText(x, y + 20, note)
            return

        # Win/Loss streak — show the most recent up to what fits (~13 circles).
        form_y = y + 10
        circle_size = 18
        circle_spacing = 22
        max_circles = max(1, min(13, width // circle_spacing))
        circles = dataset[:max_circles]

        painter.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        for i, result in enumerate(circles):
            circle_x = x + (i * circle_spacing)
            is_win = self.is_match_win(result, player_name)
            fill = "#31596F" if is_win else "#6b3562"
            painter.setBrush(QBrush(QColor(fill)))
            painter.setPen(QColor(fill))
            painter.drawEllipse(circle_x, form_y, circle_size, circle_size)
            painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            painter.drawText(QRect(circle_x, form_y, circle_size, circle_size),
                             Qt.AlignmentFlag.AlignCenter, "W" if is_win else "L")

        # Win/Loss record over the FULL selected window (not just shown circles).
        stats_y = form_y + 35
        wins = sum(1 for r in dataset if self.is_match_win(r, player_name))
        total = len(dataset)
        win_pct = (wins / total * 100) if total > 0 else 0
        window = "career" if self.current_match_count < 0 else f"last {total}"
        surf_tag = "" if self.current_surface == "All" else f" · {self.current_surface}"
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 9))
        painter.drawText(x, stats_y, f"{window.capitalize()}{surf_tag}: "
                                     f"{wins}W-{total-wins}L ({win_pct:.1f}%)")

        # Comprehensive averaged stats over the same dataset.
        all_stats = self.calculate_comprehensive_stats(dataset)
        n_stat = sum(1 for r in dataset
                     if self.get_match_stat_value(r, 'first_serve_in') is not None)
        if n_stat:
            stats_y_start = stats_y + 20
            painter.setFont(QFont("Arial", 8))
            row_height = 12

            def fpct(key):
                v = all_stats.get(key)
                return f"{v:.1f}%" if v is not None else "—"

            def fdr(key):
                v = all_stats.get(key)
                return f"{v:.2f}" if v is not None else "—"

            col1_stats = [
                f"DR: {fdr('dominance_ratio')}",
                f"1st Serve: {fpct('first_serve_in')}",
                f"Aces: {fpct('ace_rate')}",
                f"DF: {fpct('double_fault_rate')}",
            ]
            for i, t in enumerate(col1_stats):
                painter.drawText(x, stats_y_start + i * row_height, t)
            col2_x = x + 140
            col2_stats = [
                f"1st Won: {fpct('first_serve_won')}",
                f"2nd Won: {fpct('second_serve_won')}",
                f"BP Saved: {fpct('break_points_saved')}",
                f"Stat sample: {n_stat}/{total}",
            ]
            for i, t in enumerate(col2_stats):
                painter.drawText(col2_x, stats_y_start + i * row_height, t)

    @staticmethod
    def _match_date(match):
        """Parse a match date from the several formats in play -> datetime/None."""
        d = str(getattr(match, 'date', '') or '').strip()
        if not d:
            return None
        for fmt in ("%d-%b-%Y", "%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        return None

    def _form_series(self, dataset, player_name):
        """Per-match points (oldest->newest) that have a value for the current
        metric. Each point: {v, date, win, surface}. Matches are later plotted on
        an EVEN per-match x-axis (not calendar) because tennis matches cluster in
        tournament windows, which distorts a continuous date axis."""
        pts = []
        for m in dataset:               # dataset is newest-first
            v = self.get_match_stat_value(m, self.current_metric)
            if v is None:
                continue
            pts.append({
                'v': v,
                'date': self._match_date(m),
                'win': self.is_match_win(m, player_name),
                'surface': getattr(m, 'surface', ''),
            })
        pts.reverse()                   # chronological: oldest first
        return pts

    def draw_rolling_averages_graph(self, painter, x, y, width, height):
        """Draw rolling averages graph for serve statistics"""
        # Graph background - use card background to avoid green tint
        painter.fillRect(x, y, width, height, QColor(TennisTheme.CARD_BACKGROUND))

        # Graph border. Clear the brush first: the W/L form circles leave a fill
        # brush set, and drawRect would otherwise flood the plot with it.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(TennisTheme.TEXT_MUTED))
        painter.drawRect(x, y, width, height)

        # Title
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        title_rect = QRect(x, y + 10, width, 20)
        # Dynamic title based on selected metric
        metric_titles = {
            "first_serve_in": "1st Serve %",
            "dominance_ratio": "Dominance Ratio",
            "ace_rate": "Ace %",
            "double_fault_rate": "Double Fault %",
            "first_serve_won": "1st Serve Won %",
            "second_serve_won": "2nd Serve Won %",
            "break_points_saved": "Break Points Saved %"
        }
        metric_title = metric_titles.get(self.current_metric, "1st Serve %")
        surface_text = f" ({self.current_surface})" if self.current_surface != "All" else ""
        title_text = f"{metric_title} - Rolling Averages{surface_text}"
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, title_text)

        # Graph area
        graph_x = x + 40
        graph_y = y + 40
        graph_width = width - 80
        graph_height = height - 80

        # Draw axes
        painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        painter.drawLine(graph_x, graph_y + graph_height, graph_x + graph_width, graph_y + graph_height)  # X-axis
        painter.drawLine(graph_x, graph_y, graph_x, graph_y + graph_height)  # Y-axis

        # Same surface- and count-filtered datasets as the form summary so the
        # graph, the W/L record and the averaged stats are always consistent.
        filtered_p1_results = self.get_form_dataset(1)
        filtered_p2_results = self.get_form_dataset(2)

        # Dynamic Y-axis based on metric type and data range
        if self.current_metric == "dominance_ratio":
            # For DR, use a different scale (typically 0.5 to 2.0)
            min_val, max_val, step = self.calculate_dr_axis_range(filtered_p1_results, filtered_p2_results)
            y_format = "{:.1f}"
        else:
            # For percentages, calculate dynamic range based on actual data
            min_val, max_val, step = self.calculate_percentage_axis_range(filtered_p1_results, filtered_p2_results)
            y_format = "{:.0f}%"

        # Y-axis labels with dynamic range
        painter.setFont(QFont("Arial", 8))
        num_labels = int((max_val - min_val) / step) + 1
        for i in range(num_labels):
            value = min_val + (i * step)
            normalized_pos = (value - min_val) / (max_val - min_val) if max_val > min_val else 0
            label_y = int(graph_y + graph_height - (normalized_pos * graph_height))

            painter.drawText(graph_x - 35, label_y + 3, y_format.format(value))
            # Grid lines
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.drawLine(graph_x, int(label_y), graph_x + graph_width, int(label_y))
            painter.setPen(QColor(TennisTheme.TEXT_SECONDARY))

        # Build per-match series for both players (oldest->newest).
        s1 = self._form_series(filtered_p1_results, self.player1_name)
        s2 = self._form_series(filtered_p2_results, self.player2_name)

        # X-axis: even per-match spacing. Tick labels are real dates sampled at
        # index positions from the longer ("reference") series.
        ref = s1 if len(s1) >= len(s2) else s2
        ref_dates = [p['date'] for p in ref if p['date']]
        if len(ref) >= 2 and ref_dates:
            span_days = (max(ref_dates) - min(ref_dates)).days
            date_fmt = "%b'%y" if span_days > 200 else "%d %b"
            painter.setFont(QFont("Arial", 7))
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            for i in range(5):
                frac = i / 4
                tick_x = int(graph_x + frac * graph_width)
                idx = round(frac * (len(ref) - 1))
                d = ref[idx]['date']
                painter.drawLine(tick_x, graph_y + graph_height, tick_x, graph_y + graph_height + 3)
                align = (Qt.AlignmentFlag.AlignLeft if i == 0 else
                         Qt.AlignmentFlag.AlignRight if i == 4 else
                         Qt.AlignmentFlag.AlignHCenter)
                painter.drawText(QRect(tick_x - 34, graph_y + graph_height + 4, 68, 11),
                                 align | Qt.AlignmentFlag.AlignTop,
                                 d.strftime(date_fmt) if d else "")
            # "older -> newer" hint
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 7, QFont.Weight.Bold))
            painter.drawText(QRect(graph_x, graph_y + graph_height + 16, graph_width, 11),
                             Qt.AlignmentFlag.AlignHCenter, "← older          more recent →")

        # Plot each player: faint raw match dots + bold rolling-average trend line.
        t1 = self.plot_series(painter, graph_x, graph_y, graph_width, graph_height,
                              s1, TennisTheme.PRIMARY, min_val, max_val)
        t2 = self.plot_series(painter, graph_x, graph_y, graph_width, graph_height,
                              s2, TennisTheme.ACCENT, min_val, max_val)

        # Current-value boxes with a trend arrow (player identity = colour).
        p1_current = self.get_current_rolling_average(filtered_p1_results, self.current_metric)
        p2_current = self.get_current_rolling_average(filtered_p2_results, self.current_metric)
        if p1_current is not None:
            self.draw_value_box(painter, x + width - 150, y + 36,
                                p1_current, self.current_metric, TennisTheme.PRIMARY, t1)
        if p2_current is not None:
            self.draw_value_box(painter, x + width - 150, y + 78,
                                p2_current, self.current_metric, TennisTheme.ACCENT, t2)

    def plot_series(self, painter, gx, gy, gw, gh, pts, color, min_val, max_val):
        """Plot one player's metric on an even per-match x-axis: faint raw dots +
        a bold trailing rolling-average line. Returns the trend ('up'/'down'/'')."""
        n = len(pts)
        if n < 1:
            return ''
        vr = max_val - min_val if max_val > min_val else 1

        def X(i):
            return gx + (i / (n - 1) if n > 1 else 0.5) * gw

        def Y(v):
            norm = max(0.0, min(1.0, (v - min_val) / vr))
            return int(gy + gh - norm * gh)

        raw = [p['v'] for p in pts]
        k = 1 if n <= 12 else max(3, min(15, n // 8))
        smooth = [sum(raw[max(0, i - k + 1):i + 1]) / len(raw[max(0, i - k + 1):i + 1])
                  for i in range(n)]

        # Faint raw match dots (win = filled, loss = hollow) in player colour.
        # Skip them for very large windows so a full-career view stays clean.
        if n <= 60:
            dot = QColor(color); dot.setAlpha(120)
            for i, p in enumerate(pts):
                xp, yp = int(X(i)), Y(raw[i])
                if p['win']:
                    painter.setPen(QPen(dot, 1)); painter.setBrush(dot)
                else:
                    painter.setPen(QPen(dot, 1)); painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(xp - 2, yp - 2, 5, 5)

        # Bold rolling-average trend line.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color), 2))
        prev = None
        for i in range(n):
            xp, yp = int(X(i)), Y(smooth[i])
            if prev is not None:
                painter.drawLine(prev[0], prev[1], xp, yp)
            prev = (xp, yp)

        # Trend: compare the smoothed end vs the smoothed start.
        if n >= 3:
            if smooth[-1] - smooth[0] > 0.5:
                return 'up'
            if smooth[-1] - smooth[0] < -0.5:
                return 'down'
        return ''

    def get_match_stat_value(self, match, stat_key):
        """Get individual match statistic value"""
        try:
            if hasattr(match, stat_key):
                value_str = getattr(match, stat_key)
                if value_str and value_str.strip() not in ['', '--', 'N/A']:
                    # Handle different stat types
                    if stat_key == "break_points_saved":
                        # Handle fraction format "7/14" -> percentage
                        if '/' in value_str:
                            parts = value_str.split('/')
                            if len(parts) == 2 and parts[1] != '0':
                                saved = float(parts[0])
                                total = float(parts[1])
                                return (saved / total) * 100
                        else:
                            return float(value_str.replace('%', ''))
                    elif stat_key == "dominance_ratio":
                        # Dominance ratio is not a percentage
                        value = float(value_str)
                        if 0.1 <= value <= 5.0:  # Reasonable DR range
                            return value
                    else:
                        # Standard percentage values
                        value = float(value_str.replace('%', ''))
                        if 0 <= value <= 100:  # Reasonable percentage range
                            return value
        except (ValueError, AttributeError, ZeroDivisionError):
            pass
        return None

    def is_match_win(self, match_result, player_name=None):
        """Determine if match result is a win based on opponent column data"""
        # Prefer an explicit result field (historical matches carry 'Win'/'Loss').
        res = getattr(match_result, 'result', '')
        if isinstance(res, str) and res.strip() in ('Win', 'Loss'):
            return res.strip() == 'Win'

        if not hasattr(match_result, 'opponent') or not match_result.opponent:
            return True  # Default assumption if no data

        opponent_str = match_result.opponent.strip()

        # Skip incomplete matches (using "vs" instead of "d.")
        if ' vs ' in opponent_str:
            return True  # Default for incomplete matches

        # Tennis Abstract format: "Winner d. Loser"
        # Current player appears with name like "(1)PlayerName", "(14)PlayerName", etc.
        # Examples from actual data:
        # WINS: "(1)Rublev d. Emilio Nava [USA]", "(14)Rublev d. Lloyd Harris [RSA]"
        # LOSSES: "(7)Aleksandar Kovacevic [USA] d. (1)Rublev", "(2)Carlos Alcaraz [ESP] d. (14)Rublev"

        if ' d. ' in opponent_str:
            parts = opponent_str.split(' d. ')
            if len(parts) == 2:
                winner_part = parts[0].strip()
                loser_part = parts[1].strip()

                # Extract last name from full player name (e.g., "Andrey Rublev" -> "Rublev")
                if player_name:
                    # Get the last name from full name
                    last_name = player_name.split()[-1] if ' ' in player_name else player_name
                else:
                    # Try to extract player name from the context (fallback)
                    # Look for pattern like "(ranking)Name" and extract the name
                    match = re.search(r'\([^)]+\)([A-Za-z]+)', opponent_str)
                    if match:
                        last_name = match.group(1)
                    else:
                        # Fallback: check if winner starts with ranking
                        return winner_part.startswith('(')

                # Look specifically for the current player's last name with ranking prefix
                current_player_pattern = rf'\([^)]+\){re.escape(last_name)}'

                # Check if current player appears in winner or loser part
                winner_has_current_player = bool(re.search(current_player_pattern, winner_part))
                loser_has_current_player = bool(re.search(current_player_pattern, loser_part))

                if winner_has_current_player and not loser_has_current_player:
                    return True  # Current player won (appears in winner position)
                elif loser_has_current_player and not winner_has_current_player:
                    return False  # Current player lost (appears in loser position)
                else:
                    # Fallback: check if winner part starts with ranking
                    return winner_part.startswith('(')

        # No ' d. ' separator found - unclear format
        return True  # Default assumption


    def calculate_comprehensive_stats(self, recent_results):
        """Calculate comprehensive averaged statistics from recent results.

        Delegates per-match parsing to get_match_stat_value so every stat is read
        the same way it is for the graph — crucially, break_points_saved arrives
        as a "saved/faced" fraction ("4/6"), NOT a percentage, and dominance
        ratio is a bare number. Parsing those inline as percentages silently
        dropped them (ValueError), which is why DR / BP Saved showed "—"."""
        keys = ['dominance_ratio', 'first_serve_in', 'ace_rate',
                'double_fault_rate', 'first_serve_won', 'second_serve_won',
                'break_points_saved']
        counts = {key: 0 for key in keys}
        totals = {key: 0.0 for key in keys}

        for result in recent_results:
            for stat_key in keys:
                value = self.get_match_stat_value(result, stat_key)
                if value is not None:
                    totals[stat_key] += value
                    counts[stat_key] += 1

        # Average only stats that had data; mark the rest as unavailable (None)
        # so the UI shows "—" instead of a misleading 0.00.
        return {key: (totals[key] / counts[key] if counts[key] > 0 else None)
                for key in keys}

    def calculate_dr_axis_range(self, p1_results, p2_results):
        """Calculate appropriate axis range for Dominance Ratio based on individual match values"""
        all_values = []

        # Collect individual DR values from both players
        for results in [p1_results, p2_results]:
            matches_to_show = results[:self.current_match_count] if self.current_match_count > 0 else results
            for match in matches_to_show:
                value = self.get_match_stat_value(match, "dominance_ratio")
                if value is not None:
                    all_values.append(value)

        if not all_values:
            return 0.5, 2.0, 0.25  # Default DR range

        # Add padding to ensure all data points are visible
        data_min = min(all_values)
        data_max = max(all_values)
        range_padding = (data_max - data_min) * 0.1  # 10% padding

        min_val = max(0.1, data_min - max(0.1, range_padding))
        max_val = min(5.0, data_max + max(0.1, range_padding))

        # Round to nice values
        min_val = round(min_val * 4) / 4  # Round to nearest 0.25
        max_val = round(max_val * 4) / 4

        step = 0.25
        return min_val, max_val, step

    def calculate_percentage_axis_range(self, p1_results, p2_results):
        """Calculate appropriate axis range for percentage metrics based on individual match values"""
        all_values = []

        # Collect individual match values from both players
        for results in [p1_results, p2_results]:
            matches_to_show = results[:self.current_match_count] if self.current_match_count > 0 else results
            for match in matches_to_show:
                value = self.get_match_stat_value(match, self.current_metric)
                if value is not None:
                    all_values.append(value)

        if not all_values:
            return 0, 100, 25  # Default percentage range

        # Add padding to ensure all data points are visible
        data_min = min(all_values)
        data_max = max(all_values)
        range_padding = (data_max - data_min) * 0.1  # 10% padding

        min_val = max(0, data_min - max(5, range_padding))
        max_val = min(100, data_max + max(5, range_padding))

        # Round to nice values
        min_val = round(min_val / 10) * 10
        max_val = round(max_val / 10) * 10

        # Ensure reasonable range
        if max_val - min_val < 20:
            center = (min_val + max_val) / 2
            min_val = max(0, center - 15)
            max_val = min(100, center + 15)

        step = 10 if max_val - min_val > 40 else 5
        return min_val, max_val, step

    def get_current_rolling_average(self, recent_results, stat_key):
        """Get the most recent rolling average for display"""
        if not recent_results:
            return None

        window_size = self.current_match_count if self.current_match_count > 0 else len(recent_results)
        if len(recent_results) < window_size:
            window_size = len(recent_results)

        # Get the most recent window
        recent_window = recent_results[:window_size]
        return self.calculate_stat_average(recent_window, stat_key)

    def draw_value_box(self, painter, x, y, value, metric_key, color, trend=''):
        """Draw a value display box in the graph (with an optional trend arrow)."""
        box_width = 130
        box_height = 34

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.fillRect(x, y, box_width, box_height, QColor(color).darker(160))
        painter.setPen(QColor(color))
        painter.drawRect(x, y, box_width, box_height)

        # Format value based on metric type
        if metric_key == "dominance_ratio":
            value_text = f"{value:.2f}"
        else:
            value_text = f"{value:.1f}%"

        # Metric label
        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 8))
        painter.drawText(x + 6, y + 12, self.get_metric_display_name(metric_key))

        # Value + trend arrow
        painter.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        painter.drawText(x + 6, y + 28, value_text)
        if trend:
            arrow, acol = ("▲", "#4CAF50") if trend == 'up' else ("▼", "#FF6B6B")
            painter.setPen(QColor(acol))
            painter.drawText(x + box_width - 22, y + 28, arrow)

    def get_metric_display_name(self, metric_key):
        """Get display name for metric"""
        display_names = {
            "first_serve_in": "1st Serve %",
            "dominance_ratio": "Dom Ratio",
            "ace_rate": "Ace %",
            "double_fault_rate": "DF %",
            "first_serve_won": "1st Won %",
            "second_serve_won": "2nd Won %",
            "break_points_saved": "BP Saved %"
        }
        return display_names.get(metric_key, metric_key)

    def calculate_stat_average(self, match_window, stat_key):
        """Calculate average for a specific stat over a window of matches"""
        values = []
        for match in match_window:
            try:
                if hasattr(match, stat_key):
                    value_str = getattr(match, stat_key)
                    if value_str and value_str.strip() not in ['', '--', 'N/A']:
                        # Handle different stat types
                        if stat_key == "break_points_saved":
                            # Handle fraction format "7/14" -> percentage
                            if '/' in value_str:
                                parts = value_str.split('/')
                                if len(parts) == 2 and parts[1] != '0':
                                    saved = float(parts[0])
                                    total = float(parts[1])
                                    value = (saved / total) * 100
                                else:
                                    continue
                            else:
                                value = float(value_str.replace('%', ''))
                        elif stat_key == "dominance_ratio":
                            # Dominance ratio is not a percentage
                            value = float(value_str)
                            if 0.1 <= value <= 5.0:  # Reasonable DR range
                                values.append(value)
                            continue
                        else:
                            # Standard percentage values
                            value = float(value_str.replace('%', ''))

                        # Sanity check for percentage values (not DR)
                        if stat_key != "dominance_ratio" and 0 <= value <= 100:
                            values.append(value)

            except (ValueError, AttributeError, ZeroDivisionError):
                continue

        return sum(values) / len(values) if values else None


# ----------------------------------------------------------------------------- #
# Matchup analysis panel (Match Sim / Head-to-Head / Serve & Return)
# ----------------------------------------------------------------------------- #
def _to_float(value):
    """Parse a possibly-'%'-suffixed string to a float, else None."""
    if value is None:
        return None
    try:
        return float(str(value).replace('%', '').replace('\xa0', '').strip())
    except (ValueError, TypeError):
        return None


def _to_fraction(value):
    """Parse a percentage string ('66.4%') to a 0-1 fraction, else None."""
    f = _to_float(value)
    return f / 100.0 if f is not None else None


def _avg_pct(records, key):
    """Average a percentage-valued field across a list of dataclass-dicts."""
    vals = [_to_float(r.get(key)) for r in records]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _avg_num(records, key):
    vals = [_to_float(r.get(key)) for r in records]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# --- aggregation over the generic {'headers','rows'} Match-Charting tables ----- #
def _col_vals(tbl, idx):
    """Raw cell strings for column `idx` across all rows of a generic table."""
    rows = (tbl or {}).get('rows', [])
    return [r[idx] for r in rows if idx < len(r)]


def _agg_mean(tbl, idx):
    """Mean of a numeric / '%'-suffixed column, else None."""
    vals = [_to_float(v) for v in _col_vals(tbl, idx)]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _agg_max(tbl, idx):
    vals = [_to_float(v) for v in _col_vals(tbl, idx)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


_FRAC_RE = re.compile(r'\((\d+)\s*/\s*(\d+)\)')
_BARE_FRAC_RE = re.compile(r'^(\d+)\s*/\s*(\d+)$')


def _agg_rate(tbl, idx):
    """Aggregate an 'x/y' or 'pp% (x/y)' column into a true rate % by summing
    numerators and denominators across matches. Returns (pct, made, total)."""
    made = total = 0
    for v in _col_vals(tbl, idx):
        v = (v or '').strip()
        m = _FRAC_RE.search(v) or _BARE_FRAC_RE.match(v)
        if m:
            made += int(m.group(1))
            total += int(m.group(2))
    if total == 0:
        return None, 0, 0
    return made / total * 100.0, made, total


def parse_player_payload(data_dict: dict) -> dict:
    """Distil the raw scraped player dict into the fields the panel needs."""
    bio = data_dict.get('player_bio', {}) or {}
    elo = {
        'Overall': _to_float(bio.get('elo_rating')),
        'Hard': _to_float(bio.get('hard_elo')),
        'Clay': _to_float(bio.get('clay_elo')),
        'Grass': _to_float(bio.get('grass_elo')),
    }

    def _splits(key):
        out = {}
        for sp in data_dict.get(key, []) or []:
            name = sp.get('split', '')
            if name in ('Hard', 'Clay', 'Grass', 'Total'):
                out[name] = {
                    'spw': _to_fraction(sp.get('service_points_won')),
                    'rpw': _to_fraction(sp.get('return_points_won')),
                    'hold': _to_float(sp.get('hold_percentage')),
                    'brk': _to_float(sp.get('break_percentage')),
                    'dr': _to_float(sp.get('dominance_ratio')),
                    'm': _to_float(sp.get('matches')) or 0,
                }
        return out

    surf = _splits('career_splits')
    surf52 = _splits('last52_splits')   # trailing-52-week form (recent vs career)

    tac = data_dict.get('tactics', []) or []
    we = data_dict.get('winners_errors', []) or []
    style = {
        'net_freq': _avg_pct(tac, 'net_freq'),
        'net_w': _avg_pct(tac, 'net_w_pct'),
        'snv_freq': _avg_pct(tac, 'snv_freq'),
        'drop_freq': _avg_pct(tac, 'drop_freq'),
        'fh_wnr': _avg_pct(tac, 'fh_wnr_pct'),
        'bh_wnr': _avg_pct(tac, 'bh_wnr_pct'),
        'wufe': _avg_num(we, 'ratio'),
    }

    # Latest season serve detail (numeric year row with the largest year).
    latest, best_year = {}, -1
    for s in data_dict.get('tour_seasons', []) or []:
        try:
            y = int(str(s.get('year')).strip())
        except (ValueError, TypeError):
            continue
        if y > best_year:
            best_year, latest = y, s
    serve = {
        'year': str(best_year) if best_year > 0 else '',
        'matches': _to_float(latest.get('matches')),
        '1st_in': _to_float(latest.get('first_serve_in')),
        '1st_won': _to_float(latest.get('first_serve_won')),
        '2nd_won': _to_float(latest.get('second_serve_won')),
        'ace': _to_float(latest.get('ace_rate')),
        'df': _to_float(latest.get('double_fault_rate')),
    }
    # Charted-match sample size behind the playing-style averages.
    style['n'] = len(data_dict.get('tactics', []) or [])

    mcp = _aggregate_charting(data_dict)
    return {'elo': elo, 'surf': surf, 'surf52': surf52, 'style': style,
            'serve': serve, 'mcp': mcp,
            'tour': (bio.get('tour') or 'ATP'),
            'historical': data_dict.get('historical_matches', []) or []}


def _aggregate_charting(data_dict: dict) -> dict:
    """Aggregate the Match-Charting / point-by-point tables into a flat dict of
    averaged metrics for the comparison tabs. All keys -> float or None."""
    ss = data_dict.get('serve_speed_detail', {}) or {}
    sv = data_dict.get('mcp_serve', {}) or {}
    rt = data_dict.get('mcp_return', {}) or {}
    ra = data_dict.get('mcp_rally', {}) or {}
    pp = data_dict.get('pbp_points', {}) or {}
    pg = data_dict.get('pbp_games', {}) or {}
    ps = data_dict.get('pbp_stats', {}) or {}

    def rate(tbl, idx):
        return _agg_rate(tbl, idx)[0]

    m = {
        'charted_n': len(sv.get('rows', []) or ra.get('rows', []) or []),
        # --- serve speed (mph) ---
        'spd_1st': _agg_mean(ss, 3),       # 1st Avg
        'spd_1st_t': _agg_mean(ss, 5),     # 1st T Avg
        'spd_1st_wide': _agg_mean(ss, 6),  # 1st Wide Avg
        'spd_1st_max': _agg_max(ss, 7),    # Max 1st
        'spd_1st_sd': _agg_mean(ss, 4),    # 1st StDev (consistency)
        'spd_2nd': _agg_mean(ss, 9),       # 2nd Avg
        # --- serve effectiveness (mcp-serve) ---
        'srv_unret': _agg_mean(sv, 2),     # Unreturned %
        'srv_le3': _agg_mean(sv, 3),       # pts won <=3 shots %
        'srv_rip_w': _agg_mean(sv, 4),     # rally-in-play won %
        'srv_impact': _agg_mean(sv, 5),    # serve impact
        'srv_1st_unret': _agg_mean(sv, 6),
        'srv_2nd_unret': _agg_mean(sv, 13),
        # --- return (mcp-return) ---
        'ret_rip': _agg_mean(rt, 2),       # return in play %
        'ret_rip_w': _agg_mean(rt, 3),     # return pts won %
        'ret_wnr': _agg_mean(rt, 4),       # return winner %
        'ret_depth': _agg_mean(rt, 6),     # RDI (return depth index)
        'ret_slice': _agg_mean(rt, 7),     # slice %
        # --- rally (mcp-rally) ---
        'rally_len': _agg_mean(ra, 2),
        'rally_1_3': _agg_mean(ra, 5),     # win% rallies 1-3 shots
        'rally_4_6': _agg_mean(ra, 6),
        'rally_7_9': _agg_mean(ra, 7),
        'rally_10p': _agg_mean(ra, 8),     # win% rallies 10+ shots
        'rally_fh_share': _agg_mean(ra, 9),
        # --- clutch (pbp) ---
        'bp_conv': rate(pp, 3),            # break points converted
        'bp_saved': rate(pp, 6),           # break points saved
        'tb_spw': _agg_mean(pp, 9),        # tiebreak serve pts won
        'tb_rpw': _agg_mean(pp, 10),       # tiebreak return pts won
        'breakback': rate(pg, 4),          # break back %
        'hold_bpf': rate(pg, 6),           # hold when facing BP
        'consolidate': rate(pg, 7),        # consolidate a break %
        'serve_for_set': rate(pg, 8),      # held serving for the set
        'serve_for_match': rate(pg, 10),   # held serving for the match
        'deuce_spw': _agg_mean(ps, 7),
        'ad_spw': _agg_mean(ps, 9),
        'deuce_rpw': _agg_mean(ps, 10),
        'ad_rpw': _agg_mean(ps, 11),
    }
    return m


# Below this many matches a surface/season split is treated as a small,
# noise-prone sample and flagged in the UI.
LOW_SAMPLE_MATCHES = 20


def _player_overall_rates(parsed: dict):
    """Match-weighted serve/return rates across all surfaces (the player's own
    baseline), else tour average. Used as the shrinkage prior for a surface."""
    rows = [v for v in parsed.get('surf', {}).values()
            if v.get('spw') and v.get('rpw') and (v.get('m') or 0) > 0]
    if rows:
        wsum = sum(r['m'] for r in rows)
        spw = sum(r['spw'] * r['m'] for r in rows) / wsum
        rpw = sum(r['rpw'] * r['m'] for r in rows) / wsum
        return spw, rpw
    avg = tennis_sim.tour_serve_avg(parsed.get('tour', 'ATP'))
    return avg, 1.0 - avg


def surface_rates(parsed: dict, surface: str):
    """(spw, rpw) for a surface: career surface split shrunk toward a
    surface-offset prior, then blended toward the trailing-52-week split.

    Two refinements over plain own-baseline shrinkage (both standard in the
    point-based literature — Barnett/Clarke priors, Ingram's player x surface
    partial pooling and random-walk skill drift):

    * The shrinkage prior is the player's all-surface baseline PLUS the tour's
      surface offset (e.g. grass serve ~ +2.1pp over the all-tour average), so
      a player with little grass data is presumed to serve better on grass the
      way the whole tour does, instead of being dragged to his hard-court level.
    * The trailing-52-week surface split (if present) is shrunk toward the
      career estimate with a lighter pseudo-count and used as the final rate,
      weighting current form over career history (Kovalchik: ~1y windows beat
      career-to-date for established players).
    """
    base_spw, base_rpw = _player_overall_rates(parsed)
    # Tour-level surface offset applied to the player's own baseline.
    tour = parsed.get('tour', 'ATP')
    off = (tennis_sim.surface_serve_avg(surface, tour)
           - tennis_sim.tour_serve_avg(tour))
    prior_spw = base_spw + off
    prior_rpw = base_rpw - off

    s = parsed.get('surf', {}).get(surface)
    if s and s.get('spw') and s.get('rpw'):
        n = s.get('m') or 0
        spw = tennis_sim.shrink_rate(s['spw'], n, prior_spw)
        rpw = tennis_sim.shrink_rate(s['rpw'], n, prior_rpw)
    else:
        spw, rpw = prior_spw, prior_rpw

    s52 = parsed.get('surf52', {}).get(surface)
    if s52 and s52.get('spw') and s52.get('rpw'):
        n52 = s52.get('m') or 0
        spw = tennis_sim.shrink_rate(s52['spw'], n52, spw, pseudo=12.0)
        rpw = tennis_sim.shrink_rate(s52['rpw'], n52, rpw, pseudo=12.0)
    return spw, rpw




class WinProbBar(QWidget):
    """Two-sided win-probability bar with names and percentages."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(46)
        self.setMinimumWidth(300)
        self.p1_name = "Player 1"
        self.p2_name = "Player 2"
        self.p1_color = QColor(TennisTheme.PRIMARY)
        self.p2_color = QColor(TennisTheme.ACCENT)
        self.p1_prob = 0.5
        self.subtitle = ""

    def set_values(self, p1_name, p2_name, p1_prob, subtitle=""):
        self.p1_name = p1_name or "Player 1"
        self.p2_name = p2_name or "Player 2"
        self.p1_prob = max(0.0, min(1.0, p1_prob))
        self.subtitle = subtitle
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_h = 26
        y = 2
        split = int(w * self.p1_prob)

        # Bars
        p.setPen(Qt.PenStyle.NoPen)
        c1 = QColor(self.p1_color); c1.setAlpha(210)
        c2 = QColor(self.p2_color); c2.setAlpha(210)
        p.setBrush(c1)
        p.drawRoundedRect(QRect(0, y, split, bar_h), 4, 4)
        p.setBrush(c2)
        p.drawRoundedRect(QRect(split, y, w - split, bar_h), 4, 4)

        # Percent labels inside bars
        p.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRect(6, y, split - 8, bar_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   f"{self.p1_prob*100:.0f}%")
        p.drawText(QRect(split + 4, y, w - split - 10, bar_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"{(1-self.p1_prob)*100:.0f}%")

        # Names / subtitle below
        p.setFont(QFont("Arial", 8))
        p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        label = self.subtitle or f"{self.p1_name}   vs   {self.p2_name}"
        p.drawText(QRect(0, y + bar_h, w, h - bar_h - y),
                   Qt.AlignmentFlag.AlignCenter, label)
        p.end()


class DistributionView(QWidget):
    """Paints the Monte Carlo output: a total-games histogram (left) and the
    final-scoreline distribution (right, coloured by likely winner)."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(190)
        self.res = None
        self.p1_name = self.p2_name = ""
        self.p1_color = QColor(TennisTheme.PRIMARY)
        self.p2_color = QColor(TennisTheme.ACCENT)

    def set_data(self, res, p1_name, p2_name):
        self.res = res
        self.p1_name, self.p2_name = p1_name, p2_name
        self.update()

    def paintEvent(self, event):
        if not self.res:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        gap = 18
        left_w = int(w * 0.56)
        self._draw_games(p, QRect(0, 0, left_w, h))
        self._draw_scores(p, QRect(left_w + gap, 0, w - left_w - gap, h))
        p.end()

    def _draw_games(self, p, rect):
        gh = self.res.games_hist
        if not gh:
            return
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.setPen(QColor(TennisTheme.SECONDARY))
        p.drawText(rect.x(), rect.y() + 12, "Total games distribution")

        gmin, gmax = min(gh), max(gh)
        ncols = gmax - gmin + 1
        maxp = max(gh.values()) or 1.0
        top = rect.y() + 22
        # Leave room below the bars for TWO label rows (axis min/max + the
        # median/IQR caption); -26 clipped the caption on the widget's bottom
        # edge whenever the panel sat at its minimum height.
        bottom = rect.bottom() - 30
        plot_h = bottom - top
        bw = max(2.0, (rect.width() - 10) / ncols)
        med = self.res.games_quantile(0.5)
        q1 = self.res.games_quantile(0.25)
        q3 = self.res.games_quantile(0.75)

        base = QColor(self.p1_color)
        for i in range(ncols):
            g = gmin + i
            prob = gh.get(g, 0.0)
            bh = (prob / maxp) * plot_h
            x = rect.x() + 5 + i * bw
            inside = q1 <= g <= q3
            c = QColor(base)
            c.setAlpha(220 if inside else 90)  # highlight interquartile range
            p.fillRect(QRect(int(x), int(bottom - bh),
                             max(1, int(bw - 1)), int(bh)), c)

        # median marker
        med_x = rect.x() + 5 + (med - gmin + 0.5) * bw
        p.setPen(QPen(QColor(TennisTheme.SECONDARY), 1, Qt.PenStyle.DashLine))
        p.drawLine(int(med_x), top, int(med_x), bottom)

        # axis labels
        p.setFont(QFont("Arial", 8))
        p.setPen(QColor(TennisTheme.TEXT_MUTED))
        p.drawText(rect.x() + 5, bottom + 11, str(gmin))
        p.drawText(rect.right() - 18, bottom + 11, str(gmax))
        p.setPen(QColor(TennisTheme.SECONDARY))
        p.drawText(QRect(rect.x(), bottom + 15, rect.width(), 13),
                   Qt.AlignmentFlag.AlignCenter,
                   f"median {med}  ·  IQR {q1}-{q3}")

    def _draw_scores(self, p, rect):
        oriented = self.res.set_scores_oriented
        if not oriented:
            return
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.setPen(QColor(TennisTheme.SECONDARY))
        p.drawText(rect.x(), rect.y() + 12, "Most likely scorelines")

        sn1 = self.p1_name.split()[-1] if self.p1_name else "P1"
        sn2 = self.p2_name.split()[-1] if self.p2_name else "P2"
        items = list(oriented.items())[:6]
        maxp = max((v for _, v in items), default=1.0) or 1.0
        row_h = 20
        top = rect.y() + 24
        bar_x = rect.x() + 86
        bar_max = rect.right() - bar_x - 42

        p.setFont(QFont("Arial", 9))
        for i, (key, prob) in enumerate(items):
            side, sc = key.split(" ")
            who = sn1 if side == "A" else sn2
            color = self.p1_color if side == "A" else self.p2_color
            y = top + i * row_h
            p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            p.drawText(QRect(rect.x(), y, 84, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"{who} {sc}")
            bw = (prob / maxp) * bar_max
            c = QColor(color); c.setAlpha(200)
            p.fillRect(QRect(bar_x, y + 4, max(1, int(bw)), row_h - 8), c)
            p.setPen(QColor(TennisTheme.TEXT_PRIMARY))
            p.drawText(QRect(bar_x + int(bw) + 4, y, 40, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"{prob*100:.0f}%")


class ContextView(QWidget):
    """Situational-adjustment breakdown: the per-factor Elo deltas that bridge
    the static rating and the anchored sim, shown side by side for both players.

    This is what turns 'the sim and the market disagree' into an explanation —
    rest/rust, fatigue, surface adaptation and serve form each carry a signed
    Elo nudge, and the header line reconciles base Elo -> context -> headline."""

    def __init__(self):
        super().__init__()
        # Header (20px) + up to ~5 factor rows (24 + 5*15 = 99) ≈ 120px of real
        # content; reserving 150 left a dead band above the games histogram.
        # Cap it too so the panel packs tightly and the distribution below is
        # not shoved off the bottom on shorter windows.
        self.setMinimumHeight(126)
        self.setMaximumHeight(140)
        self.p1_name = self.p2_name = ""
        self.ctx1 = self.ctx2 = None
        self.base_prob = None      # pre-context Elo win prob for P1
        self.final_prob = None     # anchored headline for P1
        self.applied = True        # whether context deltas fed the anchor
        self.p1_color = QColor(TennisTheme.PRIMARY)
        self.p2_color = QColor(TennisTheme.ACCENT)

    def set_data(self, p1_name, p2_name, ctx1, ctx2, base_prob, final_prob, applied):
        self.p1_name, self.p2_name = p1_name, p2_name
        self.ctx1, self.ctx2 = ctx1, ctx2
        self.base_prob, self.final_prob = base_prob, final_prob
        self.applied = applied
        self.update()

    def paintEvent(self, event):
        if not (self.ctx1 and self.ctx2):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        # Header: reconciliation line.
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.setPen(QColor(TennisTheme.SECONDARY))
        p.drawText(0, 12, "Context adjustments" + ("" if self.applied else "  (info only — not applied)"))
        if self.base_prob is not None and self.final_prob is not None:
            sn1 = self.p1_name.split()[-1] if self.p1_name else "P1"
            p.setFont(QFont("Arial", 8))
            p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            txt = (f"{sn1}:  Elo {self.base_prob*100:.0f}%  →  "
                   f"anchored {self.final_prob*100:.0f}%")
            p.drawText(QRect(0, 2, w, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, txt)

        col_w = (w - 12) // 2
        self._draw_col(p, QRect(0, 20, col_w, self.height() - 20),
                       self.p1_name, self.ctx1, self.p1_color)
        self._draw_col(p, QRect(col_w + 12, 20, col_w, self.height() - 20),
                       self.p2_name, self.ctx2, self.p2_color)
        p.end()

    def _draw_col(self, p, rect, name, ctx, color):
        x, y = rect.x(), rect.y()
        sn = name.split()[-1] if name else "—"
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.setPen(QColor(color))
        net_txt = f"{ctx.net:+.0f}"
        p.drawText(x, y + 11, f"{sn}   net {net_txt} Elo")
        p.setFont(QFont("Arial", 8))
        row_h = 15
        for i, f in enumerate(ctx.factors):
            ry = y + 24 + i * row_h
            # delta chip colour: green positive, red negative, muted zero
            if abs(f.delta) < 0.5:
                dc = TennisTheme.TEXT_MUTED
            else:
                dc = "#4CAF50" if f.delta > 0 else "#FF6B6B"
            p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            p.drawText(QRect(x, ry, 82, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f.label)
            dtxt = f"{f.delta:+.0f}" if abs(f.delta) >= 0.5 else "0"
            p.setPen(QColor(dc))
            p.drawText(QRect(x + 82, ry, 34, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       dtxt)
            p.setPen(QColor(TennisTheme.TEXT_MUTED))
            p.setFont(QFont("Arial", 7))
            p.drawText(QRect(x + 120, ry, rect.width() - 120, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f.detail)
            p.setFont(QFont("Arial", 8))


class MatchSimTab(QWidget):
    """Surface-adjusted win probability + Monte Carlo scoreline texture."""

    mcReady = pyqtSignal(object, object, int)

    def __init__(self):
        super().__init__()
        self.p1_name = self.p2_name = ""
        self.p1 = self.p2 = None
        self._req = 0

        # Elo blend weight on the *surface* rating (remainder -> overall Elo).
        # 50/50 is the FiveThirtyEight / Tennis Abstract tested default.
        self._blend_w = {
            "50/50 Elo blend": 0.5,
            "Surface Elo": 1.0,
            "75% surface": 0.75,
            "Overall Elo": 0.0,
        }
        # Headline model. The Monte Carlo is always run for scoreline/games
        # texture; these choose what its win probability is *anchored* to:
        #   Elo + Context - context-adjusted surface Elo (default, recommended)
        #   Elo only      - plain surface-blended Elo, context shown but not applied
        #   Raw sim       - unanchored serve/return rates (diagnostic; the old,
        #                   un-opponent-adjusted behaviour that over-favours big
        #                   servers on thin samples)
        self._models = ("Elo + Context", "Elo only", "Raw sim")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        lbl_style = f"color: {TennisTheme.TEXT_SECONDARY}; font-size: 11px;"
        # Pin a font-size so the combo's metrics (used by AdjustToContents) match
        # what is actually painted, and give the drop-down arrow its own box with
        # right padding so the selected text never renders under it.
        combo_style = f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                font-size: 11px;
                padding: 2px 24px 2px 6px;
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left: 1px solid {TennisTheme.TEXT_MUTED};
            }}
            QComboBox QAbstractItemView {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                selection-background-color: {TennisTheme.PRIMARY};
            }}
        """

        def mk_combo(items, width=64):
            c = QComboBox()
            c.addItems(items)
            c.setStyleSheet(combo_style)
            # Fixed size in BOTH axes. The width comes from the combo's own
            # sizeHint, which (with the stylesheet already applied) includes
            # the QSS padding + drop-down arrow that a bare minimum width
            # ignored — the old Fixed-policy floor left "Hard" / "50/50 Elo
            # blend" clipped behind the arrow box until a manual resize.
            c.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            c.setFixedWidth(max(width, c.sizeHint().width() + 4))
            c.setFixedHeight(24)
            c.currentTextChanged.connect(lambda *_: self.recompute())
            return c

        def mk_lbl(text):
            la = QLabel(text); la.setStyleSheet(lbl_style)
            return la

        # Two compact control rows.
        ctrl = QGridLayout()
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setHorizontalSpacing(6)
        ctrl.setVerticalSpacing(3)
        self.surface_combo = mk_combo(["Hard", "Clay", "Grass"])
        self.bo_combo = mk_combo(["3", "5"], width=46)
        self.blend_combo = mk_combo(list(self._blend_w.keys()), width=120)
        self.model_combo = mk_combo(list(self._models), width=120)
        ctrl.addWidget(mk_lbl("Surface:"), 0, 0)
        ctrl.addWidget(self.surface_combo, 0, 1)
        ctrl.addWidget(mk_lbl("Best of:"), 0, 2)
        ctrl.addWidget(self.bo_combo, 0, 3)
        ctrl.addWidget(mk_lbl("Elo:"), 1, 0)
        ctrl.addWidget(self.blend_combo, 1, 1)
        ctrl.addWidget(mk_lbl("Model:"), 1, 2)
        ctrl.addWidget(self.model_combo, 1, 3)
        # Row 2: custom Elo override.
        self.custom_check = QCheckBox("Custom Elo")
        self.custom_check.setStyleSheet(
            f"color: {TennisTheme.TEXT_SECONDARY}; font-size: 11px;")
        self.custom_check.toggled.connect(lambda *_: self._on_custom_toggle())

        def mk_elo_edit():
            e = QLineEdit()
            e.setValidator(QIntValidator(800, 2600, self))
            e.setFixedWidth(60)
            e.setEnabled(False)
            e.setStyleSheet(f"""
                QLineEdit {{
                    background: {TennisTheme.SURFACE};
                    color: {TennisTheme.TEXT_PRIMARY};
                    border: 1px solid {TennisTheme.TEXT_MUTED};
                    padding: 2px; font-size: 11px;
                }}
                QLineEdit:disabled {{ color: {TennisTheme.TEXT_MUTED}; }}
            """)
            e.textEdited.connect(lambda *_: self.recompute())
            return e

        self.p1_elo_edit = mk_elo_edit()
        self.p2_elo_edit = mk_elo_edit()
        ctrl.addWidget(self.custom_check, 2, 0)
        ctrl.addWidget(self.p1_elo_edit, 2, 1)
        ctrl.addWidget(mk_lbl("vs"), 2, 2, Qt.AlignmentFlag.AlignRight)
        ctrl.addWidget(self.p2_elo_edit, 2, 3)
        ctrl.setColumnStretch(4, 1)
        layout.addLayout(ctrl)

        self.bar = WinProbBar()
        layout.addWidget(self.bar)

        # Detail labels
        self.elo_label = QLabel("")
        self.mc_label = QLabel("")
        self.score_label = QLabel("")
        for w_ in (self.elo_label, self.mc_label, self.score_label):
            w_.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY}; font-size: 11px;")
            w_.setWordWrap(True)
            layout.addWidget(w_)

        self.context_view = ContextView()
        layout.addWidget(self.context_view)

        self.dist_view = DistributionView()
        layout.addWidget(self.dist_view)
        layout.addStretch()

        self.placeholder = QLabel("Select two players to simulate the matchup.")
        self.placeholder.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(self.placeholder)

        self.mcReady.connect(self._on_mc)

    def _on_custom_toggle(self):
        on = self.custom_check.isChecked()
        self.p1_elo_edit.setEnabled(on)
        self.p2_elo_edit.setEnabled(on)
        self.recompute()

    def set_players(self, p1_name, p2_name, p1, p2):
        self.p1_name, self.p2_name = p1_name, p2_name
        self.p1, self.p2 = p1, p2
        self.recompute()

    def recompute(self):
        if not (self.p1 and self.p2):
            return
        self.placeholder.hide()
        surface = self.surface_combo.currentText()
        best_of = int(self.bo_combo.currentText())
        w_surf = self._blend_w.get(self.blend_combo.currentText(), 0.5)

        blend_name = self.blend_combo.currentText()
        e1 = tennis_sim.blend_elo(self.p1['elo'].get('Overall'),
                                  self.p1['elo'].get(surface), w_surf)
        e2 = tennis_sim.blend_elo(self.p2['elo'].get('Overall'),
                                  self.p2['elo'].get(surface), w_surf)

        if self.custom_check.isChecked():
            ce1, ce2 = _to_float(self.p1_elo_edit.text()), _to_float(self.p2_elo_edit.text())
            if ce1:
                e1 = ce1
            if ce2:
                e2 = ce2
            elo_src = "custom Elo"
        else:
            # Reflect the computed blend in the (disabled) fields for reference.
            if e1:
                self.p1_elo_edit.setText(f"{e1:.0f}")
            if e2:
                self.p2_elo_edit.setText(f"{e2:.0f}")
            elo_src = f"{blend_name} ({surface})"

        elo_p = tennis_sim.elo_win_prob(e1, e2) if (e1 and e2) else 0.5

        # Situational context (rest/rust, fatigue, surface adaptation, serve
        # form). Computed for display in every mode; only *applied* to the anchor
        # target in "Elo + Context". Cheap enough to do on the UI thread.
        model = self.model_combo.currentText()
        ctx1 = tennis_context.compute_context(self.p1, surface)
        ctx2 = tennis_context.compute_context(self.p2, surface)
        if model == "Elo + Context" and e1 and e2:
            adj_p, ea, eb = tennis_context.adjusted_win_prob(e1, e2, ctx1, ctx2)
        else:
            adj_p, ea, eb = elo_p, e1, e2

        # What the Monte Carlo is anchored to (None => raw, unanchored sim).
        if model == "Raw sim":
            anchor = None
        elif model == "Elo only":
            anchor = elo_p
        else:
            anchor = adj_p

        self.elo_label.setText(
            f"Elo · {elo_src}:  {self.p1_name} {e1:.0f}  vs  "
            f"{e2:.0f} {self.p2_name}   →  {elo_p*100:.0f}% / {(1-elo_p)*100:.0f}%"
            if (e1 and e2) else "Elo: unavailable"
        )
        self.bar.set_values(self.p1_name, self.p2_name,
                            anchor if anchor is not None else adj_p,
                            subtitle="simulating…")
        self.mc_label.setText("Running Monte Carlo…")
        self.score_label.setText("")

        spw1, rpw1 = surface_rates(self.p1, surface)
        spw2, rpw2 = surface_rates(self.p2, surface)
        self._req += 1
        req = self._req
        meta = {'elo_p': elo_p, 'base_prob': elo_p, 'anchor': anchor,
                'applied': model == "Elo + Context",
                'ctx1': ctx1, 'ctx2': ctx2, 'model': model}

        # Tour from the players' payloads (WTA if either matched the WTA Elo
        # report) — drives the serve baselines the sim prices holds against.
        tours = {self.p1.get('tour', 'ATP'), self.p2.get('tour', 'ATP')}
        tour = 'WTA' if 'WTA' in tours else 'ATP'
        serve_avg = tennis_sim.surface_serve_avg(surface, tour)

        def worker():
            try:
                res = tennis_sim.simulate_match(spw1, rpw1, spw2, rpw2,
                                                best_of=best_of, n=10000,
                                                anchor_p=anchor,
                                                serve_avg=serve_avg,
                                                form_sigma=tennis_sim.tour_form_sigma(tour))
                self.mcReady.emit(res, meta, req)
            except Exception as e:
                print(f"Match sim error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_mc(self, res, meta, req):
        if req != self._req:
            return  # stale result
        model = meta['model']
        # With anchoring the headline IS the simulated win prob (which now tracks
        # the target). Only "Raw sim" lets the serve/return rates speak alone.
        headline = res.p_a
        sub = {"Elo + Context": "Elo + context (anchored)",
               "Elo only": "Elo-anchored",
               "Raw sim": "Raw serve/return sim"}.get(model, "sim")
        if res.anchor_delta:
            sub += f"   ·  Δ{res.anchor_delta:+.2f}"
        odds1 = tennis_sim.prob_to_american(headline)
        odds2 = tennis_sim.prob_to_american(1 - headline)
        self.bar.set_values(self.p1_name, self.p2_name, headline,
                            subtitle=f"{sub}   {odds1} / {odds2}")
        self.mc_label.setText(
            f"Monte Carlo:  {self.p1_name} {res.p_a*100:.0f}% / "
            f"{res.p_b*100:.0f}% {self.p2_name}   ·   "
            f"avg {res.avg_games:.0f} games   ·   "
            f"straights {res.p_straights_winner*100:.0f}%   ·   "
            f"decider {res.p_decider*100:.0f}%")
        o, u = res.games_line_probs(round(res.avg_games) + 0.5)
        line = round(res.avg_games) + 0.5
        # Fair games handicap for whoever the sim favours, book-style sign.
        sline, scover = res.fair_spread()
        sn1 = self.p1_name.split()[-1] if self.p1_name else "P1"
        sn2 = self.p2_name.split()[-1] if self.p2_name else "P2"
        if sline <= -0.5:
            spread_txt = f"{sn1} {sline:+g} ({scover*100:.0f}%)"
        else:
            spread_txt = f"{sn2} {-sline:+g} ({(1-scover)*100:.0f}%)"
        self.score_label.setText(
            f"Total games:  median {res.games_quantile(0.5)}  ·  "
            f"O/U {line:g}  {o*100:.0f}% / {u*100:.0f}%   ·   "
            f"Fair spread:  {spread_txt}")
        self.dist_view.set_data(res, self.p1_name, self.p2_name)
        self.context_view.set_data(self.p1_name, self.p2_name,
                                   meta['ctx1'], meta['ctx2'],
                                   meta['base_prob'], headline, meta['applied'])


class HeadToHeadTab(QWidget):
    """Career head-to-head record and the list of prior meetings."""

    def __init__(self):
        super().__init__()
        self.p1_name = self.p2_name = ""
        self.p1_color = TennisTheme.PRIMARY
        self.p2_color = TennisTheme.ACCENT
        self._record = None
        self._matches = None
        self._p1_hist = None   # Tennis Abstract historical matches (richer: durations)
        self._p2_hist = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        self.header = QLabel("Select two players to load their head-to-head.")
        self.header.setStyleSheet(
            f"color: {TennisTheme.TEXT_PRIMARY}; font-size: 14px; font-weight: bold;")
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(3)
        self.rows_layout.addStretch()
        scroll.setWidget(self.rows_container)
        layout.addWidget(scroll)

    def set_players(self, p1_name, p2_name):
        self.p1_name, self.p2_name = p1_name, p2_name
        # Names can arrive after the H2H data; re-render once we have them.
        if self._has_data():
            self._render()

    def set_historical(self, p1_hist, p2_hist):
        """Tennis Abstract per-player match logs (carry durations + scores)."""
        self._p1_hist = p1_hist or []
        self._p2_hist = p2_hist or []
        if self._has_data():
            self._render()

    def set_h2h(self, record, h2h_matches):
        """record: 'p1wins-p2wins' string; h2h_matches: list of H2HMatch."""
        self._record = record
        self._matches = h2h_matches or []
        self._render()

    def _has_data(self):
        return bool(self._matches) or bool(self._p1_hist) or bool(self._p2_hist)

    @staticmethod
    def _surname(name):
        parts = (name or "").lower().split()
        return parts[-1] if parts else ""

    @staticmethod
    def _parse_sets(score):
        """Parse a winner-first score string -> [(w_games, l_games, had_tb), ...]."""
        out = []
        for tok in (score or "").split():
            tok = tok.strip()
            if "-" not in tok:
                continue
            core = tok.split("(")[0]
            try:
                a, b = core.split("-")[:2]
                out.append((int(a), int(b), "(" in tok))
            except ValueError:
                continue
        return out

    @staticmethod
    def _fmt_minutes(mins):
        if not mins:
            return "—"
        try:
            mins = int(float(mins))
        except (ValueError, TypeError):
            return "—"
        return f"{mins // 60}:{mins % 60:02d}"

    @staticmethod
    def _fmt_date(d):
        d = str(d or "")
        if len(d) == 8 and d.isdigit():       # YYYYMMDD (Tennis Abstract)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d

    def _clear_rows(self):
        while self.rows_layout.count() > 1:  # keep trailing stretch
            item = self.rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _build_meetings(self):
        """Normalised meeting list, preferring the richer TA historical logs."""
        sn1, sn2 = self._surname(self.p1_name), self._surname(self.p2_name)
        meetings = []

        # 1) Tennis Abstract historical (has match_time / durations).
        if sn1 and sn2 and self._p1_hist:
            for h in self._p1_hist:
                if sn2 in (h.get("opponent", "") or "").lower():
                    meetings.append({
                        "date": self._fmt_date(h.get("date")),
                        "sort": str(h.get("date") or ""),
                        "tournament": h.get("tournament", ""),
                        "surface": h.get("surface", ""),
                        "round": h.get("round", ""),
                        "p1_won": (h.get("result") == "Win"),
                        "score": h.get("score", ""),
                        "minutes": h.get("match_time"),
                    })
        # Fallback to player 2's log if player 1's wasn't available.
        if not meetings and sn1 and sn2 and self._p2_hist:
            for h in self._p2_hist:
                if sn1 in (h.get("opponent", "") or "").lower():
                    meetings.append({
                        "date": self._fmt_date(h.get("date")),
                        "sort": str(h.get("date") or ""),
                        "tournament": h.get("tournament", ""),
                        "surface": h.get("surface", ""),
                        "round": h.get("round", ""),
                        "p1_won": (h.get("result") == "Loss"),  # invert: p2's log
                        "score": h.get("score", ""),
                        "minutes": h.get("match_time"),
                    })
        # 2) Fallback to the Tennis Tonic H2H meeting list (no durations).
        if not meetings and self._matches:
            for m in self._matches:
                w = (m.winner or "").lower()
                meetings.append({
                    "date": self._fmt_date(m.date),
                    "sort": str(m.date or ""),
                    "tournament": m.tournament or "",
                    "surface": m.surface or "",
                    "round": m.round or "",
                    "p1_won": bool(sn1) and sn1 in w,
                    "score": m.score or "",
                    "minutes": None,
                })

        meetings.sort(key=lambda x: x["sort"], reverse=True)
        return meetings

    def _render(self):
        self._clear_rows()
        meetings = self._build_meetings()
        name1 = self.p1_name or "Player 1"
        name2 = self.p2_name or "Player 2"

        if not meetings:
            # Last resort: orient from the raw record string if present.
            p1w = p2w = 0
            rec = str(self._record or "").replace(":", "-")
            if "-" in rec:
                try:
                    p1w, p2w = (int(x) for x in rec.split("-")[:2])
                except ValueError:
                    pass
            self.header.setText(f"Head-to-Head:  {name1} {p1w} – {p2w} {name2}")
            lbl = QLabel("No prior meetings on record.")
            lbl.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 12px;")
            self.rows_layout.insertWidget(0, lbl)
            return

        # --- aggregate stats ---------------------------------------------
        p1w = sum(1 for m in meetings if m["p1_won"])
        p2w = len(meetings) - p1w
        by_surf = {}
        p1_sets = p2_sets = p1_games = p2_games = 0
        tiebreaks = deciders = 0
        total_min = 0
        n_timed = 0
        longest = (0, "")
        for m in meetings:
            s = by_surf.setdefault(m["surface"] or "?", [0, 0])
            s[0 if m["p1_won"] else 1] += 1
            sets = self._parse_sets(m["score"])
            wsets = sum(1 for a, b, _ in sets if a > b)
            lsets = sum(1 for a, b, _ in sets if b > a)
            if m["p1_won"]:
                p1_sets += wsets; p2_sets += lsets
            else:
                p2_sets += wsets; p1_sets += lsets
            wg = sum(a for a, b, _ in sets); lg = sum(b for a, b, _ in sets)
            if m["p1_won"]:
                p1_games += wg; p2_games += lg
            else:
                p2_games += wg; p1_games += lg
            tiebreaks += sum(1 for _, _, tb in sets if tb)
            if len(sets) >= 3:
                deciders += 1
            try:
                mm = int(float(m["minutes"])) if m["minutes"] else 0
            except (ValueError, TypeError):
                mm = 0
            if mm:
                total_min += mm; n_timed += 1
                if mm > longest[0]:
                    longest = (mm, f"{m['date']} {m['tournament']}")

        self.header.setText(f"Head-to-Head:  {name1} {p1w} – {p2w} {name2}  "
                            f"({len(meetings)} meetings)")

        self._add_stats_block(name1, name2, p1w, p2w, by_surf, p1_sets, p2_sets,
                              p1_games, p2_games, tiebreaks, deciders,
                              total_min, n_timed, longest)

        # --- meeting list ------------------------------------------------
        hdr = self._meeting_row(("Date", "Tournament", "Surf", "Rd", "Winner",
                                 "Score", "Time"), header=True)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, hdr)
        for m in meetings:
            color = self.p1_color if m["p1_won"] else self.p2_color
            who = (name1 if m["p1_won"] else name2).split()[-1]
            row = self._meeting_row(
                (m["date"], m["tournament"], m["surface"], m["round"], who,
                 m["score"], self._fmt_minutes(m["minutes"])),
                winner_color=color)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)

    def _meeting_row(self, cols, header=False, winner_color=None):
        row = QFrame()
        if not header:
            row.setStyleSheet(f"background: {TennisTheme.CARD_BACKGROUND}; border-radius: 3px;")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(8, 2, 8, 2)
        rl.setSpacing(8)
        widths = [78, 140, 46, 38, 84, 130, 46]
        date, tourn, surf, rnd, who, score, tm = cols
        muted = TennisTheme.TEXT_MUTED
        sec = TennisTheme.TEXT_SECONDARY

        def cell(text, w, col, bold=False, align_r=False):
            lab = QLabel(str(text))
            lab.setFixedWidth(w)
            lab.setStyleSheet(f"color: {col}; font-size: 11px; "
                              f"font-weight: {'bold' if bold else 'normal'}; background: transparent;")
            if align_r:
                lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return lab

        base = muted if header else sec
        rl.addWidget(cell(date, widths[0], base))
        rl.addWidget(cell(tourn, widths[1], muted if header else TennisTheme.TEXT_PRIMARY))
        rl.addWidget(cell(surf, widths[2], base))
        rl.addWidget(cell(rnd, widths[3], base))
        rl.addWidget(cell(who, widths[4], muted if header else (winner_color or sec), bold=not header))
        rl.addWidget(cell(score, widths[5], base))
        rl.addWidget(cell(tm, widths[6], base, align_r=True))
        rl.addStretch()
        return row

    def _add_stats_block(self, name1, name2, p1w, p2w, by_surf, p1_sets, p2_sets,
                         p1_games, p2_games, tiebreaks, deciders,
                         total_min, n_timed, longest):
        frame = QFrame()
        frame.setStyleSheet(f"background: {TennisTheme.CARD_BACKGROUND}; border-radius: 4px;")
        g = QGridLayout(frame)
        g.setContentsMargins(10, 6, 10, 6)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(2)

        sn1 = name1.split()[-1]
        sn2 = name2.split()[-1]

        def lbl(text, color=TennisTheme.TEXT_SECONDARY, bold=False, size=11):
            la = QLabel(text)
            la.setStyleSheet(f"color: {color}; font-size: {size}px; "
                             f"font-weight: {'bold' if bold else 'normal'};")
            return la

        # column headers
        g.addWidget(lbl("", size=10), 0, 0)
        g.addWidget(lbl(sn1, self.p1_color, True), 0, 1, Qt.AlignmentFlag.AlignRight)
        g.addWidget(lbl(sn2, self.p2_color, True), 0, 2, Qt.AlignmentFlag.AlignRight)
        g.addWidget(lbl("", size=10), 0, 3)
        g.addWidget(lbl("", size=10), 0, 4)
        g.addWidget(lbl("", size=10), 0, 5, Qt.AlignmentFlag.AlignRight)

        def pair_row(r, label, v1, v2, c0=4):
            g.addWidget(lbl(label, TennisTheme.TEXT_MUTED, size=10), r, c0)
            g.addWidget(lbl(str(v1), bold=True), r, c0 + 1, Qt.AlignmentFlag.AlignRight)
            g.addWidget(lbl(str(v2), bold=True), r, c0 + 2, Qt.AlignmentFlag.AlignRight)

        def stat_row(r, label, v1, v2):
            g.addWidget(lbl(label, TennisTheme.TEXT_MUTED, size=10), r, 0)
            c1 = self.p1_color if v1 > v2 else TennisTheme.TEXT_PRIMARY
            c2 = self.p2_color if v2 > v1 else TennisTheme.TEXT_PRIMARY
            l1 = lbl(str(v1), c1, True); l1.setAlignment(Qt.AlignmentFlag.AlignRight)
            l2 = lbl(str(v2), c2, True); l2.setAlignment(Qt.AlignmentFlag.AlignRight)
            g.addWidget(l1, r, 1); g.addWidget(l2, r, 2)

        stat_row(1, "Matches won", p1w, p2w)
        stat_row(2, "Sets won", p1_sets, p2_sets)
        stat_row(3, "Games won", p1_games, p2_games)

        # right-hand summary column
        total_sets = p1_sets + p2_sets
        total_games = p1_games + p2_games
        avg_min = (total_min / n_timed) if n_timed else 0
        g.addWidget(lbl("Totals", TennisTheme.SECONDARY, True, 10), 1, 4)
        g.addWidget(lbl(f"{total_sets} sets · {total_games} games", size=10), 1, 5, 1, 1)
        time_txt = (f"{total_min // 60}h{total_min % 60:02d}m played · "
                    f"avg {self._fmt_minutes(avg_min)}") if n_timed else "durations n/a"
        g.addWidget(lbl(time_txt, size=10), 2, 4, 1, 2)
        extra = f"{tiebreaks} TBs · {deciders} deciders"
        if longest[0]:
            extra += f" · longest {self._fmt_minutes(longest[0])}"
        g.addWidget(lbl(extra, size=10), 3, 4, 1, 2)

        # surface breakdown line
        surf_txt = "   ".join(
            f"{s}: {v[0]}-{v[1]}" for s, v in sorted(by_surf.items()))
        g.addWidget(lbl(f"By surface:  {surf_txt}", TennisTheme.TEXT_SECONDARY, size=10),
                    4, 0, 1, 6)

        self.rows_layout.insertWidget(self.rows_layout.count() - 1, frame)


class ServeReturnTab(QWidget):
    """Surface serve/return efficiency plus charted playing-style metrics."""

    def __init__(self):
        super().__init__()
        self.p1_name = self.p2_name = ""
        self.p1 = self.p2 = None
        self.p1_color = TennisTheme.PRIMARY
        self.p2_color = TennisTheme.ACCENT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        lbl = QLabel("Surface:")
        lbl.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY}; font-size: 11px;")
        self.surface_combo = QComboBox()
        self.surface_combo.addItems(["Hard", "Clay", "Grass"])
        self.surface_combo.setStyleSheet(f"""
            QComboBox {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.TEXT_PRIMARY};
                border: 1px solid {TennisTheme.TEXT_MUTED};
                padding: 3px; min-width: 70px;
            }}
        """)
        self.surface_combo.currentTextChanged.connect(lambda *_: self.refresh())
        controls.addWidget(lbl)
        controls.addWidget(self.surface_combo)
        controls.addStretch()
        layout.addLayout(controls)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(3)
        layout.addLayout(self.grid)
        layout.addStretch()

        self.placeholder = QLabel("Select two players to compare serve & return.")
        self.placeholder.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(self.placeholder)

    def set_players(self, p1_name, p2_name, p1, p2):
        self.p1_name, self.p2_name = p1_name, p2_name
        self.p1, self.p2 = p1, p2
        self.refresh()

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    # --- small grid builders that track the current row ---------------- #
    def _hdr(self, text, col, color):
        la = QLabel(text)
        la.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: bold;")
        if col > 0:
            la.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.grid.addWidget(la, self._r, col)

    def _section(self, text):
        sec = QLabel(text)
        sec.setStyleSheet(f"color: {TennisTheme.SECONDARY}; font-size: 10px; font-weight: bold;")
        self.grid.addWidget(sec, self._r, 0, 1, 3)
        self._r += 1

    def _note(self, text):
        n = QLabel(text)
        n.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 10px; font-style: italic;")
        self.grid.addWidget(n, self._r, 0, 1, 3)
        self._r += 1

    def _metric(self, label, v1, v2, better=None, reliable=True):
        name = QLabel(label)
        name.setStyleSheet(f"color: {TennisTheme.TEXT_SECONDARY}; font-size: 11px;")
        c1 = c2 = TennisTheme.TEXT_PRIMARY
        # Only highlight a 'winner' when the comparison is trustworthy.
        if reliable and better and better[0] is not None and better[1] is not None:
            if better[0] > better[1]:
                c1 = self.p1_color
            elif better[1] > better[0]:
                c2 = self.p2_color
        l1 = QLabel(v1); l1.setAlignment(Qt.AlignmentFlag.AlignRight)
        l1.setStyleSheet(f"color: {c1}; font-size: 11px; font-weight: bold;")
        l2 = QLabel(v2); l2.setAlignment(Qt.AlignmentFlag.AlignRight)
        l2.setStyleSheet(f"color: {c2}; font-size: 11px; font-weight: bold;")
        self.grid.addWidget(name, self._r, 0)
        self.grid.addWidget(l1, self._r, 1)
        self.grid.addWidget(l2, self._r, 2)
        self._r += 1

    def _sample_row(self, label, m1, m2):
        """Render a match-count row, flagging low/zero samples."""
        def cell(m):
            if not m:
                return "none", TennisTheme.TEXT_MUTED
            if m < LOW_SAMPLE_MATCHES:
                return f"{m:.0f}*", TennisTheme.SECONDARY  # amber: small sample
            return f"{m:.0f}", TennisTheme.TEXT_SECONDARY
        t1, col1 = cell(m1)
        t2, col2 = cell(m2)
        name = QLabel(label)
        name.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 10px;")
        l1 = QLabel(t1); l1.setAlignment(Qt.AlignmentFlag.AlignRight)
        l1.setStyleSheet(f"color: {col1}; font-size: 10px;")
        l2 = QLabel(t2); l2.setAlignment(Qt.AlignmentFlag.AlignRight)
        l2.setStyleSheet(f"color: {col2}; font-size: 10px;")
        self.grid.addWidget(name, self._r, 0)
        self.grid.addWidget(l1, self._r, 1)
        self.grid.addWidget(l2, self._r, 2)
        self._r += 1

    def refresh(self):
        if not (self.p1 and self.p2):
            return
        self.placeholder.hide()
        self._clear_grid()
        surface = self.surface_combo.currentText()

        def g(parsed, *path):
            cur = parsed
            for k in path:
                cur = (cur or {}).get(k) if isinstance(cur, dict) else None
            return cur

        s1 = self.p1.get('surf', {}).get(surface, {}) or {}
        s2 = self.p2.get('surf', {}).get(surface, {}) or {}
        m1, m2 = s1.get('m'), s2.get('m')

        def fmt(v, suffix="%"):
            return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else "—"
        def fmt_frac(v):
            return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"

        def reliable(*counts):
            return all((c is not None and c >= LOW_SAMPLE_MATCHES) for c in counts)

        # Header
        self._r = 0
        self._hdr("Metric", 0, TennisTheme.TEXT_MUTED)
        self._hdr(self.p1_name.split()[-1] if self.p1_name else "P1", 1, self.p1_color)
        self._hdr(self.p2_name.split()[-1] if self.p2_name else "P2", 2, self.p2_color)
        self._r = 1

        # --- Surface serve / return -----------------------------------
        self._section(f"Serve / Return ({surface})")
        self._sample_row("Surface matches", m1, m2)
        if not s1 and not s2:
            self._note(f"Neither player has tour-level {surface}-court data.")
        elif not s1 or not s2:
            who = self.p2_name if not s1 else self.p1_name
            self._note(f"{who.split()[-1] if who else 'One player'} has no {surface} data.")
        surf_rel = reliable(m1, m2)
        self._metric("Service pts won", fmt_frac(s1.get('spw')), fmt_frac(s2.get('spw')),
                     (s1.get('spw'), s2.get('spw')), surf_rel)
        self._metric("Return pts won", fmt_frac(s1.get('rpw')), fmt_frac(s2.get('rpw')),
                     (s1.get('rpw'), s2.get('rpw')), surf_rel)
        self._metric("Hold %", fmt(s1.get('hold')), fmt(s2.get('hold')),
                     (s1.get('hold'), s2.get('hold')), surf_rel)
        self._metric("Break %", fmt(s1.get('brk')), fmt(s2.get('brk')),
                     (s1.get('brk'), s2.get('brk')), surf_rel)
        self._metric("Dominance ratio", fmt(s1.get('dr'), ""), fmt(s2.get('dr'), ""),
                     (s1.get('dr'), s2.get('dr')), surf_rel)

        # --- Serve detail (latest season) -----------------------------
        sv1, sv2 = self.p1.get('serve', {}), self.p2.get('serve', {})
        y1, y2 = sv1.get('year') or '—', sv2.get('year') or '—'
        yr_lbl = y1 if y1 == y2 else f"{y1}/{y2}"
        self._section(f"Serve detail ({yr_lbl})")
        self._sample_row("Season matches", sv1.get('matches'), sv2.get('matches'))
        serve_rel = reliable(sv1.get('matches'), sv2.get('matches'))
        self._metric("1st serve in", fmt(sv1.get('1st_in')), fmt(sv2.get('1st_in')),
                     (sv1.get('1st_in'), sv2.get('1st_in')), serve_rel)
        self._metric("1st serve won", fmt(sv1.get('1st_won')), fmt(sv2.get('1st_won')),
                     (sv1.get('1st_won'), sv2.get('1st_won')), serve_rel)
        self._metric("2nd serve won", fmt(sv1.get('2nd_won')), fmt(sv2.get('2nd_won')),
                     (sv1.get('2nd_won'), sv2.get('2nd_won')), serve_rel)
        self._metric("Ace %", fmt(sv1.get('ace')), fmt(sv2.get('ace')),
                     (sv1.get('ace'), sv2.get('ace')), serve_rel)
        self._metric("Double fault %", fmt(sv1.get('df')), fmt(sv2.get('df')),
                     (sv2.get('df'), sv1.get('df')), serve_rel)  # lower is better

        # --- Playing style (charted) ----------------------------------
        st1, st2 = self.p1.get('style', {}), self.p2.get('style', {})
        self._section("Playing style (charted)")
        self._sample_row("Charted matches", st1.get('n'), st2.get('n'))
        style_rel = reliable(st1.get('n'), st2.get('n'))
        self._metric("Net freq", fmt(st1.get('net_freq')), fmt(st2.get('net_freq')))
        self._metric("Net win %", fmt(st1.get('net_w')), fmt(st2.get('net_w')),
                     (st1.get('net_w'), st2.get('net_w')), style_rel)
        self._metric("Drop-shot freq", fmt(st1.get('drop_freq')), fmt(st2.get('drop_freq')))
        self._metric("FH winner %", fmt(st1.get('fh_wnr')), fmt(st2.get('fh_wnr')))
        self._metric("BH winner %", fmt(st1.get('bh_wnr')), fmt(st2.get('bh_wnr')))
        self._metric("Winner/UFE ratio", fmt(st1.get('wufe'), ""), fmt(st2.get('wufe'), ""),
                     (st1.get('wufe'), st2.get('wufe')), style_rel)


class StatComparisonTab(QWidget):
    """Spec-driven two-player comparison grid over the Match-Charting aggregates.

    Spec entries:
      ('sec', title)
      ('stat', label, key, fmt, higher_better)   fmt in {pct, mph, num, ratio}
    Values come from each player's parsed['mcp'] dict.
    """

    def __init__(self, spec, scroll=False):
        super().__init__()
        self.spec = spec
        self.p1_name = self.p2_name = ""
        self.m1 = self.m2 = None
        self.n1 = self.n2 = 0
        self.p1_color = TennisTheme.PRIMARY
        self.p2_color = TennisTheme.ACCENT

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(3)

        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setStyleSheet("QScrollArea{border:none;}")
            holder = QWidget()
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addLayout(self.grid)
            hl.addStretch()
            area.setWidget(holder)
            outer.addWidget(area)
        else:
            outer.addLayout(self.grid)
            outer.addStretch()

        self.placeholder = QLabel("Select two players to compare.")
        self.placeholder.setStyleSheet(f"color: {TennisTheme.TEXT_MUTED}; font-size: 12px;")
        outer.addWidget(self.placeholder)

    def set_data(self, p1_name, p2_name, p1_parsed, p2_parsed):
        self.p1_name, self.p2_name = p1_name, p2_name
        self.m1 = (p1_parsed or {}).get('mcp', {})
        self.m2 = (p2_parsed or {}).get('mcp', {})
        self.refresh()

    def _clear(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    @staticmethod
    def _fmt(v, kind):
        if not isinstance(v, (int, float)):
            return "—"
        if kind == 'pct':
            return f"{v:.1f}%"
        if kind == 'mph':
            return f"{v:.0f}"
        if kind == 'ratio':
            return f"{v:.2f}"
        return f"{v:.1f}"

    def refresh(self):
        if not (self.m1 is not None and self.m2 is not None):
            return
        self.placeholder.hide()
        self._clear()
        r = 0

        def lbl(text, color, bold=False, size=11, align=None):
            la = QLabel(text)
            la.setStyleSheet(f"color: {color}; font-size: {size}px; "
                             f"font-weight: {'bold' if bold else 'normal'};")
            if align:
                la.setAlignment(align)
            return la

        sn1 = self.p1_name.split()[-1] if self.p1_name else "P1"
        sn2 = self.p2_name.split()[-1] if self.p2_name else "P2"
        self.grid.addWidget(lbl("Metric", TennisTheme.TEXT_MUTED, True, 10), r, 0)
        self.grid.addWidget(lbl(sn1, self.p1_color, True, 11, Qt.AlignmentFlag.AlignRight), r, 1)
        self.grid.addWidget(lbl(sn2, self.p2_color, True, 11, Qt.AlignmentFlag.AlignRight), r, 2)
        r += 1

        n1, n2 = self.m1.get('charted_n', 0), self.m2.get('charted_n', 0)
        self.grid.addWidget(lbl("Charted matches", TennisTheme.TEXT_MUTED, False, 10), r, 0)
        for col, n in ((1, n1), (2, n2)):
            c = TennisTheme.TEXT_MUTED if n else TennisTheme.TEXT_MUTED
            self.grid.addWidget(lbl(str(n) if n else "none", c, False, 10,
                                    Qt.AlignmentFlag.AlignRight), r, col)
        r += 1

        for entry in self.spec:
            if entry[0] == 'sec':
                self.grid.addWidget(lbl(entry[1], TennisTheme.SECONDARY, True, 10), r, 0, 1, 3)
                r += 1
                continue
            _, label, key, kind, better = entry
            v1, v2 = self.m1.get(key), self.m2.get(key)
            c1 = c2 = TennisTheme.TEXT_PRIMARY
            if better is not None and isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                hi = v1 > v2 if better else v1 < v2
                lo = v1 < v2 if better else v1 > v2
                if hi:
                    c1 = self.p1_color
                elif lo:
                    c2 = self.p2_color
            self.grid.addWidget(lbl(label, TennisTheme.TEXT_SECONDARY, False, 11), r, 0)
            self.grid.addWidget(lbl(self._fmt(v1, kind), c1, True, 11, Qt.AlignmentFlag.AlignRight), r, 1)
            self.grid.addWidget(lbl(self._fmt(v2, kind), c2, True, 11, Qt.AlignmentFlag.AlignRight), r, 2)
            r += 1


# Spec tables for the Match-Charting comparison tabs.
SERVE_SPEC = [
    ('sec', 'Serve speed (mph · charted)'),
    ('stat', '1st serve avg', 'spd_1st', 'mph', True),
    ('stat', '   down the T', 'spd_1st_t', 'mph', True),
    ('stat', '   out wide', 'spd_1st_wide', 'mph', True),
    ('stat', 'Fastest serve', 'spd_1st_max', 'mph', True),
    ('stat', 'Consistency (StDev↓)', 'spd_1st_sd', 'num', False),
    ('stat', '2nd serve avg', 'spd_2nd', 'mph', True),
    ('sec', 'Serve effectiveness'),
    ('stat', 'Unreturned %', 'srv_unret', 'pct', True),
    ('stat', 'Points won ≤3 shots', 'srv_le3', 'pct', True),
    ('stat', 'Rally (in-play) won %', 'srv_rip_w', 'pct', True),
    ('stat', 'Serve impact', 'srv_impact', 'pct', True),
    ('sec', 'Serving under pressure'),
    ('stat', 'Break points saved', 'bp_saved', 'pct', True),
    ('stat', 'Hold facing BP', 'hold_bpf', 'pct', True),
    ('stat', 'Held serving for set', 'serve_for_set', 'pct', True),
    ('stat', 'Held serving for match', 'serve_for_match', 'pct', True),
    ('stat', 'Deuce-court SPW', 'deuce_spw', 'pct', True),
    ('stat', 'Ad-court SPW', 'ad_spw', 'pct', True),
    ('stat', 'Tiebreak SPW', 'tb_spw', 'pct', True),
]

RETURN_RALLY_SPEC = [
    ('sec', 'Return game'),
    ('stat', 'Return in play %', 'ret_rip', 'pct', True),
    ('stat', 'Return pts won %', 'ret_rip_w', 'pct', True),
    ('stat', 'Return winner %', 'ret_wnr', 'pct', True),
    ('stat', 'Return depth (RDI)', 'ret_depth', 'num', True),
    ('stat', 'Slice return %', 'ret_slice', 'pct', None),
    ('sec', 'Returning under pressure'),
    ('stat', 'Break points converted', 'bp_conv', 'pct', True),
    ('stat', 'Break back %', 'breakback', 'pct', True),
    ('stat', 'Deuce-court RPW', 'deuce_rpw', 'pct', True),
    ('stat', 'Ad-court RPW', 'ad_rpw', 'pct', True),
    ('stat', 'Tiebreak RPW', 'tb_rpw', 'pct', True),
    ('sec', 'Rally profile'),
    ('stat', 'Avg rally length', 'rally_len', 'num', None),
    ('stat', 'Win% rallies 1-3', 'rally_1_3', 'pct', True),
    ('stat', 'Win% rallies 4-6', 'rally_4_6', 'pct', True),
    ('stat', 'Win% rallies 7-9', 'rally_7_9', 'pct', True),
    ('stat', 'Win% rallies 10+', 'rally_10p', 'pct', True),
    ('stat', 'Forehand share', 'rally_fh_share', 'pct', None),
]


class MatchupAnalysisPanel(QWidget):
    """Tabbed bottom-right panel: Match Sim / Head-to-Head / Serve & Return."""

    def __init__(self):
        super().__init__()
        self.p1_name = self.p2_name = ""
        self.p1 = self.p2 = None
        self.p1_hist = []
        self.p2_hist = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {TennisTheme.CARD_BACKGROUND};
                background: {TennisTheme.SURFACE};
            }}
            QTabBar::tab {{
                background: {TennisTheme.CARD_BACKGROUND};
                color: {TennisTheme.TEXT_SECONDARY};
                padding: 6px 14px; margin-right: 2px;
                font-size: 11px;
            }}
            QTabBar::tab:selected {{
                background: {TennisTheme.SURFACE};
                color: {TennisTheme.PRIMARY};
                border-bottom: 2px solid {TennisTheme.PRIMARY};
            }}
        """)
        self.match_sim_tab = MatchSimTab()
        self.h2h_tab = HeadToHeadTab()
        self.serve_return_tab = ServeReturnTab()
        self.serve_tab = StatComparisonTab(SERVE_SPEC, scroll=True)
        self.return_rally_tab = StatComparisonTab(RETURN_RALLY_SPEC, scroll=True)
        # Scroll-wrap the sim tab: its full content (controls + prob bar +
        # context + distributions) needs ~500px; on the initial 800px-tall
        # window the panel gets far less, and without a scroll area the
        # QVBoxLayout squeezed the control grid into overlapping/clipped rows.
        sim_scroll = QScrollArea()
        sim_scroll.setWidgetResizable(True)
        sim_scroll.setStyleSheet("QScrollArea { border: none; }")
        sim_scroll.setWidget(self.match_sim_tab)
        self.tabs.addTab(sim_scroll, "Match Sim")
        self.tabs.addTab(self.h2h_tab, "Head-to-Head")
        self.tabs.addTab(self.serve_return_tab, "Surface")
        self.tabs.addTab(self.serve_tab, "Serve")
        self.tabs.addTab(self.return_rally_tab, "Return & Rally")
        layout.addWidget(self.tabs)

        self.setStyleSheet(f"background: {TennisTheme.SURFACE};")

    def update_player(self, player_name: str, data_dict: dict, player_num: int):
        parsed = parse_player_payload(data_dict)
        hist = data_dict.get('historical_matches', []) or []
        if player_num == 1:
            self.p1_name, self.p1, self.p1_hist = player_name, parsed, hist
        else:
            self.p2_name, self.p2, self.p2_hist = player_name, parsed, hist
        self._push()

    def _push(self):
        self.match_sim_tab.set_players(self.p1_name, self.p2_name, self.p1, self.p2)
        self.serve_return_tab.set_players(self.p1_name, self.p2_name, self.p1, self.p2)
        self.serve_tab.set_data(self.p1_name, self.p2_name, self.p1, self.p2)
        self.return_rally_tab.set_data(self.p1_name, self.p2_name, self.p1, self.p2)
        self.h2h_tab.set_players(self.p1_name, self.p2_name)
        self.h2h_tab.set_historical(self.p1_hist, self.p2_hist)

    def set_h2h(self, record, h2h_matches):
        self.h2h_tab.set_h2h(record, h2h_matches)


class MarketLedgerWidget(QWidget):
    """Bottom-left panel: each selected player's recent matches with closing
    odds from the td_matches corpus (tennis-data.co.uk), plus a market summary
    line — record as favourite / underdog and the ROI of blindly backing the
    player at the closing average price. Data no other panel shows: how the
    market has priced these players lately, and whether it was right."""

    N_ROWS = 11          # recent matches listed per player
    N_SUMMARY = 50       # matches the summary line aggregates over

    def __init__(self, db_path: Optional[str] = None):
        super().__init__()
        import pathlib
        self.db_path = db_path or str(pathlib.Path(__file__).parent / "tennis_rankings.db")
        self.setFixedSize(900, 236)
        self.setStyleSheet(f"""
            MarketLedgerWidget {{
                background: {TennisTheme.CARD_BACKGROUND};
                border: 2px solid {TennisTheme.SURFACE};
                border-radius: 8px;
            }}
        """)
        self.p1_name = self.p2_name = ""
        self.p1_rows, self.p2_rows = [], []
        self.p1_summary = self.p2_summary = ""

    def set_player(self, player_name: str, player_num: int):
        rows, summary = self._load(player_name)
        if player_num == 1:
            self.p1_name, self.p1_rows, self.p1_summary = player_name, rows, summary
        else:
            self.p2_name, self.p2_rows, self.p2_summary = player_name, rows, summary
        self.update()

    def merge_ta_matches(self, player_name: str, historical_matches, player_num: int):
        """Prepend matches Tennis Abstract already has but the odds corpus
        doesn't (tennis-data lags a few days and posts slams late). These rows
        carry no closing price — shown with an em-dash — but keep the recent
        form current (e.g. this week's Wimbledon rounds)."""
        rows = self.p1_rows if player_num == 1 else self.p2_rows
        name = self.p1_name if player_num == 1 else self.p2_name
        if player_name != name:
            return
        newest_td = rows[0][0] if rows else "1970-01-01"
        fresh = []
        for m in historical_matches or []:
            d = str(m.get('date', '') if isinstance(m, dict) else getattr(m, 'date', ''))
            if len(d) != 8 or not d.isdigit():
                continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            if iso <= newest_td:
                break                      # TA log is newest-first
            get = (m.get if isinstance(m, dict) else lambda k, d="": getattr(m, k, d))
            won = get('result') == 'Win'
            # TA scores are winner-first; count sets for a compact "2-1" readout.
            sw = sl = 0
            for st in str(get('score', '')).split():
                mm = re.match(r'(\d+)-(\d+)', st)
                if mm and mm.group(1) != mm.group(2):
                    if int(mm.group(1)) > int(mm.group(2)):
                        sw += 1
                    else:
                        sl += 1
            score = f"{sw}-{sl}" if won else f"{sl}-{sw}"
            fresh.append((iso, get('tournament', ''), get('round', ''),
                          get('surface', ''), get('opponent', ''), won, 0.0, score))
        if not fresh:
            return
        merged = (fresh + rows)[:self.N_ROWS]
        if player_num == 1:
            self.p1_rows = merged
        else:
            self.p2_rows = merged
        self.update()

    def _load(self, player_name: str):
        """Recent matches (opponent, result, closing odds) + market summary."""
        try:
            from tennis_data_ingest import td_name_key
            key = td_name_key(player_name)
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("""
                SELECT date, tournament, round, surface, winner, loser,
                       winner_key, avgw, avgl, wsets, lsets, comment
                FROM td_matches
                WHERE winner_key = ? OR loser_key = ?
                ORDER BY date DESC, id DESC LIMIT ?
            """, (key, key, self.N_SUMMARY))
            raw = cur.fetchall()
            conn.close()
        except Exception as e:
            print(f"MarketLedger load error for {player_name}: {e}")
            return [], ""

        rows = []
        wins = losses = 0
        fav = [0, 0]        # [wins, n] as market favourite
        dog = [0, 0]
        units = 0.0
        priced = 0
        for (date, tourney, rnd, surf, w, l, wkey, avgw, avgl, ws, ls, comment) in raw:
            won = (wkey == key)
            opp = l if won else w
            my_odds = (avgw if won else avgl) or 0.0
            opp_odds = (avgl if won else avgw) or 0.0
            if won:
                wins += 1
            else:
                losses += 1
            if my_odds and opp_odds:
                priced += 1
                is_fav = my_odds < opp_odds
                side = fav if is_fav else dog
                side[0] += won
                side[1] += 1
                units += (my_odds - 1.0) if won else -1.0
            if len(rows) < self.N_ROWS:
                score = (f"{ws:.0f}-{ls:.0f}" if won else f"{ls:.0f}-{ws:.0f}") \
                    if ws is not None and ls is not None else ""
                if comment and str(comment).strip().lower() != "completed":
                    score += " ret"
                rows.append((date or "", tourney or "", rnd or "", surf or "",
                             opp, won, my_odds, score))
        summary = ""
        if raw:
            summary = f"{wins}-{losses} last {len(raw)}"
            if priced:
                summary += (f"   ·   fav {fav[0]}-{fav[1]-fav[0]}  dog {dog[0]}-{dog[1]-dog[0]}"
                            f"   ·   blind-back ROI {units/priced*100:+.1f}%")
        return rows, summary

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(TennisTheme.CARD_BACKGROUND))

        painter.setPen(QColor(TennisTheme.TEXT_PRIMARY))
        painter.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        painter.drawText(QRect(0, 4, self.width(), 18),
                         Qt.AlignmentFlag.AlignCenter, "Market Ledger")
        if not (self.p1_rows or self.p2_rows):
            painter.setPen(QColor(TennisTheme.TEXT_MUTED))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(QRect(0, 0, self.width(), self.height()),
                             Qt.AlignmentFlag.AlignCenter,
                             "Select players to view recent matches & closing prices")
            return
        half = self.width() // 2
        self._draw_side(painter, QRect(8, 24, half - 14, self.height() - 30),
                        self.p1_name, self.p1_rows, self.p1_summary,
                        QColor(TennisTheme.PRIMARY))
        self._draw_side(painter, QRect(half + 6, 24, half - 14, self.height() - 30),
                        self.p2_name, self.p2_rows, self.p2_summary,
                        QColor(TennisTheme.ACCENT))

    def _draw_side(self, p, rect, name, rows, summary, color):
        if not name:
            return
        x, y = rect.x(), rect.y()
        p.setPen(color)
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.drawText(x, y + 11, name)
        p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
        p.setFont(QFont("Arial", 8))
        p.drawText(QRect(x + 130, y, rect.width() - 130, 14),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, summary)
        if not rows:
            p.setPen(QColor(TennisTheme.TEXT_MUTED))
            p.drawText(x, y + 32, "No matches in odds corpus")
            return

        row_h = 16
        top = y + 18
        # columns: date, tournament, opp, W/L score, odds
        cw = {"date": 42, "tour": 108, "opp": 132, "res": 74, "odds": 44}
        p.setFont(QFont("Arial", 7))
        p.setPen(QColor(TennisTheme.TEXT_MUTED))
        cx = x
        for label, wdt in (("DATE", cw["date"]), ("EVENT", cw["tour"]),
                           ("OPPONENT", cw["opp"]), ("RESULT", cw["res"]),
                           ("CLOSE", cw["odds"])):
            p.drawText(QRect(cx, top, wdt, 12),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            cx += wdt + 6
        p.setFont(QFont("Arial", 8))
        for i, (date, tourney, rnd, surf, opp, won, odds, score) in enumerate(rows):
            ry = top + 13 + i * row_h
            if ry + row_h > rect.bottom():
                break
            cx = x
            p.setPen(QColor(TennisTheme.TEXT_MUTED))
            p.drawText(QRect(cx, ry, cw["date"], row_h),
                       Qt.AlignmentFlag.AlignVCenter, date[5:] if len(date) >= 10 else date)
            cx += cw["date"] + 6
            p.setPen(QColor(TennisTheme.TEXT_SECONDARY))
            p.drawText(QRect(cx, ry, cw["tour"], row_h), Qt.AlignmentFlag.AlignVCenter,
                       (tourney[:17] + "…") if len(tourney) > 18 else tourney)
            cx += cw["tour"] + 6
            p.drawText(QRect(cx, ry, cw["opp"], row_h), Qt.AlignmentFlag.AlignVCenter,
                       (opp[:19] + "…") if len(opp) > 20 else opp)
            cx += cw["opp"] + 6
            p.setPen(QColor("#4CAF50") if won else QColor("#FF6B6B"))
            p.drawText(QRect(cx, ry, cw["res"], row_h), Qt.AlignmentFlag.AlignVCenter,
                       f"{'W' if won else 'L'} {score}")
            cx += cw["res"] + 6
            if odds:
                # colour the closing price by fav/dog status
                p.setPen(QColor(TennisTheme.SECONDARY) if odds < 2.0
                         else QColor(TennisTheme.TEXT_PRIMARY))
                p.drawText(QRect(cx, ry, cw["odds"], row_h),
                           Qt.AlignmentFlag.AlignVCenter, f"{odds:.2f}")
            else:
                # fresh TA row — no closing price in the corpus yet
                p.setPen(QColor(TennisTheme.TEXT_MUTED))
                p.drawText(QRect(cx, ry, cw["odds"], row_h),
                           Qt.AlignmentFlag.AlignVCenter, "—")


class CompactTennisComparisonWidget(QWidget):
    """Main container combining search and player profile widgets"""

    h2hReady = pyqtSignal(object, object)  # record_str, list[H2HMatch]
    tdRefreshed = pyqtSignal()             # odds corpus current-season refresh done

    def __init__(self):
        super().__init__()
        self.h2h_scraper = TennisScraper()
        self.current_player1 = ""
        self.current_player2 = ""
        self.setup_ui()
        self.setup_connections()
        self.h2hReady.connect(self._on_h2h_ready)
        self.tdRefreshed.connect(self._on_td_refreshed)
        self._refresh_td_corpus()

    def _refresh_td_corpus(self):
        """Refresh the current tennis-data season in the background (both
        tours, ~450KB total) if the cached files are stale, so the Market
        Ledger is as current as the source allows (site lags ~3 days,
        slams sometimes post late)."""
        def worker():
            try:
                import time as _time
                from datetime import date as _date
                from tennis_data_ingest import download, ingest_season, CACHE_DIR
                year = _date.today().year
                probe = CACHE_DIR / f"{year}.xlsx"
                if probe.exists() and (_time.time() - probe.stat().st_mtime) < 6 * 3600:
                    return
                import sqlite3 as _sqlite3
                import pathlib as _pathlib
                con = _sqlite3.connect(str(_pathlib.Path(__file__).parent / "tennis_rankings.db"))
                for tour in ("ATP", "WTA"):
                    path = download(tour, year, force=True)
                    if path:
                        ingest_season(con, tour, year, path)
                con.close()
                self.tdRefreshed.emit()
            except Exception as e:
                print(f"td corpus refresh skipped: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_td_refreshed(self):
        """Re-pull ledger rows for any already-selected players (main thread)."""
        if self.current_player1:
            self.market_ledger.set_player(self.current_player1, 1)
        if self.current_player2:
            self.market_ledger.set_player(self.current_player2, 2)

    def setup_ui(self):
        """Setup the complete comparison interface"""
        self.setWindowTitle("Effort H2H Tennis Analyzer")
        self.setGeometry(100, 100, 1900, 800)  # Optimized size for stacked tables
        self.setMinimumSize(1400, 600)  # Set minimum size for proper functionality
        self.setStyleSheet(f"background: {TennisTheme.BACKGROUND};")

        # Main layout: Grid layout for better organization
        main_layout = QGridLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # Top-left area: Player comparison widgets
        top_left_widget = QWidget()
        top_left_widget.setFixedWidth(900)  # Wider to accommodate both 420px player widgets
        top_left_widget.setMinimumHeight(420)  # Reduced minimum height to allow more compact layout
        top_left_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        top_left_layout = QVBoxLayout(top_left_widget)
        top_left_layout.setContentsMargins(0, 0, 0, 0)
        top_left_layout.setSpacing(6)

        # Search widget
        self.search_widget = CompactPlayerSearchWidget()

        # Player profile widgets
        profiles_layout = QHBoxLayout()
        profiles_layout.setSpacing(8)

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
        surface_stats_layout.setSpacing(10)  # Spacing between surface table and stats widget
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

        # Bottom-left: Rankings chart, then the market ledger below it.
        main_layout.addWidget(self.ranking_chart, 1, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.market_ledger = MarketLedgerWidget()
        main_layout.addWidget(self.market_ledger, 2, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        # Right column: Momentum stacked directly above the analysis panel so
        # they pack together instead of leaving a row-driven gap between them.
        self.form_momentum_widget = RecentFormMomentumWidget()
        self.analysis_panel = MatchupAnalysisPanel()
        self.analysis_panel.setMinimumSize(560, 240)

        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(self.form_momentum_widget, 0)
        right_layout.addWidget(self.analysis_panel, 1)
        main_layout.addWidget(right_column, 0, 1, 3, 1)
        # Left column is fixed-width; let the right column absorb the extra width.
        main_layout.setColumnStretch(0, 0)
        main_layout.setColumnStretch(1, 1)


    def setup_connections(self):
        """Connect search signals to player widgets"""
        self.search_widget.player1Selected.connect(self.on_player1_selected)
        self.search_widget.player2Selected.connect(self.on_player2_selected)


        # Connect surface stats signals
        self.player1_widget.surfaceStatsLoaded.connect(self.on_surface_stats_loaded)
        self.player2_widget.surfaceStatsLoaded.connect(self.on_surface_stats_loaded)

        # Connect yearly stats signals for historical table
        self.player1_widget.yearlyStatsLoaded.connect(self.on_yearly_stats_loaded)
        self.player2_widget.yearlyStatsLoaded.connect(self.on_yearly_stats_loaded)

        # Connect raw player data signals for stats widget
        self.player1_widget.rawPlayerDataLoaded.connect(self.on_raw_player_data_loaded)
        self.player2_widget.rawPlayerDataLoaded.connect(self.on_raw_player_data_loaded)

        # Connect recent results signals for form momentum widget
        self.player1_widget.recentResultsLoaded.connect(self.on_recent_results_loaded)
        self.player2_widget.recentResultsLoaded.connect(self.on_recent_results_loaded)

        # Connect historical matches signals for enhanced momentum analysis
        self.player1_widget.historicalMatchesLoaded.connect(self.on_historical_matches_loaded)
        self.player2_widget.historicalMatchesLoaded.connect(self.on_historical_matches_loaded)

    def on_player1_selected(self, player_name: str):
        """Handle player 1 selection"""
        self.current_player1 = player_name
        self.player1_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 1)
        self.market_ledger.set_player(player_name, 1)
        self.update_status()
        self.check_and_load_h2h()

    def on_player2_selected(self, player_name: str):
        """Handle player 2 selection"""
        self.current_player2 = player_name
        self.player2_widget.set_player(player_name)
        self.ranking_chart.add_player(player_name, 2)
        self.market_ledger.set_player(player_name, 2)
        self.update_status()
        self.check_and_load_h2h()


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
        self.analysis_panel.update_player(player_name, player_data, player_num)

    def on_recent_results_loaded(self, player_name: str, recent_results: list, player_num: int):
        """Handle recent results loaded for form momentum widget"""
        self.form_momentum_widget.update_player_results(player_name, recent_results, player_num)

    def on_historical_matches_loaded(self, player_name: str, historical_matches: list, player_num: int):
        """Handle historical matches loaded for enhanced momentum analysis"""
        self.form_momentum_widget.update_player_historical_data(player_name, historical_matches, player_num)
        self.market_ledger.merge_ta_matches(player_name, historical_matches, player_num)

    def _on_h2h_ready(self, record, h2h_matches):
        """Deliver head-to-head data to the analysis panel (main thread)."""
        # Seed names from the search selection in case raw player data is still loading.
        self.analysis_panel.h2h_tab.set_players(self.current_player1, self.current_player2)
        self.analysis_panel.set_h2h(record, h2h_matches)

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
                    if h2h_data:
                        self.h2hReady.emit(h2h_data.head_to_head, h2h_data.h2h_history)
                    print(f"H2H data loaded for {self.current_player1} vs {self.current_player2}")
                except Exception as e:
                    print(f"Error loading comparison data: {e}")

            # Run in background thread
            thread = threading.Thread(target=load_h2h, daemon=True)
            thread.start()
        else:
            # No action needed when not both players selected
            pass

    def closeEvent(self, event):
        """Cleanup scrapers when window is closed"""
        try:
            # Close H2H scraper
            if hasattr(self.h2h_scraper, 'close'):
                self.h2h_scraper.close()

            print("Scrapers cleaned up successfully")
        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            event.accept()


# Test application
if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Create main comparison widget
    window = CompactTennisComparisonWidget()
    window.show()


    sys.exit(app.exec())
