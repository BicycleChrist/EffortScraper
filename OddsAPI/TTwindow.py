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
    """Thread for running async data fetching without blocking the UI"""
    
    # Qt signals for communication with main thread
    finished = pyqtSignal()
    error = pyqtSignal(str)
    status_update = pyqtSignal(str)
    
    def run(self):
        """Run the async data fetching in this thread"""
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
    
    def plot_elo_progression(self, home_data, away_data, home_name, away_name, match_limit):
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
                self.axes.plot(match_numbers, elo_values, 'r-o', label=home_name, 
                              linewidth=2, markersize=3, color='#e74c3c')
            
            # Plot away player ELO progression  
            if away_data:
                match_numbers = list(range(1, len(away_data) + 1))
                elo_values = [point['new_elo'] for point in away_data]
                self.axes.plot(match_numbers, elo_values, 'g-o', label=away_name, 
                              linewidth=2, markersize=3, color='#2ecc71')
            
            # Set labels and title
            self.axes.set_title(f"ELO Progression - Last {match_limit} Matches")
            self.axes.set_xlabel("Match Number (Recent)")
            self.axes.set_ylabel("ELO Rating")
            
            # Add legend
            self.axes.legend(loc='upper left')
            
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
        
        self.fig.tight_layout()
        self.draw()


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
        
        # Refresh button
        self.refresh_btn = QPushButton("Refresh Data")
        self.refresh_btn.clicked.connect(self.refresh_data)
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
        
        # ELO update button
        self.elo_update_btn = QPushButton("Update ELO")
        self.elo_update_btn.clicked.connect(self.update_elo_ratings)
        self.elo_update_btn.setStyleSheet("background-color: #9b59b6; padding: 8px;")
        self.control_layout.addWidget(self.elo_update_btn)
        
        # ELO Chart Section
        elo_chart_group = QGroupBox("Player ELO Progression")
        elo_chart_layout = QVBoxLayout(elo_chart_group)
        
        # Time window selector
        time_window_layout = QHBoxLayout()
        time_window_label = QLabel("Matches:")
        time_window_label.setFont(QFont("Arial", 10))
        self.elo_time_window = QComboBox()
        self.elo_time_window.addItems(["Last 10", "Last 25", "Last 50"])
        self.elo_time_window.setCurrentIndex(1)  # Default to 25
        self.elo_time_window.currentIndexChanged.connect(self.update_elo_chart)
        time_window_layout.addWidget(time_window_label)
        time_window_layout.addWidget(self.elo_time_window)
        time_window_layout.addStretch()
        
        elo_chart_layout.addLayout(time_window_layout)
        
        # ELO Chart Canvas (larger size to fill more space)
        self.elo_chart = ELOProgressionCanvas(self, width=4, height=5, dpi=80)
        elo_chart_layout.addWidget(self.elo_chart)
        
        # Give the chart group more weight in the layout
        self.control_layout.addWidget(elo_chart_group, 3)  # Weight of 3 to expand
        
        # Much smaller stretcher - let the chart take up more space
        self.control_layout.addStretch(1)
        
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
        value_font = QFont("Arial", 11)
        
        market_label = QLabel("Market")
        market_label.setFont(header_font)
        home_label = QLabel("Home")
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
        self.prob_group = QGroupBox("Win Probability")
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

  

    def toggle_odds_format(self):
        """Toggle between American and Decimal odds formats"""
        show_decimal = self.odds_format_checkbox.isChecked()
        
        if show_decimal:
            self.odds_format_checkbox.setText("American Odds")
            # Show raw decimal odds
            for key, value in self.raw_odds.items():
                if value != "-":
                    self.odds_labels[key].setText(value)
        else:
            self.odds_format_checkbox.setText("Decimal Odds")
            # Convert to American odds
            for key, value in self.raw_odds.items():
                if value != "-":
                    self.odds_labels[key].setText(self.decimal_to_american(value))
        
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
        """Refresh data by running the client script asynchronously"""
        # Don't start new fetch if one is already running
        if self.data_fetcher and self.data_fetcher.isRunning():
            return
            
        # Disable the refresh button to prevent multiple simultaneous fetches
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Fetching...")
        
        # Create and configure the async data fetcher
        self.data_fetcher = AsyncDataFetcher()
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
            self.refresh_btn.setText("Refresh Data")
    
    def on_data_fetch_error(self, error_msg: str):
        """Called when async data fetch encounters an error"""
        self.status_label.setText(f"Error refreshing data: {error_msg}")
        print(f"Data fetch error: {error_msg}")
        
        # Re-enable the refresh button
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("Refresh Data")
    
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
                
                # Color code based on odds
                try:
                    home_odd_val = float(home_odd) if 'home_odd' in locals() else 0
                    if home_odd_val < 1.5:
                        home_item.setBackground(QColor(100, 200, 100, 80))  # Green for heavy favorite
                    elif home_odd_val < 2.0:
                        home_item.setBackground(QColor(180, 180, 100, 80))  # Yellow for moderate favorite
                        
                    away_odd_val = float(away_odd) if 'away_odd' in locals() else 0
                    if away_odd_val < 1.5:
                        away_item.setBackground(QColor(100, 200, 100, 80))
                    elif away_odd_val < 2.0:
                        away_item.setBackground(QColor(180, 180, 100, 80))
                except:
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
                
                # Set display based on current format
                if self.odds_format_checkbox.isChecked():  # Decimal format
                    self.odds_labels["moneyline_home"].setText(home_decimal)
                    self.odds_labels["moneyline_away"].setText(away_decimal)
                else:  # American format
                    self.odds_labels["moneyline_home"].setText(self.decimal_to_american(home_decimal))
                    self.odds_labels["moneyline_away"].setText(self.decimal_to_american(away_decimal))
                
                # Color code based on value
                try:
                    home_odd = float(home_decimal)
                    away_odd = float(away_decimal)
                    
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
                
                # Set display based on current format
                if self.odds_format_checkbox.isChecked():  # Decimal format
                    self.odds_labels["spread_home"].setText(home_decimal)
                    self.odds_labels["spread_away"].setText(away_decimal)
                else:  # American format
                    self.odds_labels["spread_home"].setText(self.decimal_to_american(home_decimal))
                    self.odds_labels["spread_away"].setText(self.decimal_to_american(away_decimal))
                    
                self.odds_labels["spread_handicap"].setText(spread_odds[0].get('handicap', '-'))
            
            # Totals
            total_odds = market_data.get('odds', {}).get('92_3', [])
            if total_odds and len(total_odds) > 0:
                # Store raw decimal odds
                over_decimal = total_odds[0].get('over_od', '-')
                under_decimal = total_odds[0].get('under_od', '-')
                
                self.raw_odds["total_over"] = over_decimal
                self.raw_odds["total_under"] = under_decimal
                
                # Set display based on current format
                if self.odds_format_checkbox.isChecked():  # Decimal format
                    self.odds_labels["total_over"].setText(over_decimal)
                    self.odds_labels["total_under"].setText(under_decimal)
                else:  # American format
                    self.odds_labels["total_over"].setText(self.decimal_to_american(over_decimal))
                    self.odds_labels["total_under"].setText(self.decimal_to_american(under_decimal))
                    
                self.odds_labels["total_points"].setText(total_odds[0].get('handicap', '-'))
        else:
            # Clear odds
            for label in self.odds_labels.values():
                label.setText("-")
                label.setStyleSheet("")
                
            # Clear raw odds
            for key in self.raw_odds.keys():
                self.raw_odds[key] = "-"
                
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
        
        # Connect table click for set scores
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
                    self.elo_update_btn.setEnabled(True)
                else:
                    self.elo_status_label.setText(f"ELO: {processed}/{total} (100%) - Up to date")
                    self.elo_status_label.setStyleSheet("color: #2ecc71; font-size: 10px;")  # Green for complete
                    self.elo_update_btn.setEnabled(False)
            else:
                self.elo_status_label.setText("ELO: Unknown")
                self.elo_status_label.setStyleSheet("color: #e74c3c; font-size: 10px;")  # Red for error
                
            db.close()
        except Exception as e:
            print(f"Error updating ELO status: {e}")
            self.elo_status_label.setText("ELO: Error")
            self.elo_status_label.setStyleSheet("color: #e74c3c; font-size: 10px;")

    def update_elo_ratings(self):
        """Update ELO ratings for unprocessed matches"""
        try:
            self.elo_status_label.setText("ELO: Processing...")
            self.elo_status_label.setStyleSheet("color: #3498db; font-size: 10px;")
            self.elo_update_btn.setEnabled(False)
            QApplication.processEvents()
            
            db = TTDatabase()
            elo_calculator = ELOCalculator(db)
            
            # Get initial status
            initial_status = elo_calculator.get_elo_processing_status()
            unprocessed_count = initial_status.get('unprocessed_matches', 0)
            
            if unprocessed_count > 0:
                print(f"Processing {unprocessed_count} unprocessed ELO matches...")
                
                # Process unprocessed matches
                matches_processed = elo_calculator.process_unprocessed_matches()
                
                print(f"Successfully processed {matches_processed} matches for ELO calculation.")
                
                # Update status display
                self.update_elo_status()
                
                # Show completion message in status
                if matches_processed > 0:
                    self.status_label.setText(f"ELO updated: {matches_processed} matches processed")
                else:
                    self.status_label.setText("ELO update: No new matches to process")
            else:
                self.status_label.setText("ELO update: All matches already processed")
                self.update_elo_status()
                
            db.close()
            
        except Exception as e:
            print(f"Error updating ELO ratings: {e}")
            self.status_label.setText(f"ELO update error: {str(e)}")
            self.elo_status_label.setText("ELO: Error")
            self.elo_status_label.setStyleSheet("color: #e74c3c; font-size: 10px;")
            self.elo_update_btn.setEnabled(True)

    def get_player_elo_progression(self, player_name: str, league_id: int, limit: int = 25) -> list:
        """Get ELO progression data for a player"""
        try:
            db = TTDatabase()
            
            # Get player ELO history
            db.cursor.execute('''
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
            ''', (player_name, league_id, limit))
            
            results = [dict(row) for row in db.cursor.fetchall()]
            db.close()
            
            # Reverse to get chronological order (oldest to newest)
            return list(reversed(results))
            
        except Exception as e:
            print(f"Error fetching ELO progression for {player_name}: {e}")
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
        else:  # "50"
            limit = 50
        
        # Fetch ELO data for both players
        home_elo_data = self.get_player_elo_progression(home_name, league_id, limit)
        away_elo_data = self.get_player_elo_progression(away_name, league_id, limit)
        
        # Update the chart
        self.elo_chart.plot_elo_progression(home_elo_data, away_elo_data, 
                                          home_name, away_name, limit)
            

def main():
    app = QApplication(sys.argv)
    window = TableTennisGUI()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
