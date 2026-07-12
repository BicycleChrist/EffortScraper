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

import hashlib
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime
from html import escape as _html_escape

import requests
from concurrent.futures import ThreadPoolExecutor

from PyQt6 import sip
from PyQt6.QtCore import Qt, QObject, QTimer, QSize, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont, QPixmap, QIcon, QPainter
from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QLabel, QSplitter, QHeaderView,
    QLineEdit, QButtonGroup, QFrame, QStyledItemDelegate, QStyleOptionViewItem,
)

from flashscore_client import (
    FlashscoreClient, SPORT_IDS, Event, format_progress, format_to_par,
)
from OddsPortalClient import (
    OddsPortalClient, DroppingOdd, EventOdds, SearchMatch, format_odd,
)

# Odds display format (OddsPortal feeds are always decimal; we convert). Maps
# the format-selector combo labels to format_odd() codes. Fractional exists in
# the client (to_fractional, for horse racing) but isn't offered in the toggle.
ODDS_FORMATS = [("American", "us"), ("Decimal", "dec")]

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
    includes the direct (no-proxy) location. Falls back to
    Creds.ODDSPORTAL_PROXIES (same comma-separated string, or a dict) when the
    env var isn't set, so the app doesn't depend on shell environment."""
    locs = {"direct": None}
    raw = os.environ.get("ODDSPORTAL_PROXIES", "")
    if not raw:
        try:
            import Creds
            cfg = getattr(Creds, "ODDSPORTAL_PROXIES", "") or ""
            if isinstance(cfg, dict):
                locs.update({str(k): str(v) for k, v in cfg.items()})
                return locs
            raw = str(cfg)
        except ImportError:
            pass
    for i, spec in enumerate(s.strip() for s in raw.split(",") if s.strip()):
        if "=" in spec:
            label, proxy = spec.split("=", 1)
        else:
            label, proxy = f"geo{i + 1}", spec
        locs[label.strip()] = proxy.strip()
    return locs


# geo (multi-proxy) odds clients: short read timeout so one slow free proxy
# bounds the whole merged fetch instead of stalling it to the default 20s —
# a geo that can't answer in 6s is dropped from that click's merge.
GEO_CLIENT_KWARGS = {"verbose": False, "timeout": 6}


def _webshare_locations():
    """{label: proxy_url} pulled live from the Webshare proxy API. Free-tier
    proxy IPs rotate, so they're fetched fresh each run instead of hardcoded.
    Keeps ONE proxy per non-US country: the direct connection already covers
    the US book set, and extra same-geo proxies add latency, not books.
    Returns {} when no WEBSHARE_API_KEY is configured; raises on API failure
    (caller treats it as best-effort)."""
    try:
        import Creds
        key = getattr(Creds, "WEBSHARE_API_KEY", "") or ""
    except ImportError:
        key = ""
    key = os.environ.get("WEBSHARE_API_KEY", "") or key
    if not key:
        return {}
    r = requests.get(
        "https://proxy.webshare.io/api/v2/proxy/list/",
        params={"mode": "direct", "page_size": 100, "valid": "true"},
        headers={"Authorization": f"Token {key}"}, timeout=10)
    r.raise_for_status()
    locs = {}
    for p in r.json().get("results", []):
        cc = (p.get("country_code") or "").upper()
        if not p.get("valid") or cc in ("", "US") or cc.lower() in locs:
            continue
        locs[cc.lower()] = (f"http://{p['username']}:{p['password']}"
                            f"@{p['proxy_address']}:{p['port']}")
    return locs

# colors
C_BG = "#14181d"
C_PANEL = "#1b2128"
C_LIVE = "#e5484d"          # muted signal red (was a hotter #ff4d4d)
C_TEXT = "#d7dde3"
C_DIM = "#8b97a3"
C_HEADER = "#2a323c"
C_ACCENT = "#4da3ff"

# Custom item-data role marking a row as live. Drives the slim accent-bar
# delegate that replaced the old red-bulb glyphs ("●"/"🔴").
LIVE_ROLE = Qt.ItemDataRole.UserRole + 1

# Role carrying a signed move % (float) on a Move cell. Drives the heat-bar
# delegate: the cell gets a translucent pill whose width/intensity scale with
# the magnitude, so a column of drops reads like a heat map at a glance.
MOVE_ROLE = Qt.ItemDataRole.UserRole + 2
MOVE_BAR_CAP = 50.0  # |%| that renders a full-width bar

# Braille spinner frames for loading rows / status text (quiet, fixed-width).
_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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
        # bounded shared pool: the progress fan-out alone used to spawn up to
        # ~80 fresh threads per refresh tick
        self._pool = ThreadPoolExecutor(max_workers=16,
                                        thread_name_prefix="fs-sched")

    def fetch_schedule(self, token: int, sport: str, days_ahead: int):
        def run():
            try:
                days = list(range(0, days_ahead + 1))
                sched = self.client.get_schedule(sports=[sport], days=days)
                events = sched.get(sport, [])
                self.scheduleReady.emit(token, sport, events)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def fetch_live_all(self, token: int):
        def run():
            try:
                live = self.client.snapshot_live(sports=list(SPORT_IDS.keys()))
                self.liveReady.emit(token, live)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def fetch_golf(self, token: int, day: int):
        def run():
            try:
                self.golfReady.emit(token, self.client.get_golf_leaderboards(day))
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def fetch_participants(self, token: int, sport: str, day: int):
        def run():
            try:
                evs = self.client.get_participant_events(SPORT_IDS[sport], day)
                self.participantsReady.emit(token, sport, evs)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def fetch_detail(self, token: int, sport_id: int, event_id: str):
        def run():
            try:
                # lean: fetches only the sub-feed(s) the progress cell needs
                detail = self.client.get_live_progress(sport_id, event_id)
                self.detailReady.emit(token, event_id, detail)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)


class OddsWorker(QObject):
    """Runs blocking OddsPortalClient calls on daemon threads and emits results.

    Mirrors ScheduleWorker's token-guard pattern so stale results are dropped.
    """

    droppingReady = pyqtSignal(int, str, list)  # token, sport_urlname, [DroppingOdd]
    eventOddsReady = pyqtSignal(int, str, object)  # token, event_url, EventOdds
    searchMatchesReady = pyqtSignal(int, str, list)  # token, query, [SearchMatch]
    oddsPrefetched = pyqtSignal(str, object)    # event_url, EventOdds (hover warm)
    failed = pyqtSignal(int, str)               # token, message

    def __init__(self, client: OddsPortalClient):
        super().__init__()
        self.client = client
        self._pool = ThreadPoolExecutor(max_workers=8,
                                        thread_name_prefix="op-odds")

    def fetch_dropping(self, token: int, sport_urlname: str):
        def run():
            try:
                drops = self.client.dropping_odds_pages(
                    sport_urlname, DROPPING_PERIOD, DROPPING_BS,
                    max_pages=DROPPING_MAX_PAGES)
                self.droppingReady.emit(token, sport_urlname, drops)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def fetch_search_matches(self, token: int, query: str, sport=None):
        """Resolve a free-text query to a participant's historical matches
        (with inline odds + event urls) via the OddsPortal search surface.
        This is the past-results source: Flashscore only serves a ±2wk window,
        so anything older is reached here."""
        def run():
            try:
                ms = self.client.search_matches(
                    query, results=True, pages=1, sport=sport)
                self.searchMatchesReady.emit(token, query, ms)
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def _staged_odds(self, event_url: str, locations: dict, emit):
        """Fetch odds with staged delivery: with multiple geos configured, emit
        the fast direct result first (~1s, US books) so the UI renders
        immediately, then emit again with the multi-geo merged book set once
        the (slow, free) proxies land. Raises only if nothing was fetched."""
        if not locations or len(locations) <= 1:
            emit(self.client.get_event_odds(event_url))
            return
        delivered = False
        direct = OddsPortalClient._geo_client(None, GEO_CLIENT_KWARGS)
        try:
            eo = direct.get_event_odds(event_url)
            if eo.outcomes:
                emit(eo)
                delivered = True
        except Exception:
            pass
        try:  # direct result is cached, so the merge re-reads it for free
            emit(OddsPortalClient.get_event_odds_multi(
                event_url, locations, client_kwargs=GEO_CLIENT_KWARGS))
        except Exception:
            if not delivered:
                raise

    def fetch_event_odds(self, token: int, event_url: str, locations: dict):
        """Full per-bookmaker odds for one event, staged direct-first then
        multi-geo merged (see _staged_odds)."""
        def run():
            try:
                self._staged_odds(
                    event_url, locations,
                    lambda eo: self.eventOddsReady.emit(token, event_url, eo))
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)

    def prefetch_event_odds(self, event_url: str, locations: dict):
        """Warm an event's odds in the background (on hover) so the click that
        follows renders instantly. Results are delivered via oddsPrefetched
        (direct first, then merged — each overwrites the cache) and cached by
        the widget; failures are swallowed (it's speculative)."""
        def run():
            try:
                self._staged_odds(
                    event_url, locations,
                    lambda eo: self.oddsPrefetched.emit(event_url, eo))
            except Exception:
                pass
        self._pool.submit(run)

    def resolve_and_fetch_odds(self, token: int, home: str, away: str,
                               sport, start_ts, locations: dict):
        """For a listed game with no dropping-odds match: resolve the matchup to
        its OddsPortal page via participant search, then fetch full per-book odds
        (multi-geo). Emits eventOddsReady on success so the detail panel reuses
        the same render path; silent no-op (failed signal) if unresolved."""
        def run():
            try:
                # pages=1: the clicked game is always near-term, so it lives on
                # the first page of upcoming/results — halves the fetch rounds
                m = self.client.resolve_event_url(
                    home, away, sport=sport, start_ts=start_ts, pages=1)
                if m is None or not m.url:
                    self.failed.emit(token, f"no OddsPortal match for {home} v {away}")
                    return
                url = m.url.split("#")[0]
                self._staged_odds(
                    url, locations,
                    lambda eo: self.eventOddsReady.emit(token, url, eo))
            except Exception as e:
                self.failed.emit(token, str(e))
        self._pool.submit(run)


class ImageLoader(QObject):
    """Downloads Flashscore logos/headshots on daemon threads and emits the raw
    bytes back to the GUI thread (QPixmap must be built on the GUI thread). One
    in-flight request per URL; the widget owns the decoded-pixmap cache.

    A persistent on-disk byte cache (keyed by a hash of the URL) means logos
    load instantly on every run after the first — no network at all on a hit."""

    loaded = pyqtSignal(str, bytes)  # url, raw bytes (empty on failure)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inflight = set()
        self._pool = ThreadPoolExecutor(max_workers=6,
                                        thread_name_prefix="img")
        self._sess = requests.Session()
        self._sess.headers["User-Agent"] = "Mozilla/5.0"
        self._dir = os.path.join(
            os.path.expanduser("~"), ".cache", "effortodds", "logos")
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError:
            self._dir = None

    def _path(self, url):
        if not self._dir:
            return None
        h = hashlib.sha1(url.encode()).hexdigest()
        return os.path.join(self._dir, h + ".img")

    def request(self, url: str):
        if url in self._inflight:
            return
        self._inflight.add(url)

        def run():
            data = b""
            path = self._path(url)
            try:
                if path and os.path.exists(path):  # disk-cache hit: no network
                    with open(path, "rb") as fh:
                        data = fh.read()
                else:
                    r = self._sess.get(url, timeout=10)
                    if r.status_code == 200:
                        data = r.content
                        if path and data:
                            tmp = path + ".tmp"
                            with open(tmp, "wb") as fh:
                                fh.write(data)
                            os.replace(tmp, path)  # atomic
            except Exception:
                data = b""
            self._inflight.discard(url)
            self.loaded.emit(url, data)
        self._pool.submit(run)


class _RightDecorationDelegate(QStyledItemDelegate):
    """Draw the cell's icon on the trailing (right) edge instead of the leading
    edge. Used on the Home column, whose team name is right-aligned toward the
    score: with the default left decoration the logo strands at the far cell
    edge when the column is wide (fullscreen); on the right it stays snug beside
    the name. Columns without an icon are unaffected."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.decorationPosition = QStyleOptionViewItem.Position.Right


class _LiveAccentDelegate(QStyledItemDelegate):
    """Paints a slim rounded accent bar along the left edge of live rows —
    the quiet replacement for the old red-bulb glyph. Rows opt in by setting
    LIVE_ROLE truthy on column 0."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not index.data(LIVE_ROLE):
            return
        r = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C_LIVE))
        painter.drawRoundedRect(
            QRectF(r.left() + 1.0, r.top() + 3.0, 3.0, r.height() - 6.0),
            1.5, 1.5)
        painter.restore()


class _MoveBarDelegate(QStyledItemDelegate):
    """Heat-bar for Move cells: a translucent rounded pill grows from the left
    edge with the magnitude of the move (capped at MOVE_BAR_CAP%), red for
    drops / blue for rises, with the ▼/▲ % text painted on top. Cells without
    MOVE_ROLE data render normally."""

    def paint(self, painter, option, index):
        pct = index.data(MOVE_ROLE)
        if isinstance(pct, (int, float)) and pct:
            r = option.rect
            frac = min(abs(pct), MOVE_BAR_CAP) / MOVE_BAR_CAP
            c = QColor(C_LIVE if pct < 0 else C_ACCENT)
            c.setAlphaF(0.14 + 0.22 * frac)  # deeper move = hotter cell
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(
                QRectF(r.left() + 2.0, r.top() + 2.0,
                       max(6.0, (r.width() - 4.0) * frac), r.height() - 4.0),
                3.0, 3.0)
            painter.restore()
        super().paint(painter, option, index)


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
        self.odds_worker.eventOddsReady.connect(self._on_event_odds)
        self.odds_worker.searchMatchesReady.connect(self._on_search_matches)
        self.odds_worker.oddsPrefetched.connect(self._on_odds_prefetched)
        self.odds_worker.failed.connect(self._on_odds_failed)
        self._odds_locations = _oddsportal_locations()  # multi-geo book set
        # Webshare proxies are discovered async so startup never blocks on
        # their API; until (unless) they land, fetches run direct-only.
        threading.Thread(target=self._load_webshare_locations,
                         daemon=True).start()

        # hover-prefetch: warm an event's odds while the mouse rests on its row so
        # the click renders instantly (the fetch is ~0.5s otherwise). Keyed by
        # event_url; short debounce so sweeping the mouse doesn't spam requests.
        self._odds_prefetch = {}        # event_url -> (ts, EventOdds)
        self._prefetch_inflight = set() # event_urls with a warm fetch running
        self._prefetch_url = None       # row the mouse is currently resting on
        self._prefetch_ttl = 90         # seconds a prefetched result stays usable
        self._resolved_urls = {}        # (home, away) -> OddsPortal event url
        self._pending_resolve_key = None  # matchup awaiting a resolve fetch
        # last-known granular progress ("2nd Inning", "Q3"...) per live event.
        # Survives re-renders so a refresh never flashes the row back to the
        # bare "LIVE" placeholder while the async progress fetch re-runs.
        self._progress_cache = {}       # event_id -> progress text
        # scores-mode dropping overlay: cache the feed per sport for 60s so an
        # auto-refresh tick doesn't re-pull up to 10 feed pages every 20s
        self._drop_overlay_cache = {}   # op_sport -> (ts, [DroppingOdd])

        # historical (past-results) search over OddsPortal: Search mode folds
        # these in below the live in-memory hits. Stale-while-revalidate cache
        # keyed by normalized query so re-renders don't re-hit the network.
        self._hist_token = 0            # guards stale historical-search results
        self._hist_cache = {}           # norm-query -> [SearchMatch]
        self._hist_matches = {}         # event_url -> SearchMatch (click lookup)
        self._hist_fetched_q = None     # norm-query with a fetch already issued

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
        self._detail_resolving = False  # awaiting a name-resolve odds fetch
        self._odds_target_item = None   # tree row to hang the inline odds under
        self._open_odds_key = None      # (kind, id) of the row whose odds are open,
                                        # so a refresh re-render can restore them
        self._open_odds_eo = None       # last EventOdds rendered (restore w/o refetch)
        self._drop_index = {}           # (norm_home, norm_away) -> DroppingOdd
        self._drops = []                # last dropping feed for current sport
        self._row_items = {}            # event_id -> QTreeWidgetItem (all H2H rows)

        self._token = 0                 # request sequence guard
        self._current_sport = None      # None => LIVE-all view
        self._events = []               # last loaded events for current view
        self._event_map = {}            # event_id -> Event (for lookups)
        self._league_filter = None      # tournament name to restrict to, or None
        self._progress_token = 0        # guards stale live-progress results
        self._live_items = {}           # event_id -> QTreeWidgetItem (live rows)
        self._golf_rows = {}            # tournament -> LIVE-all golf agg row
        self._scores_skeleton = None    # structure fingerprint of the last full
                                        # scores rebuild; a matching refresh
                                        # updates rows in place instead
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

        # search-as-you-type debounce (local index search is instant, but a
        # debounce avoids re-rendering on every keystroke)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._apply_text_filter)
        self._all_sports_warmed = False

        # hover-prefetch debounce: fire only once the mouse rests on a row
        self._prefetch_debounce = QTimer(self)
        self._prefetch_debounce.setSingleShot(True)
        self._prefetch_debounce.timeout.connect(self._do_prefetch)

        # loading spinner (status bar + inline loading rows): one shared timer
        # cycles braille frames; rows/status register and are pruned when the
        # real content replaces them.
        self._spin_rows = []            # [(item, col, text), ...]
        self._spin_ix = 0
        self._loading_status = None     # busy text for the status bar, or None
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(90)
        self._spin_timer.timeout.connect(self._tick_spinner)

        # update-flash: changed cells (score ticks) get a brief background
        # highlight that fades out — feedback without a full-row repaint.
        self._flash_cells = {}          # (id(item), col) -> (item, col, t0)
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(40)
        self._flash_timer.timeout.connect(self._tick_flashes)

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
        """)

        # left nav
        self.nav = QTreeWidget()
        self.nav.setHeaderHidden(True)
        self.nav.setFixedWidth(240)
        self.nav.itemClicked.connect(self._on_nav_clicked)
        self.nav.itemExpanded.connect(self._on_nav_expanded)
        self.nav.setItemDelegateForColumn(0, _LiveAccentDelegate(self.nav))

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
        self.search_edit.setPlaceholderText("Search team / player / league…")
        self.search_edit.returnPressed.connect(self._apply_text_filter)
        self.search_edit.textChanged.connect(lambda _t: self._search_debounce.start(220))
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
        # Home column (col 1) name is right-aligned toward the score; keep its
        # logo on the trailing edge so it hugs the name instead of stranding far
        # left when the column stretches (fullscreen).
        self._home_icon_delegate = _RightDecorationDelegate(self.results)
        self.results.setItemDelegateForColumn(1, self._home_icon_delegate)
        # live rows carry a slim accent bar on the left edge (col 0)
        self._live_delegate = _LiveAccentDelegate(self.results)
        self.results.setItemDelegateForColumn(0, self._live_delegate)
        # Move cells (col 4 in scores AND dropping layouts) render as heat bars
        self._move_delegate = _MoveBarDelegate(self.results)
        self.results.setItemDelegateForColumn(4, self._move_delegate)
        self.results.setUniformRowHeights(True)  # big QTreeView layout win
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
        self.results.itemCollapsed.connect(self._on_item_collapsed)
        # hover-prefetch: needs mouse tracking for itemEntered to fire
        self.results.setMouseTracking(True)
        self.results.itemEntered.connect(self._on_item_hover)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(8, 8, 8, 8)
        rlay.addLayout(controls)
        rlay.addWidget(self.results)
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
        live_item = QTreeWidgetItem(["LIVE — All Sports"])
        live_item.setData(0, Qt.ItemDataRole.UserRole, LIVE_NODE)
        live_item.setData(0, LIVE_ROLE, True)
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

    def _all_score_index(self):
        """Index EVERY cached schedule's events by normalized (home, away), for
        annotating the all-sports dropping aggregation with live scores. Uses
        whatever schedules are already cached (priority prefetch + search warm);
        rows for uncached sports simply show their date/time instead."""
        idx = {}
        for kind, data in self._cache.values():
            if kind != "sched":
                continue
            for e in data:
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
        # the search box doubles as a live filter in Dropping mode (all-sports
        # aggregation) and the query box in Search mode. Clear it on switch so
        # each mode starts unfiltered (block signals to avoid a spurious render).
        show_box = mode in ("search", "dropping")
        self.search_edit.setVisible(show_box)
        self.search_edit.setPlaceholderText(
            "Filter dropping odds — team / league / sport…" if mode == "dropping"
            else "Search team / player / league…")
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        # window/auto-refresh controls are irrelevant in search mode
        for w in (self.days_spin, self.auto_chk, self.interval_spin):
            w.setEnabled(mode != "search")
        if show_box:
            self.search_edit.setFocus()
        self._apply_autorefresh()  # pauses the timer in search mode
        self.refresh()

    def _apply_text_filter(self):
        """Debounced text-box handler. In Search mode it drives the local event
        search; in Dropping mode it re-filters the loaded all-sports aggregation
        in place (no refetch)."""
        if self._mode == "search":
            self._search_local()
        elif self._mode == "dropping":
            self._render_dropping(self._drops)

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
            self._status_busy("loading…")
        # kick the matching dropping-odds feed so score rows get movement badges
        self._refresh_drop_overlay()
        if self._current_sport is None:
            self.title.setText("LIVE — All Sports")
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
        hit = self._drop_overlay_cache.get(op_sport)
        if hit and time.time() - hit[0] < 60:
            self._build_drop_index(hit[1])  # fresh enough; skip the refetch
            return
        self._odds_token += 1
        self.odds_worker.fetch_dropping(self._odds_token, op_sport)

    def _refresh_dropping(self):
        # One aggregation across ALL sports (OddsPortal sport "0"), searchable via
        # the filter box — no need to navigate per-sport.
        self.title.setText("Dropping Odds — All Sports")
        self._odds_token += 1
        self._status_busy("loading dropping odds (all sports)…")
        self.odds_worker.fetch_dropping(self._odds_token, "0")

    def _refresh_search(self):
        self.title.setText("Search — All Events")
        # warm the full event index once (all head-to-head sports, cached) so
        # the local search covers everything; results fill in as they arrive.
        self._warm_all_sports()
        self._search_local()

    def _warm_all_sports(self):
        """Background-load every head-to-head sport's schedule into the cache so
        the local search index is comprehensive. Cached + threaded; runs once."""
        if self._all_sports_warmed:
            return
        self._all_sports_warmed = True
        for s in SPORT_IDS:
            if s in self._cache or s in PARTICIPANT_SPECS or s == "golf":
                continue
            self.worker.fetch_schedule(-1, s, self.days_spin.value())

    def _hold_scroll(self, sig):
        """Re-apply the current scroll position after the pending rebuild
        settles, but ONLY when re-rendering the same view (`sig` unchanged) —
        auto-refresh must never yank the list back to the top, while switching
        sport/mode/filter still starts at the top like a fresh page."""
        vsb = self.results.verticalScrollBar()
        if getattr(self, "_last_view_sig", None) == sig:
            pos = vsb.value()
            QTimer.singleShot(0, lambda: vsb.setValue(min(pos, vsb.maximum())))
        self._last_view_sig = sig

    def _search_local(self):
        """Instant search over the in-memory event index (all cached schedules).
        Returns real matches — scores, logos, live state — clickable for odds.
        No network per keystroke; the index is warmed in the background."""
        if self._mode != "search":
            return
        q = _norm_name(self.search_edit.text())
        self._hold_scroll(("search", q))
        self.results.clear()
        self._scores_skeleton = None
        self._render_gen += 1
        self._pending_icons.clear()
        self._set_headers(golf=False)
        self._live_items = {}
        self._row_items = {}
        if not q or len(q) < 2:
            self._event_map = {}
            self._status_ready(
                "Type a team, player, or league name (2+ chars).")
            return
        # scan every cached schedule, dedupe by event id
        pool, seen = [], set()
        for _key, (kind, data) in self._cache.items():
            if kind != "sched":
                continue
            for e in data:
                if e.event_id in seen or not (e.home or e.away):
                    continue
                hay = _norm_name(
                    f"{e.home} {e.away} {e.tournament} {e.country or ''}")
                if q in hay:
                    seen.add(e.event_id)
                    pool.append(e)
        self._event_map = {e.event_id: e for e in pool}
        pool.sort(key=lambda e: (
            0 if e.is_live else (1 if e.stage in ("scheduled", "") else 2),
            e.start_ts or 0))
        sgroups = {}
        for e in pool[:300]:
            sgroups.setdefault(e.sport.replace("_", " ").title(), []).append(e)
        self._render_grouped([(g, sgroups[g]) for g in sorted(sgroups)],
                             section="MATCHES", color=C_ACCENT)
        # fold in OddsPortal historical results (past matches + closing odds).
        # Served instantly from cache when we have it for this query; otherwise
        # kick a background fetch that appends the section when it arrives.
        raw_q = self.search_edit.text().strip()
        hk = _norm_name(raw_q)
        if hk in self._hist_cache:
            self._render_hist_section(self._hist_cache[hk])
        elif len(raw_q) >= 3 and hk != self._hist_fetched_q:
            # not cached and no fetch already issued for this query (a background
            # sport-warm re-render must not re-launch the same historical fetch)
            self._hist_fetched_q = hk
            self._hist_token += 1
            self.odds_worker.fetch_search_matches(self._hist_token, raw_q)
        self.results.expandAll()
        self._restore_open_odds()  # keep a clicked-open breakdown across re-render
        stamp = datetime.now().strftime("%H:%M:%S")
        warming = "" if self._n_cached_sports() >= 18 else "  · indexing more sports…"
        self._status_ready(
            f"“{self.search_edit.text().strip()}”: {len(pool)} matches"
            f"{warming} · {stamp}")
        if self._live_items and not self._suppress_progress:
            self._fetch_live_progress()

    # -- historical (past-results) search, folded into Search mode ----------
    def _on_search_matches(self, token, query, matches):
        """OddsPortal historical matches arrived for a search query. Cache them
        (for instant re-render + click lookup) and, if still the active query,
        render the PAST RESULTS section beneath the live hits."""
        if token != self._hist_token:
            return  # a newer query superseded this
        self._hist_cache[_norm_name(query)] = matches
        for m in matches:
            if m.url:
                self._hist_matches[m.url.split("#")[0]] = m
        if self._mode != "search":
            return
        if _norm_name(self.search_edit.text()) != _norm_name(query):
            return  # user typed on; this result is for an older query string
        self._render_hist_section(matches)
        self.results.expandAll()
        self._restore_open_odds()  # a past-result row may be the open breakdown

    def _render_hist_section(self, matches):
        """(Re)build the 'PAST RESULTS — OddsPortal' section: historical matches
        grouped by tournament, newest first, each showing closing odds. Kept as
        a distinct top-level section so it can be replaced without disturbing the
        live in-memory hits above it. Click a row for the full per-book odds."""
        r = self.results
        for i in range(r.topLevelItemCount()):
            it = r.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == "__hist__":
                r.takeTopLevelItem(i)
                break
        if not matches:
            return
        section = QTreeWidgetItem([f"PAST RESULTS — OddsPortal  ({len(matches)})"])
        section.setData(0, Qt.ItemDataRole.UserRole, "__hist__")
        f = section.font(0); f.setBold(True); section.setFont(0, f)
        section.setForeground(0, QBrush(QColor(C_ACCENT)))
        section.setFirstColumnSpanned(True)
        r.addTopLevelItem(section)
        groups = {}
        for m in matches:
            groups.setdefault(m.tournament or "—", []).append(m)
        for gname in sorted(groups):
            gitem = QTreeWidgetItem([gname])
            gitem.setForeground(0, QBrush(QColor(C_DIM)))
            gitem.setFirstColumnSpanned(True)
            section.addChild(gitem)
            for m in sorted(groups[gname],
                            key=lambda x: x.start_ts or 0, reverse=True):
                gitem.addChild(self._search_match_item(m))
        section.setExpanded(True)

    def _search_match_item(self, m: SearchMatch):
        when = (datetime.fromtimestamp(m.start_ts).strftime("%Y-%m-%d")
                if m.start_ts else (m.status or ""))
        finished = (m.status or "").lower() == "finished"
        score = m.result or ("-" if finished else "vs")
        item = QTreeWidgetItem([when, m.home or "", score, m.away or "", ""])
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter | vc)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        item.setForeground(0, QBrush(QColor(C_DIM)))
        cell, tip = self._search_match_odds(m)
        if cell:
            item.setText(4, cell)
            item.setForeground(4, QBrush(QColor(C_TEXT)))
            if tip:
                item.setToolTip(4, tip)
        item.setData(0, Qt.ItemDataRole.UserRole, ("opmatch", m))
        return item

    def _search_match_odds(self, m: SearchMatch):
        """Compact closing odds (best price per outcome, user's format) for a
        historical match row. Picks the primary market (fewest-id betting type
        with >=2 outcomes). Returns (cell_text, tooltip)."""
        if not m.odds:
            return "", ""
        groups = {}
        for o in m.odds:
            groups.setdefault((o.betting_type_id, o.scope_id), []).append(o)
        best = None
        for key, outs in groups.items():
            if len(outs) >= 2 and (best is None
                                   or (key[0] or 99) < (best[0][0] or 99)):
                best = (key, outs)
        if best is None:
            return "", ""
        outs = sorted(best[1], key=lambda o: o.outcome_result_id or 0)
        vals = [self._oddval(o.max_odds) for o in outs if o.max_odds]
        if not vals:
            return "", ""
        n = max((o.bookmaker_count or 0) for o in outs)
        return " / ".join(vals), f"closing odds · {n} books"

    def _n_cached_sports(self):
        return sum(1 for v in self._cache.values() if v[0] == "sched")

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
        if self._mode == "search":
            # the index just grew — refresh results (debounced so a burst of
            # background sport-loads coalesces into one re-render)
            self._search_debounce.start(150)
            return
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
            self._status_ready(f"error: {msg}")

    # -- OddsPortal handlers ------------------------------------------------
    def _on_dropping(self, token, sport_urlname, drops):
        if token != self._odds_token:
            return  # stale (newer odds request superseded this)
        self._drop_overlay_cache[sport_urlname] = (time.time(), drops)
        self._drops = drops
        self._build_drop_index(drops)
        if self._mode == "dropping":
            self._render_dropping(drops)
        else:  # scores mode: overlay movement badges on the existing rows
            self._decorate_score_rows()

    def _on_odds_failed(self, token, msg):
        if token == self._detail_token:
            # a per-event / resolve odds fetch failed
            self._detail_resolving = False
            if self._odds_target_item is not None \
                    and not sip.isdeleted(self._odds_target_item):
                self._odds_target_item.takeChildren()
                miss = QTreeWidgetItem(["  no OddsPortal odds found for this match"])
                miss.setForeground(0, QBrush(QColor(C_DIM)))
                self._odds_target_item.addChild(miss)
                miss.setFirstColumnSpanned(True)  # after addChild so it sticks
                self._odds_target_item.setExpanded(True)
            return
        if token == self._odds_token and self._mode == "dropping":
            self._status_ready(f"odds error: {msg}")

    # -- per-row live progress ---------------------------------------------
    def _fetch_live_progress(self):
        """Pull granular progress for the live rows and inject it into the
        'When' column. Bounded by MAX_PROGRESS_FETCHES, viewport rows first —
        what the user can see gets granular progress before anything below the
        fold (previously "first N in insertion order", which starved off-screen
        rows entirely). Rows beyond the cap keep the 'LIVE' placeholder."""
        self._progress_token += 1
        token = self._progress_token
        vp = self.results.viewport().rect()
        vis, rest = [], []
        for eid, item in self._live_items.items():
            if sip.isdeleted(item):
                continue
            r = self.results.visualItemRect(item)
            (vis if (r.isValid() and r.intersects(vp)) else rest).append(eid)
        for eid in (vis + rest)[:MAX_PROGRESS_FETCHES]:
            e = self._event_map.get(eid)
            if e:
                self.worker.fetch_detail(token, e.sport_id, e.event_id)

    def _on_detail(self, token, event_id, detail):
        if token != self._progress_token:
            return  # stale (a newer refresh superseded this)
        item = self._live_items.get(event_id)
        e = self._event_map.get(event_id)
        if not item or not e or sip.isdeleted(item):
            return
        text = format_progress(e.sport_id, detail.get("decoded", {}))
        label = text if text and text != "LIVE" else "LIVE"
        self._progress_cache[event_id] = label
        if item.text(0) != label:  # avoid no-op repaints on steady state
            item.setText(0, label)

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
        for i in range(len(layout)):
            self.results.setColumnHidden(i, False)  # undo any view-specific hide
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
        self._hold_scroll(("golf", self._league_filter))
        self.results.clear()
        self._scores_skeleton = None
        self._live_items = {}
        self._set_headers(golf=True)
        if self._league_filter:
            boards = [b for b in boards if b.tournament == self._league_filter]

        n_live = sum(1 for b in boards if b.is_live)
        for b in boards:
            color = C_LIVE if b.is_live else (C_ACCENT if b.stage == "scheduled" else C_DIM)
            tag = {"live": "LIVE", "scheduled": "UPCOMING", "finished": "FINISHED"}.get(b.stage, "")
            header = QTreeWidgetItem([f"{b.tournament}   —   {tag}"])
            f = header.font(0); f.setBold(True); header.setFont(0, f)
            header.setForeground(0, QBrush(QColor(color)))
            header.setData(0, LIVE_ROLE, b.is_live)
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
        self._status_ready(
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
        self._hold_scroll(("participants", sport))
        self.results.clear()
        self._scores_skeleton = None
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
            return {"live": "LIVE", "scheduled": "UPCOMING",
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
        self._status_ready(
            f"{len(events)} events · {n_live} live · updated {stamp}")

    def _render(self):
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
        n_live = len(live)

        # LIVE-all: golf rows aren't head-to-head events (each player parses as
        # a fake matchup showing "-"); collapse them into one navigable row per
        # live tournament instead.
        golf_groups = []
        if self._current_sport is None:
            golf_live = [e for e in live if e.sport == "golf"]
            live = [e for e in live if e.sport != "golf"]
            by_tour = {}
            for e in golf_live:
                by_tour.setdefault(e.tournament or "Golf", []).append(e)
            golf_groups = sorted(by_tour.items())

        sections = self._build_sections(live, upcoming, finished)

        # Structural fingerprint of the view. When it matches the last full
        # rebuild, update the existing rows IN PLACE (progress/score/badge
        # cells) instead of tearing the tree down: no flicker, no scroll yank,
        # inline odds children survive, and a steady-state auto-refresh tick
        # does ~10x less work.
        skeleton = (
            ("scores", self._current_sport, self._league_filter),
            tuple((sec, tuple((g, tuple(e.event_id for e in evs))
                              for g, evs in groups))
                  for sec, _c, groups in sections),
            tuple(t for t, _ in golf_groups),
        )
        if skeleton == self._scores_skeleton:
            self._update_scores_in_place(sections, golf_groups)
        else:
            self._rebuild_scores(sections, golf_groups, skeleton)

        stamp = datetime.now().strftime("%H:%M:%S")
        self._status_ready(
            f"{n_live} live · {len(upcoming)} upcoming · "
            f"{len(finished)} finished · updated {stamp}"
        )
        if self._live_items and not self._suppress_progress:
            self._fetch_live_progress()

    def _build_sections(self, live, upcoming, finished):
        """[(section_label, color, [(group_label, [events])])] for the current
        view. The same structure drives full rebuilds, in-place updates, and
        the skeleton fingerprint that picks between them."""
        def grouped(evs, key):
            groups = {}
            for e in evs:
                groups.setdefault(key(e), []).append(e)
            return [(g, groups[g]) for g in sorted(groups)]

        if self._current_sport is None:
            # LIVE-all view: only in-play events, grouped by sport + the actual
            # league/tournament being played (not just the bare sport name).
            def _live_key(e):
                sport = e.sport.replace("_", " ").title()
                return f"{sport}  ·  {e.tournament}" if e.tournament else sport
            secs = [("LIVE", C_LIVE, grouped(live, _live_key))]
        else:
            def tkey(e):
                return e.tournament or "—"
            secs = [("LIVE", C_LIVE, grouped(live, tkey)),
                    ("UPCOMING", C_ACCENT, grouped(upcoming, tkey)),
                    ("FINISHED", C_DIM, grouped(finished, tkey))]
        return [(s, c, g) for s, c, g in secs if g]

    def _rebuild_scores(self, sections, golf_groups, skeleton):
        self._hold_scroll(skeleton[0])
        self.results.setUpdatesEnabled(False)
        try:
            self.results.clear()
            self._render_gen += 1
            self._pending_icons.clear()
            self._set_headers(golf=False)
            # the Move column only carries data when a dropping-odds feed
            # exists for this view (not LIVE-all / unbridged sports)
            self.results.setColumnHidden(4, self._oddsportal_sport() is None)
            self._live_items = {}
            self._row_items = {}
            self._golf_rows = {}
            for sec, color, groups in sections:
                self._render_grouped(groups, section=sec, color=color)
            self._render_golf_aggregate(golf_groups)
            self.results.expandAll()
        finally:
            self.results.setUpdatesEnabled(True)
        self._scores_skeleton = skeleton
        # drop progress strings for events that are no longer listed live
        self._progress_cache = {k: v for k, v in self._progress_cache.items()
                                if k in self._live_items}
        self._restore_open_odds()  # keep a clicked-open breakdown across refresh

    def _update_scores_in_place(self, sections, golf_groups):
        """Same structure as the last rebuild: refresh only the dynamic cells."""
        for _sec, _color, groups in sections:
            for _g, evs in groups:
                for e in evs:
                    item = self._row_items.get(e.event_id)
                    if item is not None and not sip.isdeleted(item):
                        self._update_event_item(item, e)
        for tour, evs in golf_groups:
            item = self._golf_rows.get(tour)
            if item is not None and not sip.isdeleted(item):
                item.setText(0, self._golf_agg_text(tour, evs))

    def _render_grouped(self, groups, section, color):
        """groups: [(group_label, [events])], pre-sorted."""
        if not groups:
            return
        n = sum(len(evs) for _g, evs in groups)
        section_item = QTreeWidgetItem([f"{section}  ({n})"])
        f = section_item.font(0); f.setBold(True); section_item.setFont(0, f)
        section_item.setForeground(0, QBrush(QColor(color)))
        section_item.setFirstColumnSpanned(True)
        self.results.addTopLevelItem(section_item)
        for gname, evs in groups:
            gitem = QTreeWidgetItem([gname])
            gitem.setForeground(0, QBrush(QColor(C_DIM)))
            gitem.setFirstColumnSpanned(True)
            section_item.addChild(gitem)
            for e in evs:
                gitem.addChild(self._event_item(e))

    # -- LIVE-all golf aggregation -------------------------------------------
    def _render_golf_aggregate(self, golf_groups):
        """One spanned row per live golf tournament (player count + current
        leader) that clicks through to the full leaderboard, replacing the old
        wall of per-player pseudo-matchup rows."""
        if not golf_groups:
            return
        sec = QTreeWidgetItem([f"GOLF — LIVE  ({len(golf_groups)})"])
        f = sec.font(0); f.setBold(True); sec.setFont(0, f)
        sec.setForeground(0, QBrush(QColor(C_LIVE)))
        self.results.addTopLevelItem(sec)
        sec.setFirstColumnSpanned(True)
        for tour, evs in golf_groups:
            row = QTreeWidgetItem([self._golf_agg_text(tour, evs)])
            row.setData(0, Qt.ItemDataRole.UserRole, ("golfnav", tour))
            row.setData(0, LIVE_ROLE, True)
            row.setToolTip(0, "Open the full leaderboard")
            sec.addChild(row)
            row.setFirstColumnSpanned(True)
            self._golf_rows[tour] = row

    @staticmethod
    def _golf_agg_text(tour, evs):
        # golf schedule rows carry score-to-par in home_score (AG)
        best = leader = None
        for e in evs:
            try:
                v = int((e.home_score or "").strip())
            except ValueError:
                continue
            if best is None or v < best:
                best, leader = v, e.home
        txt = f"{tour}   ·   {len(evs)} players on course"
        if leader:
            txt += f"   ·   leader {leader} {format_to_par(str(best))}"
        return txt + "   ›"

    def _event_texts(self, e: Event):
        """(when, score) display strings for a score row — shared by the
        builder and the in-place updater so they can never drift."""
        if e.is_live:
            # seed from the last-known progress so a rebuild doesn't flash the
            # row back to "LIVE" while the async progress fetch re-runs
            when = self._progress_cache.get(e.event_id, "LIVE")
        elif e.start_ts:
            when = datetime.fromtimestamp(e.start_ts).strftime("%a %H:%M")
        else:
            when = e.status or ""
        if e.home_score not in (None, "") and e.away_score not in (None, ""):
            score = f"{e.home_score} - {e.away_score}"
        else:
            score = "vs" if not e.is_live else "-"
        return when, score

    def _update_event_item(self, item, e: Event):
        """Refresh one existing score row's dynamic cells (progress, score,
        odds badge) in place; flash the score cell when it changes."""
        when, score = self._event_texts(e)
        if item.text(0) != when:
            item.setText(0, when)
        if item.text(2) != score:
            item.setText(2, score)
            self._flash_cell(item, 2)
        if e.note:
            item.setToolTip(0, e.note)
        self._apply_drop_badge(item, e)

    def _event_item(self, e: Event):
        when, score = self._event_texts(e)
        item = QTreeWidgetItem([when, e.home or "", score, e.away or "", ""])
        item.setData(0, LIVE_ROLE, e.is_live)
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

    def _apply_drop_badge(self, item, e):
        """Stamp the Move column of a score row with the matched dropping-odds
        movement (e.g. '▼27%') + heat bar; full price move in the tooltip."""
        d = self._match_drop(e)
        if d is None:
            if item.data(4, MOVE_ROLE):  # drop fell out of the feed: clear
                item.setText(4, "")
                item.setData(4, MOVE_ROLE, None)
                item.setToolTip(4, "")
            return
        pct = d.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        item.setText(4, f"{arrow}{abs(pct):.0f}%")
        item.setData(4, MOVE_ROLE, pct)
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        f = item.font(4); f.setBold(True); item.setFont(4, f)
        item.setToolTip(4, f"{self._mkt_label(d.betting_type)}: {self._drop_odds_str(d)}  "
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
    def _dropping_event_key(self, d):
        return (_norm_name(d.home or ""), _norm_name(d.away or ""))

    def _dropping_expanded_keys(self):
        """Which event rows the user currently has expanded, so an auto-refresh
        re-render can restore them instead of collapsing everything. Walks the
        Sport → Tournament → Event nesting of the all-sports aggregation."""
        keys = set()
        r = self.results
        for i in range(r.topLevelItemCount()):
            shead = r.topLevelItem(i)            # sport
            for j in range(shead.childCount()):
                thead = shead.child(j)           # tournament
                for k in range(thead.childCount()):
                    ev = thead.child(k)          # event
                    payload = ev.data(0, Qt.ItemDataRole.UserRole)
                    if ev.isExpanded() and isinstance(payload, tuple) \
                            and payload[0] == "dropevt":
                        keys.add(self._dropping_event_key(payload[1]))
        return keys

    def _render_dropping(self, drops):
        """Render the all-sports dropping-odds aggregation grouped Sport →
        Tournament → Event → Market. Each event collapses its many dropping
        markets (1X2, AH, O/U, DNB, HT/FT…) under one row showing the live score
        + the worst move; child rows name the market and show open→now odds. The
        filter box narrows the whole aggregation in place (team/league/sport)."""
        expanded = self._dropping_expanded_keys()  # preserve across re-render
        self._hold_scroll(("dropping", _norm_name(self.search_edit.text())))
        self.results.clear()
        self._scores_skeleton = None
        self._render_gen += 1
        self._pending_icons.clear()
        self._live_items = {}
        self._row_items = {}
        self._configure_columns([
            ("When / Market", "l", False, 230), ("Home", "r", True),
            ("Score", "c", False), ("Away", "l", True),
            ("Move", "c", False, 70), ("Odds (open→now)", "l", True)])
        score_idx = self._all_score_index()

        if self._league_filter:
            drops = [d for d in drops if d.tournament == self._league_filter]
        q = _norm_name(self.search_edit.text()) if self._mode == "dropping" else ""
        if q:
            drops = [d for d in drops if q in _norm_name(
                f"{d.home} {d.away} {d.tournament} {d.sport} {d.country}")]

        # sport -> tournament -> event(home,away) -> [markets]
        from collections import OrderedDict
        sports = OrderedDict()
        for d in drops:
            sports.setdefault(d.sport or "—", OrderedDict()) \
                  .setdefault(d.tournament or "—", OrderedDict()) \
                  .setdefault((d.home or "", d.away or ""), []).append(d)

        def evt_worst(markets):
            return min(x.drop_pct for x in markets)

        def tour_worst(events):
            return min(evt_worst(m) for m in events.values())

        def sport_worst(tours):
            return min(tour_worst(e) for e in tours.values())

        n_matched = n_events = 0
        for sname, tours in sorted(sports.items(), key=lambda kv: sport_worst(kv[1])):
            n_sport_ev = sum(len(ev) for ev in tours.values())
            shead = QTreeWidgetItem([
                f"{sname.replace('-', ' ').title()}   ({n_sport_ev})"])
            sf = shead.font(0); sf.setBold(True); shead.setFont(0, sf)
            shead.setForeground(0, QBrush(QColor(C_ACCENT)))
            self.results.addTopLevelItem(shead)
            shead.setFirstColumnSpanned(True)  # after add so it sticks
            for tname, events in sorted(tours.items(), key=lambda kv: tour_worst(kv[1])):
                # dim country prefix mirrors the Flashscore league naming
                country = next(iter(events.values()))[0].country or ""
                tlabel = f"{country.upper()}: {tname}" if country else tname
                thead = QTreeWidgetItem([f"{tlabel}  ({len(events)})"])
                thead.setForeground(0, QBrush(QColor(C_DIM)))
                shead.addChild(thead)
                thead.setFirstColumnSpanned(True)
                for (home, away), markets in sorted(
                        events.items(), key=lambda kv: evt_worst(kv[1])):
                    markets.sort(key=lambda d: d.drop_pct)
                    ev = (score_idx.get((_norm_name(home), _norm_name(away)))
                          or score_idx.get((_norm_name(away), _norm_name(home))))
                    evrow = self._dropping_event_item(markets, ev)
                    thead.addChild(evrow)
                    # restore the user's expansion after (re-)parenting the row
                    if self._dropping_event_key(markets[0]) in expanded:
                        evrow.setExpanded(True)
                    n_events += 1
                    if ev is not None:
                        n_matched += 1
                thead.setExpanded(True)
            shead.setExpanded(True)

        self._restore_open_odds()  # re-attach an open per-book table
        stamp = datetime.now().strftime("%H:%M:%S")
        fnote = f" · filter “{self.search_edit.text().strip()}”" if q else ""
        self._status_ready(
            f"{len(sports)} sports · {n_events} events · "
            f"{len(drops)} dropping markets{fnote} · updated {stamp}")

    def _dropping_event_item(self, markets, ev=None):
        """One event row carrying its dropping markets as children."""
        d0 = markets[0]
        worst = min(markets, key=lambda d: d.drop_pct)
        when = d0.time or "—"
        if ev is not None and ev.is_live:
            when = self._progress_cache.get(ev.event_id, "LIVE")
        if ev is not None and ev.home_score not in (None, "") \
                and ev.away_score not in (None, ""):
            score = f"{ev.home_score} - {ev.away_score}"
        else:
            score = "—"
        pct = worst.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        n = len(markets)
        # single-market events surface their odds move right on the event row;
        # multi-market events summarize and drill down on click
        if n == 1:
            summary = self._drop_odds_str(worst)
        else:
            summary = (f"{n} markets · worst "
                       f"{self._mkt_label(worst.betting_type)}")
        item = QTreeWidgetItem([
            when, d0.home or "", score, d0.away or "",
            f"{arrow}{abs(pct):.0f}%", summary])
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter | vc)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        ef = item.font(1); ef.setBold(True)
        item.setFont(1, ef); item.setFont(3, ef)
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        item.setData(4, MOVE_ROLE, pct)  # heat bar
        mf = item.font(4); mf.setBold(True); item.setFont(4, mf)
        item.setForeground(5, QBrush(QColor(C_DIM if n > 1 else C_TEXT)))
        if ev is not None and ev.is_live:
            item.setData(0, LIVE_ROLE, True)
            item.setForeground(0, QBrush(QColor(C_LIVE)))
            item.setForeground(2, QBrush(QColor(C_LIVE)))
            sf = item.font(2); sf.setBold(True); item.setFont(2, sf)
        # click toggles the market drill-down; markets carry the odds links
        item.setData(0, Qt.ItemDataRole.UserRole, ("dropevt", worst))
        self._attach_logos(item, ev)
        for d in markets:
            item.addChild(self._market_item(d))
        return item

    @staticmethod
    def _mkt_label(bt):
        """Friendlier market name: OddsPortal's 'H/A' (Home/Away — the 2-way
        moneyline) shown as 'ML', keeping any scope suffix (', 1st Set', ', OT')."""
        return (bt or "—").replace("H/A", "ML")

    def _market_item(self, d: DroppingOdd):
        """A single dropping market under an event: name + drop% heat bar +
        open→now odds. Click expands the full per-book table inline."""
        pct = d.drop_pct
        arrow = "▼" if pct < 0 else "▲"
        item = QTreeWidgetItem([f"    {self._mkt_label(d.betting_type)}", "", "", "",
                                f"{arrow}{abs(pct):.0f}%", self._drop_odds_str(d)])
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft | vc)
        item.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter | vc)
        item.setForeground(0, QBrush(QColor(C_TEXT)))
        item.setForeground(4, QBrush(QColor(C_LIVE if pct < 0 else C_ACCENT)))
        item.setData(4, MOVE_ROLE, pct)  # heat bar
        item.setForeground(5, QBrush(QColor(C_TEXT)))
        tip = (f"{self._mkt_label(d.betting_type)}  ·  {d.bookies} books  ·  "
               f"max {self._oddval(d.max_odds)} ({d.max_provider or '—'})")
        for col in (0, 5):
            item.setToolTip(col, tip + "\nclick for the per-book breakdown")
        item.setData(0, Qt.ItemDataRole.UserRole, ("drop", d))
        return item

    def _load_webshare_locations(self):
        """Background: merge Webshare's current proxy list into the geo set.
        Replaces the dict atomically; in-flight fetches keep the old one.
        Then seeds the bookmaker id→name map: one OPEN (prematch) event fetched
        through every geo carries betslip links naming that geo's whole book
        set — without this, the pregame odds of in-play games (whose betslip
        links are dropped) would show numeric #ids until the user happened to
        click an open market through the new geos."""
        try:
            ws = _webshare_locations()
        except Exception:
            return
        if not ws:
            return
        self._odds_locations = {**self._odds_locations, **ws}
        try:
            drops = self.odds_client.dropping_odds_pages(
                "0", DROPPING_PERIOD, DROPPING_BS, max_pages=1)
            d = next((x for x in drops if x.event_url), None)
            if d:
                OddsPortalClient.get_event_odds_multi(
                    d.event_url, self._odds_locations,
                    client_kwargs=GEO_CLIENT_KWARGS)
        except Exception:
            pass  # speculative warm; organic clicks teach the map anyway

    # -- click routing (all odds render inline; no bottom panel) ------------
    def _on_result_clicked(self, item, _col):
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(payload, tuple) and payload[0] == "golfnav":
            # LIVE-all golf aggregate row -> that tournament's leaderboard
            self._current_sport = "golf"
            self._league_filter = payload[1]
            self.refresh()
            return
        if isinstance(payload, tuple) and payload[0] == "opmatch":
            self._show_match_detail(payload[1], item)  # historical OddsPortal match
            return
        if isinstance(payload, tuple) and payload[0] == "dropevt":
            # dropping event row: single click drills into its market children
            item.setExpanded(not item.isExpanded())
            return
        if isinstance(payload, tuple) and payload[0] == "drop":
            # a dropping MARKET row: per-book odds render inline beneath it
            self._show_drop_market_detail(payload[1], item)
            return
        # a Flashscore score row (Scores / Search live hits): show the odds
        # breakdown INLINE under the row. If the dropping feed already gave us
        # the OddsPortal url, use it directly (fast); otherwise resolve by name.
        e = self._event_map.get(payload) if isinstance(payload, str) else None
        if e is None or not (e.home and e.away):
            return
        d = self._match_drop(e)
        url = (d.event_url if d is not None and d.event_url
               else self._resolved_urls.get((e.home, e.away)))
        if url:
            self._detail_resolving = False
            self._begin_inline_odds(item)
            eo = self._prefetched(url)
            if eo is not None:
                self._deliver_inline_odds(item, eo)
            else:
                self._detail_token += 1
                self.odds_worker.fetch_event_odds(
                    self._detail_token, url, self._odds_locations)
        else:
            self._show_event_resolve_detail(e, item)

    def _show_drop_market_detail(self, d: DroppingOdd, item):
        """A dropping market row was clicked: expand the full per-book odds
        table inline beneath it (prefetch-warm when the mouse rested first)."""
        self._detail_resolving = False
        if not d.event_url:
            return
        self._begin_inline_odds(item)
        eo = self._prefetched(d.event_url)
        if eo is not None:
            self._deliver_inline_odds(item, eo)
        else:
            self._detail_token += 1
            self.odds_worker.fetch_event_odds(
                self._detail_token, d.event_url, self._odds_locations)

    def _oddsportal_sport_for(self, e):
        """OddsPortal url-name for a specific event's sport (independent of the
        current nav selection — matters in LIVE-all / search views)."""
        return SPORT_BRIDGE.get(getattr(e, "sport", None))

    def _show_event_resolve_detail(self, e, item):
        """A listed head-to-head game with no dropping-odds match was clicked:
        expand a loading row under it and kick a background resolve+odds fetch
        from OddsPortal. Odds arrive via eventOddsReady and render inline."""
        self._detail_resolving = True
        self._pending_resolve_key = (e.home, e.away)
        self._begin_inline_odds(item)
        self._detail_token += 1
        self.odds_worker.resolve_and_fetch_odds(
            self._detail_token, e.home, e.away, self._oddsportal_sport_for(e),
            e.start_ts, self._odds_locations)

    def _show_match_detail(self, m: SearchMatch, item):
        """A historical OddsPortal match row was clicked: render the per-book odds
        inline. Served instantly from the hover-prefetch cache when warm;
        otherwise a loading row shows while the fetch runs."""
        self._detail_resolving = False
        self._begin_inline_odds(item)
        url = (m.url or "").split("#")[0]
        eo = self._prefetched(url) if url else None
        if eo is not None:
            self._deliver_inline_odds(item, eo)
        elif url:
            self._detail_token += 1
            self.odds_worker.fetch_event_odds(
                self._detail_token, url, self._odds_locations)

    # -- inline expandable odds breakdown (per-book, with logos) ------------
    def _begin_inline_odds(self, item):
        """Arm the clicked row and drop a 'loading…' child so the row visibly
        expands while the multi-geo odds fetch runs."""
        self._odds_target_item = item
        # remember which row is open so a refresh re-render can restore it
        self._open_odds_key = self._odds_key_for(item)
        self._open_odds_eo = None
        if item is None or sip.isdeleted(item):
            return
        item.takeChildren()
        n = len(self._odds_locations)
        note = f"loading odds across {n} geos…" if n > 1 else "loading odds…"
        child = QTreeWidgetItem([""])
        child.setForeground(0, QBrush(QColor(C_DIM)))
        item.addChild(child)
        child.setFirstColumnSpanned(True)  # after addChild so it sticks
        self._register_spinner_row(child, 0, note)
        item.setExpanded(True)

    def _deliver_inline_odds(self, item, eo):
        """Render odds under a row + record them for restore-across-refresh."""
        if item is None or sip.isdeleted(item):
            return
        self._odds_target_item = item
        self._populate_odds_children(item, eo)
        self._open_odds_eo = eo

    def _on_event_odds(self, token, event_url, eo):
        if token != self._detail_token or eo is None:
            return
        if self._detail_resolving and self._pending_resolve_key:
            # remember the name-resolved url so re-clicks/hovers on this matchup
            # skip the multi-request participant-search resolve (~10-20s)
            self._resolved_urls[self._pending_resolve_key] = event_url
            self._pending_resolve_key = None
        self._detail_resolving = False
        # every fetched result warms the prefetch cache: re-clicking the same
        # row inside the TTL renders instantly with zero network
        self._odds_prefetch[event_url] = (time.time(), eo)
        # The target row may have been rebuilt by an auto-refresh while the odds
        # were loading — re-find it by key so the result doesn't land on a dead
        # item (which was the "refresh resets the view" bug).
        item = self._odds_target_item
        if item is None or sip.isdeleted(item):
            item = (self._find_row_by_key(self._open_odds_key)
                    if self._open_odds_key else None)
        if item is not None:
            self._deliver_inline_odds(item, eo)

    # -- hover-prefetch: warm odds before the click ------------------------
    def _hover_odds_url(self, item):
        """The OddsPortal event url a row's odds live at, if directly known
        (opmatch / dropping row / a score row already matched to a dropping
        odd). Resolve-only score rows have no cheap url, so they aren't
        prefetched on hover."""
        if item is None or sip.isdeleted(item):
            return None
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(payload, tuple) and payload[0] == "opmatch":
            return (payload[1].url or "").split("#")[0] or None
        if isinstance(payload, tuple) and payload[0] in ("drop", "dropevt"):
            return payload[1].event_url or None
        if isinstance(payload, str):
            e = self._event_map.get(payload)
            if e is None:
                return None
            d = self._match_drop(e)
            if d is not None and d.event_url:
                return d.event_url
            return self._resolved_urls.get((e.home, e.away))
        return None

    def _on_item_hover(self, item, _col):
        url = self._hover_odds_url(item)
        if not url or url in self._prefetch_inflight or self._prefetched(url):
            return
        self._prefetch_url = url
        self._prefetch_debounce.start(140)  # fire once the mouse rests

    def _do_prefetch(self):
        url = self._prefetch_url
        if not url or url in self._prefetch_inflight or self._prefetched(url):
            return
        self._prefetch_inflight.add(url)
        self.odds_worker.prefetch_event_odds(url, self._odds_locations)

    def _on_odds_prefetched(self, url, eo):
        self._prefetch_inflight.discard(url)
        if eo is not None:
            self._odds_prefetch[url] = (time.time(), eo)

    def _prefetched(self, url):
        """A fresh prefetched EventOdds for url, or None."""
        hit = self._odds_prefetch.get(url)
        if hit and time.time() - hit[0] < self._prefetch_ttl:
            return hit[1]
        return None

    # -- keep the open odds breakdown alive across refresh re-renders -------
    def _odds_key_for(self, item):
        """Stable identity of an odds-bearing row (opmatch url / score event id
        / dropping market), so the same row can be re-found after a rebuild."""
        if item is None or sip.isdeleted(item):
            return None
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(payload, tuple) and payload[0] == "opmatch":
            url = (payload[1].url or "").split("#")[0]
            return ("opmatch", url) if url else None
        if isinstance(payload, tuple) and payload[0] == "drop":
            d = payload[1]
            return ("drop", d.event_id, d.betting_type)
        if isinstance(payload, str):
            return ("event", payload)
        return None

    def _find_row_by_key(self, key):
        """Locate the (rebuilt) row matching an odds key by walking the tree."""
        stack = [self.results.topLevelItem(i)
                 for i in range(self.results.topLevelItemCount())]
        while stack:
            node = stack.pop()
            if self._odds_key_for(node) == key:
                return node
            for c in range(node.childCount()):
                stack.append(node.child(c))
        return None

    def _restore_open_odds(self):
        """After a refresh rebuilds the tree, re-attach the open odds breakdown to
        its row so the user's expanded view isn't lost on auto-refresh. Uses the
        cached EventOdds when we have it (instant, no refetch); if the odds are
        still in flight, keeps a loading placeholder so it doesn't look reset."""
        if not self._open_odds_key:
            return
        item = self._find_row_by_key(self._open_odds_key)
        if item is None:
            return  # that event is no longer listed (e.g. game finished/dropped)
        self._odds_target_item = item
        if self._open_odds_eo is not None:
            self._populate_odds_children(item, self._open_odds_eo)
        else:
            item.takeChildren()
            note = QTreeWidgetItem([""])
            note.setForeground(0, QBrush(QColor(C_DIM)))
            item.addChild(note)
            note.setFirstColumnSpanned(True)
            self._register_spinner_row(note, 0, "loading odds…")
            item.setExpanded(True)

    def _on_item_collapsed(self, item):
        """If the user collapses the open odds row, stop restoring it on refresh."""
        if self._open_odds_key and self._odds_key_for(item) == self._open_odds_key:
            self._open_odds_key = None
            self._open_odds_eo = None

    def _attach_icon_url(self, item, col, url):
        """Like _attach_one_logo but for a full image URL (OddsPortal book
        logos). Reuses the async loader + disk cache + render-gen guard, so a
        logo shows instantly on a cache hit and never blocks the GUI thread."""
        if not url:
            return
        pm = self._pixmaps.get(url)
        if pm is not None:
            if not pm.isNull():
                item.setIcon(col, QIcon(pm))
            return
        self._pending_icons.setdefault(url, []).append((self._render_gen, item, col))
        self.image_loader.request(url)

    def _populate_odds_children(self, item, eo: EventOdds):
        """Render the full per-book odds as child rows under the clicked match
        row (replacing the loading row): a compact match-info header, then one row
        per bookmaker with its logo and price under each team's column, best price
        bolded. Columns map onto the shared When/Home/Score/Away/Move layout
        (2-way + 1X2)."""
        if item is None or sip.isdeleted(item):
            return
        item.takeChildren()
        n = len(eo.outcomes)
        if n == 0:
            miss = QTreeWidgetItem(["  no odds available"])
            miss.setForeground(0, QBrush(QColor(C_DIM)))
            item.addChild(miss)
            miss.setFirstColumnSpanned(True)  # after addChild so it sticks
            item.setExpanded(True)
            return

        # single compact header (the per-outcome max/avg/open lines were removed —
        # the per-book table below already conveys prices + the bold best price;
        # book count + tournament/date is the only non-redundant bit worth keeping)
        when = (datetime.fromtimestamp(eo.start_ts).strftime("%Y-%m-%d %H:%M")
                if eo.start_ts else "")
        nbooks = max((o.n_books for o in eo.outcomes), default=0)
        live_tag = "LIVE" if getattr(eo, "live", False) else ""
        subbits = "   ·   ".join(
            b for b in (live_tag, eo.tournament, when, f"{nbooks} books") if b)
        htext = f"{eo.home}  v  {eo.away}" + (f"   —   {subbits}" if subbits else "")
        hdr = QTreeWidgetItem([htext])
        hf = hdr.font(0); hf.setBold(True); hdr.setFont(0, hf)
        hdr.setForeground(0, QBrush(QColor(C_LIVE if live_tag else C_ACCENT)))
        hdr.setData(0, LIVE_ROLE, bool(live_tag))
        item.addChild(hdr)
        hdr.setFirstColumnSpanned(True)

        # per-book rows — odds under each team's column, best price highlighted.
        # Section labels sit in the book column (col 2) so they render centered
        # over the odds table rather than stranded at the far-left cell edge.
        # the draw/3rd-outcome column: col 4 in the scores layout; col 5 in the
        # dropping layout, whose col 4 is the narrow Move heat-bar column
        mid_col = 5 if self._mode == "dropping" else 4
        has_pre = bool(getattr(eo, "pre_outcomes", None))
        if live_tag and has_pre:
            self._odds_section_row(item, "LIVE", C_LIVE)
        self._odds_guide_row(item, eo, mid_col=mid_col)
        self._add_book_rows(item, eo.outcomes, eo.book_logos, mid_col=mid_col)

        # in-play: the frozen pregame (closing) odds beneath the live table
        if has_pre:
            self._odds_section_row(item, "PREGAME (closing)", C_DIM)
            self._odds_guide_row(item, eo, pre=True, mid_col=mid_col)
            self._add_book_rows(item, eo.pre_outcomes, eo.book_logos,
                                mid_col=mid_col)
        item.setExpanded(True)

    def _odds_section_row(self, item, text, color):
        """Section divider for the inline odds table, centered in the book
        column (between the two odds columns)."""
        row = QTreeWidgetItem(["", "", text, "", ""])
        row.setTextAlignment(
            2, Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        f = row.font(2); f.setBold(True); row.setFont(2, f)
        row.setForeground(2, QBrush(QColor(color)))
        item.addChild(row)

    def _odds_guide_row(self, item, eo, pre=False, mid_col=4):
        """Dim column-guide row naming which side each odds column belongs to
        (home left, away right, draw in the mid_col for 1X2)."""
        outs = eo.pre_outcomes if pre else eo.outcomes
        cells = [""] * max(5, mid_col + 1)
        cells[1] = eo.home or "home"
        cells[3] = eo.away or "away"
        if len(outs) == 3:
            cells[mid_col] = "draw"
        row = QTreeWidgetItem(cells)
        vc = Qt.AlignmentFlag.AlignVCenter
        row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        row.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
        row.setTextAlignment(mid_col, Qt.AlignmentFlag.AlignCenter | vc)
        for col in (1, 3, mid_col):
            gf = row.font(col); gf.setItalic(True); row.setFont(col, gf)
            row.setForeground(col, QBrush(QColor(C_DIM)))
        item.addChild(row)

    def _add_book_rows(self, item, outcomes, book_logos, mid_col=4):
        """One row per bookmaker for an outcome set: logo + price under each
        team's column (2-way + 1X2 layouts), best price per column bolded.
        `mid_col` hosts the draw/3rd outcome (4 in the scores layout, 5 in the
        dropping layout)."""
        n = len(outcomes)
        if n == 0:
            return
        home_idx, away_idx = 0, n - 1
        mid_idx = 1 if n == 3 else None

        def best_of(i):
            o = outcomes[i]
            return max(o.books.items(), key=lambda kv: kv[1])[0] if o.books else None
        best = {i: best_of(i) for i in range(n)}
        allbooks = set()
        for o in outcomes:
            allbooks.update(o.books)

        vc = Qt.AlignmentFlag.AlignVCenter
        for book in sorted(allbooks, key=str.lower):
            ho = outcomes[home_idx].books.get(book)
            ao = outcomes[away_idx].books.get(book)
            mo = outcomes[mid_idx].books.get(book) if mid_idx is not None else None
            # OddsPortal drops betslip links on closed markets, leaving numeric
            # ids as the "name"; show those as #id (the logo carries the brand).
            blabel = f"#{book}" if book.isdigit() else book
            cells = [""] * max(5, mid_col + 1)
            cells[1] = self._oddval(ho) if ho else ""
            cells[2] = blabel
            cells[3] = self._oddval(ao) if ao else ""
            cells[mid_col] = self._oddval(mo) if mo else ""
            row = QTreeWidgetItem(cells)
            row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter | vc)
            row.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
            row.setTextAlignment(mid_col, Qt.AlignmentFlag.AlignCenter | vc)
            row.setForeground(2, QBrush(QColor(C_DIM)))
            for col, idx in ((1, home_idx), (3, away_idx), (mid_col, mid_idx)):
                if idx is not None and book == best.get(idx):
                    row.setForeground(col, QBrush(QColor(C_ACCENT)))
                    bf = row.font(col); bf.setBold(True); row.setFont(col, bf)
            self._attach_icon_url(row, 2, book_logos.get(book))
            item.addChild(row)

    # -- loading spinner + update flash --------------------------------------
    def _register_spinner_row(self, item, col, text):
        """Animate a loading row with a quiet braille spinner until its item is
        replaced (takeChildren deletes it) or its text is overwritten."""
        item.setText(col, f"{_SPIN_FRAMES[self._spin_ix]} {text}")
        self._spin_rows.append((item, col, text))
        if not self._spin_timer.isActive():
            self._spin_timer.start()

    def _status_busy(self, text):
        """Show a busy status with a spinner; cleared by _status_ready."""
        self._loading_status = text
        self.status_lbl.setText(f"{_SPIN_FRAMES[self._spin_ix]} {text}")
        if not self._spin_timer.isActive():
            self._spin_timer.start()

    def _status_ready(self, text):
        self._loading_status = None
        self.status_lbl.setText(text)

    def _tick_spinner(self):
        self._spin_ix = (self._spin_ix + 1) % len(_SPIN_FRAMES)
        frame = _SPIN_FRAMES[self._spin_ix]
        alive = []
        for item, col, text in self._spin_rows:
            # drop rows that were deleted or whose text was replaced by content
            if sip.isdeleted(item) or not item.text(col).startswith(tuple(_SPIN_FRAMES)):
                continue
            item.setText(col, f"{frame} {text}")
            alive.append((item, col, text))
        self._spin_rows = alive
        if self._loading_status is not None:
            self.status_lbl.setText(f"{frame} {self._loading_status}")
        elif not self._spin_rows:
            self._spin_timer.stop()

    def _flash_cell(self, item, col):
        """Briefly highlight a cell whose value just changed (fades ~0.9s)."""
        self._flash_cells[(id(item), col)] = (item, col, time.time())
        if not self._flash_timer.isActive():
            self._flash_timer.start()

    def _tick_flashes(self):
        now = time.time()
        done = []
        for key, (item, col, t0) in self._flash_cells.items():
            if sip.isdeleted(item):
                done.append(key)
                continue
            frac = (now - t0) / 0.9
            if frac >= 1.0:
                item.setBackground(col, QBrush())  # reset to default
                done.append(key)
                continue
            c = QColor(C_ACCENT)
            c.setAlphaF(0.30 * (1.0 - frac))
            item.setBackground(col, QBrush(c))
        for k in done:
            self._flash_cells.pop(k, None)
        if not self._flash_cells:
            self._flash_timer.stop()

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
    rc = app.exec()
    # Hard exit: ThreadPoolExecutor workers (multi-geo odds, flashscore
    # fan-outs) are non-daemon and the interpreter joins them at shutdown, so
    # an in-flight proxy fetch (≤6s timeout) held the closed app open. All
    # background work here is abandonable network I/O — nothing to flush.
    os._exit(rc)


if __name__ == "__main__":
    main()
