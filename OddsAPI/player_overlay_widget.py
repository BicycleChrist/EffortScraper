"""
player_overlay.py
-----------------
Collapsible player selection overlay for the MLB Ball Flight Simulator.

Sits as a child widget over the UmpireView3D OpenGL panel.  Does not
participate in any layout — positioned absolutely and animated via
QPropertyAnimation so the 3D view renders uninterrupted behind it.

Architecture:
  PlayerBBEOverlay          — outer shell, toggle button, animation
    └── _OverlayContent     — stacked widget with two pages:
          Page 0 – Search page: search bar + team filter + player list
          Page 1 – Detail page: back button + headshot + name +
                                percentile bars + full BBE event table

Data flow:
  BBEDataStore (loaded in QThread on startup)
    → PlayerBBEOverlay.set_data_store()
    → player selected → navigate to detail page
    → BBE row clicked  → simulate_bbe signal emitted
                        → MLBWeatherApp.on_simulate_bbe() fires sim

Headshots:
  Fetched async via QNetworkAccessManager, cached to headshots/ on disk.
  URL: img.mlbstatic.com/.../people/{player_id}/headshot/67/current
"""

import os
import io
import math
import numpy as np
import pandas as pd
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QScrollArea, QFrame, QSizePolicy, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QProgressBar, QStackedWidget, QCheckBox,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QPropertyAnimation,
    QEasingCurve, QRect, QSize, QUrl,
)
from PyQt6.QtGui import (
    QPixmap, QColor, QPainter, QBrush, QPen, QFont,
    QLinearGradient, QPalette,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
HEADSHOT_DIR   = Path("headshots")
HEADSHOT_URL   = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "d_people:generic:headshot:67:current.png/"
    "w_213,q_auto:best/v1/people/{player_id}/headshot/67/current"
)
LEADERBOARD_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast"
    "?type=batter&year={year}&position=&team=&min=1&csv=true"
)
PITCHER_LEADERBOARD_URL = (
    "https://baseballsavant.mlb.com/leaderboard/statcast"
    "?type=pitcher&year={year}&position=&team=&min=1&csv=true"
)

OVERLAY_WIDTH_EXPANDED  = 460
OVERLAY_WIDTH_COLLAPSED = 0
ANIM_DURATION_MS        = 220

# Stats to show as percentile bars: (display_label, csv_column, higher_is_better)
PERCENTILE_STATS = [
    ("Avg EV",       "avg_hit_speed",          True),
    ("Max EV",       "max_hit_speed",           True),
    ("EV50",         "ev50",                    True),
    ("Barrel %",     "brl_percent",             True),
    ("Hard Hit %",   "ev95percent",             True),
    ("Sweet Spot %", "anglesweetspotpercent",   True),
    ("Avg Distance", "avg_distance",            True),
]

PITCHER_PERCENTILE_STATS = [
    ("Avg EV",       "avg_hit_speed",          False),
    ("Max EV",       "max_hit_speed",           False),
    ("EV50",         "ev50",                    False),
    ("Barrel %",     "brl_percent",             False),
    ("Hard Hit %",   "ev95percent",             False),
    ("Sweet Spot %", "anglesweetspotpercent",   False),
    ("Avg Distance", "avg_distance",            False),
]

# Colour stops for percentile bar: 0 → red, 50 → yellow, 100 → green
def percentile_colour(pct: float) -> QColor:
    pct = max(0.0, min(100.0, pct))
    if pct < 50:
        r, g = 220, int(pct / 50 * 200)
        return QColor(r, g, 30)
    else:
        r, g = int((1 - (pct - 50) / 50) * 200), 200
        return QColor(r, g, 30)


# ---------------------------------------------------------------------------
# Background data loader
# ---------------------------------------------------------------------------
class BBEDataStore:
    """Holds all loaded data; passed to the overlay once ready."""
    def __init__(self):
        self.leaderboard: pd.DataFrame   = pd.DataFrame()
        self.bbe: pd.DataFrame           = pd.DataFrame()
        self.percentiles: dict           = {}   # player_id → {col: pct}
        self.by_player: dict             = {}   # player_id → list of BBE dicts
        self.teams: list                 = []   # sorted unique team abbreviations
        self.player_list: list           = []   # [(display_name, player_id, team), ...]
        self.ready: bool                 = False


class DataLoaderWorker(QObject):
    """Runs in a QThread.  Emits finished(BBEDataStore) when done."""
    finished  = pyqtSignal(object)
    progress  = pyqtSignal(str)
    error     = pyqtSignal(str)

    def __init__(self, bbe_csv_path: str, leaderboard_csv_path: str | None = None,
                 year: int = 2024, player_type: str = "batter"):
        super().__init__()
        self.bbe_csv_path        = bbe_csv_path
        self.leaderboard_csv_path = leaderboard_csv_path
        self.year                = year
        self.player_type         = player_type

    def run(self):
        import requests
        store = BBEDataStore()
        try:
            # ---- leaderboard ----
            self.progress.emit("Loading leaderboard…")
            if self.leaderboard_csv_path and Path(self.leaderboard_csv_path).exists():
                lb = pd.read_csv(self.leaderboard_csv_path)
            else:
                self.progress.emit("Fetching leaderboard from Baseball Savant…")
                lb_url = (PITCHER_LEADERBOARD_URL if self.player_type == "pitcher"
                          else LEADERBOARD_URL)
                r = requests.get(lb_url.format(year=self.year), timeout=30)
                r.raise_for_status()
                lb = pd.read_csv(io.StringIO(r.text))

            lb.columns = [c.strip().lower().replace(" ", "_").replace(",", "") for c in lb.columns]
            # canonical id column
            _id_candidates = (["player_id", "pitcher", "mlbam_id"]
                              if self.player_type == "pitcher"
                              else ["player_id", "batter", "mlbam_id"])
            for cand in _id_candidates:
                if cand in lb.columns:
                    lb = lb.rename(columns={cand: "player_id"})
                    break
            lb["player_id"] = lb["player_id"].astype(int)
            store.leaderboard = lb

            # ---- percentiles (vectorized + disk cache) ----
            import json, hashlib
            cache_dir = Path("headshots")          # reuse existing cache dir
            cache_dir.mkdir(exist_ok=True)

            # Select percentile stat definitions based on player type
            _pct_stats = (PITCHER_PERCENTILE_STATS if self.player_type == "pitcher"
                          else PERCENTILE_STATS)

            # Cache key: hash of player_ids + stat columns present + player_type
            _key_src = (self.player_type + str(sorted(lb["player_id"].tolist()))
                        + str([c for _, c, _ in _pct_stats if c in lb.columns]))
            _cache_name = f"pct_cache_{hashlib.md5(_key_src.encode()).hexdigest()[:12]}.json"
            _cache_path = cache_dir / _cache_name

            if _cache_path.exists():
                self.progress.emit("Loading cached percentiles…")
                with open(_cache_path) as f:
                    # JSON keys are strings; convert back to int
                    pcts = {int(k): v for k, v in json.load(f).items()}
            else:
                self.progress.emit("Computing percentiles…")
                pcts = {}
                pids = lb["player_id"].astype(int).values
                for _, col, higher_is_better in _pct_stats:
                    if col not in lb.columns:
                        continue
                    series = lb[col]
                    n_valid = series.notna().sum()
                    if n_valid == 0:
                        continue
                    # rank() is O(n log n) vs the old O(n²) loop
                    ranked = series.rank(method="max", na_option="keep")
                    pct_series = ranked / n_valid * 100
                    if not higher_is_better:
                        pct_series = 100 - pct_series
                    pct_series = pct_series.fillna(50.0).round(1)
                    for pid, pct in zip(pids, pct_series):
                        pcts.setdefault(int(pid), {})[col] = float(pct)

                # Persist for next launch
                try:
                    with open(_cache_path, "w") as f:
                        json.dump(pcts, f)
                except OSError:
                    pass

            store.percentiles = pcts

            # ---- BBE events ----
            self.progress.emit("Loading BBE events…")
            bbe = pd.read_csv(self.bbe_csv_path)
            bbe.columns = [c.strip().lower() for c in bbe.columns]
            bbe["player_id"] = bbe["player_id"].astype(int)

            # Index by player
            by_player: dict = {}
            for pid, grp in bbe.groupby("player_id"):
                by_player[int(pid)] = grp.sort_values("game_date", ascending=False).to_dict("records")
            store.by_player = by_player

            # ---- player list & teams ----
            self.progress.emit("Building player index…")
            name_col = next(
                (c for c in ("last_name_first_name", "player_name", "last_name") if c in lb.columns),
                None
            )

            # Derive each player's team from BBE home_team/away_team
            # (leaderboard CSV has no team column).
            # The player's own team dominates the combined frequency.
            player_team_map: dict[int, str] = {}
            if "home_team" in bbe.columns and "away_team" in bbe.columns:
                for pid, grp in bbe.groupby("player_id"):
                    teams = pd.concat([grp["home_team"], grp["away_team"]])
                    mode = teams.value_counts()
                    player_team_map[int(pid)] = mode.index[0] if len(mode) > 0 else "—"

            player_list = []
            for _, row in lb.iterrows():
                pid  = int(row["player_id"])
                name = str(row[name_col]) if name_col else f"ID {pid}"
                team = player_team_map.get(pid, "—")
                player_list.append((name, pid, team))

            player_list.sort(key=lambda x: x[0])
            store.player_list = player_list
            store.teams       = sorted({t for _, _, t in player_list if t != "—"})
            store.ready       = True

            self.finished.emit(store)

        except Exception as exc:
            import traceback
            self.error.emit(f"Data load failed: {exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Percentile bar widget
# ---------------------------------------------------------------------------
class PercentileBar(QWidget):
    """Single labelled percentile bar."""
    def __init__(self, label: str, percentile: float = 50.0, parent=None):
        super().__init__(parent)
        self.label      = label
        self.percentile = percentile
        self.setFixedHeight(22)
        self.setMinimumWidth(200)

    def set_percentile(self, pct: float):
        self.percentile = max(0.0, min(100.0, pct))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        label_w = 90
        bar_x   = label_w + 4
        bar_w   = w - bar_x - 38
        bar_h   = 10
        bar_y   = (h - bar_h) // 2

        # Label
        p.setPen(QColor(200, 200, 200))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(0, 0, label_w, h, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.label)

        # Track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(50, 50, 55)))
        p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 4, 4)

        # Fill
        fill_w = max(4, int(bar_w * self.percentile / 100))
        colour = percentile_colour(self.percentile)
        grad   = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
        grad.setColorAt(0, colour.darker(140))
        grad.setColorAt(1, colour)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(bar_x, bar_y, fill_w, bar_h, 4, 4)

        # Percentile number
        p.setPen(QColor(220, 220, 220))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(bar_x + bar_w + 4, 0, 34, h,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"{self.percentile:.0f}")


# ---------------------------------------------------------------------------
# Player summary widget (headshot + name + percentile bars) — detail page
# ---------------------------------------------------------------------------
class PlayerSummaryWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #1a1a22;")
        self._build_ui()
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_headshot_reply)
        HEADSHOT_DIR.mkdir(exist_ok=True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(4)

        # ---- top row: headshot left, identity + BBE count right ----
        top = QHBoxLayout()
        top.setSpacing(10)
        top.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.headshot_lbl = QLabel()
        self.headshot_lbl.setFixedSize(67, 67)
        self.headshot_lbl.setStyleSheet(
            "border-radius: 6px; background: #2a2a35; border: 1px solid #444;"
        )
        self.headshot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.headshot_lbl, 0, Qt.AlignmentFlag.AlignTop)

        identity = QVBoxLayout()
        identity.setSpacing(2)
        identity.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.name_lbl = QLabel("—")
        self.name_lbl.setStyleSheet("color: #f0f0f0; font-size: 13px; font-weight: bold;")
        self.name_lbl.setWordWrap(True)
        self.team_lbl = QLabel("—")
        self.team_lbl.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        self.attempts_lbl = QLabel("")
        self.attempts_lbl.setStyleSheet("color: #888888; font-size: 10px;")
        identity.addWidget(self.name_lbl)
        identity.addWidget(self.team_lbl)
        identity.addWidget(self.attempts_lbl)
        top.addLayout(identity, 1)
        root.addLayout(top)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #333340;")
        root.addWidget(line)

        # ---- percentile bars ----
        self.pct_bars: dict[str, PercentileBar] = {}
        for label, col, _ in PERCENTILE_STATS:
            bar = PercentileBar(label, 50.0)
            self.pct_bars[col] = bar
            root.addWidget(bar)

    def load_player(self, name: str, team: str, player_id: int,
                    lb_row: pd.Series | None, percentiles: dict):
        self.name_lbl.setText(name)
        self.team_lbl.setText(team)

        if lb_row is not None:
            attempts = lb_row.get("attempts", "")
            self.attempts_lbl.setText(f"{attempts} BBEs" if attempts else "")
        else:
            self.attempts_lbl.setText("")

        # Percentile bars
        player_pcts = percentiles.get(player_id, {})
        for col, bar in self.pct_bars.items():
            bar.set_percentile(player_pcts.get(col, 50.0))

        # Headshot
        self._load_headshot(player_id)

    def _load_headshot(self, player_id: int):
        cache_path = HEADSHOT_DIR / f"{player_id}.png"
        if cache_path.exists():
            self._set_headshot_pixmap(QPixmap(str(cache_path)))
        else:
            self.headshot_lbl.setText("…")
            url = HEADSHOT_URL.format(player_id=player_id)
            req = QNetworkRequest(QUrl(url))
            req.setAttribute(QNetworkRequest.Attribute.User, player_id)
            self._pending_player_id = player_id
            self._nam.get(req)

    def _on_headshot_reply(self, reply: QNetworkReply):
        player_id = reply.request().attribute(QNetworkRequest.Attribute.User)
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            px   = QPixmap()
            px.loadFromData(data)
            if not px.isNull():
                cache_path = HEADSHOT_DIR / f"{player_id}.png"
                px.save(str(cache_path))
                if player_id == getattr(self, "_pending_player_id", None):
                    self._set_headshot_pixmap(px)
        reply.deleteLater()

    def _set_headshot_pixmap(self, px: QPixmap):
        self.headshot_lbl.setText("")
        scaled = px.scaled(
            67, 67,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.headshot_lbl.setPixmap(scaled)

    def clear(self):
        self.name_lbl.setText("—")
        self.team_lbl.setText("—")
        self.attempts_lbl.setText("")
        self.headshot_lbl.clear()
        self.headshot_lbl.setText("")
        for bar in self.pct_bars.values():
            bar.set_percentile(50.0)


# ---------------------------------------------------------------------------
# BBE event table
# ---------------------------------------------------------------------------
class BBETableWidget(QTableWidget):
    bbe_selected = pyqtSignal(object)   # dict: full BBE event record + player_id

    COLS = [
        ("Date",     "game_date"),
        ("Event",    "events"),
        ("EV",       "launch_speed"),
        ("LA°",      "launch_angle"),
        ("Dist",     "hit_distance_sc"),
        ("BB Type",  "bb_type"),
        ("Pitch",    "pitch_name"),
        ("Velo",     "release_speed"),
        ("Spin",     "release_spin_rate"),
        ("Inn",      "inning"),
    ]

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels([c[0] for c in self.COLS])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setStyleSheet("""
            QTableWidget {
                background: #12121a;
                alternate-background-color: #1a1a24;
                color: #dddddd;
                gridline-color: #2a2a35;
                font-size: 11px;
            }
            QHeaderView::section {
                background: #22222e;
                color: #aaaaaa;
                border: none;
                padding: 3px;
                font-size: 10px;
            }
            QTableWidget::item:selected {
                background: #2a4a6a;
            }
        """)
        self.cellClicked.connect(self._on_cell_clicked)
        self._player_id = None
        self._events: list = []

    def load_events(self, events: list, player_id: int):
        self._player_id = player_id
        self._events = list(events)
        self.setRowCount(0)
        for record in events:
            row = self.rowCount()
            self.insertRow(row)
            for col_idx, (_, key) in enumerate(self.COLS):
                val = record.get(key, "")
                if isinstance(val, float):
                    if math.isnan(val):
                        text = "—"
                    elif key in ("release_spin_rate", "inning"):
                        text = f"{val:.0f}"
                    else:
                        text = f"{val:.1f}"
                elif isinstance(val, int):
                    text = str(val)
                else:
                    text = str(val) if val else "—"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                # Colour-code EV column
                if key == "launch_speed" and text != "—":
                    try:
                        ev = float(text)
                        if ev >= 100:
                            item.setForeground(QColor(80, 220, 100))
                        elif ev >= 95:
                            item.setForeground(QColor(180, 220, 80))
                        elif ev < 80:
                            item.setForeground(QColor(180, 100, 100))
                    except ValueError:
                        pass

                # Colour-code events
                if key == "events":
                    colour_map = {
                        "home_run":   QColor(255, 200, 50),
                        "triple":     QColor(100, 220, 255),
                        "double":     QColor(100, 180, 255),
                        "single":     QColor(160, 220, 160),
                        "foul":       QColor(120, 120, 120),
                    }
                    item.setForeground(colour_map.get(text, QColor(200, 200, 200)))

                self.setItem(row, col_idx, item)

    def _on_cell_clicked(self, row: int, _col: int):
        if self._player_id is None or row >= len(self._events):
            return
        record = self._events[row]
        record["player_id"] = self._player_id
        self.bbe_selected.emit(record)

    def clear_events(self):
        self._player_id = None
        self._events = []
        self.setRowCount(0)


# ---------------------------------------------------------------------------
# Page 0 – Search / player list
# ---------------------------------------------------------------------------
class _SearchPage(QWidget):
    """Full-panel player search and list.  Emits player_selected when a row is clicked."""
    player_selected      = pyqtSignal(int, str, str)   # player_id, name, team
    season_changed       = pyqtSignal(int)             # year
    player_type_changed  = pyqtSignal(str)             # "batter" or "pitcher"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #14141e;")
        self._store: BBEDataStore | None = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- header bar ----
        header = QWidget()
        header.setFixedHeight(36)
        header.setStyleSheet("background: #1e1e2e; border-bottom: 1px solid #333345;")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(6)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search player…")
        self.search.setStyleSheet(
            "background: #2a2a3a; color: #eee; border: 1px solid #444; "
            "border-radius: 4px; padding: 2px 6px; font-size: 11px;"
        )
        self.search.textChanged.connect(self._apply_filter)
        hl.addWidget(self.search, 2)

        # ---- hitters / pitchers toggle ----
        _toggle_style_active = (
            "background: #3a5a8a; color: #fff; border: 1px solid #4a6a9a; "
            "border-radius: 3px; font-size: 10px; padding: 1px 6px;"
        )
        _toggle_style_inactive = (
            "background: #2a2a3a; color: #888; border: 1px solid #444; "
            "border-radius: 3px; font-size: 10px; padding: 1px 6px;"
        )
        self._hitter_btn = QPushButton("Hit")
        self._hitter_btn.setFixedSize(34, 22)
        self._hitter_btn.setStyleSheet(_toggle_style_active)
        self._hitter_btn.clicked.connect(lambda: self._set_player_type("batter"))
        hl.addWidget(self._hitter_btn)

        self._pitcher_btn = QPushButton("Pit")
        self._pitcher_btn.setFixedSize(34, 22)
        self._pitcher_btn.setStyleSheet(_toggle_style_inactive)
        self._pitcher_btn.clicked.connect(lambda: self._set_player_type("pitcher"))
        hl.addWidget(self._pitcher_btn)

        self._player_type = "batter"
        self._toggle_style_active = _toggle_style_active
        self._toggle_style_inactive = _toggle_style_inactive

        self.team_cb = QComboBox()
        self.team_cb.setStyleSheet(
            "background: #2a2a3a; color: #eee; border: 1px solid #444; "
            "border-radius: 4px; font-size: 11px;"
        )
        self.team_cb.setMinimumWidth(70)
        self.team_cb.addItem("All Teams")
        self.team_cb.currentTextChanged.connect(self._apply_filter)
        hl.addWidget(self.team_cb, 1)

        self.season_cb = QComboBox()
        self.season_cb.setStyleSheet(
            "background: #2a2a3a; color: #eee; border: 1px solid #444; "
            "border-radius: 4px; font-size: 11px;"
        )
        self.season_cb.setFixedWidth(58)
        # Populated later by set_available_seasons()
        self.season_cb.currentTextChanged.connect(self._on_season_changed)
        hl.addWidget(self.season_cb, 0)
        root.addWidget(header)

        # ---- loading label ----
        self.loading_lbl = QLabel("Loading player data…")
        self.loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_lbl.setStyleSheet("color: #888; font-size: 12px; padding: 20px;")
        root.addWidget(self.loading_lbl)

        # ---- player list (fills remaining height) ----
        self.player_list_widget = QTableWidget(0, 3)
        self.player_list_widget.hide()
        self.player_list_widget.setHorizontalHeaderLabels(["Player", "Team", "Avg EV"])
        self.player_list_widget.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.player_list_widget.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.player_list_widget.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.player_list_widget.verticalHeader().setVisible(False)
        self.player_list_widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.player_list_widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.player_list_widget.setAlternatingRowColors(True)
        self.player_list_widget.setStyleSheet("""
            QTableWidget {
                background: #0e0e18;
                alternate-background-color: #16161f;
                color: #cccccc;
                gridline-color: #252530;
                font-size: 11px;
                border: none;
            }
            QHeaderView::section {
                background: #1c1c28;
                color: #999;
                border: none;
                padding: 3px;
                font-size: 10px;
            }
            QTableWidget::item:selected { background: #2a4060; }
        """)
        self.player_list_widget.cellClicked.connect(self._on_player_clicked)
        root.addWidget(self.player_list_widget, 1)

    def set_data_store(self, store: BBEDataStore):
        self._store = store
        self.loading_lbl.hide()
        self.player_list_widget.show()

        self.team_cb.blockSignals(True)
        self.team_cb.clear()
        self.team_cb.addItem("All Teams")
        for team in store.teams:
            self.team_cb.addItem(team)
        self.team_cb.blockSignals(False)

        self._apply_filter()

    def set_loading_message(self, msg: str):
        self.loading_lbl.setText(msg)
        self.loading_lbl.show()
        self.player_list_widget.hide()

    def set_available_seasons(self, years: list[int], current: int):
        """Populate the season combo and select the active year."""
        self.season_cb.blockSignals(True)
        self.season_cb.clear()
        for y in sorted(years, reverse=True):
            self.season_cb.addItem(str(y))
        idx = self.season_cb.findText(str(current))
        if idx >= 0:
            self.season_cb.setCurrentIndex(idx)
        self.season_cb.blockSignals(False)

    def _on_season_changed(self, text: str):
        if text:
            self.season_changed.emit(int(text))

    def _set_player_type(self, ptype: str):
        if ptype == self._player_type:
            return
        self._player_type = ptype
        if ptype == "pitcher":
            self._pitcher_btn.setStyleSheet(self._toggle_style_active)
            self._hitter_btn.setStyleSheet(self._toggle_style_inactive)
        else:
            self._hitter_btn.setStyleSheet(self._toggle_style_active)
            self._pitcher_btn.setStyleSheet(self._toggle_style_inactive)
        self.player_type_changed.emit(ptype)

    def _apply_filter(self):
        if self._store is None:
            return
        query = self.search.text().strip().lower()
        team  = self.team_cb.currentText()
        filtered = [
            (name, pid, t)
            for name, pid, t in self._store.player_list
            if (not query or query in name.lower())
            and (team == "All Teams" or t == team)
        ]
        self._populate_player_table(filtered)

    def _populate_player_table(self, players: list):
        self.player_list_widget.setRowCount(0)
        lb = self._store.leaderboard if self._store else pd.DataFrame()

        for name, pid, team in players:
            row = self.player_list_widget.rowCount()
            self.player_list_widget.insertRow(row)

            avg_ev = "—"
            if not lb.empty and "player_id" in lb.columns and "avg_hit_speed" in lb.columns:
                match = lb[lb["player_id"] == pid]
                if not match.empty:
                    v = match.iloc[0].get("avg_hit_speed")
                    avg_ev = f"{v:.1f}" if pd.notna(v) else "—"

            for col_idx, text in enumerate([name, team, avg_ev]):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, pid)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.player_list_widget.setItem(row, col_idx, item)

    def _on_player_clicked(self, row: int, _col: int):
        if self._store is None:
            return
        item = self.player_list_widget.item(row, 0)
        if item is None:
            return
        pid  = item.data(Qt.ItemDataRole.UserRole)
        name = item.text()
        team_item = self.player_list_widget.item(row, 1)
        team = team_item.text() if team_item else "—"
        self.player_selected.emit(pid, name, team)


# ---------------------------------------------------------------------------
# Page 1 – Player detail (back button + summary + full BBE table)
# ---------------------------------------------------------------------------
class _DetailPage(QWidget):
    """Full-panel player detail view with back button."""
    back_clicked = pyqtSignal()
    bbe_selected = pyqtSignal(object)   # dict: full BBE event record
    play_spray   = pyqtSignal(object, bool)  # events, include_fouls
    clear_spray  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #14141e;")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- back bar ----
        back_bar = QWidget()
        back_bar.setFixedHeight(36)
        back_bar.setStyleSheet("background: #1e1e2e; border-bottom: 1px solid #333345;")
        bl = QHBoxLayout(back_bar)
        bl.setContentsMargins(6, 0, 8, 0)
        bl.setSpacing(6)

        back_btn = QPushButton("◀  Player List")
        back_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #7ab0e0;
                border: none;
                font-size: 12px;
                padding: 2px 6px;
                text-align: left;
            }
            QPushButton:hover { color: #ffffff; }
        """)
        back_btn.clicked.connect(self.back_clicked)
        bl.addWidget(back_btn)
        bl.addStretch()
        root.addWidget(back_bar)

        # ---- player summary (fixed height — headshot + bars) ----
        self.summary = PlayerSummaryWidget()
        # Calculate fixed height: top margin(8) + headshot(67) + divider(~8) +
        # 7 bars × 22px + spacing(6×8) + bottom margin(6) ≈ 285
        self.summary.setFixedHeight(285)
        root.addWidget(self.summary)

        # ---- BBE header + spray controls ----
        bbe_bar = QWidget()
        bbe_bar.setFixedHeight(26)
        bbe_bar.setStyleSheet(
            "background: #1e1e2e; border-top: 1px solid #333345; "
            "border-bottom: 1px solid #333345;"
        )
        bb_lay = QHBoxLayout(bbe_bar)
        bb_lay.setContentsMargins(6, 0, 6, 0)
        bb_lay.setSpacing(6)

        bbe_lbl = QLabel("BBE Events")
        bbe_lbl.setStyleSheet("color: #aaa; font-size: 10px; border: none;")
        bb_lay.addWidget(bbe_lbl)

        self._pitch_filter = QComboBox()
        self._pitch_filter.setStyleSheet(
            "QComboBox { background: #2a2a3a; color: #ccc; border: 1px solid #444; "
            "border-radius: 3px; font-size: 10px; padding: 1px 4px; }"
            "QComboBox::drop-down { border: none; }"
        )
        self._pitch_filter.setFixedWidth(82)
        self._pitch_filter.addItem("All Pitches")
        self._pitch_filter.currentTextChanged.connect(self._on_pitch_filter_changed)
        bb_lay.addWidget(self._pitch_filter)

        bb_lay.addStretch()

        self._fouls_cb = QCheckBox("Fouls")
        self._fouls_cb.setChecked(False)
        self._fouls_cb.setStyleSheet(
            "QCheckBox { color: #888; font-size: 10px; border: none; }"
            "QCheckBox::indicator { width: 12px; height: 12px; }"
        )
        bb_lay.addWidget(self._fouls_cb)

        _btn_style = """
            QPushButton {
                background: #2a4a6a; color: #ccc; border: 1px solid #3a5a7a;
                border-radius: 3px; font-size: 10px;
            }
            QPushButton:hover { background: #3a5a8a; color: #fff; }
        """
        self._play_btn = QPushButton("▶ Play")
        self._play_btn.setFixedSize(58, 20)
        self._play_btn.setStyleSheet(_btn_style)
        self._play_btn.clicked.connect(self._on_play_clicked)
        bb_lay.addWidget(self._play_btn)

        self._clear_btn = QPushButton("✕ Clear")
        self._clear_btn.setFixedSize(58, 20)
        self._clear_btn.setStyleSheet(_btn_style)
        self._clear_btn.clicked.connect(self.clear_spray)
        bb_lay.addWidget(self._clear_btn)
        root.addWidget(bbe_bar)

        # ---- BBE table fills all remaining height ----
        self.bbe_table = BBETableWidget()
        self.bbe_table.bbe_selected.connect(self.bbe_selected)
        root.addWidget(self.bbe_table, 1)

        self._current_events: list = []
        self._all_events: list = []
        self._current_player_id: int = 0

    def load_player(self, name: str, team: str, player_id: int,
                    lb_row, percentiles: dict, events: list):
        self.summary.load_player(name, team, player_id, lb_row, percentiles)
        self._all_events = events
        self._current_player_id = player_id

        # Populate pitch-type filter from this player's events
        pitch_types = sorted({
            str(e.get("pitch_name", ""))
            for e in events if e.get("pitch_name")
        })
        self._pitch_filter.blockSignals(True)
        self._pitch_filter.clear()
        self._pitch_filter.addItem("All Pitches")
        for pt in pitch_types:
            self._pitch_filter.addItem(pt)
        self._pitch_filter.blockSignals(False)

        self._apply_pitch_filter()

    def _on_pitch_filter_changed(self, _text: str):
        self._apply_pitch_filter()

    def _apply_pitch_filter(self):
        """Filter events by selected pitch type and reload table."""
        selected = self._pitch_filter.currentText()
        if selected == "All Pitches":
            self._current_events = self._all_events
        else:
            self._current_events = [
                e for e in self._all_events
                if str(e.get("pitch_name", "")) == selected
            ]
        self.bbe_table.load_events(self._current_events,
                                   getattr(self, "_current_player_id", 0))

    def _on_play_clicked(self):
        if self._current_events:
            self.play_spray.emit(self._current_events, self._fouls_cb.isChecked())


# ---------------------------------------------------------------------------
# Inner content widget — stacked search + detail pages
# ---------------------------------------------------------------------------
class _OverlayContent(QWidget):
    player_changed  = pyqtSignal(int)
    player_selected_with_data = pyqtSignal(int, object)  # player_id, list of BBE dicts
    play_spray      = pyqtSignal(object, bool)           # events, include_fouls
    clear_spray     = pyqtSignal()
    bbe_selected    = pyqtSignal(object)               # dict: full BBE event record
    season_changed  = pyqtSignal(int)                  # year
    player_type_changed = pyqtSignal(str)              # "batter" or "pitcher"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #14141e; border-left: 1px solid #333345;")
        self._store: BBEDataStore | None = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()

        # Page 0 — search
        self._search_page = _SearchPage()
        self._search_page.player_selected.connect(self._on_player_selected)
        self._search_page.season_changed.connect(self.season_changed)
        self._search_page.player_type_changed.connect(self.player_type_changed)
        self._stack.addWidget(self._search_page)

        # Page 1 — detail
        self._detail_page = _DetailPage()
        self._detail_page.back_clicked.connect(self._go_back)
        self._detail_page.bbe_selected.connect(self.bbe_selected)
        self._detail_page.play_spray.connect(self.play_spray)
        self._detail_page.clear_spray.connect(self.clear_spray)
        self._stack.addWidget(self._detail_page)

        root.addWidget(self._stack, 1)

    # ------------------------------------------------------------------ #
    # Public API (kept compatible with existing callers)
    # ------------------------------------------------------------------ #
    def set_data_store(self, store: BBEDataStore):
        self._store = store
        self._search_page.set_data_store(store)
        self._stack.setCurrentIndex(0)

    def set_loading_message(self, msg: str):
        self._search_page.set_loading_message(msg)

    def set_available_seasons(self, years: list[int], current: int):
        self._search_page.set_available_seasons(years, current)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def _on_player_selected(self, pid: int, name: str, team: str):
        if self._store is None:
            return

        lb_row = None
        if "player_id" in self._store.leaderboard.columns:
            match = self._store.leaderboard[self._store.leaderboard["player_id"] == pid]
            if not match.empty:
                lb_row = match.iloc[0]

        events = self._store.by_player.get(pid, [])
        self._detail_page.load_player(name, team, pid, lb_row, self._store.percentiles, events)
        self._stack.setCurrentIndex(1)
        self.player_changed.emit(pid)
        self.player_selected_with_data.emit(pid, events)

    def _go_back(self):
        self._stack.setCurrentIndex(0)
        self.player_selected_with_data.emit(0, [])  # clear signal


# ---------------------------------------------------------------------------
# Inline sidebar panel — lives in the layout next to the 3D viewport
# ---------------------------------------------------------------------------
class PlayerBBEOverlay(QWidget):
    """
    Collapsible sidebar that sits in the same QHBoxLayout as the UmpireView3D.
    When collapsed only a narrow toggle button is visible.  When expanded the
    content panel slides open and the 3D viewport shrinks to make room — no
    native-window overlay hacks needed.
    """
    simulate_bbe = pyqtSignal(object)   # dict: full BBE event record
    player_selected_with_data = pyqtSignal(int, object)  # player_id, list of BBE dicts
    play_spray = pyqtSignal(object, bool)               # events, include_fouls
    clear_spray = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PlayerBBEOverlay")
        self._expanded  = False
        self._loader_thread: QThread | None = None
        self._current_year: int = 0
        self._available_years: list[int] = []
        self._player_type: str = "batter"
        self._store_cache: dict[tuple[int, str], BBEDataStore] = {}  # (year, player_type) → store

        self._build_ui()
        self._build_animation()

        # Start collapsed
        self.panel.hide()
        self.panel.setMaximumWidth(0)
        self.panel.setMinimumWidth(0)

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- toggle tab (always visible) ----
        self.toggle_btn = QPushButton("◀")
        self.toggle_btn.setFixedWidth(24)
        self.toggle_btn.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.toggle_btn.setStyleSheet("""
            QPushButton {
                background: #22223a;
                color: #aaaacc;
                border: none;
                border-left: 1px solid #333345;
                border-radius: 0px;
                font-size: 14px;
            }
            QPushButton:hover { background: #2a2a4a; color: #ffffff; }
        """)
        self.toggle_btn.clicked.connect(self.toggle)
        outer.addWidget(self.toggle_btn)

        # ---- sliding content panel ----
        self.panel = _OverlayContent()
        self.panel.setMinimumWidth(0)
        self.panel.setMaximumWidth(0)
        self.panel.bbe_selected.connect(self.simulate_bbe)
        self.panel.player_selected_with_data.connect(self.player_selected_with_data)
        self.panel.play_spray.connect(self.play_spray)
        self.panel.clear_spray.connect(self.clear_spray)
        self.panel.season_changed.connect(self._on_season_changed)
        self.panel.player_type_changed.connect(self._on_player_type_changed)
        outer.addWidget(self.panel)

    def _build_animation(self):
        self._anim_min = QPropertyAnimation(self.panel, b"minimumWidth")
        self._anim_min.setDuration(ANIM_DURATION_MS)
        self._anim_min.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_max = QPropertyAnimation(self.panel, b"maximumWidth")
        self._anim_max.setDuration(ANIM_DURATION_MS)
        self._anim_max.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_max.finished.connect(self._on_anim_finished)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def attach(self, umpire_view: QWidget):
        """Kept for API compatibility — no-op since we're in the layout now."""
        pass

    def position_overlay(self):
        """Kept for API compatibility — no-op since layout handles sizing."""
        pass

    def toggle(self):
        if self._expanded:
            self._collapse()
        else:
            self._expand()

    def load_data(self, bbe_csv: str, leaderboard_csv: str | None = None,
                  year: int = 2024, player_type: str = "batter"):
        """Start background data load for a given season."""
        self._player_type = player_type

        # Detect available season CSVs on disk for the current player type
        self._available_years = self._detect_years(player_type) or [year]

        self._current_year = year
        self.panel.set_available_seasons(self._available_years, year)

        self._load_year(bbe_csv, leaderboard_csv, year)

    def _detect_years(self, player_type: str) -> list[int]:
        """Find available season years on disk for the given player type."""
        import glob, re
        if player_type == "pitcher":
            pattern = "savant_pitcher-bbe_*.csv"
            regex = r"savant_pitcher-bbe_(\d{4})\.csv$"
        else:
            pattern = "savant_bbe_*.csv"
            regex = r"savant_bbe_(\d{4})\.csv$"
        return sorted({
            int(m.group(1))
            for f in glob.glob(pattern)
            if (m := re.search(regex, f))
        }, reverse=True)

    def _load_year(self, bbe_csv: str, leaderboard_csv: str | None, year: int):
        """Kick off the background loader thread (cache hit skips the thread)."""
        cache_key = (year, self._player_type)
        if cache_key in self._store_cache:
            self.panel.set_loading_message(f"Restoring {year} {'pitcher' if self._player_type == 'pitcher' else 'hitter'} data…")
            self._on_data_ready(self._store_cache[cache_key])
            return

        # Kill previous loader if still running
        if self._loader_thread and self._loader_thread.isRunning():
            self._loader_thread.quit()
            self._loader_thread.wait(2000)

        self.panel.set_loading_message(f"Loading {year} {'pitcher' if self._player_type == 'pitcher' else 'hitter'} data…")
        worker = DataLoaderWorker(bbe_csv, leaderboard_csv, year=year,
                                  player_type=self._player_type)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._on_data_ready)
        worker.progress.connect(self.panel.set_loading_message)
        worker.error.connect(lambda msg: self.panel.set_loading_message(f"Error: {msg[:80]}"))
        thread.started.connect(worker.run)
        thread.start()
        self._loader_thread = thread
        self._loader_worker = worker

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _expand(self):
        self._expanded = True
        self.toggle_btn.setText("▶")
        self.panel.show()
        self._anim_min.setStartValue(0)
        self._anim_min.setEndValue(OVERLAY_WIDTH_EXPANDED)
        self._anim_max.setStartValue(0)
        self._anim_max.setEndValue(OVERLAY_WIDTH_EXPANDED)
        self._anim_min.start()
        self._anim_max.start()

    def _collapse(self):
        self._expanded = False
        self.toggle_btn.setText("◀")
        cur = self.panel.width()
        self._anim_min.setStartValue(cur)
        self._anim_min.setEndValue(0)
        self._anim_max.setStartValue(cur)
        self._anim_max.setEndValue(0)
        self._anim_min.start()
        self._anim_max.start()

    def _on_anim_finished(self):
        if not self._expanded:
            self.panel.hide()
            self.panel.setMinimumWidth(0)
            self.panel.setMaximumWidth(0)

    def _on_season_changed(self, year: int):
        """User picked a different season in the dropdown."""
        if year == self._current_year:
            return
        self._current_year = year
        self._reload_for_current_type(year)

    def _on_player_type_changed(self, ptype: str):
        """User toggled between Hitters and Pitchers."""
        if ptype == self._player_type:
            return
        self._player_type = ptype

        # Re-detect available years for this player type
        self._available_years = self._detect_years(ptype) or [self._current_year]
        year = self._available_years[0]  # most recent
        self._current_year = year
        self.panel.set_available_seasons(self._available_years, year)

        self._reload_for_current_type(year)

    def _reload_for_current_type(self, year: int):
        """Reload data for the current player_type and year."""
        import os
        if self._player_type == "pitcher":
            bbe_csv = f"savant_pitcher-bbe_{year}.csv"
        else:
            bbe_csv = f"savant_bbe_{year}.csv"
        lb_csv = f"savant_lb_{year}.csv"
        lb_csv = lb_csv if os.path.exists(lb_csv) else None
        self._load_year(bbe_csv, lb_csv, year)
        # Clear spray chart via empty signal
        self.player_selected_with_data.emit(0, [])

    def _on_data_ready(self, store: BBEDataStore):
        self._store_cache[(self._current_year, self._player_type)] = store
        self.panel.set_data_store(store)
        if self._loader_thread:
            self._loader_thread.quit()
