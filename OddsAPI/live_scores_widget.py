"""
live_scores_widget.py — standalone live-score browser for Flashscore data.

Two panes:
  * left  — navigation tree: a "LIVE — All Sports" item plus every sport;
            expanding a sport lazily lists its leagues for sub-navigation.
  * right — event list for the current selection: LIVE events at the top,
            UPCOMING below, grouped by league. A days-ahead control widens
            the schedule window; auto-refresh keeps it current.

Driven by FlashscoreClient (flashscore_client.py). All network work runs on
background threads; results are marshalled back to the GUI thread via signals.

Run standalone:
    python3 live_scores_widget.py
"""

import os
import re
import sys
import threading
import unicodedata
from datetime import datetime
from html import escape as _html_escape

import requests

from PyQt6 import sip
from PyQt6.QtCore import Qt, QObject, QTimer, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont, QPixmap, QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QLabel, QSplitter, QHeaderView,
    QLineEdit, QButtonGroup, QFrame,
)

from flashscore_client import (
    FlashscoreClient, SPORT_IDS, Event, format_progress, format_to_par,
)
from OddsPortalClient import (
    OddsPortalClient, DroppingOdd, EventOdds, format_odd,
)

# Odds display format (OddsPortal feeds are always decimal; we convert). Maps
# the format-selector combo labels to format_odd() codes.
ODDS_FORMATS = [("American", "us"), ("Decimal", "dec"), ("Fractional", "frac")]

# Cap on how many live rows we fetch granular progress for per refresh, to
# bound request volume (the LIVE-all view can have hundreds of live events).
MAX_PROGRESS_FETCHES = 80

# Field-of-competitors sports (not head-to-head, not golf). Each spec defines
# the table columns and how to pull each cell from a raw participant record.
# "nested" sports group events as venue -> races (by start time).
_L = "l"; _C = "c"; _R = "r"
PARTICIPANT_SPECS = {
    "horse_racing": {
        "nested": True,
        "cols": [
            ("#", _R, lambda e: e.get("NI", "")),
            ("Horse", _L, lambda e: e.get("AE", "")),
            ("Jockey / Trainer", _L, lambda e: e.get("NA", "")),
            ("Age", _C, lambda e: e.get("NN", "")),
            ("Wt", _C, lambda e: e.get("NL", "")),
            ("SP", _C, lambda e: e.get("NM", "")),
        ],
    },
    "motorsport": {
        "nested": False,
        "cols": [
            ("Pos", _C, lambda e: e.get("CX", "")),
            ("Driver / Car", _L, lambda e: e.get("AE", "")),
            ("Team", _L, lambda e: e.get("NA", "")),
        ],
    },
    "cycling": {
        "nested": False,
        "cols": [
            ("Pos", _C, lambda e: e.get("CX", "")),
            ("Rider", _L, lambda e: e.get("AE", "")),
            ("Team", _L, lambda e: e.get("NA", "")),
        ],
    },
}

# Sports surfaced first in the nav (the rest follow alphabetically).
PRIORITY_SPORTS = ["baseball", "football", "basketball", "tennis", "hockey"]
DEFAULT_SPORT = "baseball"
LIVE_NODE = "__live_all__"

# Bridge from Flashscore sport keys (SPORT_IDS, underscored) to the OddsPortal
# sport url-name used by the dropping-odds feed. Only sports OddsPortal carries
# an odds feed for are listed; a sport absent here simply gets no odds overlay.
SPORT_BRIDGE = {
    "football": "football", "tennis": "tennis", "basketball": "basketball",
    "hockey": "hockey", "american_football": "american-football",
    "baseball": "baseball", "handball": "handball", "rugby_union": "rugby-union",
    "floorball": "floorball", "futsal": "futsal", "volleyball": "volleyball",
    "cricket": "cricket", "darts": "darts", "snooker": "snooker",
    "boxing": "boxing", "aussie_rules": "aussie-rules",
    "rugby_league": "rugby-league", "badminton": "badminton",
    "table_tennis": "table-tennis", "esports": "esports",
}

# Dropping-odds fetch knobs (period: 2=last 12h; bs: 1=min 10% of books moved).
DROPPING_PERIOD = 2
DROPPING_BS = 1
DROPPING_MAX_PAGES = 10

# Flashscore image CDN — team logos / player headshots (Event.home_logo etc.).
LOGO_BASE = "https://static.flashscore.com/res/image/data/"
LOGO_PX = 18  # rendered icon size

_PUNCT_RE = re.compile(r"[^a-z0-9 ]")
_WS_RE = re.compile(r"\s+")


def _norm_name(name):
    """Normalize a team/player name for cross-source matching: strip accents,
    lowercase, drop punctuation (so 'Tüfekci C. E.' == 'tufekci c e'). Both
    Flashscore and OddsPortal use the same 'Surname X.' convention, so a
    normalized exact match is reliable for head-to-head events."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT_RE.sub(" ", s.lower())
    return _WS_RE.sub(" ", s).strip()


def _oddsportal_locations():
    """Build the {label: proxy} map for multi-geo odds aggregation from the
    ODDSPORTAL_PROXIES env var (comma-separated 'label=proxy' or bare proxy
    URLs). OddsPortal geo-filters its bookmaker set by egress IP, so each extra
    geo surfaces books (Pinnacle, Asian books, exchanges) hidden from the rest;
    the per-event odds fetch hits them all concurrently and merges. Always
    includes the direct (no-proxy) location."""
    locs = {"direct": None}
    raw = os.environ.get("ODDSPORTAL_PROXIES", "")
    for i, spec in enumerate(s.strip() for s in raw.split(",") if s.strip()):
        if "=" in spec:
            label, proxy = spec.split("=", 1)
        else:
            label, proxy = f"geo{i + 1}", spec
        locs[label.strip()] = proxy.strip()
    return locs

# colors
C_BG = "#14181d"
C_PANEL = "#1b2128"
C_LIVE = "#ff4d4d"
C_TEXT = "#d7dde3"
C_DIM = "#8b97a3"
C_HEADER = "#2a323c"
C_ACCENT = "#4da3ff"


class ScheduleWorker(QObject):
    """Runs blocking FlashscoreClient calls on daemon threads and emits results.

    Each request carries a monotonically increasing token; the widget ignores
    results whose token is stale (a newer request superseded them).
    """

    scheduleReady = pyqtSignal(int, str, list)   # token, sport, [Event]
    liveReady = pyqtSignal(int, list)            # token, [Event]
    detailReady = pyqtSignal(int, str, dict)     # token, event_id, detail
    golfReady = pyqtSignal(int, list)            # token, [GolfLeaderboard]
    participantsReady = pyqtSignal(int, str, list)  # token, sport, [ParticipantEvent]
    failed = pyqtSignal(int, str)                # token, message

    def __init__(self, client: FlashscoreClient):
        super().__init__()
        self.client = client

    def fetch_schedule(self, token: int, sport: str, days_ahead: int):
        def run():
            try:
                days = list(range(0, days_ahead + 1))
                sched = self.client.get_schedule(sports=[sport], days=days)
                events = sched.get(sport, [])
                self.scheduleReady.emit(token, sport, events)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_live_all(self, token: int):
        def run():
            try:
                live = self.client.snapshot_live(sports=list(SPORT_IDS.keys()))
                self.liveReady.emit(token, live)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_golf(self, token: int, day: int):
        def run():
            try:
                self.golfReady.emit(token, self.client.get_golf_leaderboards(day))
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_participants(self, token: int, sport: str, day: int):
        def run():
            try:
                evs = self.client.get_participant_events(SPORT_IDS[sport], day)
                self.participantsReady.emit(token, sport, evs)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_detail(self, token: int, sport_id: int, event_id: str):
        def run():
            try:
                # lean: fetches only the sub-feed(s) the progress cell needs
                detail = self.client.get_live_progress(sport_id, event_id)
                self.detailReady.emit(token, event_id, detail)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()


class OddsWorker(QObject):
    """Runs blocking OddsPortalClient calls on daemon threads and emits results.

    Mirrors ScheduleWorker's token-guard pattern so stale results are dropped.
    """

    droppingReady = pyqtSignal(int, str, list)  # token, sport_urlname, [DroppingOdd]
    searchReady = pyqtSignal(int, str, list)    # token, query, [participant dict]
    eventOddsReady = pyqtSignal(int, str, object)  # token, event_url, EventOdds
    failed = pyqtSignal(int, str)               # token, message

    def __init__(self, client: OddsPortalClient):
        super().__init__()
        self.client = client

    def fetch_dropping(self, token: int, sport_urlname: str):
        def run():
            try:
                drops = self.client.dropping_odds_pages(
                    sport_urlname, DROPPING_PERIOD, DROPPING_BS,
                    max_pages=DROPPING_MAX_PAGES)
                self.droppingReady.emit(token, sport_urlname, drops)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_search(self, token: int, query: str):
        def run():
            try:
                parts = self.client.search_participants(query)
                self.searchReady.emit(token, query, parts)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fetch_event_odds(self, token: int, event_url: str, locations: dict):
        """Full per-bookmaker odds for one event. Aggregates across geos
        concurrently when >1 location is configured (more books); falls back to
        a single direct fetch otherwise."""
        def run():
            try:
                if locations and len(locations) > 1:
                    eo = OddsPortalClient.get_event_odds_multi(
                        event_url, locations, client_kwargs={"verbose": False})
                else:
                    eo = self.client.get_event_odds(event_url)
                self.eventOddsReady.emit(token, event_url, eo)
            except Exception as e:
                self.failed.emit(token, str(e))
        threading.Thread(target=run, daemon=True).start()


class ImageLoader(QObject):
    """Downloads Flashscore logos/headshots on daemon threads and emits the raw
    bytes back to the GUI thread (QPixmap must be built on the GUI thread). One
    in-flight request per URL; the widget owns the decoded-pixmap cache."""

    loaded = pyqtSignal(str, bytes)  # url, raw bytes (empty on failure)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inflight = set()
        self._sess = requests.Session()
        self._sess.headers["User-Agent"] = "Mozilla/5.0"

    def request(self, url: str):
        if url in self._inflight:
            return
        self._inflight.add(url)

        def run():
            data = b""
            try:
                r = self._sess.get(url, timeout=10)
                if r.status_code == 200:
                    data = r.content
            except Exception:
                data = b""
            self._inflight.discard(url)
            self.loaded.emit(url, data)
        threading.Thread(target=run, daemon=True).start()


class LiveScoresWidget(QWidget):
    def __init__(self, client: FlashscoreClient = None, parent=None):
        super().__init__(parent)
        self.client = client or FlashscoreClient(verbose=False)
        self.worker = ScheduleWorker(self.client)
        self.worker.scheduleReady.connect(self._on_schedule)
        self.worker.liveReady.connect(self._on_live)
        self.worker.detailReady.connect(self._on_detail)
        self.worker.golfReady.connect(self._on_golf)
        self.worker.participantsReady.connect(self._on_participants)
        self.worker.failed.connect(self._on_failed)

        # OddsPortal: odds movement (dropping feed) + participant search.
        self.odds_client = OddsPortalClient(verbose=False)
        self.odds_worker = OddsWorker(self.odds_client)
        self.odds_worker.droppingReady.connect(self._on_dropping)
        self.odds_worker.searchReady.connect(self._on_search)
        self.odds_worker.eventOddsReady.connect(self._on_event_odds)
        self.odds_worker.failed.connect(self._on_odds_failed)
        self._odds_locations = _oddsportal_locations()  # multi-geo book set

        # logo/headshot loading: bytes fetched off-thread, pixmaps built + cached
        # here on the GUI thread. _pending_icons maps a not-yet-loaded url to the
        # (render_gen, item, col) sites awaiting it; the gen guard drops sites
        # whose row was rebuilt by a newer render before the image arrived.
        self.image_loader = ImageLoader(self)
        self.image_loader.loaded.connect(self._on_image_loaded)
        self._pixmaps = {}              # url -> QPixmap (null pixmap = failed)
        self._pending_icons = {}        # url -> [(render_gen, item, col), ...]
        self._render_gen = 0

        self._mode = "scores"           # "scores" | "dropping" | "search"
        self._odds_fmt = "us"           # display format: us | dec | frac
        self._odds_token = 0            # request sequence guard for odds calls
        self._detail_token = 0          # guards stale per-event odds fetches
        self._detail_drop = None        # DroppingOdd backing the open detail panel
        self._drop_index = {}           # (norm_home, norm_away) -> DroppingOdd
        self._drops = []                # last dropping feed for current sport
        self._row_items = {}            # event_id -> QTreeWidgetItem (all H2H rows)
        self._search_results = []       # last participant search results

        self._token = 0                 # request sequence guard
        self._current_sport = None      # None => LIVE-all view
        self._events = []               # last loaded events for current view
        self._event_map = {}            # event_id -> Event (for lookups)
        self._league_filter = None      # tournament name to restrict to, or None
        self._progress_token = 0        # guards stale live-progress results
        self._live_items = {}           # event_id -> QTreeWidgetItem (live rows)
        self._golf_boards = []          # cached leaderboards (for in-place filter)
        self._participant_events = []   # cached participant events (for filter)
        # stale-while-revalidate cache: view-key -> (kind, data). Lets a sport
        # switch render instantly from the last result while a fresh fetch runs.
        self._cache = {}
        self._cache_days = 0
        self._suppress_progress = False  # skip live-progress fan-out on cached render

        self._build_ui()
        self._populate_nav()

        # auto-refresh
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self._apply_autorefresh()

        # initial selection + background prefetch of the priority sports so the
        # first switch to each is instant (served from cache).
        self._select_sport(DEFAULT_SPORT)
        self._prefetch_priority()

    # -- UI -----------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("Flashscore — Live Scores")
        self.resize(1000, 720)
        self.setStyleSheet(f"""
            QWidget {{ background-color: {C_BG}; color: {C_TEXT};
                       font-family: 'Segoe UI', sans-serif; font-size: 13px; }}
            QTreeWidget {{ background-color: {C_PANEL}; border: 1px solid #2a323c;
                           outline: 0; }}
            QTreeWidget::item {{ padding: 3px 2px; }}
            QTreeWidget::item:selected {{ background-color: {C_ACCENT}; color: #0b0e12; }}
            QHeaderView::section {{ background-color: {C_HEADER}; color: {C_DIM};
                                    border: none; padding: 4px; }}
            QComboBox, QSpinBox {{ background-color: {C_PANEL}; border: 1px solid #2a323c;
                                   padding: 3px 6px; border-radius: 3px; }}
            QPushButton {{ background-color: {C_HEADER}; border: 1px solid #3a444f;
                           padding: 4px 12px; border-radius: 3px; }}
            QPushButton:hover {{ background-color: {C_ACCENT}; color: #0b0e12; }}
            QPushButton#modeBtn {{ padding: 4px 14px; border-radius: 0;
                                   border-left: none; }}
            QPushButton#modeBtn:checked {{ background-color: {C_ACCENT};
                                           color: #0b0e12; font-weight: 600; }}
            QLineEdit {{ background-color: {C_PANEL}; border: 1px solid #2a323c;
                         padding: 4px 8px; border-radius: 3px; }}
            QLabel#title {{ font-size: 16px; font-weight: 600; }}
            QLabel#detail {{ background-color: {C_PANEL}; border: 1px solid #2a323c;
                             border-radius: 3px; padding: 8px; }}
        """)

        # left nav
        self.nav = QTreeWidget()
        self.nav.setHeaderHidden(True)
        self.nav.setFixedWidth(240)
        self.nav.itemClicked.connect(self._on_nav_clicked)
        self.nav.itemExpanded.connect(self._on_nav_expanded)

        # right header controls
        self.title = QLabel("Live Scores")
        self.title.setObjectName("title")

        # mode segmented control: Scores | Dropping Odds | Search
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self._mode_btns = {}
        mode_bar = QHBoxLayout()
        mode_bar.setSpacing(0)
        for key, label in (("scores", "Scores"),
                           ("dropping", "Dropping Odds"),
                           ("search", "Search")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setObjectName("modeBtn")
            b.clicked.connect(lambda _c, k=key: self._set_mode(k))
            self.mode_group.addButton(b)
            self._mode_btns[key] = b
            mode_bar.addWidget(b)
        self._mode_btns["scores"].setChecked(True)

        # search box (visible only in search mode)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search player / team…")
        self.search_edit.returnPressed.connect(self._run_search)
        self.search_edit.setVisible(False)
        self.search_edit.setMinimumWidth(220)

        # odds-format selector (we convert client-side; the feeds are decimal)
        self.fmt_combo = QComboBox()
        for label, _code in ODDS_FORMATS:
            self.fmt_combo.addItem(label)
        self.fmt_combo.setToolTip("Odds display format")
        self.fmt_combo.currentIndexChanged.connect(self._on_format_changed)

        self.days_spin = QSpinBox()
        self.days_spin.setRange(0, 14)
        self.days_spin.setValue(0)
        self.days_spin.setPrefix("+")
        self.days_spin.setSuffix(" d")
        self.days_spin.setToolTip("Days ahead to include in the schedule")
        self.days_spin.valueChanged.connect(self._on_days_changed)

        self.auto_chk = QCheckBox("Auto")
        self.auto_chk.setChecked(True)
        self.auto_chk.setToolTip("Auto-refresh")
        self.auto_chk.stateChanged.connect(self._apply_autorefresh)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 300)
        self.interval_spin.setValue(20)
        self.interval_spin.setSuffix("s")
        self.interval_spin.valueChanged.connect(self._apply_autorefresh)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {C_DIM};")

        controls = QHBoxLayout()
        controls.addWidget(self.title)
        controls.addSpacing(12)
        controls.addLayout(mode_bar)
        controls.addWidget(self.search_edit)
        controls.addStretch(1)
        controls.addWidget(QLabel("Odds:"))
        controls.addWidget(self.fmt_combo)
        controls.addWidget(QLabel("Window:"))
        controls.addWidget(self.days_spin)
        controls.addWidget(self.auto_chk)
        controls.addWidget(self.interval_spin)
        controls.addWidget(self.refresh_btn)

        # right results tree
        self.results = QTreeWidget()
        self.results.setColumnCount(4)
        self.results.setHeaderLabels(["When", "Home", "Score", "Away"])
        self.results.setRootIsDecorated(True)
        self.results.setIconSize(QSize(LOGO_PX, LOGO_PX))
        # align the Home/Score/Away headers with their (right/center/left) cells
        hitem = self.results.headerItem()
        hitem.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hitem.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
        hitem.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr = self.results.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.results.itemClicked.connect(self._on_result_clicked)

        # detail panel: full odds + open->current movement for the clicked event
        # (populated from the matched DroppingOdd). Hidden until a row is clicked.
        self.detail_lbl = QLabel("")
        self.detail_lbl.setObjectName("detail")
        self.detail_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.detail_lbl.setWordWrap(True)
        self.detail_lbl.setVisible(False)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(8, 8, 8, 8)
        rlay.addLayout(controls)
        rlay.addWidget(self.results)
        rlay.addWidget(self.detail_lbl)
        rlay.addWidget(self.status_lbl)

        splitter = QSplitter()
        splitter.addWidget(self.nav)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter)

    def _populate_nav(self):
        self.nav.clear()
        live_item = QTreeWidgetItem(["🔴  LIVE — All Sports"])
        live_item.setData(0, Qt.ItemDataRole.UserRole, LIVE_NODE)
        f = live_item.font(0); f.setBold(True); live_item.setFont(0, f)
        live_item.setForeground(0, QBrush(QColor(C_LIVE)))
        self.nav.addTopLevelItem(live_item)

        ordered = PRIORITY_SPORTS + [s for s in sorted(SPORT_IDS) if s not in PRIORITY_SPORTS]
        for sport in ordered:
            item = QTreeWidgetItem([sport.replace("_", " ").title()])
            item.setData(0, Qt.ItemDataRole.UserRole, sport)
            # placeholder child so the expander arrow shows; replaced on expand
            item.addChild(QTreeWidgetItem(["…"]))
            self.nav.addTopLevelItem(item)

    # -- navigation events --------------------------------------------------
    def _on_nav_clicked(self, item, _col):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key == LIVE_NODE:
            self._current_sport = None
            self._league_filter = None
            self.refresh()
        elif key and key.startswith("venue::"):
            self._league_filter = key.split("::", 1)[1]
            self._render_participants(self._participant_events, self._current_sport)
        elif key and key.startswith("league::"):
            self._league_filter = key.split("::", 1)[1]
            if self._mode == "dropping":
                self._render_dropping(self._drops)  # filter in place
            elif self._current_sport == "golf":
                self._render_golf(self._golf_boards)
            elif self._current_sport in PARTICIPANT_SPECS:
                self._render_participants(self._participant_events, self._current_sport)
            else:
                self._render()  # filter in place, no refetch
        elif key in SPORT_IDS:
            self._select_sport(key)

    def _on_nav_expanded(self, item):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key in SPORT_IDS:
            # selecting + expanding a sport loads its schedule; leagues get
            # filled in once results arrive (_refill_league_children).
            self._select_sport(key)

    def _select_sport(self, sport):
        self._current_sport = sport
        self._league_filter = None
        self.refresh()

    # -- data flow ----------------------------------------------------------
    def _cache_key(self):
        return self._current_sport if self._current_sport is not None else "__live__"

    def _render_from_cache(self, cached):
        """Render the previously-fetched data instantly (no progress fan-out)."""
        kind, data = cached
        self._suppress_progress = True
        try:
            if kind == "golf":
                self._golf_boards = data
                self._render_golf(data)
            elif kind == "part":
                self._participant_events = data
                self._render_participants(data, self._current_sport)
            else:  # "sched" or "live"
                self._events = data
                self._render()
        finally:
            self._suppress_progress = False

    # -- cross-source matching ---------------------------------------------
    def _oddsportal_sport(self):
        """OddsPortal url-name for the current sport, or None if unsupported."""
        if self._current_sport is None:
            return None
        return SPORT_BRIDGE.get(self._current_sport)

    def _build_drop_index(self, drops):
        """Index dropping odds by normalized (home, away) for row decoration."""
        idx = {}
        for d in drops:
            idx[(_norm_name(d.home), _norm_name(d.away))] = d
        self._drop_index = idx

    def _match_drop(self, e):
        """Find the DroppingOdd for a Flashscore Event (orientation-agnostic)."""
        if not self._drop_index or not e.home or not e.away:
            return None
        return (self._drop_index.get((_norm_name(e.home), _norm_name(e.away)))
                or self._drop_index.get((_norm_name(e.away), _norm_name(e.home))))

    def _score_index(self):
        """Index the current sport's cached schedule events by normalized
        (home, away), for annotating dropping-odds rows with live scores."""
        cached = self._cache.get(self._current_sport)
        events = cached[1] if cached and cached[0] == "sched" else []
        idx = {}
        for e in events:
            if e.home and e.away:
                idx[(_norm_name(e.home), _norm_name(e.away))] = e
        return idx

    # -- mode switching -----------------------------------------------------
    def _set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        btn = self._mode_btns.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)  # keep the segmented control in sync
        self.search_edit.setVisible(mode == "search")
        self.detail_lbl.setVisible(False)
        # window/auto-refresh controls are irrelevant in search mode
        for w in (self.days_spin, self.auto_chk, self.interval_spin):
            w.setEnabled(mode != "search")
        if mode == "search":
            self.search_edit.setFocus()
        self._apply_autorefresh()  # pauses the timer in search mode
        self.refresh()

    def _sport_label(self):
        return (self._current_sport or "all sports").replace("_", " ").title()

    def refresh(self):
        """Dispatch a refresh for the active mode. The shared left sport-nav
        selection (self._current_sport) drives every mode."""
        if self._mode == "dropping":
            self._refresh_dropping()
        elif self._mode == "search":
            self._refresh_search()
        else:
            self._refresh_scores()

    def _refresh_scores(self):
        self._token += 1
        cached = self._cache.get(self._cache_key())
        if cached is not None:
            self._render_from_cache(cached)  # instant; fresh data replaces it below
        else:
            self.status_lbl.setText("loading…")
        # kick the matching dropping-odds feed so score rows get movement badges
        self._refresh_drop_overlay()
        if self._current_sport is None:
            self.title.setText("🔴  LIVE — All Sports")
            self.worker.fetch_live_all(self._token)
        elif self._current_sport == "golf":
            self.title.setText("Golf")
            self.worker.fetch_golf(self._token, self.days_spin.value())
        elif self._current_sport in PARTICIPANT_SPECS:
            self.title.setText(self._current_sport.replace("_", " ").title())
            self.worker.fetch_participants(
                self._token, self._current_sport, self.days_spin.value())
        else:
            label = self._current_sport.replace("_", " ").title()
            self.title.setText(label)
            self.worker.fetch_schedule(self._token, self._current_sport, self.days_spin.value())

    def _refresh_drop_overlay(self):
        """Fetch the dropping feed for the current sport to overlay movement
        badges on score rows. No-op for LIVE-all and unsupported sports."""
        op_sport = self._oddsportal_sport()
        if op_sport is None:
            self._drop_index = {}
            return
        self._odds_token += 1
        self.odds_worker.fetch_dropping(self._odds_token, op_sport)

    def _refresh_dropping(self):
        op_sport = self._oddsportal_sport()
        self.title.setText(f"📉 Dropping Odds — {self._sport_label()}")
        if op_sport is None:
            self.results.clear()
            self.status_lbl.setText(
                "Dropping odds: pick a sport OddsPortal carries (e.g. tennis, "
                "football, basketball).")
            return
        self._odds_token += 1
        self.status_lbl.setText("loading dropping odds…")
        # also ensure the sport's schedule is cached so we can annotate scores
        if self._cache.get(self._current_sport) is None:
            self._token += 1
            self.worker.fetch_schedule(self._token, self._current_sport,
                                       self.days_spin.value())
        self.odds_worker.fetch_dropping(self._odds_token, op_sport)

    def _refresh_search(self):
        self.title.setText("🔎 Search — Historical Odds")
        q = self.search_edit.text().strip()
        if not q:
            self.results.clear()
            self.status_lbl.setText("Type a player or team name and press Enter.")
            return
        self._run_search()

    def _run_search(self):
        q = self.search_edit.text().strip()
        if not q:
            return
        self._mode = "search"
        self._mode_btns["search"].setChecked(True)
        self.search_edit.setVisible(True)
        self._odds_token += 1
        self.status_lbl.setText(f"searching “{q}”…")
        self.odds_worker.fetch_search(self._odds_token, q)

    def _on_days_changed(self, _value):
        # cached data is for the old window; drop it and re-warm the priority set
        self._cache.clear()
        self.refresh()
        self._prefetch_priority()

    def _prefetch_priority(self):
        """Warm the cache for the priority sports in the background (token -1 =
        cache-only, never rendered) so the first switch to each is instant."""
        for s in PRIORITY_SPORTS:
            if s in self._cache or s == self._current_sport:
                continue
            if s in PARTICIPANT_SPECS:
                self.worker.fetch_participants(-1, s, self.days_spin.value())
            else:
                self.worker.fetch_schedule(-1, s, self.days_spin.value())

    def _on_schedule(self, token, sport, events):
        self._cache[sport] = ("sched", events)  # cache even prefetched results
        if token != self._token or sport != self._current_sport:
            return
        if self._mode == "dropping":
            # schedule was fetched only to warm the score index; re-render the
            # dropping view so newly-available live scores get annotated.
            self._refill_league_children(sport, events)
            self._render_dropping(self._drops)
            return
        if self._mode != "scores":
            return
        self._events = events
        self._refill_league_children(sport, events)
        self._render()

    def _on_live(self, token, events):
        self._cache["__live__"] = ("live", events)
        if token != self._token or self._current_sport is not None:
            return
        self._events = events
        self._render()

    def _on_golf(self, token, boards):
        self._cache["golf"] = ("golf", boards)
        if token != self._token or self._current_sport != "golf":
            return
        self._golf_boards = boards
        self._render_golf(boards)
        # leagues = tournament names, for left-nav sub-navigation
        for i in range(self.nav.topLevelItemCount()):
            top = self.nav.topLevelItem(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == "golf":
                top.takeChildren()
                for b in boards:
                    child = QTreeWidgetItem([b.tournament])
                    child.setData(0, Qt.ItemDataRole.UserRole, f"league::{b.tournament}")
                    child.setForeground(0, QBrush(QColor(C_LIVE if b.is_live else C_DIM)))
                    top.addChild(child)
                break

    def _on_participants(self, token, sport, events):
        self._cache[sport] = ("part", events)
        if token != self._token or self._current_sport != sport:
            return
        self._participant_events = events
        self._render_participants(events, sport)
        # left-nav children: venues (nested sports) or events
        labels = []
        if PARTICIPANT_SPECS[sport]["nested"]:
            seen = set()
            for e in events:
                v = e.venue or e.title
                if v not in seen:
                    seen.add(v); labels.append((v, f"venue::{v}", e.is_live))
        else:
            labels = [(e.title, f"league::{e.title}", e.is_live) for e in events]
        for i in range(self.nav.topLevelItemCount()):
            top = self.nav.topLevelItem(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == sport:
                top.takeChildren()
                for text, key, live in labels:
                    child = QTreeWidgetItem([text])
                    child.setData(0, Qt.ItemDataRole.UserRole, key)
                    child.setForeground(0, QBrush(QColor(C_LIVE if live else C_DIM)))
                    top.addChild(child)
                break

    def _on_failed(self, token, msg):
        if token in (self._token, self._progress_token):
            self.status_lbl.setText(f"error: {msg}")

    # -- OddsPortal handlers ------------------------------------------------
    def _on_dropping(self, token, sport_urlname, drops):
        if token != self._odds_token:
            return  # stale (newer odds request superseded this)
        self._drops = drops
        self._build_drop_index(drops)
        if self._mode == "dropping":
            self._render_dropping(drops)
        else:  # scores mode: overlay movement badges on the existing rows
            self._decorate_score_rows()

    def _on_search(self, token, query, participants):
        if token != self._odds_token or self._mode != "search":
            return
        self._search_results = participants
        self._render_search(query, participants)

    def _on_odds_failed(self, token, msg):
        if token == self._odds_token and self._mode in ("dropping", "search"):
            self.status_lbl.setText(f"odds error: {msg}")

    # -- per-row live progress ---------------------------------------------
    def _fetch_live_progress(self):
        """Pull granular progress for the live rows and inject it into the
        'When' column. Bounded by MAX_PROGRESS_FETCHES; rows beyond the cap
        keep the '● LIVE' placeholder."""
        self._progress_token += 1
        token = self._progress_token
        for eid in list(self._live_items)[:MAX_PROGRESS_FETCHES]:
            e = self._event_map.get(eid)
            if e:
                self.worker.fetch_detail(token, e.sport_id, e.event_id)

    def _on_detail(self, token, event_id, detail):
        if token != self._progress_token:
            return  # stale (a newer refresh superseded this)
        item = self._live_items.get(event_id)
        e = self._event_map.get(event_id)
        if not item or not e:
            return
        text = format_progress(e.sport_id, detail.get("decoded", {}))
        item.setText(0, f"● {text}" if text and text != "LIVE" else "● LIVE")

    def _refill_league_children(self, sport, events):
        # find the sport's nav item and replace its children with real leagues
        for i in range(self.nav.topLevelItemCount()):
            top = self.nav.topLevelItem(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == sport:
                top.takeChildren()
                leagues = sorted({e.tournament for e in events if e.tournament})
                for lg in leagues:
                    child = QTreeWidgetItem([lg])
                    child.setData(0, Qt.ItemDataRole.UserRole, f"league::{lg}")
                    child.setForeground(0, QBrush(QColor(C_DIM)))
                    top.addChild(child)
                break

    # -- rendering ----------------------------------------------------------
    _ALIGN = {
        "l": Qt.AlignmentFlag.AlignLeft, "c": Qt.AlignmentFlag.AlignCenter,
        "r": Qt.AlignmentFlag.AlignRight,
    }

    def _configure_columns(self, layout):
        """layout: list of (label, align 'l'/'c'/'r', stretch bool[, width int]).

        A fixed `width` pins the column — needed for the first column, whose
        auto-size otherwise inflates to fit the full-width spanned group headers
        (venue/race/section rows), pushing the data columns far to the right.
        """
        self.results.setColumnCount(len(layout))
        self.results.setHeaderLabels([c[0] for c in layout])
        hdr = self.results.header()
        hitem = self.results.headerItem()
        for i, col in enumerate(layout):
            label, al, stretch = col[0], col[1], col[2]
            width = col[3] if len(col) > 3 else None
            hitem.setTextAlignment(i, self._ALIGN[al] | Qt.AlignmentFlag.AlignVCenter)
            if stretch:
                hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            elif width:
                hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
                self.results.setColumnWidth(i, width)
            else:
                hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)

    def _set_headers(self, golf: bool):
        if golf:
            self._configure_columns([
                ("Pos", "c", False), ("Player", "l", True),
                ("To Par", "c", False), ("Thru", "c", False)])
        else:
            self._configure_columns([
                ("When", "c", False), ("Home", "r", True),
                ("Score", "c", False), ("Away", "l", True),
                ("Move", "c", False, 96)])  # OddsPortal dropping-odds overlay

    def _render_golf(self, boards):
        self.results.clear()
        self._live_items = {}
        self._set_headers(golf=True)
        if self._league_filter:
            boards = [b for b in boards if b.tournament == self._league_filter]

        n_live = sum(1 for b in boards if b.is_live)
        for b in boards:
            color = C_LIVE if b.is_live else (C_ACCENT if b.stage == "scheduled" else C_DIM)
            tag = {"live": "🔴 LIVE", "scheduled": "UPCOMING", "finished": "FINISHED"}.get(b.stage, "")
            header = QTreeWidgetItem([f"{b.tournament}   —   {tag}"])
            f = header.font(0); f.setBold(True); header.setFont(0, f)
            header.setForeground(0, QBrush(QColor(color)))
            header.setFirstColumnSpanned(True)
            self.results.addTopLevelItem(header)
            # detect tied positions to prefix "T"
            counts = {}
            for e in b.entries:
                counts[e.position] = counts.get(e.position, 0) + 1
            for e in b.entries:
                header.addChild(self._golf_item(e, tied=counts.get(e.position, 0) > 1))
            header.setExpanded(b.is_live)  # live boards open, others collapsed

        stamp = datetime.now().strftime("%H:%M:%S")
        self.status_lbl.setText(
            f"{len(boards)} tournaments · {n_live} live · updated {stamp}"
        )

    def _golf_item(self, e, tied):
        pos = e.position or ""
        if tied and pos:
            pos = f"T{pos}"
        if e.thru == 18:
            thru = "F"
        elif e.thru is not None:
            thru = str(e.thru)
        elif e.tee_ts:
            thru = datetime.fromtimestamp(e.tee_ts).strftime("%H:%M")
        else:
            thru = "-"
        to_par = format_to_par(e.to_par)
        player = e.player or ""
        if e.country:
            player = f"{player}  ({e.country})"
        item = QTreeWidgetItem([pos, player, to_par, thru])
        item.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)
        # color the to-par: under par red-hot, over par dim
        if to_par.startswith("-"):
            item.setForeground(2, QBrush(QColor(C_LIVE)))
            ff = item.font(2); ff.setBold(True); item.setFont(2, ff)
        elif to_par in ("E", "") or to_par.startswith("+"):
            item.setForeground(2, QBrush(QColor(C_DIM)))
        return item

    def _render_participants(self, events, sport):
        spec = PARTICIPANT_SPECS[sport]
        cols = spec["cols"]
        self.results.clear()
        self._live_items = {}
        self._configure_columns([(h, al, al == "l") for (h, al, _fn) in cols])

        def add_entries(parent, entries):
            for e in entries:
                row = [str(fn(e)) for (_h, _al, fn) in cols]
                child = QTreeWidgetItem(row)
                for i, (_h, al, _fn) in enumerate(cols):
                    child.setTextAlignment(i, self._ALIGN[al] | Qt.AlignmentFlag.AlignVCenter)
                parent.addChild(child)

        def stage_tag(stg):
            return {"live": "🔴 LIVE", "scheduled": "UPCOMING",
                    "finished": "FINISHED"}.get(stg, "")

        def stage_color(stg):
            return {"live": C_LIVE, "scheduled": C_ACCENT}.get(stg, C_DIM)

        n_live = 0
        if spec["nested"]:
            # group races under their venue/meeting, ordered by start time
            from collections import OrderedDict
            venues = OrderedDict()
            for e in events:
                if self._league_filter and (e.venue or e.title) != self._league_filter:
                    continue
                venues.setdefault((e.country, e.venue or e.title), []).append(e)
            for (country, venue), races in venues.items():
                races.sort(key=lambda r: r.start_ts or 0)
                v_live = any(r.is_live for r in races)
                n_live += sum(1 for r in races if r.is_live)
                vhead = QTreeWidgetItem([f"{country}: {venue}" if country else venue])
                f = vhead.font(0); f.setBold(True); vhead.setFont(0, f)
                vhead.setForeground(0, QBrush(QColor(C_LIVE if v_live else C_TEXT)))
                vhead.setFirstColumnSpanned(True)
                self.results.addTopLevelItem(vhead)
                for r in races:
                    t = datetime.fromtimestamp(r.start_ts).strftime("%H:%M") if r.start_ts else "--:--"
                    extra = f"  ·  {r.info}" if r.info else ""
                    rhead = QTreeWidgetItem([f"{t}  {r.race_label or r.title}{extra}  —  {stage_tag(r.stage)}"])
                    rhead.setForeground(0, QBrush(QColor(stage_color(r.stage))))
                    rhead.setFirstColumnSpanned(True)
                    vhead.addChild(rhead)
                    add_entries(rhead, r.entries)
                    rhead.setExpanded(r.is_live)
                vhead.setExpanded(True)
        else:
            evs = [e for e in events
                   if not self._league_filter or e.title == self._league_filter]
            evs.sort(key=lambda e: (0 if e.is_live else 1, e.start_ts or 0))
            n_live = sum(1 for e in evs if e.is_live)
            for e in evs:
                head = QTreeWidgetItem([f"{e.title}  —  {stage_tag(e.stage)}"])
                f = head.font(0); f.setBold(True); head.setFont(0, f)
                head.setForeground(0, QBrush(QColor(stage_color(e.stage))))
                head.setFirstColumnSpanned(True)
                self.results.addTopLevelItem(head)
                add_entries(head, e.entries)
                head.setExpanded(e.is_live)

        stamp = datetime.now().strftime("%H:%M:%S")
        self.status_lbl.setText(
            f"{len(events)} events · {n_live} live · updated {stamp}")

    def _render(self):
        self.results.clear()
        self._render_gen += 1
        self._pending_icons.clear()
        self._set_headers(golf=False)
        self._live_items = {}
        self._row_items = {}
        events = self._events
        self._event_map = {e.event_id: e for e in events}
        if self._league_filter:
            events = [e for e in events if e.tournament == self._league_filter]

        live = [e for e in events if e.stage == "live"]
        upcoming = [e for e in events if e.stage == "scheduled" or e.stage == ""]
        finished = [e for e in events if e.stage == "finished"]
        live.sort(key=lambda e: (e.tournament or "", e.start_ts or 0))
        upcoming.sort(key=lambda e: (e.start_ts or 0, e.tournament or ""))
        finished.sort(key=lambda e: (e.start_ts or 0, e.tournament or ""))

        if self._current_sport is None:
            # LIVE-all view: only in-play events, grouped by sport
            self._render_grouped(live, key=lambda e: e.sport.replace("_", " ").title(),
                                  section="LIVE", color=C_LIVE)
        else:
            self._render_grouped(live, key=lambda e: e.tournament or "—",
                                 section="🔴 LIVE NOW", color=C_LIVE)
            self._render_grouped(upcoming, key=lambda e: e.tournament or "—",
                                 section="UPCOMING", color=C_ACCENT)
            self._render_grouped(finished, key=lambda e: e.tournament or "—",
                                 section="FINISHED", color=C_DIM)

        self.results.expandAll()
        stamp = datetime.now().strftime("%H:%M:%S")
        self.status_lbl.setText(
            f"{len(live)} live · {len(upcoming)} upcoming · "
            f"{len(finished)} finished · updated {stamp}"
        )
        if self._live_items and not self._suppress_progress:
            self._fetch_live_progress()

    def _render_grouped(self, events, key, section, color):
        if not events:
            return
        section_item = QTreeWidgetItem([f"{section}  ({len(events)})"])
        f = section_item.font(0); f.setBold(True); section_item.setFont(0, f)
        section_item.setForeground(0, QBrush(QColor(color)))
        section_item.setFirstColumnSpanned(True)
        self.results.addTopLevelItem(section_item)

        # group within the section
        groups = {}
        for e in events:
            groups.setdefault(key(e), []).append(e)
        for gname in sorted(groups):
            gitem = QTreeWidgetItem([gname])
            gitem.setForeground(0, QBrush(QColor(C_DIM)))
            gitem.setFirstColumnSpanned(True)
            section_item.addChild(gitem)
            for e in groups[gname]:
                gitem.addChild(self._event_item(e))

    def _event_item(self, e: Event):
        if e.is_live:
            when = "● LIVE"
        elif e.start_ts:
            when = datetime.fromtimestamp(e.start_ts).strftime("%a %H:%M")
        else:
            when = e.status or ""
        if e.home_score not in (None, "") and e.away_score not in (None, ""):
            score = f"{e.home_score} - {e.away_score}"
        else:
            score = "vs" if not e.is_live else "-"
        item = QTreeWidgetItem([when, e.home or "", score, e.away or "", ""])
        # Home hugs the right, Away hugs the left, so the score sits centered
        # between the two team names rather than drifting to the far right.
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        if e.note:
            item.setToolTip(0, e.note)  # e.g. series score / aggregate
        if e.is_live:
            item.setForeground(0, QBrush(QColor(C_LIVE)))
            item.setForeground(2, QBrush(QColor(C_LIVE)))
            f = item.font(2); f.setBold(True); item.setFont(2, f)
        else:
            item.setForeground(0, QBrush(QColor(C_DIM)))
        item.setData(0, Qt.ItemDataRole.UserRole, e.event_id)
        if e.is_live:
            self._live_items[e.event_id] = item  # progress fills col 0 later
        self._row_items[e.event_id] = item       # for async odds-badge overlay
        self._apply_drop_badge(item, e)
        self._attach_logos(item, e)
        return item

    # -- odds overlay (dropping-odds movement on score rows) ----------------
    # -- odds-format display (client-side; feeds are always decimal) --------
    def _oddval(self, v):
        """Format one decimal odd in the user's chosen display format."""
        return format_odd(v, self._odds_fmt)

    def _drop_odds_str(self, d: DroppingOdd) -> str:
        """DroppingOdd.odds_str but in the active display format (the dropped
        outcome's open→now first, then the rest)."""
        moved = d.dropped_outcome
        parts = []
        if moved:
            parts.append(f"{moved['name']}: {self._oddval(moved['prev_odd'])}"
                         f"→{self._oddval(moved['odd'])}")
        for o in d.outcomes:
            if o is moved or o.get("odd") is None:
                continue
            parts.append(f"{o['name']} {self._oddval(o['odd'])}")
        return "  |  ".join(parts)

    def _on_format_changed(self, idx):
        self._odds_fmt = ODDS_FORMATS[idx][1]
        # re-render the current view from cached data (no refetch needed)
        if self._mode == "dropping":
            self._render_dropping(self._drops)
        elif self._mode == "scores":
            self._render()
        if self._detail_drop is not None:
            self._show_drop_detail(self._detail_drop)

    def _apply_drop_badge(self, item, e):
        """Stamp the Move column of a score row with the matched dropping-odds
        movement (e.g. '▼27%'), red, full price move in the tooltip."""
        d = self._match_drop(e)
        if d is None:
            return
        pct = d.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        item.setText(4, f"{arrow}{abs(pct):.0f}%")
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        f = item.font(4); f.setBold(True); item.setFont(4, f)
        item.setToolTip(4, f"{d.betting_type}: {self._drop_odds_str(d)}  "
                           f"({d.bookies} books)")

    def _decorate_score_rows(self):
        """Re-apply odds badges to all current score rows (after a fresh
        dropping feed arrives asynchronously)."""
        for eid, item in self._row_items.items():
            e = self._event_map.get(eid)
            if e is not None:
                self._apply_drop_badge(item, e)

    # -- logos / headshots --------------------------------------------------
    def _attach_logos(self, item, ev):
        """Put the home/away team logo (or player headshot) on a row's Home/Away
        columns. Served from cache instantly; otherwise fetched async and filled
        in when the bytes arrive."""
        if ev is None:
            return
        self._attach_one_logo(item, 1, getattr(ev, "home_logo", None))
        self._attach_one_logo(item, 3, getattr(ev, "away_logo", None))

    def _attach_one_logo(self, item, col, logo):
        if not logo:
            return
        url = LOGO_BASE + logo
        pm = self._pixmaps.get(url)
        if pm is not None:
            if not pm.isNull():
                item.setIcon(col, QIcon(pm))
            return
        self._pending_icons.setdefault(url, []).append((self._render_gen, item, col))
        self.image_loader.request(url)

    def _on_image_loaded(self, url, data):
        pm = QPixmap()
        if data:
            pm.loadFromData(data)
        if not pm.isNull():
            pm = pm.scaled(LOGO_PX, LOGO_PX, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._pixmaps[url] = pm  # cache even a null result (don't refetch failures)
        for gen, item, col in self._pending_icons.pop(url, []):
            # gen guard skips rows from a superseded render; sip.isdeleted guards
            # against the C++ QTreeWidgetItem already being freed by results.clear()
            # (setIcon on a freed item segfaults — not a catchable RuntimeError).
            if gen != self._render_gen or pm.isNull() or sip.isdeleted(item):
                continue
            item.setIcon(col, QIcon(pm))

    # -- dropping-odds mode (market-first, score-annotated) -----------------
    def _render_dropping(self, drops):
        """Render the sport's dropping-odds feed grouped Tournament → Event →
        Market. Each event collapses its many dropping markets (1X2, AH, O/U,
        DNB, HT/FT…) under one row showing the live score + the worst move; the
        child rows name the market and show its open→now odds."""
        self.results.clear()
        self._render_gen += 1
        self._pending_icons.clear()
        self._live_items = {}
        self._row_items = {}
        self._configure_columns([
            ("When / Market", "l", False, 230), ("Home", "r", True),
            ("Score", "c", False), ("Away", "l", True),
            ("Move", "c", False, 70), ("Odds (open→now)", "l", True)])
        score_idx = self._score_index()

        if self._league_filter:
            drops = [d for d in drops if d.tournament == self._league_filter]
        # tournament -> event(home,away) -> [markets]
        from collections import OrderedDict
        tours = OrderedDict()
        for d in drops:
            tours.setdefault(d.tournament or "—", OrderedDict()) \
                 .setdefault((d.home or "", d.away or ""), []).append(d)

        def evt_worst(markets):
            return min(x.drop_pct for x in markets)

        def tour_worst(events):
            return min(evt_worst(m) for m in events.values())

        ordered = sorted(tours.items(), key=lambda kv: tour_worst(kv[1]))
        n_matched = n_events = 0
        for tname, events in ordered:
            thead = QTreeWidgetItem([f"{tname}  ({len(events)})"])
            tf = thead.font(0); tf.setBold(True); thead.setFont(0, tf)
            thead.setForeground(0, QBrush(QColor(C_DIM)))
            thead.setFirstColumnSpanned(True)
            self.results.addTopLevelItem(thead)
            for (home, away), markets in sorted(
                    events.items(), key=lambda kv: evt_worst(kv[1])):
                markets.sort(key=lambda d: d.drop_pct)
                ev = (score_idx.get((_norm_name(home), _norm_name(away)))
                      or score_idx.get((_norm_name(away), _norm_name(home))))
                thead.addChild(self._dropping_event_item(markets, ev))
                n_events += 1
                if ev is not None:
                    n_matched += 1
            thead.setExpanded(True)

        stamp = datetime.now().strftime("%H:%M:%S")
        self.status_lbl.setText(
            f"{n_events} events · {len(drops)} dropping markets · "
            f"{n_matched} matched to live data · updated {stamp}")

    def _dropping_event_item(self, markets, ev=None):
        """One event row carrying its dropping markets as children."""
        d0 = markets[0]
        worst = min(markets, key=lambda d: d.drop_pct)
        when = d0.time or "—"
        if ev is not None and ev.is_live:
            when = "● LIVE"
        if ev is not None and ev.home_score not in (None, "") \
                and ev.away_score not in (None, ""):
            score = f"{ev.home_score} - {ev.away_score}"
        else:
            score = "—"
        pct = worst.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        n = len(markets)
        item = QTreeWidgetItem([
            when, d0.home or "", score, d0.away or "",
            f"{arrow}{abs(pct):.0f}%",
            f"{n} market{'s' if n != 1 else ''} dropping"])
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter | vc)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        ef = item.font(1); ef.setBold(True)
        item.setFont(1, ef); item.setFont(3, ef)
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        mf = item.font(4); mf.setBold(True); item.setFont(4, mf)
        item.setForeground(5, QBrush(QColor(C_DIM)))
        if ev is not None and ev.is_live:
            item.setForeground(0, QBrush(QColor(C_LIVE)))
            item.setForeground(2, QBrush(QColor(C_LIVE)))
            sf = item.font(2); sf.setBold(True); item.setFont(2, sf)
        # event row drills into full odds via the worst-moving market's url
        item.setData(0, Qt.ItemDataRole.UserRole, ("drop", worst))
        self._attach_logos(item, ev)
        for d in markets:
            item.addChild(self._market_item(d))
        return item

    def _market_item(self, d: DroppingOdd):
        """A single dropping market under an event: name + drop% + odds move."""
        pct = d.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        item = QTreeWidgetItem([f"    {d.betting_type or '—'}", "", "", "",
                                f"{arrow}{abs(pct):.0f}%", self._drop_odds_str(d)])
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        item.setForeground(0, QBrush(QColor(C_TEXT)))
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        item.setForeground(5, QBrush(QColor(C_TEXT)))
        item.setToolTip(5, f"{d.betting_type}  ·  {d.bookies} books  ·  "
                           f"max {self._oddval(d.max_odds)} ({d.max_provider or '—'})")
        item.setData(0, Qt.ItemDataRole.UserRole, ("drop", d))
        return item

    # -- search mode (participant resolution) -------------------------------
    def _render_search(self, query, participants):
        self.results.clear()
        self._live_items = {}
        self._row_items = {}
        self._configure_columns([
            ("Sport", "l", False), ("Name", "l", True),
            ("Country", "l", False), ("Events", "c", False)])
        for p in participants:
            item = QTreeWidgetItem([
                (p.get("sport") or "").replace("-", " ").title(),
                p.get("name", ""), p.get("country", ""),
                str(len(p.get("event_ids") or []))])
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)
            item.setData(0, Qt.ItemDataRole.UserRole, ("participant", p))
            self.results.addTopLevelItem(item)
        stamp = datetime.now().strftime("%H:%M:%S")
        note = "" if participants else "  (no matches)"
        self.status_lbl.setText(
            f"“{query}”: {len(participants)} participants{note} · {stamp}  ·  "
            f"full per-book odds available by clicking events in Scores/Dropping; "
            f"participant→match listing still JS-gated")

    # -- detail panel (full per-bookmaker odds for the clicked event) -------
    def _on_result_clicked(self, item, _col):
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        d = None
        if isinstance(payload, tuple) and payload[0] == "drop":
            d = payload[1]
        elif isinstance(payload, str):  # score row -> event_id
            e = self._event_map.get(payload)
            if e is not None:
                d = self._match_drop(e)
        if d is None:
            self.detail_lbl.setVisible(False)
            self._detail_drop = None
            return
        self._detail_drop = d
        self._show_drop_detail(d)
        # then pull the full per-bookmaker odds (multi-geo) for this event
        if d.event_url:
            self._detail_token += 1
            n = len(self._odds_locations)
            note = (f"loading odds across {n} geos…" if n > 1 else "loading odds…")
            self.detail_lbl.setText(
                self.detail_lbl.text()
                + f"<br><span style='color:{C_DIM}'>{note}</span>")
            self.odds_worker.fetch_event_odds(
                self._detail_token, d.event_url, self._odds_locations)

    def _on_event_odds(self, token, event_url, eo):
        if token != self._detail_token or eo is None:
            return
        self._render_event_odds(eo)

    def _render_event_odds(self, eo: EventOdds):
        """Full per-bookmaker odds: best/avg/opening + drift + every book's
        price, best first. Books are unioned across the configured geos."""
        sub = eo.tournament or eo.sport.replace("-", " ").title()
        lines = [f"<b>{_html_escape(eo.home)}</b> v <b>{_html_escape(eo.away)}</b>"
                 f"  —  <span style='color:{C_DIM}'>{_html_escape(sub)}</span>"]
        for o in eo.outcomes:
            drift = o.drift
            dtxt = ""
            if drift is not None and abs(drift) >= 0.005:
                col = C_LIVE if drift < 0 else C_ACCENT
                ar = "▼" if drift < 0 else "▲"
                dtxt = f"  <span style='color:{col}'>{ar}{abs(drift) * 100:.0f}%</span>"
            lines.append(
                f"<b>{_html_escape(o.name)}</b>:  max <b>{self._oddval(o.max_odds)}</b> "
                f"<span style='color:{C_DIM}'>({_html_escape(o.max_book or '—')})</span>"
                f"  ·  avg {self._oddval(o.avg_odds)}  ·  open {self._oddval(o.opening_avg)}{dtxt}"
                f"  ·  <span style='color:{C_DIM}'>{o.n_books} books</span>")
            books = sorted(o.books.items(), key=lambda kv: kv[1], reverse=True)
            bstr = "   ".join(f"{_html_escape(b)} {self._oddval(v)}" for b, v in books)
            if bstr:
                lines.append(f"<span style='color:{C_DIM}'>&nbsp;&nbsp;{bstr}</span>")
        self.detail_lbl.setText("<br>".join(lines))
        self.detail_lbl.setVisible(True)

    def _show_drop_detail(self, d: DroppingOdd):
        """Show the full odds breakdown + open→current movement for an event."""
        moved = d.dropped_outcome
        lines = [f"<b>{_html_escape(d.home)}</b> v <b>{_html_escape(d.away)}</b>"
                 f"  —  <span style='color:{C_DIM}'>{_html_escape(d.tournament)}</span>"]
        lines.append(f"<span style='color:{C_DIM}'>{_html_escape(d.betting_type)} · "
                     f"{d.bookies} books · max {self._oddval(d.max_odds)} "
                     f"({_html_escape(d.max_provider or '—')})</span>")
        cells = []
        for o in d.outcomes:
            name = _html_escape(str(o.get("name")))
            cur = o.get("odd")
            if o is moved and o.get("prev_odd") is not None:
                col = C_LIVE if d.drop_pct < 0 else C_ACCENT
                cells.append(f"<b>{name}</b>: <span style='color:{C_DIM}'>"
                             f"{self._oddval(o['prev_odd'])}</span> → <span style='color:{col}'>"
                             f"<b>{self._oddval(cur)}</b></span>  ({d.drop})")
            elif cur is not None:
                cells.append(f"{name}: <b>{self._oddval(cur)}</b>")
        lines.append("  &nbsp;|&nbsp;  ".join(cells))
        self.detail_lbl.setText("<br>".join(lines))
        self.detail_lbl.setVisible(True)

    # -- auto-refresh -------------------------------------------------------
    def _apply_autorefresh(self):
        if self.auto_chk.isChecked() and self._mode != "search":
            self.refresh_timer.start(self.interval_spin.value() * 1000)
        else:
            self.refresh_timer.stop()


def main():
    app = QApplication(sys.argv)
    w = LiveScoresWidget()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
