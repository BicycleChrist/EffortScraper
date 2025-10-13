import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import GUItunein
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QScrollBar,
    QComboBox, QLabel, QPushButton, QLineEdit, QFrame, QListWidgetItem
)
import webbrowser


class StreamLoadWorker(QThread):
    """Background worker for loading stream links without blocking UI"""

    # Signals for communication with main thread
    stage1_complete = pyqtSignal(list)  # Emits game URLs
    stage2_progress = pyqtSignal(int, int)  # Emits (current, total)
    stage2_complete = pyqtSignal(dict)  # Emits {game_url: [stream_dicts]}
    error_occurred = pyqtSignal(str)  # Emits error message

    def __init__(self, urls):
        super().__init__()
        self.urls = urls
        self._is_running = True

    def stop(self):
        """Stop the worker gracefully"""
        self._is_running = False

    def run(self):
        """Run the two-stage scraping in background thread"""
        try:
            if not self._is_running:
                return

            # Stage 1: Get all game page URLs
            all_game_urls = []
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(GUItunein.get_href_links, url) for url in self.urls]
                for future in concurrent.futures.as_completed(futures):
                    if not self._is_running:
                        return
                    try:
                        game_urls = future.result()
                        all_game_urls.extend(game_urls)
                    except Exception as e:
                        self.error_occurred.emit(f"Error fetching game links: {e}")

            if not self._is_running:
                return

            # Emit stage 1 completion
            self.stage1_complete.emit(all_game_urls)

            # Stage 2: Get stream links from each game page
            all_streams = {}
            total_games = len(all_game_urls)
            completed = 0

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_game = {executor.submit(GUItunein.get_stream_links, game_url): game_url
                                  for game_url in all_game_urls}
                for future in concurrent.futures.as_completed(future_to_game):
                    if not self._is_running:
                        return

                    game_url = future_to_game[future]
                    try:
                        streams = future.result()
                        if streams:  # Only store if there are streams
                            all_streams[game_url] = streams
                    except Exception as e:
                        self.error_occurred.emit(f"Error fetching streams from {game_url}: {e}")

                    completed += 1
                    self.stage2_progress.emit(completed, total_games)

            if not self._is_running:
                return

            # Emit stage 2 completion
            self.stage2_complete.emit(all_streams)

        except Exception as e:
            self.error_occurred.emit(f"Worker error: {e}")


class TuneInWidget(QWidget):
    """PyQt6 implementation of TuneIn widget for displaying streaming links"""
    
    # Signal emitted when links are loaded
    linksLoaded = pyqtSignal(list)
    
    def __init__(self, parent=None):
        super().__init__(parent)

        # URLs to be parsed (totalsportek main page)
        self.urls = [
            "https://today.totalsportek.army/",
        ]

        # League names for filtering
        self.league_filter_options = [
            "All Leagues",
            "NBA",
            "NFL",
            "MLB",
            "NHL",
            "Soccer",
            "Tennis",
            "Cricket",
            "Boxing/MMA",
            "Rugby"
        ]

        # Map keywords to leagues for filtering (based on game URLs)
        self.league_keywords = {
            "NBA": ["nba", "basketball"],
            "NFL": ["nfl", "chiefs", "cowboys", "patriots", "bills", "eagles"],
            "MLB": ["mlb", "baseball", "yankees", "dodgers", "red-sox", "blue-jays"],
            "NHL": ["nhl", "hockey", "bruins", "maple-leaf", "canucks", "oilers"],
            "Soccer": ["vs-", "fc-", "united", "city", "real-", "barcelona"],
            "Tennis": ["tennis", "open", "masters"],
            "Cricket": ["cricket", "india", "pakistan", "england"],
            "Boxing/MMA": ["boxing", "ufc", "mma", "fight"],
            "Rugby": ["rugby", "harlequins", "saracens"]
        }

        # Store game URLs and their associated streams
        self.all_game_urls = []
        self.all_streams = {}  # {game_url: [stream_dicts]}
        self.filtered_streams = []  # List of (game_name, provider, url) tuples

        # Background worker for loading streams
        self.worker = None
        self.worker_thread = None

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
        self.links_list.itemDoubleClicked.connect(self.open_stream_link)
        self.links_list.setFixedHeight(110)
        
        # Build the layout
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.links_list)
        
        # Fixed overall size
        self.setFixedHeight(140)

        # Start loading links in background (non-blocking)
        self.start_background_load()
        
    def start_background_load(self):
        """Start loading streams in background thread"""
        # Stop any existing worker
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()

        # Clear UI
        self.links_list.clear()
        self.all_game_urls.clear()
        self.all_streams.clear()

        # Update UI state
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Loading...")

        # Create and configure worker
        self.worker = StreamLoadWorker(self.urls)
        self.worker.stage1_complete.connect(self.on_stage1_complete)
        self.worker.stage2_progress.connect(self.on_stage2_progress)
        self.worker.stage2_complete.connect(self.on_stage2_complete)
        self.worker.error_occurred.connect(self.on_worker_error)

        # Start worker
        self.worker.start()

    @pyqtSlot(list)
    def on_stage1_complete(self, game_urls):
        """Handle completion of stage 1 (game URLs loaded)"""
        self.all_game_urls = game_urls
        print(f"Found {len(game_urls)} games. Fetching streams...")

    @pyqtSlot(int, int)
    def on_stage2_progress(self, completed, total):
        """Handle progress updates from stage 2"""
        self.refresh_button.setText(f"{completed}/{total}")

    @pyqtSlot(dict)
    def on_stage2_complete(self, all_streams):
        """Handle completion of stage 2 (all streams loaded)"""
        self.all_streams = all_streams
        print(f"Loaded {len(all_streams)} games with streams")

        # Filter and display links
        self.filter_links()

        # Re-enable refresh button
        self.refresh_button.setText("Refresh")
        self.refresh_button.setEnabled(True)

        # Emit the loaded links signal
        self.linksLoaded.emit(list(self.all_streams.keys()))

    @pyqtSlot(str)
    def on_worker_error(self, error_message):
        """Handle errors from worker"""
        print(f"Stream loading error: {error_message}")

    @pyqtSlot()
    def load_links(self):
        """Trigger a refresh of stream links"""
        self.start_background_load()
        
    def extract_game_name(self, game_url):
        """Extract readable game name from URL"""
        # URL format: https://totalsportek.army/game/team1-vs-team2/ID/
        try:
            parts = game_url.rstrip('/').split('/')
            if len(parts) >= 5 and parts[-3] == 'game':
                game_name = parts[-2].replace('-', ' ').title()
                return game_name
        except:
            pass
        return game_url

    @pyqtSlot(QListWidgetItem)
    def open_stream_link(self, item):
        """Open the stream URL stored in the item's data"""
        url = item.data(Qt.ItemDataRole.UserRole)
        if url:
            webbrowser.open(url)

    @pyqtSlot()
    def filter_links(self):
        """Filter streams based on selected league and search text"""
        selected_league = self.league_filter.currentText()
        search_text = self.search_box.text().lower()

        # Build list of (game_name, provider, url) tuples for display
        self.filtered_streams = []

        for game_url, streams in self.all_streams.items():
            game_name = self.extract_game_name(game_url)

            # Apply league filter
            if selected_league != "All Leagues":
                keywords = self.league_keywords.get(selected_league, [])
                if not any(keyword in game_url.lower() for keyword in keywords):
                    continue

            # Apply search filter
            if search_text and search_text not in game_url.lower() and search_text not in game_name.lower():
                continue

            # Add all streams for this game
            for stream in streams:
                provider = stream.get('provider', 'Unknown')
                url = stream.get('url', '')
                self.filtered_streams.append((game_name, provider, url))

        # Update the list widget
        self.links_list.clear()
        for game_name, provider, url in self.filtered_streams:
            # Format: "Game Name | Provider"
            display_text = f"{game_name} | {provider}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, url)  # Store actual URL in item data
            self.links_list.addItem(item)

    def closeEvent(self, event):
        """Clean up worker when widget is closed"""
        if self.worker and self.worker.isRunning():
            print("Stopping stream loading worker...")
            self.worker.stop()
            if not self.worker.wait(2000):  # Wait max 2 seconds
                print("Worker didn't stop gracefully, terminating...")
                self.worker.terminate()
                self.worker.wait(1000)
        event.accept()

