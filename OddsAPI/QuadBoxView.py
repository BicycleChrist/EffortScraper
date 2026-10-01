import sys
import os
from PyQt6.QtCore import QUrl, Qt, QSize, QTimer
from PyQt6.QtWidgets import (QApplication, QMainWindow, QToolBar, QLineEdit,
                            QVBoxLayout, QHBoxLayout, QWidget, QGridLayout,
                            QPushButton, QComboBox, QLabel, QSplitter,
                             QMenu)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtGui import QIcon, QKeySequence, QAction, QShortcut


#TODO: Make quadbox acessible from main GUI window once QWebengine is recompiled properly by PIP
# Currently this file only works with pyqt6 libs from Arch Repo

# Set environment variables for better media support
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--enable-gpu-rasterization "
    "--enable-features=WebRTC-H264WithOpenH264FFmpeg,MediaFoundationRendererEnabled,HardwareMediaKeyHandling "
    "--enable-native-gpu-memory-buffers "
    "--enable-accelerated-video-decode "
    "--autoplay-policy=no-user-gesture-required "
    "--ignore-gpu-blocklist "
    "--use-gl=angle "
    "--use-angle=default "
    "--enable-accelerated-2d-canvas "
    "--disable-gpu-sandbox "
)


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

        # Video isolation button
        self.video_isolate_btn = QPushButton("📹")
        self.video_isolate_btn.setFixedWidth(30)
        self.video_isolate_btn.setToolTip(f"Isolate Video for Stream {index+1}")
        self.video_isolate_btn.clicked.connect(self.toggle_video_isolation)

        # Maximize button
        self.maximize_btn = QPushButton("⤢")
        self.maximize_btn.setFixedWidth(30)
        self.maximize_btn.setToolTip(f"Maximize Stream {index+1}")

        # Add to navigation layout
        nav_layout.addWidget(self.back_btn)
        nav_layout.addWidget(self.forward_btn)
        nav_layout.addWidget(self.refresh_btn)
        nav_layout.addWidget(self.video_isolate_btn)
        nav_layout.addWidget(self.url_input)
        nav_layout.addWidget(self.maximize_btn)

        # Web view with enhanced settings for video playback
        self.web_view = QWebEngineView()

        # Configure profile for better compatibility
        profile = self.web_view.page().profile()

        # Enhanced settings for media playbook
        settings = self.web_view.settings()
        settings.setAttribute(settings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(settings.WebAttribute.JavascriptCanOpenWindows, True)
        settings.setAttribute(settings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(settings.WebAttribute.AllowWindowActivationFromJavaScript, True)
        settings.setAttribute(settings.WebAttribute.ShowScrollBars, False)
        settings.setAttribute(settings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(settings.WebAttribute.FullScreenSupportEnabled, True)
        settings.setAttribute(settings.WebAttribute.AllowRunningInsecureContent, True)
        settings.setAttribute(settings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(settings.WebAttribute.AutoLoadImages, True)
        settings.setAttribute(settings.WebAttribute.WebGLEnabled, True)
        settings.setAttribute(settings.WebAttribute.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(settings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(settings.WebAttribute.AllowGeolocationOnInsecureOrigins, True)

        self.web_view.loadFinished.connect(self.update_url)

        # Add to main layout
        self.layout.addWidget(self.nav_container)
        self.layout.addWidget(self.web_view, 1)

        # Set initial URL
        self.initial_url = "https://thestreameast.top"
        self.web_view.load(QUrl(self.initial_url))
        self.url_input.setText(self.initial_url)

        # Track states
        self.clean_view_mode = False
        self.video_isolated = False
        self.original_page_style = None

    def toggle_clean_view(self, enable):
        """Toggle between clean view (no controls) and normal view"""
        self.clean_view_mode = enable
        if enable:
            self.nav_container.hide()
            self.layout.setContentsMargins(0, 0, 0, 0)
        else:
            self.nav_container.show()
            self.layout.setContentsMargins(2, 2, 2, 2)

    def toggle_video_isolation(self):
        """Toggle video isolation mode - shows only the video element"""
        if not self.video_isolated:
            self.isolate_video()
        else:
            self.restore_page()

    def isolate_video(self):
        """Inject JavaScript to isolate and maximize the video element"""
        js_code = """
        (function() {
            try {
                // Store original styles if not already stored
                if (!window.originalPageStyles) {
                window.originalPageStyles = {
                    bodyStyle: document.body.style.cssText,
                    htmlStyle: document.documentElement.style.cssText,
                    // list of {el, style} for every element we touch, so restore is exact
                    modified: []
                };
            }

            // Helper: remember an element's inline style before we change it
            function remember(el) {
                window.originalPageStyles.modified.push({ el: el, style: el.getAttribute('style') });
            }

            // Find all video elements
            const videos = document.querySelectorAll('video');
            let targetVideo = null;

            // Find the largest or most likely main video
            if (videos.length > 0) {
                targetVideo = Array.from(videos).reduce((prev, current) => {
                    const prevArea = prev.offsetWidth * prev.offsetHeight;
                    const currentArea = current.offsetWidth * current.offsetHeight;
                    return currentArea > prevArea ? current : prev;
                });
            }

            // If no video found, try to find iframe players
            if (!targetVideo) {
                const iframes = document.querySelectorAll('iframe');
                for (let iframe of iframes) {
                    if (iframe.src.includes('youtube') ||
                        iframe.src.includes('twitch') ||
                        iframe.src.includes('player') ||
                        iframe.offsetWidth > 400) {
                        targetVideo = iframe;
                        break;
                    }
                }
            }

            // If still no video, try to find video containers
            if (!targetVideo) {
                const selectors = [
                    '[class*="video"]', '[id*="video"]',
                    '[class*="player"]', '[id*="player"]',
                    '[class*="stream"]', '[id*="stream"]',
                    '.jwplayer', '.video-js', '.vjs-tech'
                ];

                for (let selector of selectors) {
                    const elements = document.querySelectorAll(selector);
                    if (elements.length > 0) {
                        targetVideo = elements[0];
                        break;
                    }
                }
            }

            if (targetVideo) {
                // Figure out the ACTUAL media element to display. targetVideo may be a real
                // <video>/<iframe>, or it may be a container div (the selector fallback). If
                // it's a container, dig out the largest media descendant inside it -- that's
                // what the user is actually watching.
                let media = targetVideo;
                const ttag = targetVideo.tagName;
                if (ttag !== 'VIDEO' && ttag !== 'IFRAME' && ttag !== 'CANVAS') {
                    const inner = targetVideo.querySelectorAll('video, canvas, iframe');
                    if (inner.length > 0) {
                        media = Array.from(inner).reduce((p, c) =>
                            (c.offsetWidth * c.offsetHeight) > (p.offsetWidth * p.offsetHeight) ? c : p);
                    }
                }

                // The element we pin fullscreen. If we found a real media element, pin IT
                // directly (simplest, most reliable). Only fall back to the container when no
                // media descendant exists.
                const pinned = (media && media !== targetVideo) ? media : targetVideo;

                // THE GOTCHA: position:fixed is relative to the nearest ancestor that has a
                // transform / filter / perspective / will-change (any of these creates a
                // "containing block"). On StreamEast a player wrapper has a transform, so a
                // naive fixed element ends up relative to that wrapper -> "slight zoom" instead
                // of true fullscreen. So we must neutralize those properties on EVERY ancestor.
                let ancestor = pinned.parentElement;
                while (ancestor && ancestor !== document.documentElement) {
                    const cs = window.getComputedStyle(ancestor);
                    const needsFix = (cs.transform !== 'none') ||
                                     (cs.filter !== 'none') ||
                                     (cs.perspective !== 'none') ||
                                     (cs.willChange !== 'auto') ||
                                     (cs.contain !== 'none') ||
                                     (cs.overflow !== 'visible');
                    if (needsFix) {
                        remember(ancestor);
                        ancestor.style.setProperty('transform', 'none', 'important');
                        ancestor.style.setProperty('filter', 'none', 'important');
                        ancestor.style.setProperty('perspective', 'none', 'important');
                        ancestor.style.setProperty('will-change', 'auto', 'important');
                        ancestor.style.setProperty('contain', 'none', 'important');
                        ancestor.style.setProperty('overflow', 'visible', 'important');
                    }
                    ancestor = ancestor.parentElement;
                }

                // Pin the chosen element fullscreen. object-fit: cover fills the whole panel
                // (cropping the overflowing edges) instead of letterboxing with black bars.
                remember(pinned);
                const baseFixed = 'position: fixed !important; top: 0 !important; left: 0 !important; right: 0 !important; bottom: 0 !important; width: 100vw !important; height: 100vh !important; max-width: 100vw !important; max-height: 100vh !important; min-width: 0 !important; min-height: 0 !important; margin: 0 !important; padding: 0 !important; transform: none !important; z-index: 2147483647 !important; background: #000 !important; border: none !important;';
                if (pinned.tagName === 'VIDEO' || pinned.tagName === 'CANVAS') {
                    // For a real media element object-fit: cover crops to fill the panel.
                    pinned.style.cssText = baseFixed + ' object-fit: cover !important;';
                } else if (pinned.tagName === 'IFRAME') {
                    // Cross-origin iframe: we can't touch the inner video, and object-fit does
                    // nothing on an iframe. The inner player letterboxes its 16:9 video into
                    // whatever box we give the iframe -> black bars. So instead we size the
                    // IFRAME ITSELF to a 16:9 box scaled to COVER the viewport, centered. The
                    // viewport clips the overflow; the inner 16:9 video now matches the iframe's
                    // 16:9 box and fills it with no internal bars.
                    //
                    // Expressed in pure CSS (vw/vh + max()) so it RECOMPUTES automatically on
                    // every panel resize / monitor move -- no stale pixel math, fully
                    // resolution-independent (1920x1080, 2560x1080 ultrawide, HiDPI, etc.):
                    //   16:9 cover  ->  width  = max(100vw, (16/9)*100vh) = max(100vw, 177.78vh)
                    //                   height = max(100vh, (9/16)*100vw) = max(100vh, 56.25vw)
                    // Centered via top/left 50% + translate(-50%,-50%) (ancestor transforms
                    // were already neutralized above, so this transform is viewport-relative).
                    pinned.style.cssText = 'position: fixed !important; top: 50% !important; left: 50% !important; width: max(100vw, 177.78vh) !important; height: max(100vh, 56.25vw) !important; max-width: none !important; max-height: none !important; min-width: 0 !important; min-height: 0 !important; margin: 0 !important; padding: 0 !important; transform: translate(-50%, -50%) !important; border: none !important; z-index: 2147483647 !important; background: #000 !important;';
                } else {
                    // container fallback
                    pinned.style.cssText = baseFixed;
                }

                // If we had to pin a CONTAINER (no reachable media element, e.g. cross-origin
                // iframe nested oddly), also force every wrapper from the container down to the
                // media to fill 100%, so the inner media stretches to the panel instead of
                // keeping its native 16:9 box (the black-bar-half-fill bug).
                if (pinned === targetVideo && media && media !== targetVideo) {
                    let node = media.parentElement;
                    while (node && node !== targetVideo) {
                        remember(node);
                        node.style.cssText = 'display: block !important; width: 100% !important; height: 100% !important; max-width: none !important; max-height: none !important; min-width: 0 !important; min-height: 0 !important; aspect-ratio: auto !important; position: relative !important; top: 0 !important; left: 0 !important; margin: 0 !important; padding: 0 !important; transform: none !important; overflow: hidden !important;';
                        node = node.parentElement;
                    }
                    remember(media);
                    if (media.tagName === 'VIDEO' || media.tagName === 'CANVAS') {
                        media.style.cssText = 'display: block !important; width: 100% !important; height: 100% !important; max-width: none !important; max-height: none !important; object-fit: cover !important; position: relative !important; top: 0 !important; left: 0 !important; transform: none !important; margin: 0 !important; background: #000 !important;';
                    } else {
                        media.style.cssText = 'display: block !important; width: 100% !important; height: 100% !important; border: none !important; position: relative !important; top: 0 !important; left: 0 !important; margin: 0 !important;';
                    }
                }

                // Hide EVERYTHING except the video's branch. Walk from the pinned element up
                // to <body> and, at each level, hide all siblings of the current node. This
                // leaves only the chain of ancestors that contain the video (and the video
                // itself) visible -> kills the live-chat iframe, ads, headers, and any other
                // page chrome that would otherwise show around/under the video.
                let cur = pinned;
                while (cur && cur !== document.body && cur !== document.documentElement) {
                    const par = cur.parentElement;
                    if (par) {
                        const kids = par.children;
                        for (let i = 0; i < kids.length; i++) {
                            if (kids[i] !== cur) {
                                remember(kids[i]);
                                kids[i].style.setProperty('display', 'none', 'important');
                            }
                        }
                    }
                    cur = par;
                }

                // Style the page background black (keep it scrollable for navigation)
                document.body.style.cssText = 'margin: 0 !important; padding: 0 !important; background-color: #000 !important; overflow: hidden !important;';
                document.documentElement.style.cssText = 'margin: 0 !important; padding: 0 !important; background-color: #000 !important; overflow: hidden !important;';

                // Diagnostics: report exactly what we grabbed and how it ended up rendering.
                const allVids = document.querySelectorAll('video');
                const allIframes = document.querySelectorAll('iframe');
                const mr = media.getBoundingClientRect();
                const mcs = window.getComputedStyle(media);
                const diag = ' [DIAG'
                    + ' viewport=' + window.innerWidth + 'x' + window.innerHeight
                    + ' videos=' + allVids.length
                    + ' iframes=' + allIframes.length
                    + ' pinned=' + pinned.tagName + '.' + (pinned.className || '').toString().slice(0,40)
                    + ' media=' + media.tagName + '.' + (media.className || '').toString().slice(0,40)
                    + ' mediaRect=' + Math.round(mr.width) + 'x' + Math.round(mr.height)
                    + ' mediaObjFit=' + mcs.objectFit
                    + ' mediaPos=' + mcs.position
                    + ']';

                return 'Video isolated successfully' + diag;
            }

                return 'No video element found';
            } catch (error) {
                return 'Error: ' + error.message;
            }
        })();
        """

        self.web_view.page().runJavaScript(js_code, self._on_video_isolation_result)

    def _on_video_isolation_result(self, result):
        """Handle the result of video isolation JavaScript"""
        if result and "successfully" in result:
            self.video_isolated = True
            self.video_isolate_btn.setText("🔄")
            self.video_isolate_btn.setToolTip(f"Restore Page View for Stream {self.index+1}")
            print(f"\n===== ISOLATE DIAG (Stream {self.index+1}) =====\n{result}\n=================================\n")
        else:
            print(f"Stream {self.index+1}: Failed to isolate video - {result}")

    def restore_page(self):
        """Restore the original page layout"""
        js_code = """
        (function() {
            try {
                if (window.originalPageStyles) {
                    // Restore body and html styles
                    document.body.style.cssText = window.originalPageStyles.bodyStyle;
                    document.documentElement.style.cssText = window.originalPageStyles.htmlStyle;

                    // Restore every element we touched, in reverse order, to its exact
                    // original inline style (null/absent => remove the style attribute).
                    const modified = window.originalPageStyles.modified || [];
                    for (let i = modified.length - 1; i >= 0; i--) {
                        const entry = modified[i];
                        if (!entry || !entry.el) continue;
                        if (entry.style === null || entry.style === undefined) {
                            entry.el.removeAttribute('style');
                        } else {
                            entry.el.setAttribute('style', entry.style);
                        }
                    }

                    // Clear stored styles
                    window.originalPageStyles = null;

                    return 'Page restored successfully';
                }
                return 'No original styles found';
            } catch (error) {
                return 'Error restoring: ' + error.message;
            }
        })();
        """

        self.web_view.page().runJavaScript(js_code, self._on_page_restoration_result)

    def _on_page_restoration_result(self, result):
        """Handle the result of page restoration JavaScript"""
        self.video_isolated = False
        self.video_isolate_btn.setText("📹")
        self.video_isolate_btn.setToolTip(f"Isolate Video for Stream {self.index+1}")
        print(f"Stream {self.index+1}: {result}")

    def navigate_to_url(self):
        """Navigate to the URL entered in the input field"""
        # If video is isolated, restore page first
        if self.video_isolated:
            self.restore_page()

        url = self.url_input.text()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        self.web_view.load(QUrl(url))

    def update_url(self, success):
        """Update URL input field when page is loaded"""
        if success:
            current_url = self.web_view.url().toString()
            self.url_input.setText(current_url)
            # Reset video isolation state on new page load
            self.video_isolated = False
            self.video_isolate_btn.setText("📹")
            self.video_isolate_btn.setToolTip(f"Isolate Video for Stream {self.index+1}")

    def go_back(self):
        """Navigate backward in history"""
        if self.video_isolated:
            self.restore_page()
        self.web_view.back()

    def go_forward(self):
        """Navigate forward in history"""
        if self.video_isolated:
            self.restore_page()
        self.web_view.forward()

    def refresh(self):
        """Refresh the current page"""
        if self.video_isolated:
            self.restore_page()
        self.web_view.reload()

    def load_url(self, url):
        """Load a specified URL"""
        if self.video_isolated:
            self.restore_page()

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

        # Create top toolbar for global controls
        self.toolbar = QToolBar("Global Controls")
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        # Create theater mode button (combines fullscreen + clean view + video isolation)
        self.fullscreen_action = QAction("Theater Mode", self)
        self.fullscreen_action.setShortcut(QKeySequence("F11"))
        self.fullscreen_action.triggered.connect(self.toggle_theater_mode)
        self.toolbar.addAction(self.fullscreen_action)

        # Video isolation for all streams (kept for manual control)
        self.video_isolate_all_action = QAction("Isolate Videos", self)
        self.video_isolate_all_action.setShortcut(QKeySequence("F9"))
        self.video_isolate_all_action.triggered.connect(self.toggle_all_video_isolation)
        self.toolbar.addAction(self.video_isolate_all_action)

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
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self.browsers[0].toggle_video_isolation())
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self.browsers[1].toggle_video_isolation())
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self.browsers[2].toggle_video_isolation())
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self.browsers[3].toggle_video_isolation())
        QShortcut(QKeySequence("Esc"), self, self.handle_escape)

        # Add mouseover visibility timer for clean view mode
        self.mouseover_timer = None
        self.setMouseTracking(True)
        self.content_container.setMouseTracking(True)

    def toggle_theater_mode(self):
        """Toggle theater mode: fullscreen + clean view + video isolation"""
        if not self.is_fullscreen:
            # Enter theater mode
            self.showFullScreen()
            self.is_fullscreen = True
            self.fullscreen_action.setText("Exit Theater Mode")

            # Enable clean view
            if not self.is_clean_view:
                self.toggle_clean_view(True)

            # Isolate videos in all visible panels
            visible_browsers = [b for b in self.browsers if b.isVisible()]
            for browser in visible_browsers:
                if not browser.video_isolated:
                    browser.isolate_video()
        else:
            # Exit theater mode
            self.showNormal()
            self.is_fullscreen = False
            self.fullscreen_action.setText("Theater Mode")

            # Disable clean view
            if self.is_clean_view:
                self.toggle_clean_view(False)

            # Restore all isolated videos
            for browser in self.browsers:
                if browser.video_isolated:
                    browser.restore_page()

    def toggle_all_video_isolation(self):
        """Toggle video isolation for all visible browser panels"""
        visible_browsers = [b for b in self.browsers if b.isVisible()]
        if visible_browsers:
            # Check if any video is currently isolated
            any_isolated = any(b.video_isolated for b in visible_browsers)

            # If any are isolated, restore all; otherwise isolate all
            for browser in visible_browsers:
                if any_isolated and browser.video_isolated:
                    browser.restore_page()
                elif not any_isolated:
                    browser.isolate_video()

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
            self.showNormal()
            self.is_fullscreen = False

    def toggle_clean_view(self, enable=None):
        """Toggle clean view mode (hide all UI controls)"""
        if enable is None:
            enable = not self.is_clean_view

        self.is_clean_view = enable

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
        # If in theater mode (fullscreen), exit completely
        if self.is_fullscreen:
            self.toggle_theater_mode()
            return

        # If any videos are isolated but not in fullscreen, just restore videos
        isolated_browsers = [b for b in self.browsers if b.video_isolated]
        if isolated_browsers:
            for browser in isolated_browsers:
                browser.restore_page()
            return

        # If a browser is maximized, restore grid
        if self.maximized_browser is not None:
            self.restore_grid()
            return

        # If in clean view mode without fullscreen, exit clean view
        if self.is_clean_view:
            self.toggle_clean_view(False)

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
        if event.key() == Qt.Key.Key_Escape:
            self.handle_escape()
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
