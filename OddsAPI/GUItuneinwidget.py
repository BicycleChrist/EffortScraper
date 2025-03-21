import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import GUItunein
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QScrollBar,
    QComboBox, QLabel, QPushButton, QLineEdit, QFrame, QListWidgetItem
)
import webbrowser


class TuneInWidget(QWidget):
    """PyQt6 implementation of TuneIn widget for displaying streaming links"""
    
    # Signal emitted when links are loaded
    linksLoaded = pyqtSignal(list)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # URLs to be parsed
        self.urls = [
            "https://the.streameast.app/nhlstreams3",
            "https://the.streameast.app/mlbstreams17",
            "https://the.streameast.app/nbastreams64",
            "https://the.streameast.app/nflstreams3",
        ]
        
        # League names for filtering
        self.league_filter_options = [
            "All Leagues",
            "NBA",
            "NFL",
            "MLB", 
            "NHL",
            "College Football",
            "College Basketball",
            "MMA"
        ]
        
        # Map keywords to leagues for filtering
        self.league_keywords = {
            "NBA": ["nba", "basketball"],
            "NFL": ["nfl", "football"],
            "MLB": ["mlb", "baseball"],
            "NHL": ["nhl", "hockey"],
            "College Football": ["ncaaf", "college football"],
            "College Basketball": ["ncaab", "college basketball"],
            "MMA": ["ufc", "mma", "fight"]
        }
        
        self.all_links = []
        self.filtered_links = []
        
        self.init_ui()
        
    def init_ui(self):
        """Initialize the user interface for the widget"""
        
        # Use a style that matches the main application
        self.setStyleSheet("""
            QListWidget {
                background-color: #1b1010;
                color: white;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 2px;
                font-size: 9pt;
            }
            QComboBox {
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 2px 4px;
                max-width: 85px;
                font-size: 9pt;
            }
            QPushButton {
                background-color: #007bff;
                color: white;
                border: 1px solid #0056b3;
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 9pt;
                max-width: 60px;
            }
            QPushButton:disabled {
                background-color: #e9ecef;
                color: #6c757d;
                border-color: #dee2e6;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 9pt;
            }
            QLineEdit {
                font-size: 9pt;
                padding: 2px 4px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                max-width: 150px;
            }
        """)
        
        # Super compact layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.setSpacing(2)
        
        # Compact controls row
        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(2)
        
        # League filter dropdown
        self.league_filter = QComboBox()
        self.league_filter.addItems(self.league_filter_options)
        self.league_filter.currentTextChanged.connect(self.filter_links)
        self.league_filter.setMaximumWidth(85)
        controls_layout.addWidget(self.league_filter)
        

        
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter...")
        self.search_box.textChanged.connect(self.filter_links)
        self.search_box.setMaximumWidth(130)
        controls_layout.addWidget(self.search_box)
        
        # Refresh button (right side)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.load_links)
        self.refresh_button.setFixedWidth(60)
        controls_layout.addWidget(self.refresh_button)
        
        # Links list with proper styling and fixed size
        self.links_list = QListWidget()
        self.links_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.links_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.links_list.itemDoubleClicked.connect(lambda item: webbrowser.open(item.text()))
        self.links_list.setFixedHeight(110)
        
        # Build the layout
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.links_list)
        
        # Fixed overall size
        self.setFixedHeight(140)
        
        # Load links
        self.load_links()
        
    @pyqtSlot()
    def load_links(self):
        """Load all links from the URLs using ThreadPoolExecutor"""
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Loading...")
        self.links_list.clear()
        self.all_links.clear()
        
        # Use a thread executor to avoid blocking the UI
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(GUItunein.get_href_links, url) for url in self.urls]
            for future in concurrent.futures.as_completed(futures):
                try:
                    href_links = future.result()
                    self.all_links.extend(href_links)
                except Exception as e:
                    print(f"Error fetching links: {e}")
        
        # Filter links based on current selection
        self.filter_links()
        
        # Re-enable refresh button
        self.refresh_button.setText("Refresh")
        self.refresh_button.setEnabled(True)
        
        # Emit the loaded links signal
        self.linksLoaded.emit(self.all_links)
        
    @pyqtSlot()
    def filter_links(self):
        """Filter links based on selected league and search text"""
        selected_league = self.league_filter.currentText()
        search_text = self.search_box.text().lower()
        
        if selected_league == "All Leagues":
            base_links = self.all_links
        else:
            keywords = self.league_keywords.get(selected_league, [])
            base_links = [link for link in self.all_links if (any(keyword in link for keyword in keywords))]
        
        if search_text == "": self.filtered_links = base_links
        else: self.filtered_links = [link for link in base_links if (search_text in link)]
        
        self.links_list.clear()
        self.links_list.addItems(self.filtered_links)
        
