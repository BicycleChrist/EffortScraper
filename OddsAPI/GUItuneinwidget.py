import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import GUItunein
from PyQt6.QtCore import Qt, QUrl, QPoint, QRect, pyqtSignal, pyqtSlot, QThread, QEvent
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QVBoxLayout, QHBoxLayout, QListWidget,
    QPushButton, QLineEdit, QFrame, QListWidgetItem, QSizePolicy
)


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

            # ATP Challenger TV discovery (official OTT API, ~30 status checks)
            # runs concurrently with the whole box-network scrape and is
            # merged into all_streams just before the stage-2 emit.
            chtv_executor = ThreadPoolExecutor(max_workers=1)
            chtv_future = chtv_executor.submit(GUItunein.get_challenger_tv_streams)

            # Stage 1: Get all game page URLs.
            # One worker per URL: every entry is a different domain, so full
            # fan-out doesn't increase per-site burst (each site's own league
            # pages are throttled inside get_box_network_links).
            all_game_urls = []
            with ThreadPoolExecutor(max_workers=max(1, len(self.urls))) as executor:
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

            # Merge the Challenger TV live feeds scraped in parallel
            try:
                all_streams.update(chtv_future.result(timeout=60))
            except Exception as e:
                self.error_occurred.emit(f"Challenger TV feeds failed: {e}")
            finally:
                chtv_executor.shutdown(wait=False)

            # Emit stage 2 completion
            self.stage2_complete.emit(all_streams)

        except Exception as e:
            self.error_occurred.emit(f"Worker error: {e}")


class TuneInWidget(QFrame):
    """Floating streaming-links dropdown. Drops below the toggle button on show."""

    linksLoaded = pyqtSignal(list)

    def __init__(self, toggle_btn=None):
        # Parent to the main window so we can use mapTo() for correct positioning
        # on both X11 and Wayland — no separate top-level window needed.
        parent_win = toggle_btn.window() if toggle_btn else None
        super().__init__(parent_win)
        self._toggle_btn = toggle_btn
        self.hide()
        # Outside-click dismiss + parent-resize repositioning only matter
        # while the panel is on screen, so both event filters are installed
        # on show and removed on hide (see show/hideEvent). The old permanent
        # app-wide install in __init__ made EVERY event in the application
        # run this widget's Python eventFilter — a per-event tax the stall
        # watchdog caught mid-cascade during other widgets' show storms
        # (e.g. the injury-news toggle).

        # URLs to be parsed (streaming aggregator main pages)
        self.urls = [
            #"https://www.nflbite.is/",          # NFL games with 10-15 streams each
            "https://istreameast.app/v52",  # Multi-sport aggregator
            # "Box" network — one domain per sport (mlbbox.me nav-items).
            # GUItunein crawls each domain's league sub-pages for game links.
            "https://nflbox.io",                                    # NFL
            "https://nflbox.io/football/college-football",          # CFB
            "https://nbabox.co",                                    # NBA
            "https://nbabox.co/watch-college-basketball-online",    # NCAAM
            "https://mlbbox.me",                                    # MLB
            "https://nhlbox.me",                                    # NHL
            "https://mmastream.me",                                 # UFC/MMA
            "https://boxingbox.net",                                # Boxing
            "https://tennisonline.me",                              # Tennis
            "https://soccerbox.me",                                 # Soccer
            "https://rugbybox.me",                                  # Rugby
            "https://f1box.co",                                     # F1
            "https://motogpstream.me",                              # MotoGP
            "https://golfstreams.me",                               # Golf
            "https://dartsstreams.com",                             # Darts
            "https://cricwatch.io",                                 # Cricket
            # "https://cracksports.me",  # multi-sport hub of the same network;
            #                            # skipped — duplicates the per-sport domains
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
            QPushButton {
                background-color: #007bff;
                color: white;
                border: 1px solid #0056b3;
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 9pt;
            }
            QPushButton:disabled {
                background-color: #e9ecef;
                color: #6c757d;
                border-color: #dee2e6;
            }
            QLineEdit {
                font-size: 9pt;
                padding: 2px 4px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                background-color: #2a2a2a;
                color: white;
            }
        """)

        # Main layout with tight margins
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header row: search + refresh
        header = QHBoxLayout()
        header.setContentsMargins(2, 2, 2, 2)
        header.setSpacing(4)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter streams...")
        self.search_box.setFixedHeight(22)
        self.search_box.textChanged.connect(self.filter_links)
        header.addWidget(self.search_box)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setFixedSize(70, 22)
        self.refresh_button.clicked.connect(self.load_links)
        header.addWidget(self.refresh_button)
        main_layout.addLayout(header)

        # Stream list — single-click to open, always-on scrollbar
        self.links_list = QListWidget(self)
        self.links_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.links_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.links_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.links_list.itemClicked.connect(self.open_stream_link)
        self.links_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout.addWidget(self.links_list)

        self.setFixedWidth(460)

        # Start loading links in background (non-blocking)
        self.start_background_load()

    def _reposition(self):
        if self._toggle_btn and self.parent():
            btn = self._toggle_btn
            pos = btn.mapTo(self.parent(), QPoint(0, btn.height() + 2))
            # Cap height so we never exceed the parent's lower boundary
            parent_bottom = self.parent().height()
            available_h = max(100, parent_bottom - pos.y() - 4)
            self.setFixedHeight(available_h)
            self.move(pos)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            gp = event.globalPosition().toPoint()
            in_panel = self.rect().contains(self.mapFromGlobal(gp))
            in_btn   = (self._toggle_btn is not None and
                        self._toggle_btn.rect().contains(self._toggle_btn.mapFromGlobal(gp)))
            if not in_panel and not in_btn:
                self.hide()
        # Stay anchored under the button when parent resizes or moves
        if obj is self.parent() and self.isVisible():
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self._reposition()
        return False

    def showEvent(self, event):
        QApplication.instance().installEventFilter(self)
        if self.parent():
            self.parent().installEventFilter(self)  # reposition on resize/move
        super().showEvent(event)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        if self.parent():
            self.parent().removeEventFilter(self)
        super().hideEvent(event)
        if self._toggle_btn:
            self._toggle_btn.setChecked(False)
            self._toggle_btn.setText("Show Streaming Links ▼")

    def popup_below(self, btn):
        # Re-resolve the button's real top-level window at popup time. At
        # construction the button may not be attached to the main window yet
        # (its window() then returns an intermediate container, whose bounds
        # would clip the dropdown just below the ticker). Re-parenting here
        # makes the overlay float over the full main window regardless of
        # widget-tree build order.
        win = btn.window()
        if self.parent() is not win:
            self.setParent(win)
        self._reposition()
        self.show()
        self.raise_()

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

        # If no streams found, use game URLs directly as stream links
        if not all_streams and self.all_game_urls:
            print(f"No individual streams found, using {len(self.all_game_urls)} game page URLs as links")
            # Create a single "Stream Page" entry for each game URL
            for game_url in self.all_game_urls:
                self.all_streams[game_url] = [{'provider': 'Stream Page', 'url': game_url}]

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
        """Extract readable game name from URL.

        istreameast structure:
          https://istreameast.app/nhl-playoffs/vegas-golden-knights-colorado-avalanche/42249792
          parts[-1] = "42249792"  (numeric ID)
          parts[-2] = "vegas-golden-knights-colorado-avalanche"  (team slug)
          parts[-3] = "nhl-playoffs"  (sport/league slug)

        Box network structure:
          https://mlbbox.me/mlb/boston-red-sox-vs-texas-rangers-stream
          https://boxingbox.net/watch-billam-smith-vs-rozicki-live-stream-online
        """
        try:
            if GUItunein.is_challenger_tv_url(game_url):
                return GUItunein.challenger_game_name(game_url)

            parts = game_url.rstrip('/').split('/')

            if len(parts) >= 2 and parts[-1].isnumeric():
                game_name = parts[-2].replace('-', ' ').title()
                return game_name

            if GUItunein.is_box_network_url(game_url):
                slug = parts[-1]
                slug = slug.removeprefix('watch-')
                for suffix in ('-live-stream-online', '-stream-online',
                               '-live-stream', '-online-stream', '-stream'):
                    if slug.endswith(suffix):
                        slug = slug.removesuffix(suffix)
                        break
                return slug.replace('-', ' ').title()
        except:
            pass

        return game_url

    @pyqtSlot(QListWidgetItem)
    def open_stream_link(self, item):
        """Open the stream URL stored in the item's data"""
        url = item.data(Qt.ItemDataRole.UserRole)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    @pyqtSlot()
    def filter_links(self):
        """Filter streams based on search text only"""
        search_text = self.search_box.text().lower()

        # Build list of (game_name, provider, url) tuples for display
        self.filtered_streams = []

        for game_url, streams in self.all_streams.items():
            game_name = self.extract_game_name(game_url)

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
