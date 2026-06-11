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

import sys
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont
from PyQt6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QComboBox, QSpinBox, QCheckBox, QPushButton, QLabel, QSplitter, QHeaderView,
)

from flashscore_client import (
    FlashscoreClient, SPORT_IDS, Event, format_progress, format_to_par,
)

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
                detail = self.client.get_live_detail(sport_id, event_id)
                self.detailReady.emit(token, event_id, detail)
            except Exception as e:
                self.failed.emit(token, str(e))
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

        self._token = 0                 # request sequence guard
        self._current_sport = None      # None => LIVE-all view
        self._events = []               # last loaded events for current view
        self._event_map = {}            # event_id -> Event (for lookups)
        self._league_filter = None      # tournament name to restrict to, or None
        self._progress_token = 0        # guards stale live-progress results
        self._live_items = {}           # event_id -> QTreeWidgetItem (live rows)
        self._golf_boards = []          # cached leaderboards (for in-place filter)
        self._participant_events = []   # cached participant events (for filter)

        self._build_ui()
        self._populate_nav()

        # auto-refresh
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self._apply_autorefresh()

        # initial selection
        self._select_sport(DEFAULT_SPORT)

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
            QLabel#title {{ font-size: 16px; font-weight: 600; }}
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

        self.days_spin = QSpinBox()
        self.days_spin.setRange(0, 14)
        self.days_spin.setValue(0)
        self.days_spin.setPrefix("+")
        self.days_spin.setSuffix(" d")
        self.days_spin.setToolTip("Days ahead to include in the schedule")
        self.days_spin.valueChanged.connect(lambda _: self.refresh())

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
        controls.addStretch(1)
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
            if self._current_sport == "golf":
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
    def refresh(self):
        self._token += 1
        self.status_lbl.setText("loading…")
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

    def _on_schedule(self, token, sport, events):
        if token != self._token or sport != self._current_sport:
            return
        self._events = events
        self._refill_league_children(sport, events)
        self._render()

    def _on_live(self, token, events):
        if token != self._token or self._current_sport is not None:
            return
        self._events = events
        self._render()

    def _on_golf(self, token, boards):
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
                ("Score", "c", False), ("Away", "l", True)])

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
        self._set_headers(golf=False)
        self._live_items = {}
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
        if self._live_items:
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
        item = QTreeWidgetItem([when, e.home or "", score, e.away or ""])
        # Home hugs the right, Away hugs the left, so the score sits centered
        # between the two team names rather than drifting to the far right.
        vc = Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | vc)
        item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(3, Qt.AlignmentFlag.AlignLeft | vc)
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
        return item

    # -- auto-refresh -------------------------------------------------------
    def _apply_autorefresh(self):
        if self.auto_chk.isChecked():
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
