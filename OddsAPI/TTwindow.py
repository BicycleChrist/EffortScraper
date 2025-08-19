import sys
import json
import os
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QComboBox, QTabWidget, QTableWidget, QTableWidgetItem, 
    QPushButton, QLineEdit, QSplitter, QGroupBox, QScrollArea, 
    QGridLayout, QHeaderView, QSizePolicy, QDialog, QDialogButtonBox,QStackedWidget
)
from PyQt6.QtCore import Qt, QTimer, QSize, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QIcon
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import asyncio
from TableTennisClient import main as fetch_data
from tt_elo import ELOCalculator
from ttDB import TTDatabase
import pathlib # For icon


class AsyncDataFetcher(QThread):
    """Thread for running async data fetching and ELO updates without blocking the UI"""
    
    # Qt signals for communication with main thread
    finished = pyqtSignal()
    error = pyqtSignal(str)
    status_update = pyqtSignal(str)
    
    def __init__(self, include_elo_update=False):
        super().__init__()
        self.include_elo_update = include_elo_update
    
    def run(self):
        """Run the async data fetching and optionally ELO updates in this thread"""
        try:
            # Update status
            self.status_update.emit("Fetching data...")
            
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                # Run the async fetch_data function
                loop.run_until_complete(fetch_data())
                self.status_update.emit("Data fetch completed")
                
                # If ELO update is requested, run it as well
                if self.include_elo_update:
                    self.status_update.emit("Updating ELO ratings...")
                    
                    # Import here to avoid circular imports
                    from ttDB import TTDatabase
                    from tt_elo import ELOCalculator
                    
                    # Run ELO update asynchronously
                    db = TTDatabase()
                    elo_calculator = ELOCalculator(db)
                    matches_processed = loop.run_until_complete(
                        elo_calculator.process_unprocessed_matches_async()
                    )
                    db.close()
                    
                    if matches_processed > 0:
                        self.status_update.emit(f"ELO update completed: {matches_processed} matches processed")
                    else:
                        self.status_update.emit("ELO update completed: No new matches to process")
                
                self.finished.emit()
            finally:
                loop.close()
                
        except Exception as e:
            self.error.emit(str(e))


class PointProgressionCanvas(FigureCanvas):
    """Canvas for drawing point progression charts"""
    def __init__(self, parent=None, width=5, height=4, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        
        # Set dark theme style for the figure
        self.fig.patch.set_facecolor('#2d2d2d')
        self.axes.set_facecolor('#2d2d2d')
        self.axes.spines['bottom'].set_color('#7f8c8d')
        self.axes.spines['top'].set_color('#7f8c8d') 
        self.axes.spines['right'].set_color('#7f8c8d')
        self.axes.spines['left'].set_color('#7f8c8d')
        self.axes.tick_params(axis='x', colors='#f0f0f0')
        self.axes.tick_params(axis='y', colors='#f0f0f0')
        self.axes.yaxis.label.set_color('#f0f0f0')
        self.axes.xaxis.label.set_color('#f0f0f0')
        self.axes.title.set_color('#f0f0f0')
        self.axes.grid(color='#3a3a3a', linestyle='-', linewidth=0.5, alpha=0.7)
        
        super().__init__(self.fig)
        self.setMinimumSize(400, 300)
    
    def plot_point_progression(self, set_data, home_name, away_name, set_number):
        """Plot point progression for a single set"""
        # Clear previous plot
        self.axes.clear()
        
        # Set style again after clearing
        self.axes.set_facecolor('#2d2d2d')
        self.axes.spines['bottom'].set_color('#7f8c8d')
        self.axes.spines['top'].set_color('#7f8c8d')
        self.axes.spines['right'].set_color('#7f8c8d')
        self.axes.spines['left'].set_color('#7f8c8d')
        self.axes.tick_params(axis='x', colors='#f0f0f0')
        self.axes.tick_params(axis='y', colors='#f0f0f0')
        self.axes.grid(color='#3a3a3a', linestyle='-', linewidth=0.5, alpha=0.7)
        
        # Extract data
        point_numbers = [point["point_num"] for point in set_data]
        home_scores = [point["home_score"] for point in set_data]
        away_scores = [point["away_score"] for point in set_data]
        
        # Plot home team points in red and away team in green
        home_line, = self.axes.plot(point_numbers, home_scores, 'r-o', label=home_name, linewidth=2, markersize=4)
        away_line, = self.axes.plot(point_numbers, away_scores, 'g-o', label=away_name, linewidth=2, markersize=4)
        
        # Set labels and title
        self.axes.set_title(f"Set {set_number}")
        self.axes.set_xlabel("Point Number")
        self.axes.set_ylabel("Score")
        
        # Add legend
        self.axes.legend(loc='upper left')
        
        # Add grid
        self.axes.grid(True)
        
        # Set y-axis to start from 0 and go to max score + 1
        max_score = max(max(home_scores), max(away_scores)) if home_scores and away_scores else 0
        self.axes.set_ylim(0, max_score + 1)
        
        # Set x-axis to match points
        self.axes.set_xlim(1, max(point_numbers) if point_numbers else 1)
        
        # Show all integer ticks on y-axis
        self.axes.set_yticks(list(range(0, max_score + 2)))
        
        # Add annotation for the final score
        if point_numbers:
            final_point = len(point_numbers) - 1
            final_home = home_scores[final_point]
            final_away = away_scores[final_point]
            
            # Add final score annotation
            self.axes.annotate(f"{final_home}", 
                xy=(point_numbers[final_point], home_scores[final_point]),
                xytext=(10, 0),
                textcoords="offset points",
                color='red',
                fontweight='bold')
                
            self.axes.annotate(f"{final_away}", 
                xy=(point_numbers[final_point], away_scores[final_point]),
                xytext=(10, 0),
                textcoords="offset points",
                color='green',
                fontweight='bold')
        
        self.fig.tight_layout()
        self.draw()


class ELOProgressionCanvas(FigureCanvas):
    """Canvas for drawing ELO progression charts"""
    def __init__(self, parent=None, width=4, height=3, dpi=80):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        
        # Set dark theme style for the figure
        self.fig.patch.set_facecolor('#2d2d2d')
        self.axes.set_facecolor('#2d2d2d')
        self.axes.spines['bottom'].set_color('#7f8c8d')
        self.axes.spines['top'].set_color('#7f8c8d') 
        self.axes.spines['right'].set_color('#7f8c8d')
        self.axes.spines['left'].set_color('#7f8c8d')
        self.axes.tick_params(axis='x', colors='#f0f0f0')
        self.axes.tick_params(axis='y', colors='#f0f0f0')
        self.axes.yaxis.label.set_color('#f0f0f0')
        self.axes.xaxis.label.set_color('#f0f0f0')
        self.axes.title.set_color('#f0f0f0')
        self.axes.grid(color='#3a3a3a', linestyle='-', linewidth=0.5, alpha=0.7)
        
        super().__init__(self.fig)
        self.setMinimumSize(300, 350)
        
        # Initialize with empty chart
        self.clear_chart()
    
    def clear_chart(self):
        """Clear the chart and show placeholder text"""
        self.axes.clear()
        
        # Set style again after clearing
        self.axes.set_facecolor('#2d2d2d')
        self.axes.spines['bottom'].set_color('#7f8c8d')
        self.axes.spines['top'].set_color('#7f8c8d')
        self.axes.spines['right'].set_color('#7f8c8d')
        self.axes.spines['left'].set_color('#7f8c8d')
        self.axes.tick_params(axis='x', colors='#f0f0f0')
        self.axes.tick_params(axis='y', colors='#f0f0f0')
        self.axes.grid(color='#3a3a3a', linestyle='-', linewidth=0.5, alpha=0.7)
        
        # Add placeholder text
        self.axes.text(0.5, 0.5, 'Select a match to view\nELO progression', 
                      horizontalalignment='center', verticalalignment='center',
                      transform=self.axes.transAxes, color='#f0f0f0', fontsize=10)
        
        self.axes.set_xlim(0, 1)
        self.axes.set_ylim(0, 1)
        self.axes.set_title("Player ELO Progression")
        
        self.fig.tight_layout()
        self.draw()
    
    def plot_elo_progression(self, home_data, away_data, home_name, away_name, match_limit, h2h_matches=None):
        """Plot ELO progression for both players"""
        # Clear previous plot
        self.axes.clear()
        
        # Set style again after clearing
        self.axes.set_facecolor('#2d2d2d')
        self.axes.spines['bottom'].set_color('#7f8c8d')
        self.axes.spines['top'].set_color('#7f8c8d')
        self.axes.spines['right'].set_color('#7f8c8d')
        self.axes.spines['left'].set_color('#7f8c8d')
        self.axes.tick_params(axis='x', colors='#f0f0f0')
        self.axes.tick_params(axis='y', colors='#f0f0f0')
        self.axes.grid(color='#3a3a3a', linestyle='-', linewidth=0.5, alpha=0.7)
        
        if not home_data and not away_data:
            self.axes.text(0.5, 0.5, 'No ELO data available\nfor these players', 
                          horizontalalignment='center', verticalalignment='center',
                          transform=self.axes.transAxes, color='#f0f0f0', fontsize=10)
            self.axes.set_xlim(0, 1)
            self.axes.set_ylim(0, 1)
        else:
            # Plot home player ELO progression
            if home_data:
                match_numbers = list(range(1, len(home_data) + 1))
                elo_values = [point['new_elo'] for point in home_data]
                self.axes.plot(match_numbers, elo_values, '-o', label=home_name, 
                              linewidth=2, markersize=3, color='#e74c3c')
            
            # Plot away player ELO progression  
            if away_data:
                match_numbers = list(range(1, len(away_data) + 1))
                elo_values = [point['new_elo'] for point in away_data]
                self.axes.plot(match_numbers, elo_values, '-o', label=away_name, 
                              linewidth=2, markersize=3, color='#2ecc71')
            
            # Set labels and title
            if match_limit == -1:
                self.axes.set_title(f"ELO Progression - All Time")
            else:
                self.axes.set_title(f"ELO Progression - Last {match_limit} Matches")
            self.axes.set_xlabel("Match Number (Recent)")
            self.axes.set_ylabel("ELO Rating")
            
            # Add legend with transparency and better positioning
            legend = self.axes.legend(loc='upper left', bbox_to_anchor=(0.02, 0.98))
            legend.get_frame().set_alpha(0.7)  # Make legend transparent
            
            # Set axis limits with some padding
            all_elos = []
            if home_data:
                all_elos.extend([p['new_elo'] for p in home_data])
            if away_data:
                all_elos.extend([p['new_elo'] for p in away_data])
            
            if all_elos:
                min_elo = min(all_elos)
                max_elo = max(all_elos)
                elo_range = max_elo - min_elo
                padding = max(20, elo_range * 0.1)  # At least 20 ELO padding
                
                self.axes.set_ylim(min_elo - padding, max_elo + padding)
                
                max_matches = max(len(home_data) if home_data else 0, 
                                len(away_data) if away_data else 0)
                if max_matches > 0:
                    self.axes.set_xlim(0.5, max_matches + 0.5)
                    
                # Add H2H vertical lines if data is available (but not for All Time)
                if h2h_matches and (home_data or away_data) and match_limit != -1:
                    self.add_h2h_vertical_lines(h2h_matches, home_data, away_data, home_name, away_name)
        
        self.fig.tight_layout()
        self.draw()

    def add_h2h_vertical_lines(self, h2h_matches, home_data, away_data, home_name, away_name):
        """Add vertical lines and labels for H2H matches within the plotted timeframe"""
        if not h2h_matches:
            return
            
        # Create combined time-ordered ELO data for position mapping
        combined_data = []
        if home_data:
            for i, point in enumerate(home_data):
                combined_data.append({
                    'match_time': point['match_time'],
                    'match_number': i + 1,
                    'player': home_name
                })
        if away_data:
            for i, point in enumerate(away_data):
                combined_data.append({
                    'match_time': point['match_time'],
                    'match_number': i + 1,
                    'player': away_name
                })
        
        # Sort by time to get proper ordering
        combined_data.sort(key=lambda x: x['match_time'])
        
        # Add vertical lines for each H2H match
        for h2h_match in h2h_matches:
            match_time = h2h_match.get('match_time_timestamp', 0)
            
            # Find the closest match position in our ELO data
            closest_match = None
            min_time_diff = float('inf')
            
            for data_point in combined_data:
                time_diff = abs(data_point['match_time'] - match_time)
                if time_diff < min_time_diff:
                    min_time_diff = time_diff
                    closest_match = data_point
            
            if closest_match:
                x_pos = closest_match['match_number']
                
                # Add thin vertical line
                self.axes.axvline(x=x_pos, color='#7f8c8d', linestyle='--', linewidth=1, alpha=0.7)
                
                # Create label with color-coded score based on who won
                home_score = h2h_match['home_score']
                away_score = h2h_match['away_score']
                
                # Get y position for label (near top of plot)
                y_min, y_max = self.axes.get_ylim()
                y_pos = y_min + (y_max - y_min) * 0.9
                
                # Get the scores and determine colors based on which line player scored what
                # home_name is ALWAYS the red line, away_name is ALWAYS the green line
                home_line_color = '#e74c3c'  # Red line color
                away_line_color = '#2ecc71'  # Green line color
                
                if h2h_match['home_player_name'] == home_name:
                    # home_name was home in this match, away_name was away
                    home_line_score = str(home_score)  # Red line player's score
                    away_line_score = str(away_score)  # Green line player's score
                else:
                    # home_name was away in this match, away_name was home
                    home_line_score = str(away_score)  # Red line player's score  
                    away_line_score = str(home_score)  # Green line player's score
                
                # Add the "h2h" label
                self.axes.text(x_pos, y_pos + (y_max - y_min) * 0.02, "h2h", rotation=90,
                             verticalalignment='bottom', horizontalalignment='center',
                             fontsize=7, color='#f0f0f0', alpha=0.8)
                
                # Add home line player's score in red
                self.axes.text(x_pos - 0.1, y_pos, home_line_score, rotation=90,
                             verticalalignment='top', horizontalalignment='center',
                             fontsize=8, color=home_line_color, alpha=0.9, weight='bold')
                
                # Add dash
                self.axes.text(x_pos, y_pos - (y_max - y_min) * 0.01, "-", rotation=90,
                             verticalalignment='top', horizontalalignment='center',
                             fontsize=8, color='#f0f0f0', alpha=0.8)
                
                # Add away line player's score in green
                self.axes.text(x_pos + 0.1, y_pos - (y_max - y_min) * 0.02, away_line_score, rotation=90,
                             verticalalignment='top', horizontalalignment='center',
                             fontsize=8, color=away_line_color, alpha=0.9, weight='bold')


class SetScoreDialog(QDialog):
    """Dialog to display detailed set scores and point progression for a match"""
    def __init__(self, parent=None, match_data=None):
        super().__init__(parent)
        self.setWindowTitle("Set Scores")
        self.setMinimumSize(1000, 700)  # Increased size to accommodate charts
        
        # Main layout
        layout = QVBoxLayout(self)
        
        # Match title
        if match_data:
            home_name = match_data.get('home', {}).get('name', 'Home')
            away_name = match_data.get('away', {}).get('name', 'Away')
            date_str = "Unknown Date"
            if match_data.get('time'):
                try:
                    match_time = int(match_data.get('time'))
                    date_str = datetime.fromtimestamp(match_time).strftime('%Y-%m-%d')
                except:
                    pass
            
            # Match title with date
            title_label = QLabel(f"{home_name} vs {away_name} ({date_str})")
            title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
            title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title_label)
            
            # Final score
            score_str = match_data.get('ss', 'Unknown Score')
            score_label = QLabel(f"Final Score: {score_str}")
            score_label.setFont(QFont("Arial", 12))
            score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(score_label)
            
            # Player names in a larger display
            players_widget = QWidget()
            players_layout = QHBoxLayout(players_widget)
            
            home_player_label = QLabel(home_name)
            home_player_label.setFont(QFont("Arial", 18, QFont.Weight.Bold))
            home_player_label.setStyleSheet("color: #e74c3c;")  # Red for home player
            home_player_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            vs_label = QLabel("vs")
            vs_label.setFont(QFont("Arial", 16))
            vs_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            away_player_label = QLabel(away_name)
            away_player_label.setFont(QFont("Arial", 18, QFont.Weight.Bold))
            away_player_label.setStyleSheet("color: #2ecc71;")  # Green for away player
            away_player_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            players_layout.addWidget(home_player_label)
            players_layout.addWidget(vs_label)
            players_layout.addWidget(away_player_label)
            
            layout.addWidget(players_widget)
            
            # Set scores and charts
            detailed_scores = match_data.get('detailed_scores', {})
            if detailed_scores:
                # Add tab option for viewing individual sets
                tab_option = QPushButton("View Sets in Tabs")
                tab_option.setCheckable(True)
                tab_option.setChecked(False)
                tab_option.toggled.connect(lambda checked: self.toggle_view_mode(checked, match_data, home_name, away_name))
                tab_option.setMaximumWidth(200)
                tab_option.setStyleSheet("""
                    QPushButton {
                        background-color: #34495e;
                        padding: 5px;
                        border-radius: 3px;
                    }
                    QPushButton:checked {
                        background-color: #3498db;
                    }
                """)
                
                layout.addWidget(tab_option, alignment=Qt.AlignmentFlag.AlignCenter)
                
                # Create stacked widget to switch between views
                self.stacked_widget = QStackedWidget()
                layout.addWidget(self.stacked_widget)
                
                # 1. Create comprehensive view (all sets at once)
                all_sets_widget = QScrollArea()
                all_sets_widget.setWidgetResizable(True)
                all_sets_content = QWidget()
                all_sets_layout = QVBoxLayout(all_sets_content)
                
                # Keep track of which sets have timeline data
                sets_with_timeline = set()
                
                # Get timeline data if available
                timeline_data = match_data.get('processed_timeline', {})
                
                # Create a set score summary at the top
                summary_group = QGroupBox("Set Scores Summary")
                summary_layout = QGridLayout()
                
                # Headers
                set_header = QLabel("Set")
                set_header.setFont(QFont("Arial", 11, QFont.Weight.Bold))
                home_header = QLabel(home_name)
                home_header.setFont(QFont("Arial", 11, QFont.Weight.Bold))
                away_header = QLabel(away_name)
                away_header.setFont(QFont("Arial", 11, QFont.Weight.Bold))
                
                summary_layout.addWidget(set_header, 0, 0)
                summary_layout.addWidget(home_header, 0, 1)
                summary_layout.addWidget(away_header, 0, 2)
                
                # Add set scores
                for i, (set_num, set_data) in enumerate(sorted(detailed_scores.items(), key=lambda x: int(x[0]))):
                    set_label = QLabel(f"Set {set_num}")
                    home_score = set_data.get('home', '-')
                    away_score = set_data.get('away', '-')
                    
                    home_score_label = QLabel(home_score)
                    away_score_label = QLabel(away_score)
                    
                    # Make score bold
                    home_score_label.setFont(QFont("Arial", 11))
                    away_score_label.setFont(QFont("Arial", 11))
                    
                    # Highlight winner
                    try:
                        if int(home_score) > int(away_score):
                            home_score_label.setStyleSheet("font-weight: bold; color: #2ecc71;")
                        else:
                            away_score_label.setStyleSheet("font-weight: bold; color: #2ecc71;")
                    except:
                        pass
                    
                    summary_layout.addWidget(set_label, i+1, 0)
                    summary_layout.addWidget(home_score_label, i+1, 1)
                    summary_layout.addWidget(away_score_label, i+1, 2)
                
                summary_group.setLayout(summary_layout)
                all_sets_layout.addWidget(summary_group)
                
                # Create grid for charts (2 columns if 4+ sets)
                use_grid = len(detailed_scores) >= 3
                if use_grid:
                    charts_widget = QWidget()
                    if len(detailed_scores) >= 4:
                        charts_layout = QGridLayout(charts_widget)
                    else:
                        charts_layout = QVBoxLayout(charts_widget)
                    
                    # Add charts to grid/column
                    row, col = 0, 0
                    for set_num, set_data in sorted(detailed_scores.items(), key=lambda x: int(x[0])):
                        set_group = QGroupBox(f"Set {set_num}")
                        set_layout = QVBoxLayout(set_group)
                        
                        if timeline_data and set_num in timeline_data:
                            # Create point progression chart
                            chart = PointProgressionCanvas(self, width=5, height=3, dpi=100)
                            chart.plot_point_progression(timeline_data[set_num], home_name, away_name, set_num)
                            set_layout.addWidget(chart)
                            sets_with_timeline.add(set_num)
                        else:
                            # No point progression data
                            no_data = QLabel("No point progression data")
                            no_data.setAlignment(Qt.AlignmentFlag.AlignCenter)
                            set_layout.addWidget(no_data)
                        
                        if len(detailed_scores) >= 4:
                            charts_layout.addWidget(set_group, row, col)
                            col += 1
                            if col > 1:  # 2 columns max
                                col = 0
                                row += 1
                        else:
                            charts_layout.addWidget(set_group)
                    
                    all_sets_layout.addWidget(charts_widget)
                else:
                    # For 1-2 sets, just add charts directly in a column
                    for set_num, set_data in sorted(detailed_scores.items(), key=lambda x: int(x[0])):
                        set_group = QGroupBox(f"Set {set_num}")
                        set_layout = QVBoxLayout(set_group)
                        
                        if timeline_data and set_num in timeline_data:
                            # Create point progression chart
                            chart = PointProgressionCanvas(self, width=6, height=3, dpi=100)
                            chart.plot_point_progression(timeline_data[set_num], home_name, away_name, set_num)
                            set_layout.addWidget(chart)
                            sets_with_timeline.add(set_num)
                        else:
                            # No point progression data
                            no_data = QLabel("No point progression data")
                            no_data.setAlignment(Qt.AlignmentFlag.AlignCenter)
                            set_layout.addWidget(no_data)
                            
                        all_sets_layout.addWidget(set_group)
                
                # If we have no timeline data at all, show a message
                if not sets_with_timeline and detailed_scores:
                    note_label = QLabel("Note: Point progression data is not available for this match.")
                    note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    note_label.setStyleSheet("color: #f39c12;")  # Orange warning color
                    all_sets_layout.addWidget(note_label)
                
                all_sets_widget.setWidget(all_sets_content)
                self.stacked_widget.addWidget(all_sets_widget)
                
                # 2. Create tab view (each set in a tab)
                tab_widget = QTabWidget()
                for set_num, set_data in sorted(detailed_scores.items(), key=lambda x: int(x[0])):
                    set_tab = QWidget()
                    set_layout = QVBoxLayout(set_tab)
                    
                    # Score summary
                    score_group = QGroupBox(f"Set {set_num} Score")
                    score_layout = QHBoxLayout(score_group)
                    
                    home_score = set_data.get('home', '-')
                    away_score = set_data.get('away', '-')
                    
                    home_label = QLabel(f"{home_name}: ")
                    home_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
                    home_score_label = QLabel(home_score)
                    home_score_label.setFont(QFont("Arial", 10))
                    
                    away_label = QLabel(f"{away_name}: ")
                    away_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
                    away_score_label = QLabel(away_score)
                    away_score_label.setFont(QFont("Arial", 10))
                    
                    # Highlight winner
                    try:
                        if int(home_score) > int(away_score):
                            home_score_label.setStyleSheet("font-weight: bold; color: #2ecc71;")
                        else:
                            away_score_label.setStyleSheet("font-weight: bold; color: #2ecc71;")
                    except:
                        pass
                    
                    score_layout.addWidget(home_label)
                    score_layout.addWidget(home_score_label)
                    score_layout.addStretch()
                    score_layout.addWidget(away_label)
                    score_layout.addWidget(away_score_label)
                    
                    set_layout.addWidget(score_group)
                    
                    # Check if we have point progression data for this set
                    if timeline_data and set_num in timeline_data:
                        # Create and add point progression chart
                        point_chart = PointProgressionCanvas(self, width=6, height=4, dpi=100)
                        point_chart.plot_point_progression(timeline_data[set_num], home_name, away_name, set_num)
                        set_layout.addWidget(point_chart)
                    else:
                        # No point progression data
                        no_chart_label = QLabel("No point progression data available for this set")
                        no_chart_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                        set_layout.addWidget(no_chart_label)
                    
                    tab_widget.addTab(set_tab, f"Set {set_num}")
                
                self.stacked_widget.addWidget(tab_widget)
                
                # Default to comprehensive view
                self.stacked_widget.setCurrentIndex(0)
            else:
                no_details_label = QLabel("No detailed set scores available for this match")
                no_details_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(no_details_label)
        else:
            error_label = QLabel("No match data available")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
        
        # Button box
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
        # Apply dark theme
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #f0f0f0;
            }
            QLabel {
                color: #f0f0f0;
            }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
                color: #3498db;
            }
            QTabWidget::pane {
                border: 1px solid #3a3a3a;
                background-color: #2d2d2d;
            }
            QTabBar::tab {
                background-color: #2c3e50;
                color: #f0f0f0;
                padding: 8px 12px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background-color: #3498db;
                font-weight: bold;
            }
            QScrollArea {
                border: none;
            }
        """)
    
    def toggle_view_mode(self, tab_mode, match_data, home_name, away_name):
        """Toggle between comprehensive and tab view modes"""
        if tab_mode:
            self.stacked_widget.setCurrentIndex(1)  # Tab view
        else:
            self.stacked_widget.setCurrentIndex(0)  # Comprehensive view


def get_player_streak_data(player_id, league_id, limit=20):
    """
    Calculate hot streak data for a player
    Returns dict with current streak, recent form, and streak history
    """
    db = TTDatabase()
    
    # Get recent ELO history for the player
    query = """
    SELECT eh.old_elo, eh.new_elo, eh.match_date, eh.match_id,
           m.home_player_id, m.away_player_id, m.home_score, m.away_score
    FROM elo_history eh
    JOIN matches m ON eh.match_id = m.id
    WHERE eh.player_id = ? AND eh.league_id = ?
    ORDER BY eh.match_date DESC
    LIMIT ?
    """
    
    db.cursor.execute(query, (player_id, league_id, limit))
    results = db.cursor.fetchall()
    db.close()
    
    if not results:
        return {
            'current_streak': 0,
            'streak_type': 'none',
            'recent_form': {'wins': 0, 'total': 0, 'win_rate': 0.0},
            'form_5': {'wins': 0, 'total': 0, 'win_rate': 0.0},
            'form_10': {'wins': 0, 'total': 0, 'win_rate': 0.0},
            'elo_change': 0,
            'form_trend': 'neutral'
        }
    
    # Analyze matches
    wins = []
    elo_changes = []
    
    for result in results:
        old_elo, new_elo = result['old_elo'], result['new_elo']
        
        # Determine if player won
        won = new_elo > old_elo
        wins.append(won)
        elo_changes.append(new_elo - old_elo)
    
    # Calculate current streak
    current_streak = 0
    streak_type = 'none'
    
    if wins:
        streak_type = 'win' if wins[0] else 'loss'
        for won in wins:
            if (streak_type == 'win' and won) or (streak_type == 'loss' and not won):
                current_streak += 1
            else:
                break
    
    # Calculate form over different periods
    def calc_form(games_back):
        if len(wins) >= games_back:
            recent_wins = sum(wins[:games_back])
            return {
                'wins': recent_wins,
                'total': games_back,
                'win_rate': recent_wins / games_back
            }
        elif wins:
            recent_wins = sum(wins)
            return {
                'wins': recent_wins,
                'total': len(wins),
                'win_rate': recent_wins / len(wins)
            }
        else:
            return {'wins': 0, 'total': 0, 'win_rate': 0.0}
    
    # ELO change over recent period
    elo_change = sum(elo_changes) if elo_changes else 0
    
    # Determine form trend
    form_trend = 'neutral'
    if len(wins) >= 10:
        recent_5_wr = calc_form(5)['win_rate']
        older_5_wr = sum(wins[5:10]) / 5 if len(wins) >= 10 else calc_form(len(wins) - 5)['win_rate']
        if recent_5_wr > older_5_wr + 0.2:
            form_trend = 'improving'
        elif recent_5_wr < older_5_wr - 0.2:
            form_trend = 'declining'
    
    return {
        'current_streak': current_streak,
        'streak_type': streak_type,
        'recent_form': calc_form(limit),
        'form_5': calc_form(5),
        'form_10': calc_form(10),
        'elo_change': elo_change,
        'form_trend': form_trend
    }


def get_streak_color(streak_data):
    """Return color code based on streak data"""
    if streak_data['streak_type'] == 'win' and streak_data['current_streak'] >= 3:
        return "#27ae60"  # Green for hot streak
    elif streak_data['streak_type'] == 'loss' and streak_data['current_streak'] >= 3:
        return "#e74c3c"  # Red for cold streak
    elif streak_data['form_5']['win_rate'] >= 0.7:
        return "#f39c12"  # Orange for good form
    elif streak_data['form_5']['win_rate'] <= 0.3:
        return "#e67e22"  # Orange for poor form
    else:
        return "#95a5a6"  # Gray for neutral


class TableTennisGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Table Tennis Tracker")
        self.setMinimumSize(1200, 800)
        
        # Store data
        self.data = {}
        self.leagues = {
            "22307": "Setka Cup",
            "22742": "Czech Republic Liga Pro",
            "22534": "TT CUP",
            "24536": "Poland TT Elite Series"
        }
        
        # Setup UI
        self.setup_ui()
        
        # Load data from JSON files
        self.load_data()
        
        # Update ELO status
        self.update_elo_status()
        
        # Initialize ELO chart
        self.elo_chart.clear_chart()
        
        # Initialize async data fetcher
        self.data_fetcher = None
        
        # Set up auto-refresh timer (every 30 minutes)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.refresh_timer.start(1800000)  # 30 minutes

    def setup_ui(self):
        # Create central widget and main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        
        # Create main layout with splitter
        self.main_layout = QHBoxLayout(self.central_widget)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_layout.addWidget(self.splitter)
        
        # Set up the three panels
        self.setup_control_panel()
        self.setup_matches_panel()
        self.setup_details_panel()
        
        # Set splitter sizes
        self.splitter.setSizes([250, 500, 450])
        
        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))
        
        # Apply dark theme
        self.apply_dark_theme()

    def setup_control_panel(self):
        # Left control panel
        self.control_panel = QWidget()
        self.control_layout = QVBoxLayout(self.control_panel)
        
        # Logo or title
        title_label = QLabel("TABLE TENNIS TRACKER")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #3498db; margin: 10px;")
        self.control_layout.addWidget(title_label)
        
        # League selection
        league_group = QGroupBox("Leagues")
        league_layout = QVBoxLayout(league_group)
        
        self.league_checkboxes = {}
        for league_id, league_name in self.leagues.items():
            checkbox = QPushButton(league_name)
            checkbox.setCheckable(True)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.filter_matches)
            checkbox.setStyleSheet("""
                QPushButton {
                    text-align: left;
                    padding: 5px;
                    border: none;
                    border-radius: 3px;
                    background-color: #2c3e50;
                }
                QPushButton:checked {
                    background-color: #3498db;
                }
            """)
            self.league_checkboxes[league_id] = checkbox
            league_layout.addWidget(checkbox)
        
        self.control_layout.addWidget(league_group)
        
        # Search box
        search_group = QGroupBox("Search")
        search_layout = QVBoxLayout(search_group)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by player name...")
        self.search_input.textChanged.connect(self.filter_matches)
        search_layout.addWidget(self.search_input)
        
        self.control_layout.addWidget(search_group)
        
        # Time filter
        time_group = QGroupBox("Time Window")
        time_layout = QVBoxLayout(time_group)
        
        self.time_combo = QComboBox()
        self.time_combo.addItems(["Next 1 hour", "Next 3 hours", "Next 6 hours", "All matches"])
        self.time_combo.setCurrentIndex(2)  # Default to 6 hours
        self.time_combo.currentIndexChanged.connect(self.filter_matches)
        time_layout.addWidget(self.time_combo)
        
        self.control_layout.addWidget(time_group)
        
        # Combined refresh and ELO update button
        self.refresh_btn = QPushButton("Refresh Data & Update ELO")
        self.refresh_btn.clicked.connect(self.refresh_data_and_elo)
        self.refresh_btn.setStyleSheet("background-color: #27ae60; padding: 8px;")
        self.control_layout.addWidget(self.refresh_btn)
        
        # Status label
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.control_layout.addWidget(self.status_label)
        
        # ELO status label
        self.elo_status_label = QLabel("ELO: Unknown")
        self.elo_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elo_status_label.setStyleSheet("color: #f39c12; font-size: 10px;")
        self.control_layout.addWidget(self.elo_status_label)
        
        # ELO Chart Section
        elo_chart_group = QGroupBox("Player ELO Progression")
        elo_chart_layout = QVBoxLayout(elo_chart_group)
        
        # Time window selector
        time_window_layout = QHBoxLayout()
        time_window_label = QLabel("Matches:")
        time_window_label.setFont(QFont("Arial", 10))
        self.elo_time_window = QComboBox()
        self.elo_time_window.addItems(["Last 10", "Last 25", "Last 50", "All Time"])
        self.elo_time_window.setCurrentIndex(1)  # Default to 25
        self.elo_time_window.currentIndexChanged.connect(self.update_elo_chart)
        time_window_layout.addWidget(time_window_label)
        time_window_layout.addWidget(self.elo_time_window)
        time_window_layout.addStretch()
        
        elo_chart_layout.addLayout(time_window_layout)
        
        # ELO Chart Canvas (dynamically sized with proper constraints)
        self.elo_chart = ELOProgressionCanvas(self, width=4, height=5, dpi=80)
        self.elo_chart.setMinimumHeight(300)  # Increased minimum height
        # Remove maximum height constraint to allow growth but use stretch factors to control
        elo_chart_layout.addWidget(self.elo_chart, 1)  # Give stretch factor to expand
        
        # Player Info Panel (Hot Streak Analysis) - compact design
        self.player_info_panel = QGroupBox("Player Form Analysis")
        player_info_layout = QHBoxLayout(self.player_info_panel)  # Use horizontal layout instead
        
        # Home player info (vertical layout within)
        home_info_widget = QWidget()
        home_info_layout = QVBoxLayout(home_info_widget)
        home_info_layout.setContentsMargins(5, 5, 5, 5)
        home_info_layout.setSpacing(2)
        
        self.home_player_label = QLabel("Home Player")
        self.home_player_label.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.home_streak_label = QLabel("No data")
        self.home_form_label = QLabel("Form: -")
        
        home_info_layout.addWidget(self.home_player_label)
        home_info_layout.addWidget(self.home_streak_label)
        home_info_layout.addWidget(self.home_form_label)
        
        # Away player info (vertical layout within)
        away_info_widget = QWidget()
        away_info_layout = QVBoxLayout(away_info_widget)
        away_info_layout.setContentsMargins(5, 5, 5, 5)
        away_info_layout.setSpacing(2)
        
        self.away_player_label = QLabel("Away Player")
        self.away_player_label.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.away_streak_label = QLabel("No data")
        self.away_form_label = QLabel("Form: -")
        
        away_info_layout.addWidget(self.away_player_label)
        away_info_layout.addWidget(self.away_streak_label)
        away_info_layout.addWidget(self.away_form_label)
        
        # Add both player info widgets to horizontal layout
        player_info_layout.addWidget(home_info_widget)
        player_info_layout.addWidget(away_info_widget)
        
        # Set reasonable fixed height
        self.player_info_panel.setFixedHeight(85)
        
        # Add to ELO chart layout with no stretch factor (fixed size)
        elo_chart_layout.addWidget(self.player_info_panel, 0)
        
        # Give the chart group significant weight to expand vertically
        self.control_layout.addWidget(elo_chart_group, 2)  # Increased weight for more space
        
        # Minimal bottom stretch to use available space efficiently
        self.control_layout.addStretch(0)
        
        # Add to splitter
        self.splitter.addWidget(self.control_panel)

    def setup_matches_panel(self):
        # Center matches panel
        self.matches_panel = QWidget()
        self.matches_layout = QVBoxLayout(self.matches_panel)
        
        # Matches count and header
        self.matches_header = QLabel("Upcoming Matches (0)")
        self.matches_header.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.matches_layout.addWidget(self.matches_header)
        
        # Matches table
        self.matches_table = QTableWidget(0, 5)
        self.matches_table.setHorizontalHeaderLabels(["Time", "League", "Home", "Away", "Odds"])
        self.matches_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.matches_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.matches_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.matches_table.verticalHeader().setVisible(False)
        self.matches_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.matches_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.matches_table.itemSelectionChanged.connect(self.show_match_details)
        
        self.matches_layout.addWidget(self.matches_table)
        
        # Add to splitter
        self.splitter.addWidget(self.matches_panel)

    def setup_details_panel(self):
        # Right details panel
        self.details_panel = QScrollArea()
        self.details_panel.setWidgetResizable(True)
        
        # Container widget for scroll area
        self.details_widget = QWidget()
        self.details_layout = QVBoxLayout(self.details_widget)
        
        # Match details header
        self.match_title = QLabel("Select a match to view details")
        self.match_title.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.match_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(self.match_title)
        
        # Match summary
        self.match_summary = QGroupBox("Match Info")
        summary_layout = QGridLayout(self.match_summary)
        
        self.summary_labels = {
            "league": QLabel("League: "),
            "time": QLabel("Time: "),
            "home": QLabel("Home: "),
            "away": QLabel("Away: ")
        }
        
        row = 0
        label_keys = list(self.summary_labels.keys())  # Create a static list to iterate over
        for key in label_keys:
            label = self.summary_labels[key]
            value_label = QLabel("-")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.summary_labels[key + "_val"] = value_label
            summary_layout.addWidget(label, row, 0)
            summary_layout.addWidget(value_label, row, 1)
            row += 1
            
        self.details_layout.addWidget(self.match_summary)
        
        # Player Analysis - Two Side-by-Side Panels
        player_analysis_container = QHBoxLayout()
        
        # Home Player Panel
        self.home_player_group = QGroupBox("Home Player")
        self.home_player_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        home_layout = QVBoxLayout(self.home_player_group)
        
        self.home_player_name = QLabel("Home Player")
        self.home_player_name.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.home_player_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.home_elo_display = QLabel("ELO: -")
        self.home_elo_display.setFont(QFont("Arial", 11))
        self.home_elo_display.setStyleSheet("color: #3498db; font-weight: bold;")
        self.home_elo_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        home_prob_layout = QHBoxLayout()
        self.home_elo_win_prob = QLabel("ELO: -%")
        self.home_elo_win_prob.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self.home_elo_win_prob.setStyleSheet("color: #27ae60;")
        self.home_elo_win_prob.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.home_odds_win_prob = QLabel("Odds: -%")
        self.home_odds_win_prob.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self.home_odds_win_prob.setStyleSheet("color: #f39c12;")
        self.home_odds_win_prob.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        home_prob_layout.addStretch()
        home_prob_layout.addWidget(self.home_elo_win_prob)
        home_prob_layout.addWidget(self.home_odds_win_prob)
        home_prob_layout.addStretch()
        
        home_layout.addWidget(self.home_player_name)
        home_layout.addWidget(self.home_elo_display)
        home_layout.addLayout(home_prob_layout)
        
        # Away Player Panel
        self.away_player_group = QGroupBox("Away Player")
        self.away_player_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        away_layout = QVBoxLayout(self.away_player_group)
        
        self.away_player_name = QLabel("Away Player")
        self.away_player_name.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.away_player_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.away_elo_display = QLabel("ELO: -")
        self.away_elo_display.setFont(QFont("Arial", 11))
        self.away_elo_display.setStyleSheet("color: #3498db; font-weight: bold;")
        self.away_elo_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        away_prob_layout = QHBoxLayout()
        self.away_elo_win_prob = QLabel("ELO: -%")
        self.away_elo_win_prob.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self.away_elo_win_prob.setStyleSheet("color: #e74c3c;")
        self.away_elo_win_prob.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.away_odds_win_prob = QLabel("Odds: -%")
        self.away_odds_win_prob.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self.away_odds_win_prob.setStyleSheet("color: #f39c12;")
        self.away_odds_win_prob.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        away_prob_layout.addStretch()
        away_prob_layout.addWidget(self.away_elo_win_prob)
        away_prob_layout.addWidget(self.away_odds_win_prob)
        away_prob_layout.addStretch()
        
        away_layout.addWidget(self.away_player_name)
        away_layout.addWidget(self.away_elo_display)
        away_layout.addLayout(away_prob_layout)
        
        # Add both panels to container
        player_analysis_container.addWidget(self.home_player_group)
        player_analysis_container.addWidget(self.away_player_group)
        
        # Create a widget to hold the container layout
        player_analysis_widget = QWidget()
        player_analysis_widget.setLayout(player_analysis_container)
        
        self.details_layout.addWidget(player_analysis_widget)
        
        # Odds details
        self.odds_group = QGroupBox("Betting Odds")
        self.odds_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        odds_layout = QGridLayout(self.odds_group)
        
        # Add odds format toggle at the top
        odds_format_layout = QHBoxLayout()
        odds_format_label = QLabel("Odds Format:")
        odds_format_label.setFont(QFont("Arial", 10))
        self.odds_format_checkbox = QPushButton("Decimal Odds")
        self.odds_format_checkbox.setCheckable(True)
        self.odds_format_checkbox.setChecked(False)  # Default to American odds (unchecked)
        self.odds_format_checkbox.toggled.connect(self.toggle_odds_format)
        self.odds_format_checkbox.setStyleSheet("""
            QPushButton {
                text-align: center;
                padding: 3px;
                border: none;
                border-radius: 3px;
                background-color: #2c3e50;
            }
            QPushButton:checked {
                background-color: #3498db;
            }
        """)
        odds_format_layout.addWidget(odds_format_label)
        odds_format_layout.addWidget(self.odds_format_checkbox)
        odds_format_layout.addStretch()
        
        odds_layout.addLayout(odds_format_layout, 0, 0, 1, 4)
        
        # Increase font size for headers and values
        header_font = QFont("Arial", 11, QFont.Weight.Bold)
        value_font = QFont("Arial", 10)
        small_font = QFont("Arial", 9)
        
        market_label = QLabel("Market")
        market_label.setFont(header_font)
        home_label = QLabel("Home (NoVig)")
        home_label.setFont(header_font)
        away_label = QLabel("Away/Under")
        away_label.setFont(header_font)
        handicap_label = QLabel("Handicap/Total")
        handicap_label.setFont(header_font)
        
        odds_layout.addWidget(market_label, 1, 0)
        odds_layout.addWidget(home_label, 1, 1)
        odds_layout.addWidget(away_label, 1, 2)
        odds_layout.addWidget(handicap_label, 1, 3)
        
        self.odds_labels = {
            "moneyline_home": QLabel("-"),
            "moneyline_away": QLabel("-"),
            "spread_home": QLabel("-"),
            "spread_away": QLabel("-"),
            "spread_handicap": QLabel("-"),
            "total_over": QLabel("-"),
            "total_under": QLabel("-"),
            "total_points": QLabel("-")
        }
        
        # Set larger font for all odds labels
        for label in self.odds_labels.values():
            label.setFont(value_font)
        
        moneyline_label = QLabel("Moneyline")
        moneyline_label.setFont(value_font)
        spread_label = QLabel("Spread")
        spread_label.setFont(value_font)
        total_label = QLabel("Total")
        total_label.setFont(value_font)
        
        odds_layout.addWidget(moneyline_label, 2, 0)
        odds_layout.addWidget(self.odds_labels["moneyline_home"], 2, 1)
        odds_layout.addWidget(self.odds_labels["moneyline_away"], 2, 2)
        odds_layout.addWidget(QLabel("-"), 2, 3)
        
        odds_layout.addWidget(spread_label, 3, 0)
        odds_layout.addWidget(self.odds_labels["spread_home"], 3, 1)
        odds_layout.addWidget(self.odds_labels["spread_away"], 3, 2)
        odds_layout.addWidget(self.odds_labels["spread_handicap"], 3, 3)
        
        odds_layout.addWidget(total_label, 4, 0)
        odds_layout.addWidget(self.odds_labels["total_over"], 4, 1)
        odds_layout.addWidget(self.odds_labels["total_under"], 4, 2)
        odds_layout.addWidget(self.odds_labels["total_points"], 4, 3)
        
        # Make row heights taller
        odds_layout.setRowMinimumHeight(2, 40)
        odds_layout.setRowMinimumHeight(3, 40)
        odds_layout.setRowMinimumHeight(4, 40)
        
        self.details_layout.addWidget(self.odds_group)
        
        # Store the raw decimal odds for conversion between formats
        self.raw_odds = {
            "moneyline_home": "-",
            "moneyline_away": "-",
            "spread_home": "-",
            "spread_away": "-",
            "total_over": "-",
            "total_under": "-"
        }
        
        # Store NoVig odds
        self.novig_odds = {
            "moneyline_home": "-",
            "moneyline_away": "-",
            "spread_home": "-",
            "spread_away": "-",
            "total_over": "-",
            "total_under": "-"
        }
        
        # Head-to-head
        self.h2h_group = QGroupBox("Head-to-Head History")
        h2h_layout = QVBoxLayout(self.h2h_group)
        
        self.h2h_summary = QLabel("No head-to-head data available")
        self.h2h_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h2h_layout.addWidget(self.h2h_summary)
        
        self.h2h_table = QTableWidget(0, 5)  # Updated to 5 columns to include the "Sets" button
        self.h2h_table.setHorizontalHeaderLabels(["Date", "Home", "Away", "Score", "Details"])
        self.h2h_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.h2h_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.h2h_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.h2h_table.verticalHeader().setVisible(False)
        self.h2h_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h2h_layout.addWidget(self.h2h_table)
        
        self.details_layout.addWidget(self.h2h_group)
        
        # Win probability chart
        self.prob_group = QGroupBox("H2H results")
        prob_layout = QHBoxLayout(self.prob_group)
        
        self.home_prob = QLabel("Home: 50%")
        self.away_prob = QLabel("Away: 50%")
        prob_layout.addWidget(self.home_prob)
        prob_layout.addWidget(self.away_prob)
        
        self.details_layout.addWidget(self.prob_group)
        
        # Set the widget to the scroll area
        self.details_panel.setWidget(self.details_widget)
        
        # Add to splitter
        self.splitter.addWidget(self.details_panel)

    def apply_dark_theme(self):
        # Set dark theme colors
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #f0f0f0;
            }
            QTableWidget {
                background-color: #2d2d2d;
                alternate-background-color: #353535;
                gridline-color: #3a3a3a;
                border: 1px solid #3a3a3a;
            }
            QTableWidget::item:selected {
                background-color: #2980b9;
            }
            QHeaderView::section {
                background-color: #2c3e50;
                padding: 4px;
                border: 1px solid #2c3e50;
                color: white;
            }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
                color: #3498db;
            }
            QLineEdit, QComboBox {
                background-color: #2d2d2d;
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                padding: 5px;
                color: white;
            }
            QPushButton {
                background-color: #2980b9;
                color: white;
                border: none;
                border-radius: 3px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #3498db;
            }
            QPushButton:pressed {
                background-color: #1c6ea4;
            }
            QLabel {
                color: #f0f0f0;
            }
            QDialog {
                background-color: #1e1e1e;
                color: #f0f0f0;
            }
            QDialogButtonBox > QPushButton {
                min-width: 80px;
                padding: 6px;
            }
        """)

    def decimal_to_american(self, decimal_odds):
        """Convert decimal odds to American format"""
        try:
            decimal = float(decimal_odds)
            if decimal >= 2.0:
                # Positive American odds (underdog)
                return f"+{int((decimal - 1) * 100)}"
            else:
                # Negative American odds (favorite)
                return f"-{int(100 / (decimal - 1))}"
        except (ValueError, ZeroDivisionError):
            return "-"  # Return a dash if conversion fails

    def calculate_novig_odds(self, odds1, odds2):
        """Calculate no-vig fair odds from two decimal odds"""
        try:
            decimal1 = float(odds1)
            decimal2 = float(odds2)
            
            # Convert to implied probabilities
            prob1 = 1 / decimal1
            prob2 = 1 / decimal2
            
            # Calculate the vig (overround)
            total_prob = prob1 + prob2
            
            # Remove vig by normalizing probabilities
            fair_prob1 = prob1 / total_prob
            fair_prob2 = prob2 / total_prob
            
            # Convert back to decimal odds
            fair_odds1 = 1 / fair_prob1
            fair_odds2 = 1 / fair_prob2
            
            return round(fair_odds1, 2), round(fair_odds2, 2)
        except (ValueError, ZeroDivisionError):
            return "-", "-"

    def calculate_elo_win_probability(self, elo1, elo2):
        """Calculate win probability based on ELO difference"""
        try:
            elo_diff = float(elo1) - float(elo2)
            # Standard ELO formula: P = 1 / (1 + 10^(elo_diff/400))
            win_prob1 = 1 / (1 + 10**(-elo_diff / 400))
            win_prob2 = 1 - win_prob1
            return round(win_prob1 * 100, 1), round(win_prob2 * 100, 1)
        except (ValueError, TypeError):
            return "-", "-"

    def calculate_implied_probability(self, decimal_odds):
        """Calculate implied probability from decimal odds"""
        try:
            odds = float(decimal_odds)
            implied_prob = (1 / odds) * 100
            return round(implied_prob, 1)
        except (ValueError, ZeroDivisionError):
            return "-"

    def get_player_current_elo(self, player_name, league_id):
        """Get current ELO for a player from database (fast lookup)"""
        try:
            db = TTDatabase()
            
            # Find player ID by name and league
            db.cursor.execute("""
                SELECT id FROM players 
                WHERE name = ? AND league_id = ?
            """, (player_name, league_id))
            
            result = db.cursor.fetchone()
            if not result:
                return 1500  # Default ELO for new players
                
            player_id = result[0]
            
            # Get current ELO from fast current_elo table
            db.cursor.execute("""
                SELECT elo FROM current_elo 
                WHERE player_id = ? AND league_id = ?
            """, (player_id, league_id))
            
            elo_result = db.cursor.fetchone()
            if elo_result:
                return elo_result[0]
            else:
                return 1500  # Default ELO if not found
                
        except Exception as e:
            print(f"Error getting ELO for {player_name}: {e}")
            return 1500

    def get_player_elo_from_history(self, player_name, league_id):
        """Get most recent ELO from history table (for chart consistency)"""
        try:
            db = TTDatabase()
            
            # Get the most recent ELO from elo_history table (same source as chart)
            db.cursor.execute("""
                SELECT eh.new_elo
                FROM elo_history eh
                JOIN players p ON eh.player_id = p.id AND eh.league_id = p.league_id
                JOIN matches m ON eh.match_id = m.id
                WHERE p.name = ? AND eh.league_id = ?
                ORDER BY m.match_time DESC
                LIMIT 1
            """, (player_name, league_id))
            
            result = db.cursor.fetchone()
            if result:
                return result[0]
            else:
                # Fallback to current_elo table
                return self.get_player_current_elo(player_name, league_id)
                
        except Exception as e:
            print(f"Error getting ELO from history for {player_name}: {e}")
            return self.get_player_current_elo(player_name, league_id)

    def calculate_elo_vs_bookmaker_deviation(self, home_name, away_name, home_odds, away_odds, league_id):
        """Calculate deviation between ELO and bookmaker probabilities"""
        try:
            # Ensure league_id is an integer
            if isinstance(league_id, str):
                league_id = int(league_id)
            
            # Get ELO ratings for both players
            home_elo = self.get_player_current_elo(home_name, league_id)
            away_elo = self.get_player_current_elo(away_name, league_id)
            
            # Calculate ELO-based win probabilities
            home_elo_prob, away_elo_prob = self.calculate_elo_win_probability(home_elo, away_elo)
            
            # Convert to decimal values (remove the percentage)
            if home_elo_prob != "-" and away_elo_prob != "-":
                home_elo_prob = float(home_elo_prob) / 100
                away_elo_prob = float(away_elo_prob) / 100
            else:
                return None, None  # No ELO data available
            
            # Calculate bookmaker implied probabilities
            home_bookmaker_prob = self.calculate_implied_probability(home_odds)
            away_bookmaker_prob = self.calculate_implied_probability(away_odds)
            
            # Convert to decimal values
            if home_bookmaker_prob != "-" and away_bookmaker_prob != "-":
                home_bookmaker_prob = float(home_bookmaker_prob) / 100
                away_bookmaker_prob = float(away_bookmaker_prob) / 100
            else:
                return None, None  # No odds data available
            
            # Calculate deviations (ELO probability - Bookmaker probability)
            # Positive = ELO thinks player is undervalued by bookmaker
            # Negative = ELO thinks player is overvalued by bookmaker
            home_deviation = home_elo_prob - home_bookmaker_prob
            away_deviation = away_elo_prob - away_bookmaker_prob
            
            return home_deviation, away_deviation
            
        except Exception as e:
            print(f"Error calculating ELO vs bookmaker deviation: {e}")
            return None, None

    def get_value_bet_colors(self, home_deviation, away_deviation):
        """Get colors based on ELO vs bookmaker deviation"""
        from PyQt6.QtGui import QColor
        
        def deviation_to_color(deviation):
            if deviation is None:
                return None  # No color (default)
            elif deviation > 0.15:  # >15% deviation - strong value
                return QColor(0, 150, 0, 100)  # Bright green
            elif deviation > 0.05:  # 5-15% deviation - potential value
                return QColor(100, 200, 100, 80)  # Light green
            elif deviation < -0.15:  # <-15% deviation - strong avoid
                return QColor(200, 50, 50, 100)  # Bright red
            elif deviation < -0.05:  # -15% to -5% deviation - avoid
                return QColor(200, 150, 150, 80)  # Light red
            else:  # -5% to +5% deviation - fair line
                return None  # No color (default/gray)
        
        home_color = deviation_to_color(home_deviation)
        away_color = deviation_to_color(away_deviation)
        
        return home_color, away_color

    def toggle_odds_format(self):
        """Toggle between American and Decimal odds formats"""
        show_decimal = self.odds_format_checkbox.isChecked()
        
        if show_decimal:
            self.odds_format_checkbox.setText("American Odds")
            # Show combined decimal odds (original and NoVig)
            for key, value in self.raw_odds.items():
                if value != "-" and key in self.odds_labels:
                    novig_value = self.novig_odds.get(key, "-")
                    if novig_value != "-":
                        combined_display = f"{value} ({novig_value})"
                    else:
                        combined_display = value
                    self.odds_labels[key].setText(combined_display)
        else:
            self.odds_format_checkbox.setText("Decimal Odds")
            # Show combined American odds (original and NoVig)
            for key, value in self.raw_odds.items():
                if value != "-" and key in self.odds_labels:
                    orig_american = self.decimal_to_american(value)
                    novig_value = self.novig_odds.get(key, "-")
                    if novig_value != "-":
                        novig_american = self.decimal_to_american(novig_value)
                        combined_display = f"{orig_american} ({novig_american})"
                    else:
                        combined_display = orig_american
                    self.odds_labels[key].setText(combined_display)
        
        # Keep styling
        for key in self.raw_odds.keys():
            if key.startswith("moneyline"):
                try:
                    if show_decimal:
                        odds_value = float(self.raw_odds[key])
                    else:
                        american = self.odds_labels[key].text()
                        if american.startswith("+"):
                            odds_value = float(american[1:]) / 100 + 1
                        else:
                            odds_value = 100 / float(american[1:]) + 1
                    
                    color = self.get_odds_color(odds_value)
                    self.odds_labels[key].setStyleSheet(f"color: {color};")
                except:
                    pass

    def refresh_data(self):
        """Refresh data by running the client script asynchronously (data only)"""
        self._start_data_fetch(include_elo=False)
    
    def refresh_data_and_elo(self):
        """Refresh data and update ELO ratings asynchronously"""
        self._start_data_fetch(include_elo=True)
    
    def _start_data_fetch(self, include_elo=False):
        """Internal method to start data fetching with optional ELO update"""
        # Don't start new fetch if one is already running
        if self.data_fetcher and self.data_fetcher.isRunning():
            return
            
        # Disable the refresh button to prevent multiple simultaneous fetches
        self.refresh_btn.setEnabled(False)
        if include_elo:
            self.refresh_btn.setText("Refreshing & Updating ELO...")
        else:
            self.refresh_btn.setText("Fetching...")
        
        # Create and configure the async data fetcher
        self.data_fetcher = AsyncDataFetcher(include_elo_update=include_elo)
        self.data_fetcher.finished.connect(self.on_data_fetch_finished)
        self.data_fetcher.error.connect(self.on_data_fetch_error)
        self.data_fetcher.status_update.connect(self.on_data_fetch_status)
        
        # Start the async fetch
        self.data_fetcher.start()
    
    def on_data_fetch_finished(self):
        """Called when async data fetch completes successfully"""
        try:
            # Load the updated data
            self.load_data()
            self.status_label.setText(f"Data refreshed: {datetime.now().strftime('%H:%M:%S')}")
            
            # Update ELO status after data refresh
            self.update_elo_status()
            
        except Exception as e:
            self.status_label.setText(f"Error loading data: {str(e)}")
            print(f"Error: {str(e)}")
        finally:
            # Re-enable the refresh button
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText("Refresh Data & Update ELO")
    
    def on_data_fetch_error(self, error_msg: str):
        """Called when async data fetch encounters an error"""
        self.status_label.setText(f"Error refreshing data: {error_msg}")
        print(f"Data fetch error: {error_msg}")
        
        # Re-enable the refresh button
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Refresh Data & Update ELO")
    
    def on_data_fetch_status(self, status_msg: str):
        """Called when async data fetch provides status updates"""
        self.status_label.setText(status_msg)

    def load_data(self):
        """Load data from JSON files"""
        try:
            # Path to save directory
            save_dir = Path.cwd() / "TTT_savedata"
            
            # Check if directory exists
            if not save_dir.exists():
                self.status_label.setText("No data found. Click 'Refresh Data' to fetch.")
                return
                
            # Load all-upcoming-events.json
            all_events_path = save_dir / "all-upcoming-events.json"
            if all_events_path.exists():
                with open(all_events_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                    
                # Update status and UI
                self.status_label.setText(f"Data loaded: {datetime.now().strftime('%H:%M:%S')}")
                self.populate_matches_table()
            else:
                # Try to load individual league files
                self.data = {}
                for league_name in self.leagues.values():
                    league_file = save_dir / f"{league_name.replace(' ', '-')}.json"
                    if league_file.exists():
                        with open(league_file, 'r', encoding='utf-8') as f:
                            league_data = json.load(f)
                            self.data[league_name] = league_data
                
                if self.data:
                    self.status_label.setText(f"Data loaded: {datetime.now().strftime('%H:%M:%S')}")
                    self.populate_matches_table()
                else:
                    self.status_label.setText("No data found. Click 'Refresh Data' to fetch.")
        except Exception as e:
            self.status_label.setText(f"Error loading data: {str(e)}")
            print(f"Error: {str(e)}")

    def populate_matches_table(self):
        """Populate matches table with filtered data"""
        # Clear table
        self.matches_table.setRowCount(0)
        
        total_matches = 0
        row_index = 0
        
        # Determine time filter
        time_filter_hours = 6  # Default
        time_filter_text = self.time_combo.currentText()
        if "1 hour" in time_filter_text:
            time_filter_hours = 1
        elif "3 hours" in time_filter_text:
            time_filter_hours = 3
        
        current_time = int(datetime.now().timestamp())
        max_time = current_time + (time_filter_hours * 3600)
        
        # Get active leagues
        active_leagues = [league_id for league_id, checkbox in self.league_checkboxes.items() 
                         if checkbox.isChecked()]
        
        # Get search text
        search_text = self.search_input.text().lower()
        
        # Process each league
        for league_name, league_data in self.data.items():
            if not isinstance(league_data, dict) or 'results' not in league_data:
                continue
                
            # Filter by league
            league_id = None
            for lid, lname in self.leagues.items():
                if lname == league_name:
                    league_id = lid
                    break
                    
            if league_id not in active_leagues:
                continue
                
            # Process each match
            for match in league_data.get('results', []):
                # Get match time
                match_time = int(match.get('time', 0))
                
                # Apply time filter
                if not (current_time <= match_time <= max_time) and "All matches" not in time_filter_text:
                    continue
                    
                # Get player names
                home_name = match.get('home', {}).get('name', '')
                away_name = match.get('away', {}).get('name', '')
                
                # Apply search filter
                if search_text and search_text not in home_name.lower() and search_text not in away_name.lower():
                    continue
                
                # Add to table
                self.matches_table.insertRow(row_index)
                
                # Format time
                match_datetime = datetime.fromtimestamp(match_time)
                time_str = match_datetime.strftime("%H:%M")
                
                # Add basic info
                time_item = QTableWidgetItem(time_str)
                league_item = QTableWidgetItem(league_name)
                home_item = QTableWidgetItem(home_name)
                away_item = QTableWidgetItem(away_name)
                
                # Add odds if available
                odds_str = "-"
                for market_data in league_data.get('markets', []):
                    if market_data.get('event_id') == match.get('id'):
                        market_odds = market_data.get('markets', {}).get('odds', {}).get('92_1', [])
                        if market_odds and len(market_odds) > 0:
                            home_odd = market_odds[0].get('home_od', '-')
                            away_odd = market_odds[0].get('away_od', '-')
                            
                            # Convert to American format for display in table
                            if not self.odds_format_checkbox.isChecked():  # American format is default
                                home_american = self.decimal_to_american(home_odd)
                                away_american = self.decimal_to_american(away_odd)
                                odds_str = f"{home_american} / {away_american}"
                            else:
                                odds_str = f"{home_odd} / {away_odd}"
                            break
                
                odds_item = QTableWidgetItem(odds_str)
                
                # Set items
                self.matches_table.setItem(row_index, 0, time_item)
                self.matches_table.setItem(row_index, 1, league_item)
                self.matches_table.setItem(row_index, 2, home_item)
                self.matches_table.setItem(row_index, 3, away_item)
                self.matches_table.setItem(row_index, 4, odds_item)
                
                # Store event ID and league in hidden data
                time_item.setData(Qt.ItemDataRole.UserRole, match.get('id'))
                time_item.setData(Qt.ItemDataRole.UserRole + 1, league_name)
                
                # Color code based on ELO vs Bookmaker deviation
                try:
                    # Only apply colors if we have valid odds data
                    if 'home_odd' in locals() and 'away_odd' in locals() and home_odd != '-' and away_odd != '-':
                        # Calculate ELO vs bookmaker deviation
                        home_deviation, away_deviation = self.calculate_elo_vs_bookmaker_deviation(
                            home_name, away_name, home_odd, away_odd, league_id
                        )
                        
                        # Get colors based on deviation
                        home_color, away_color = self.get_value_bet_colors(home_deviation, away_deviation)
                        
                        # Apply colors if available
                        if home_color is not None:
                            home_item.setBackground(home_color)
                        if away_color is not None:
                            away_item.setBackground(away_color)
                except Exception as e:
                    # Silently continue if color calculation fails
                    pass
                
                row_index += 1
                total_matches += 1
        
        # Update header with match count
        self.matches_header.setText(f"Upcoming Matches ({total_matches})")
        
        # Auto-resize rows for better readability
        self.matches_table.resizeRowsToContents()
    
    def filter_matches(self):
        """Apply filters and repopulate the table"""
        self.populate_matches_table()
        # Clear ELO chart since selection may have changed
        self.elo_chart.clear_chart()

    def show_match_details(self):
        """Display details for the selected match"""
        selected_items = self.matches_table.selectedItems()
        if not selected_items:
            return
            
        # Get event ID and league from the first cell
        row = selected_items[0].row()
        time_item = self.matches_table.item(row, 0)
        
        event_id = time_item.data(Qt.ItemDataRole.UserRole)
        league_name = time_item.data(Qt.ItemDataRole.UserRole + 1)
        
        if not event_id or not league_name:
            return
            
        # Get match data
        league_data = self.data.get(league_name, {})
        
        # Find match in results
        match_data = None
        for match in league_data.get('results', []):
            if match.get('id') == event_id:
                match_data = match
                break
                
        if not match_data:
            return
            
        # Update match title
        home_name = match_data.get('home', {}).get('name', '')
        away_name = match_data.get('away', {}).get('name', '')
        self.match_title.setText(f"{home_name} vs {away_name}")
        
        # Update summary info
        self.summary_labels["league_val"].setText(league_name)
        self.summary_labels["time_val"].setText(match_data.get('formatted_time', '-'))
        self.summary_labels["home_val"].setText(home_name)
        self.summary_labels["away_val"].setText(away_name)
        
        # Find market data
        market_data = None
        for market in league_data.get('markets', []):
            if market.get('event_id') == event_id:
                market_data = market.get('markets', {})
                break
                
        # Update odds if available
        if market_data:
            # Reset raw odds
            for key in self.raw_odds.keys():
                self.raw_odds[key] = "-"
                
            # Moneyline
            moneyline_odds = market_data.get('odds', {}).get('92_1', [])
            if moneyline_odds and len(moneyline_odds) > 0:
                # Store raw decimal odds
                home_decimal = moneyline_odds[0].get('home_od', '-')
                away_decimal = moneyline_odds[0].get('away_od', '-')
                
                self.raw_odds["moneyline_home"] = home_decimal
                self.raw_odds["moneyline_away"] = away_decimal
                
                # Calculate NoVig odds for moneyline
                try:
                    home_odd = float(home_decimal)
                    away_odd = float(away_decimal)
                    
                    novig_home, novig_away = self.calculate_novig_odds(home_decimal, away_decimal)
                    self.novig_odds["moneyline_home"] = novig_home
                    self.novig_odds["moneyline_away"] = novig_away
                    
                    # Set display based on current format - combine original and NoVig
                    if self.odds_format_checkbox.isChecked():  # Decimal format
                        home_display = f"{home_decimal} ({novig_home})"
                        away_display = f"{away_decimal} ({novig_away})"
                    else:  # American format
                        home_orig = self.decimal_to_american(home_decimal)
                        away_orig = self.decimal_to_american(away_decimal)
                        home_novig = self.decimal_to_american(novig_home)
                        away_novig = self.decimal_to_american(novig_away)
                        home_display = f"{home_orig} ({home_novig})"
                        away_display = f"{away_orig} ({away_novig})"
                    
                    self.odds_labels["moneyline_home"].setText(home_display)
                    self.odds_labels["moneyline_away"].setText(away_display)
                    
                    # Set color based on odds value
                    home_color = self.get_odds_color(home_odd)
                    away_color = self.get_odds_color(away_odd)
                    
                    self.odds_labels["moneyline_home"].setStyleSheet(f"color: {home_color};")
                    self.odds_labels["moneyline_away"].setStyleSheet(f"color: {away_color};")
                except:
                    pass
            
            # Spread
            spread_odds = market_data.get('odds', {}).get('92_2', [])
            if spread_odds and len(spread_odds) > 0:
                # Store raw decimal odds
                home_decimal = spread_odds[0].get('home_od', '-')
                away_decimal = spread_odds[0].get('away_od', '-')
                
                self.raw_odds["spread_home"] = home_decimal
                self.raw_odds["spread_away"] = away_decimal
                
                self.odds_labels["spread_handicap"].setText(spread_odds[0].get('handicap', '-'))
                
                # Calculate NoVig odds for spread and combine display
                try:
                    novig_home, novig_away = self.calculate_novig_odds(home_decimal, away_decimal)
                    self.novig_odds["spread_home"] = novig_home
                    self.novig_odds["spread_away"] = novig_away
                    
                    # Set display based on current format - combine original and NoVig
                    if self.odds_format_checkbox.isChecked():  # Decimal format
                        home_display = f"{home_decimal} ({novig_home})"
                        away_display = f"{away_decimal} ({novig_away})"
                    else:  # American format
                        home_orig = self.decimal_to_american(home_decimal)
                        away_orig = self.decimal_to_american(away_decimal)
                        home_novig = self.decimal_to_american(novig_home)
                        away_novig = self.decimal_to_american(novig_away)
                        home_display = f"{home_orig} ({home_novig})"
                        away_display = f"{away_orig} ({away_novig})"
                    
                    self.odds_labels["spread_home"].setText(home_display)
                    self.odds_labels["spread_away"].setText(away_display)
                except:
                    # Fallback to just original odds
                    if self.odds_format_checkbox.isChecked():
                        self.odds_labels["spread_home"].setText(home_decimal)
                        self.odds_labels["spread_away"].setText(away_decimal)
                    else:
                        self.odds_labels["spread_home"].setText(self.decimal_to_american(home_decimal))
                        self.odds_labels["spread_away"].setText(self.decimal_to_american(away_decimal))
            
            # Totals
            total_odds = market_data.get('odds', {}).get('92_3', [])
            if total_odds and len(total_odds) > 0:
                # Store raw decimal odds
                over_decimal = total_odds[0].get('over_od', '-')
                under_decimal = total_odds[0].get('under_od', '-')
                
                self.raw_odds["total_over"] = over_decimal
                self.raw_odds["total_under"] = under_decimal
                
                self.odds_labels["total_points"].setText(total_odds[0].get('handicap', '-'))
                
                # Calculate NoVig odds for totals and combine display
                try:
                    novig_over, novig_under = self.calculate_novig_odds(over_decimal, under_decimal)
                    self.novig_odds["total_over"] = novig_over
                    self.novig_odds["total_under"] = novig_under
                    
                    # Set display based on current format - combine original and NoVig
                    if self.odds_format_checkbox.isChecked():  # Decimal format
                        over_display = f"{over_decimal} ({novig_over})"
                        under_display = f"{under_decimal} ({novig_under})"
                    else:  # American format
                        over_orig = self.decimal_to_american(over_decimal)
                        under_orig = self.decimal_to_american(under_decimal)
                        over_novig = self.decimal_to_american(novig_over)
                        under_novig = self.decimal_to_american(novig_under)
                        over_display = f"{over_orig} ({over_novig})"
                        under_display = f"{under_orig} ({under_novig})"
                    
                    self.odds_labels["total_over"].setText(over_display)
                    self.odds_labels["total_under"].setText(under_display)
                except:
                    # Fallback to just original odds
                    if self.odds_format_checkbox.isChecked():
                        self.odds_labels["total_over"].setText(over_decimal)
                        self.odds_labels["total_under"].setText(under_decimal)
                    else:
                        self.odds_labels["total_over"].setText(self.decimal_to_american(over_decimal))
                        self.odds_labels["total_under"].setText(self.decimal_to_american(under_decimal))
        else:
            # Clear odds
            for label in self.odds_labels.values():
                label.setText("-")
                label.setStyleSheet("")
                
            # Clear raw odds
            for key in self.raw_odds.keys():
                self.raw_odds[key] = "-"
                
            # Clear NoVig odds
            for key in self.novig_odds.keys():
                self.novig_odds[key] = "-"
                
        # Get league ID for ELO lookup
        league_id = None
        for lid, lname in self.leagues.items():
            if lname == league_name:
                league_id = lid
                break
        
        # Update ELO analysis panel
        if league_id:
            try:
                home_elo = self.get_player_elo_from_history(home_name, league_id)
                away_elo = self.get_player_elo_from_history(away_name, league_id)
                
                # Update player names in analysis panel
                self.home_player_name.setText(home_name)
                self.away_player_name.setText(away_name)
                
                # Display current ELO scores
                self.home_elo_display.setText(f"ELO: {home_elo}")
                self.away_elo_display.setText(f"ELO: {away_elo}")
                
                # Calculate ELO-based win probabilities
                home_win_prob, away_win_prob = self.calculate_elo_win_probability(home_elo, away_elo)
                
                # Display ELO win probabilities
                self.home_elo_win_prob.setText(f"ELO: {home_win_prob}%")
                self.away_elo_win_prob.setText(f"ELO: {away_win_prob}%")
                
            except Exception as e:
                print(f"Error updating ELO information: {e}")
                # Clear ELO labels on error
                self.home_player_name.setText("Home Player")
                self.away_player_name.setText("Away Player")
                self.home_elo_display.setText("ELO: -")
                self.away_elo_display.setText("ELO: -")
                self.home_elo_win_prob.setText("ELO: -%")
                self.away_elo_win_prob.setText("ELO: -%")
                self.home_odds_win_prob.setText("Odds: -%")
                self.away_odds_win_prob.setText("Odds: -%")
        else:
            # Clear ELO labels if no league ID found
            self.home_player_name.setText("Home Player")
            self.away_player_name.setText("Away Player")
            self.home_elo_display.setText("ELO: -")
            self.away_elo_display.setText("ELO: -")
            self.home_elo_win_prob.setText("ELO: -%")
            self.away_elo_win_prob.setText("ELO: -%")
            self.home_odds_win_prob.setText("Odds: -%")
            self.away_odds_win_prob.setText("Odds: -%")
        
        # Calculate and display odds implied probabilities (from moneyline)
        home_odds_prob = "-"
        away_odds_prob = "-"
        if market_data:
            moneyline_odds = market_data.get('odds', {}).get('92_1', [])
            if moneyline_odds and len(moneyline_odds) > 0:
                home_decimal = moneyline_odds[0].get('home_od', '-')
                away_decimal = moneyline_odds[0].get('away_od', '-')
                if home_decimal != '-' and away_decimal != '-':
                    home_odds_prob = self.calculate_implied_probability(home_decimal)
                    away_odds_prob = self.calculate_implied_probability(away_decimal)
        
        self.home_odds_win_prob.setText(f"Odds: {home_odds_prob}%")
        self.away_odds_win_prob.setText(f"Odds: {away_odds_prob}%")
                
        # Find H2H data
        h2h_data = None
        for history in league_data.get('history', []):
            if history.get('event_id') == event_id:
                h2h_data = history.get('history', {})
                break
                
        # Update H2H table
        self.update_h2h_data(h2h_data, home_name, away_name)
        
        # Update ELO chart
        self.update_elo_chart()
        
        # Map league name to ID for player info panel
        league_id = None
        for lid, lname in self.leagues.items():
            if lname == league_name:
                league_id = int(lid)
                break
        
        # Update player info panel
        if league_id:
            self.update_player_info_panel(home_name, away_name, league_id)
        
    def update_h2h_data(self, h2h_data, home_name, away_name):
        """Update head-to-head data display"""
        # Clear table
        self.h2h_table.setRowCount(0)
        
        if not h2h_data or not h2h_data.get('h2h'):
            self.h2h_summary.setText("No head-to-head data available")
            
            # Reset win probability
            self.home_prob.setText("Home: 50%")
            self.away_prob.setText("Away: 50%")
            
            return
            
        # Count wins
        home_wins = 0
        away_wins = 0
        total_matches = 0
        
        # Populate H2H table
        h2h_matches = h2h_data.get('h2h', [])
        
        for i, match in enumerate(h2h_matches):
            self.h2h_table.insertRow(i)
            
            # Get match details
            match_time = match.get('time', 0)
            match_date = datetime.fromtimestamp(int(match_time)).strftime("%Y-%m-%d")
            
            h2h_home = match.get('home', {}).get('name', '')
            h2h_away = match.get('away', {}).get('name', '')
            score = match.get('ss', '-')
            
            # Add to table
            date_item = QTableWidgetItem(match_date)
            home_item = QTableWidgetItem(h2h_home)
            away_item = QTableWidgetItem(h2h_away)
            score_item = QTableWidgetItem(score)
            
            self.h2h_table.setItem(i, 0, date_item)
            self.h2h_table.setItem(i, 1, home_item)
            self.h2h_table.setItem(i, 2, away_item)
            self.h2h_table.setItem(i, 3, score_item)
            
            # Add "View Sets" button if detailed scores are available
            has_detailed_scores = 'detailed_scores' in match and match['detailed_scores']
            
            # Create button cell
            btn_cell = QTableWidgetItem("View Sets" if has_detailed_scores else "N/A")
            if has_detailed_scores:
                btn_cell.setForeground(QColor("#3498db"))
                btn_cell.setFlags(btn_cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # Store match data for retrieval when clicked
                btn_cell.setData(Qt.ItemDataRole.UserRole, match)
            else:
                btn_cell.setForeground(QColor("#7f8c8d"))  # Gray text for N/A
            
            self.h2h_table.setItem(i, 4, btn_cell)
            
            # Count wins for probability
            total_matches += 1
            
            if score and '-' in score:
                home_score, away_score = score.split('-')
                try:
                    # Check who won
                    if int(home_score) > int(away_score):
                        # Home team won
                        if h2h_home == home_name:
                            home_wins += 1
                        else:
                            away_wins += 1
                    else:
                        # Away team won
                        if h2h_away == home_name:
                            home_wins += 1
                        else:
                            away_wins += 1
                except:
                    pass
        
        # Connect table click for set scores (disconnect first to avoid multiple connections)
        try:
            self.h2h_table.cellClicked.disconnect(self.handle_h2h_cell_click)
        except TypeError:
            # No connection exists yet, which is fine
            pass
        self.h2h_table.cellClicked.connect(self.handle_h2h_cell_click)
        
        # Update H2H summary
        self.h2h_summary.setText(f"Head-to-Head: {len(h2h_matches)} previous matches")
        
        # Update win probability
        if total_matches > 0:
            home_pct = int((home_wins / total_matches) * 100)
            away_pct = int((away_wins / total_matches) * 100)
            
            # Account for no wins case
            if home_pct == 0 and away_pct == 0:
                home_pct = 50
                away_pct = 50
            
            self.home_prob.setText(f"Home: {home_pct}%")
            self.away_prob.setText(f"Away: {away_pct}%")
            
            # Set color based on probability
            if home_pct > 60:
                self.home_prob.setStyleSheet("color: #2ecc71; font-weight: bold;")
            elif home_pct < 40:
                self.home_prob.setStyleSheet("color: #e74c3c; font-weight: bold;")
            else:
                self.home_prob.setStyleSheet("color: #f39c12; font-weight: bold;")
                
            if away_pct > 60:
                self.away_prob.setStyleSheet("color: #2ecc71; font-weight: bold;")
            elif away_pct < 40:
                self.away_prob.setStyleSheet("color: #e74c3c; font-weight: bold;")
            else:
                self.away_prob.setStyleSheet("color: #f39c12; font-weight: bold;")
                
    def handle_h2h_cell_click(self, row, column):
        """Handle clicks on the H2H table to show set scores"""
        if column == 4:  # "View Sets" column
            item = self.h2h_table.item(row, column)
            if item and item.text() == "View Sets":
                # Get match data stored in the item
                match_data = item.data(Qt.ItemDataRole.UserRole)
                if match_data:
                    # Show set score dialog
                    dialog = SetScoreDialog(self, match_data)
                    dialog.exec()
    
    def get_odds_color(self, odds_value):
        """Return a color based on odds value"""
        if odds_value <= 1.5:
            return "#2ecc71"  # Green - strong favorite
        elif odds_value <= 2.0:
            return "#f39c12"  # Orange - slight favorite
        elif odds_value <= 3.0:
            return "#e67e22"  # Dark orange - slight underdog
        else:
            return "#e74c3c"  # Red - strong underdog

    def update_elo_status(self):
        """Update the ELO status display"""
        try:
            db = TTDatabase()
            elo_calculator = ELOCalculator(db)
            
            status = elo_calculator.get_elo_processing_status()
            
            if status:
                processed = status.get('processed_matches', 0)
                total = status.get('total_matches', 0)
                unprocessed = status.get('unprocessed_matches', 0)
                percentage = status.get('processed_percentage', 0)
                
                if unprocessed > 0:
                    self.elo_status_label.setText(f"ELO: {processed}/{total} ({percentage:.1f}%) - {unprocessed} pending")
                    self.elo_status_label.setStyleSheet("color: #f39c12; font-size: 10px;")  # Orange for pending
                else:
                    self.elo_status_label.setText(f"ELO: {processed}/{total} (100%) - Up to date")
                    self.elo_status_label.setStyleSheet("color: #2ecc71; font-size: 10px;")  # Green for complete
            else:
                self.elo_status_label.setText("ELO: Unknown")
                self.elo_status_label.setStyleSheet("color: #e74c3c; font-size: 10px;")  # Red for error
                
            db.close()
        except Exception as e:
            print(f"Error updating ELO status: {e}")
            self.elo_status_label.setText("ELO: Error")
            self.elo_status_label.setStyleSheet("color: #e74c3c; font-size: 10px;")

    # Deprecated: ELO updates are now handled by the combined refresh_data_and_elo method
    # def update_elo_ratings(self):
    #     """Update ELO ratings for unprocessed matches - DEPRECATED"""
    #     pass

    def get_player_elo_progression(self, player_name: str, league_id: int, limit: int = 25) -> list:
        """Get ELO progression data for a player"""
        try:
            db = TTDatabase()
            
            # Build query based on whether limit is set (All Time vs limited)
            if limit == -1:  # All Time
                query = '''
                SELECT 
                    eh.new_elo,
                    eh.old_elo,
                    eh.new_elo - eh.old_elo as elo_change,
                    m.match_time,
                    ROW_NUMBER() OVER (ORDER BY m.match_time DESC) as match_number
                FROM elo_history eh
                JOIN players p ON eh.player_id = p.id AND eh.league_id = p.league_id
                JOIN matches m ON eh.match_id = m.id
                WHERE p.name = ? AND eh.league_id = ?
                ORDER BY m.match_time DESC
                '''
                db.cursor.execute(query, (player_name, league_id))
            else:
                query = '''
                SELECT 
                    eh.new_elo,
                    eh.old_elo,
                    eh.new_elo - eh.old_elo as elo_change,
                    m.match_time,
                    ROW_NUMBER() OVER (ORDER BY m.match_time DESC) as match_number
                FROM elo_history eh
                JOIN players p ON eh.player_id = p.id AND eh.league_id = p.league_id
                JOIN matches m ON eh.match_id = m.id
                WHERE p.name = ? AND eh.league_id = ?
                ORDER BY m.match_time DESC
                LIMIT ?
                '''
                db.cursor.execute(query, (player_name, league_id, limit))
            
            # Convert results and handle timestamp conversion
            results = []
            from datetime import datetime
            for row in db.cursor.fetchall():
                row_dict = dict(row)
                # Convert match_time string to timestamp
                try:
                    dt = datetime.strptime(row_dict['match_time'], '%Y-%m-%d %H:%M:%S')
                    row_dict['match_time'] = dt.timestamp()
                except:
                    row_dict['match_time'] = 0
                results.append(row_dict)
            db.close()
            
            # Reverse to get chronological order (oldest to newest)
            return list(reversed(results))
            
        except Exception as e:
            print(f"Error fetching ELO progression for {player_name}: {e}")
            return []

    def get_h2h_matches_in_timeframe(self, player1_name: str, player2_name: str, league_id: int, elo_data: list) -> list:
        """Get head-to-head matches between two players within the ELO data timeframe"""
        try:
            if not elo_data:
                return []
                
            db = TTDatabase()
            
            # Get the time range from the ELO data (convert to timestamps)
            from datetime import datetime
            earliest_time = min(match.get('match_time', 0) for match in elo_data)
            latest_time = max(match.get('match_time', 0) for match in elo_data)
            
            # Convert numeric timestamps to datetime strings for database query
            earliest_dt = datetime.fromtimestamp(earliest_time).strftime('%Y-%m-%d %H:%M:%S')
            latest_dt = datetime.fromtimestamp(latest_time).strftime('%Y-%m-%d %H:%M:%S')
            
            # Query for matches between these two players in this timeframe
            query = '''
            SELECT 
                m.match_time,
                m.home_player_name,
                m.away_player_name,
                m.home_score,
                m.away_score,
                m.id as match_id
            FROM matches m
            WHERE m.league_id = ?
            AND m.match_time BETWEEN ? AND ?
            AND (
                (m.home_player_name = ? AND m.away_player_name = ?) OR
                (m.home_player_name = ? AND m.away_player_name = ?)
            )
            ORDER BY m.match_time ASC
            '''
            
            db.cursor.execute(query, (
                league_id, earliest_dt, latest_dt,
                player1_name, player2_name,
                player2_name, player1_name
            ))
            
            results = []
            for row in db.cursor.fetchall():
                row_dict = dict(row)
                # Convert the datetime string back to timestamp for comparison
                try:
                    dt = datetime.strptime(row_dict['match_time'], '%Y-%m-%d %H:%M:%S')
                    row_dict['match_time_timestamp'] = dt.timestamp()
                except:
                    row_dict['match_time_timestamp'] = 0
                results.append(row_dict)
                
            db.close()
            
            return results
            
        except Exception as e:
            print(f"Error fetching H2H matches: {e}")
            return []

    def update_elo_chart(self):
        """Update the ELO chart based on current match selection"""
        # Get currently selected match
        selected_items = self.matches_table.selectedItems()
        if not selected_items:
            self.elo_chart.clear_chart()
            return
            
        # Get match details
        row = selected_items[0].row()
        home_name = self.matches_table.item(row, 2).text()  # Home player
        away_name = self.matches_table.item(row, 3).text()  # Away player
        league_name = self.matches_table.item(row, 1).text()  # League
        
        # Map league name to ID
        league_id = None
        for lid, lname in self.leagues.items():
            if lname == league_name:
                league_id = int(lid)
                break
        
        if not league_id:
            self.elo_chart.clear_chart()
            return
        
        # Get time window
        time_window_text = self.elo_time_window.currentText()
        if "10" in time_window_text:
            limit = 10
        elif "25" in time_window_text:
            limit = 25
        elif "50" in time_window_text:
            limit = 50
        else:  # "All Time"
            limit = -1
        
        # Fetch ELO data for both players
        home_elo_data = self.get_player_elo_progression(home_name, league_id, limit)
        away_elo_data = self.get_player_elo_progression(away_name, league_id, limit)
        
        # Get h2h matches within the timeframe (use the longer of the two datasets)
        combined_elo_data = home_elo_data + away_elo_data
        h2h_matches = self.get_h2h_matches_in_timeframe(home_name, away_name, league_id, combined_elo_data)
        
        # Update the chart
        self.elo_chart.plot_elo_progression(home_elo_data, away_elo_data, 
                                          home_name, away_name, limit, h2h_matches)
    
    def update_player_info_panel(self, home_name, away_name, league_id):
        """Update the player info panel with streak data"""
        try:
            # Get player IDs
            db = TTDatabase()
            
            # Get home player ID
            db.cursor.execute("SELECT id FROM players WHERE name = ? AND league_id = ?", (home_name, league_id))
            home_result = db.cursor.fetchone()
            home_player_id = home_result['id'] if home_result else None
            
            # Get away player ID  
            db.cursor.execute("SELECT id FROM players WHERE name = ? AND league_id = ?", (away_name, league_id))
            away_result = db.cursor.fetchone()
            away_player_id = away_result['id'] if away_result else None
            
            db.close()
            
            # Update home player info
            self.home_player_label.setText(f"{home_name}")
            if home_player_id:
                home_streak_data = get_player_streak_data(home_player_id, league_id)
                home_color = get_streak_color(home_streak_data)
                
                # Format streak text
                if home_streak_data['current_streak'] > 0:
                    streak_type = "W" if home_streak_data['streak_type'] == 'win' else "L"
                    streak_text = f"{streak_type}{home_streak_data['current_streak']}"
                else:
                    streak_text = "No streak"
                
                # Format form text
                form_5 = home_streak_data['form_5']
                form_text = f"L5: {form_5['wins']}-{form_5['total']-form_5['wins']} ({form_5['win_rate']:.1%})"
                
                self.home_streak_label.setText(streak_text)
                self.home_streak_label.setStyleSheet(f"color: {home_color}; font-weight: bold;")
                self.home_form_label.setText(form_text)
                self.home_form_label.setStyleSheet(f"color: {home_color};")
            else:
                self.home_streak_label.setText("No data")
                self.home_form_label.setText("Form: -")
                self.home_streak_label.setStyleSheet("color: #95a5a6;")
                self.home_form_label.setStyleSheet("color: #95a5a6;")
            
            # Update away player info
            self.away_player_label.setText(f"{away_name}")
            if away_player_id:
                away_streak_data = get_player_streak_data(away_player_id, league_id)
                away_color = get_streak_color(away_streak_data)
                
                # Format streak text
                if away_streak_data['current_streak'] > 0:
                    streak_type = "W" if away_streak_data['streak_type'] == 'win' else "L"
                    streak_text = f"{streak_type}{away_streak_data['current_streak']}"
                else:
                    streak_text = "No streak"
                
                # Format form text
                form_5 = away_streak_data['form_5']
                form_text = f"L5: {form_5['wins']}-{form_5['total']-form_5['wins']} ({form_5['win_rate']:.1%})"
                
                self.away_streak_label.setText(streak_text)
                self.away_streak_label.setStyleSheet(f"color: {away_color}; font-weight: bold;")
                self.away_form_label.setText(form_text)
                self.away_form_label.setStyleSheet(f"color: {away_color};")
            else:
                self.away_streak_label.setText("No data")
                self.away_form_label.setText("Form: -")
                self.away_streak_label.setStyleSheet("color: #95a5a6;")
                self.away_form_label.setStyleSheet("color: #95a5a6;")
                
        except Exception as e:
            print(f"Error updating player info panel: {e}")
            # Reset to default state
            self.home_streak_label.setText("Error")
            self.home_form_label.setText("Form: -")
            self.away_streak_label.setText("Error")
            self.away_form_label.setText("Form: -")
            

def main():
    app = QApplication(sys.argv)
    window = TableTennisGUI()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
