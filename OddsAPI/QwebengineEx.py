import sys
import os
from PyQt5.QtCore import QUrl, Qt, QSize, QTimer
from PyQt5.QtWidgets import (QApplication, QMainWindow, QToolBar, QLineEdit,
                            QVBoxLayout, QHBoxLayout, QWidget, QGridLayout,
                            QPushButton, QComboBox, QLabel, QAction, QSplitter,
                            QShortcut, QMenu)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile
from PyQt5.QtGui import QIcon, QKeySequence

# Set environment variables for better media support
# Add these additional flags to your environment variables
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--enable-gpu-rasterization "
    "--enable-features=WebRTC-H264WithOpenH264FFmpeg,MediaFoundationRendererEnabled,HardwareMediaKeyHandling "
    "--enable-native-gpu-memory-buffers "
    "--enable-accelerated-video-decode "
    # "--disable-web-security "  # Helps with some cross-origin issues in streams
    "--autoplay-policy=no-user-gesture-required "
    "--ignore-gpu-blocklist "
    "--use-gl=desktop "        # Force hardware acceleration
    #"--register-pepper-plugins=\"/usr/lib/chromium/libffmpeg.so;application/x-ppapi-ffmpeg\""  # Point to your ffmpeg
)

# os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
#     "--webEngineArgs "
#     "--v=1 "
#     "--disable-gpu "
# )


class BrowserPanel(QWidget):
    """Individual browser panel with its own navigation controls"""
    def __init__(self, parent=None, index=0):
        super().__init__(parent)
        self.index = index
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(2, 2, 2, 2)
        self.layout.setSpacing(1)

        # Navigation container (can be hidden in fullscreen mode)
        self.nav_container = QWidget()
        nav_layout = QHBoxLayout(self.nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)

        # URL input field
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(f"Enter URL for Stream {index+1}")
        self.url_input.returnPressed.connect(self.navigate_to_url)

        # Navigation buttons
        self.back_btn = QPushButton("←")
        self.back_btn.setFixedWidth(30)
        self.back_btn.clicked.connect(self.go_back)

        self.forward_btn = QPushButton("→")
        self.forward_btn.setFixedWidth(30)
        self.forward_btn.clicked.connect(self.go_forward)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(30)
        self.refresh_btn.clicked.connect(self.refresh)

        # Maximize button
        self.maximize_btn = QPushButton("⤢")
        self.maximize_btn.setFixedWidth(30)
        self.maximize_btn.setToolTip(f"Maximize Stream {index+1}")

        # Add to navigation layout
        nav_layout.addWidget(self.back_btn)
        nav_layout.addWidget(self.forward_btn)
        nav_layout.addWidget(self.refresh_btn)
        nav_layout.addWidget(self.url_input)
        nav_layout.addWidget(self.maximize_btn)

        # Web view with enhanced settings for video playback
        self.web_view = QWebEngineView()

        # Configure profile for better compatibility
        profile = self.web_view.page().profile()
        # profile.setHttpUserAgent(
        #     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        # )
        # profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        # print("profile loaded: ")
        # print(profile.__dict__)

        # Enhanced settings for media playback
        settings = self.web_view.settings()
        settings.setAttribute(settings.PluginsEnabled, True)
        settings.setAttribute(settings.JavascriptCanOpenWindows, True)
        settings.setAttribute(settings.LocalStorageEnabled, True)
        settings.setAttribute(settings.AllowWindowActivationFromJavaScript, True)
        settings.setAttribute(settings.ShowScrollBars, False)
        settings.setAttribute(settings.PlaybackRequiresUserGesture, False)
        settings.setAttribute(settings.FullScreenSupportEnabled, True)
        settings.setAttribute(settings.AllowRunningInsecureContent, True)
        settings.setAttribute(settings.JavascriptEnabled, True)
        settings.setAttribute(settings.AutoLoadImages, True)
        settings.setAttribute(settings.WebGLEnabled, True)
        settings.setAttribute(settings.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(settings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(settings.AllowGeolocationOnInsecureOrigins, True)
        print("settings loaded:")
        print(settings)


        settings.globalSettings().setAttribute(settings.PluginsEnabled, True)
        settings.globalSettings().setAttribute(settings.JavascriptCanOpenWindows, True)
        settings.globalSettings().setAttribute(settings.LocalStorageEnabled, True)
        settings.globalSettings().setAttribute(settings.AllowWindowActivationFromJavaScript, True)
        settings.globalSettings().setAttribute(settings.ShowScrollBars, False)
        settings.globalSettings().setAttribute(settings.PlaybackRequiresUserGesture, False)
        settings.globalSettings().setAttribute(settings.FullScreenSupportEnabled, True)
        settings.globalSettings().setAttribute(settings.AllowRunningInsecureContent, True)
        settings.globalSettings().setAttribute(settings.JavascriptEnabled, True)
        settings.globalSettings().setAttribute(settings.AutoLoadImages, True)
        settings.globalSettings().setAttribute(settings.WebGLEnabled, True)
        settings.globalSettings().setAttribute(settings.Accelerated2dCanvasEnabled, True)
        settings.globalSettings().setAttribute(settings.LocalContentCanAccessRemoteUrls, True)
        settings.globalSettings().setAttribute(settings.AllowGeolocationOnInsecureOrigins, True)

        self.web_view.loadFinished.connect(self.update_url)

        # Add to main layout
        self.layout.addWidget(self.nav_container)
        self.layout.addWidget(self.web_view, 1)  # Give the web view a stretch factor of 1

        # Set initial URL to YouTube as it has good compatibility
        self.initial_url = "https://the.streameast.app"
        # self.initial_url = "chrome://gpu"
        self.web_view.load(QUrl(self.initial_url))
        self.url_input.setText(self.initial_url)

        # Track clean view state
        self.clean_view_mode = False

    def toggle_clean_view(self, enable):
        """Toggle between clean view (no controls) and normal view"""
        self.clean_view_mode = enable
        if enable:
            self.nav_container.hide()
            self.layout.setContentsMargins(0, 0, 0, 0)
        else:
            self.nav_container.show()
            self.layout.setContentsMargins(2, 2, 2, 2)

    def navigate_to_url(self):
        """Navigate to the URL entered in the input field"""
        url = self.url_input.text()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        self.web_view.load(QUrl(url))

    def update_url(self, success):
        """Update URL input field when page is loaded"""
        if success:
            current_url = self.web_view.url().toString()
            self.url_input.setText(current_url)

    def go_back(self):
        """Navigate backward in history"""
        self.web_view.back()

    def go_forward(self):
        """Navigate forward in history"""
        self.web_view.forward()

    def refresh(self):
        """Refresh the current page"""
        self.web_view.reload()

    def load_url(self, url):
        """Load a specified URL"""
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        self.web_view.load(QUrl(url))
        self.url_input.setText(url)


class QuadBoxBrowser(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Quad Box Sports Streamer")
        self.setGeometry(100, 50, 1600, 900)

        # Create central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Create top toolbar for global controls (can be hidden in fullscreen)
        self.toolbar = QToolBar("Global Controls")
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        # Create fullscreen button
        self.fullscreen_action = QAction("Fullscreen", self)
        self.fullscreen_action.setShortcut("F11")
        self.fullscreen_action.triggered.connect(self.toggle_fullscreen)
        self.toolbar.addAction(self.fullscreen_action)

        # Create clean view toggle button
        self.clean_view_action = QAction("Clean View", self)
        self.clean_view_action.setShortcut("F10")
        self.clean_view_action.setCheckable(True)
        self.clean_view_action.toggled.connect(self.toggle_clean_view)
        self.toolbar.addAction(self.clean_view_action)

        # Stream layout dropdown
        self.layout_selector = QComboBox()
        self.layout_selector.addItems(["2x2 Grid", "1x2 Split", "2x1 Split", "Single"])
        self.layout_selector.setCurrentIndex(0)
        self.layout_selector.currentIndexChanged.connect(self.change_layout)
        self.toolbar.addWidget(QLabel("Layout: "))
        self.toolbar.addWidget(self.layout_selector)

        # Quick access for common sports streaming sites
        self.toolbar.addWidget(QLabel("Quick Access: "))
        self.stream_presets = QComboBox()
        self.stream_presets.addItems([
            "Select Stream Preset",
            "ESPN",
            "NFL Network",
            "CBS Sports",
            "NBC Sports",
            "Fox Sports",
            "NBA TV",
            "MLB TV",
            "YouTube TV",
            "Hulu Live",
            "Sling TV",
            "FuboTV",
            "DAZN",
            "Peacock"
        ])
        self.stream_presets.currentIndexChanged.connect(self.load_preset)
        self.toolbar.addWidget(self.stream_presets)

        # Stream target selector
        self.toolbar.addWidget(QLabel("Target: "))
        self.target_selector = QComboBox()
        self.target_selector.addItems(["All Streams", "Stream 1", "Stream 2", "Stream 3", "Stream 4"])
        self.toolbar.addWidget(self.target_selector)

        # Load button
        self.load_btn = QPushButton("Load")
        self.load_btn.clicked.connect(self.load_to_target)
        self.toolbar.addWidget(self.load_btn)

        # Create content container widget
        self.content_container = QWidget()
        main_layout.addWidget(self.content_container)

        # Create grid for browsers
        self.browser_grid = QGridLayout(self.content_container)
        self.browser_grid.setSpacing(4)

        # Create browser panels
        self.browsers = []
        for i in range(4):
            browser = BrowserPanel(index=i)
            row, col = divmod(i, 2)
            self.browser_grid.addWidget(browser, row, col)
            self.browsers.append(browser)
            # Connect maximize button
            browser.maximize_btn.clicked.connect(lambda _, idx=i: self.maximize_browser(idx))

        # Track current state
        self.is_fullscreen = False
        self.is_clean_view = False
        self.maximized_browser = None
        self.original_layout = None

        # Create keyboard shortcuts
        QShortcut(QKeySequence("Alt+1"), self, lambda: self.maximize_browser(0))
        QShortcut(QKeySequence("Alt+2"), self, lambda: self.maximize_browser(1))
        QShortcut(QKeySequence("Alt+3"), self, lambda: self.maximize_browser(2))
        QShortcut(QKeySequence("Alt+4"), self, lambda: self.maximize_browser(3))
        QShortcut(QKeySequence("Esc"), self, self.handle_escape)

        # Add mouseover visibility timer for clean view mode
        self.mouseover_timer = None
        self.setMouseTracking(True)
        self.content_container.setMouseTracking(True)

    def toggle_fullscreen(self):
        """Toggle fullscreen mode for the entire application"""
        try:
            if not self.is_fullscreen:
                self.showFullScreen()
                self.is_fullscreen = True
                self.fullscreen_action.setText("Exit Fullscreen")
                # Auto-enable clean view in fullscreen mode
                if not self.is_clean_view:
                    self.toggle_clean_view(True)
            else:
                self.showNormal()
                self.is_fullscreen = False
                self.fullscreen_action.setText("Fullscreen")
                # Auto-disable clean view when exiting fullscreen
                if self.is_clean_view:
                    self.toggle_clean_view(False)
        except Exception as e:
            print(f"Fullscreen toggle error: {e}")
            # Attempt recovery
            self.showNormal()
            self.is_fullscreen = False

    def toggle_clean_view(self, enable=None):
        """Toggle clean view mode (hide all UI controls)"""
        if enable is None:
            enable = not self.is_clean_view

        self.is_clean_view = enable
        self.clean_view_action.setChecked(enable)

        # Hide/show global toolbar
        if enable:
            self.toolbar.hide()
        else:
            self.toolbar.show()

        # Update each browser panel
        for browser in self.browsers:
            browser.toggle_clean_view(enable)

    def handle_escape(self):
        """Handle escape key press"""
        if self.maximized_browser is not None:
            # If a browser is maximized, restore grid
            self.restore_grid()
        elif self.is_clean_view:
            # If in clean view mode, exit clean view
            self.toggle_clean_view(False)
        elif self.is_fullscreen:
            # If in fullscreen mode, exit fullscreen
            self.toggle_fullscreen()

    def mouseMoveEvent(self, event):
        """Handle mouse movement to temporarily show controls in clean view mode"""
        super().mouseMoveEvent(event)

        if self.is_clean_view and self.is_fullscreen:
            # Show UI temporarily
            if self.mouseover_timer:
                self.mouseover_timer.stop()

            # Show controls briefly
            self.toolbar.show()
            for browser in self.browsers:
                browser.nav_container.show()

            # Set timer to hide them again
            self.mouseover_timer = QTimer()
            self.mouseover_timer.timeout.connect(self.hide_controls)
            self.mouseover_timer.setSingleShot(True)
            self.mouseover_timer.start(3000)  # Hide after 3 seconds of inactivity

    def hide_controls(self):
        """Hide controls after mouseover timeout"""
        if self.is_clean_view:
            self.toolbar.hide()
            for browser in self.browsers:
                browser.nav_container.hide()

    def maximize_browser(self, index):
        """Maximize a specific browser panel"""
        if index >= len(self.browsers):
            return

        if self.maximized_browser is None:
            # Save current layout
            self.original_layout = self.layout_selector.currentIndex()

            # Hide all browsers
            for i, browser in enumerate(self.browsers):
                if i != index:
                    browser.hide()

            self.maximized_browser = index
            self.layout_selector.setCurrentIndex(3)  # Set to Single layout
        else:
            self.restore_grid()

    def restore_grid(self):
        """Restore the grid layout after maximizing a browser"""
        if self.maximized_browser is not None:
            # Show all browsers
            for browser in self.browsers:
                browser.show()

            # Restore layout
            if self.original_layout is not None:
                self.layout_selector.setCurrentIndex(self.original_layout)

            self.maximized_browser = None
            self.original_layout = None

    def change_layout(self, index):
        """Change the layout of browser panels"""
        # Safely remove widgets from layout
        for i in reversed(range(self.browser_grid.count())):
            item = self.browser_grid.itemAt(i)
            if item and item.widget():
                item.widget().setParent(None)

        # Set new layout
        if index == 0:  # 2x2 Grid
            for i, browser in enumerate(self.browsers):
                row, col = divmod(i, 2)
                browser.show()
                self.browser_grid.addWidget(browser, row, col)
        elif index == 1:  # 1x2 Split (Horizontal)
            self.browsers[0].show()
            self.browsers[1].show()
            self.browsers[2].hide()
            self.browsers[3].hide()
            self.browser_grid.addWidget(self.browsers[0], 0, 0)
            self.browser_grid.addWidget(self.browsers[1], 0, 1)
        elif index == 2:  # 2x1 Split (Vertical)
            self.browsers[0].show()
            self.browsers[1].hide()
            self.browsers[2].show()
            self.browsers[3].hide()
            self.browser_grid.addWidget(self.browsers[0], 0, 0)
            self.browser_grid.addWidget(self.browsers[2], 1, 0)
        elif index == 3:  # Single
            visible_browser = 0
            if self.maximized_browser is not None:
                visible_browser = self.maximized_browser

            for i, browser in enumerate(self.browsers):
                if i == visible_browser:
                    browser.show()
                    self.browser_grid.addWidget(browser, 0, 0)
                else:
                    browser.hide()

    def load_preset(self, index):
        """Load a preset streaming site"""
        if index == 0:  # "Select Stream Preset"
            return

        preset_urls = {
            1: "https://www.espn.com/watch/",
            2: "https://www.nfl.com/network/watch/",
            3: "https://www.cbssports.com/live/",
            4: "https://www.nbcsports.com/watch/",
            5: "https://www.foxsports.com/live/",
            6: "https://www.nba.com/watch/",
            7: "https://www.mlb.com/live-stream-games/",
            8: "https://tv.youtube.com/",
            9: "https://www.hulu.com/live-tv",
            10: "https://www.sling.com/",
            11: "https://www.fubo.tv/welcome",
            12: "https://www.dazn.com/",
            13: "https://www.peacocktv.com/sports"
        }

        if index in preset_urls:
            selected_url = preset_urls[index]
            self.stream_presets.setCurrentIndex(0)  # Reset selection

            # Determine target and load URL
            target_index = self.target_selector.currentIndex()
            if target_index == 0:  # All Streams
                for browser in self.browsers:
                    browser.load_url(selected_url)
            elif 1 <= target_index <= 4:
                self.browsers[target_index-1].load_url(selected_url)

    def load_to_target(self):
        """Load the current preset to the selected target"""
        preset_index = self.stream_presets.currentIndex()
        if preset_index > 0:
            self.load_preset(preset_index)

    def keyPressEvent(self, event):
        """Handle key press events"""
        if event.key() == Qt.Key_Escape and self.maximized_browser is not None:
            # If Esc is pressed while a browser is maximized, restore grid
            self.restore_grid()
        else:
            super().keyPressEvent(event)


if __name__ == "__main__":
    # Create the application instance
    app = QApplication(sys.argv)

    # Create and show the main window
    window = QuadBoxBrowser()
    window.show()

    # Start the event loop
    sys.exit(app.exec())
