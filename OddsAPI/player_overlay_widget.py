"""
player_overlay.py
-----------------
Collapsible player selection overlay for the MLB Ball Flight Simulator.

Sits as a child widget over the UmpireView3D OpenGL panel.  Does not
participate in any layout — positioned absolutely and animated via
QPropertyAnimation so the 3D view renders uninterrupted behind it.

Architecture:
  PlayerBBEOverlay          — outer shell, toggle button, animation
    └── _OverlayContent     — scroll area containing all inner widgets
          ├── search + team filter bar
          ├── PlayerSummaryWidget   — headshot, name, team, percentile bars
          └── BBETableWidget        — scrollable event list

Data flow:
  BBEDataStore (loaded in QThread on startup)
    → PlayerBBEOverlay.set_data_store()
    → player selected → PlayerSummaryWidget populated
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
    QProgressBar,
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
    "?type=batter&year=2024&position=&team=&min=1&csv=true"
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

    def __init__(self, bbe_csv_path: str, leaderboard_csv_path: str | None = None):
        super().__init__()
        self.bbe_csv_path        = bbe_csv_path
        self.leaderboard_csv_path = leaderboard_csv_path

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
                r = requests.get(LEADERBOARD_URL, timeout=30)
                r.raise_for_status()
                lb = pd.read_csv(io.StringIO(r.text))

            lb.columns = [c.strip().lower().replace(" ", "_").replace(",", "") for c in lb.columns]
            # canonical id column
            for cand in ("player_id", "batter", "mlbam_id"):
                if cand in lb.columns:
                    lb = lb.rename(columns={cand: "player_id"})
                    break
            lb["player_id"] = lb["player_id"].astype(int)
            store.leaderboard = lb

            # ---- percentiles ----
            self.progress.emit("Computing percentiles…")
            pcts = {}
            for _, col, higher_is_better in PERCENTILE_STATS:
                if col not in lb.columns:
                    continue
                vals = lb[col].dropna().values
                for _, row in lb.iterrows():
                    pid = int(row["player_id"])
                    v   = row.get(col)
                    if pd.isna(v):
                        pct = 50.0
                    else:
                        rank = np.sum(vals <= v)
                        pct  = rank / len(vals) * 100
                        if not higher_is_better:
                            pct = 100 - pct
                    pcts.setdefault(pid, {})[col] = round(pct, 1)
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
            team_col = next(
                (c for c in ("team_name_abbrev", "team", "team_abbrev") if c in lb.columns),
                None
            )

            player_list = []
            for _, row in lb.iterrows():
                pid  = int(row["player_id"])
                name = str(row[name_col]) if name_col else f"ID {pid}"
                team = str(row[team_col]) if team_col else "—"
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
# Player summary widget (headshot + name + percentile bars)
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
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        # ---- top row: headshot + identity ----
        top = QHBoxLayout()
        top.setSpacing(10)

        self.headshot_lbl = QLabel()
        self.headshot_lbl.setFixedSize(67, 67)
        self.headshot_lbl.setStyleSheet(
            "border-radius: 6px; background: #2a2a35; border: 1px solid #444;"
        )
        self.headshot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.headshot_lbl)

        identity = QVBoxLayout()
        identity.setSpacing(2)
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
        identity.addStretch()
        top.addLayout(identity)
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

        root.addStretch()

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
            # Show placeholder while fetching
            self.headshot_lbl.setText("…")
            url = HEADSHOT_URL.format(player_id=player_id)
            req = QNetworkRequest(QUrl(url))
            req.setAttribute(
                QNetworkRequest.Attribute.User,
                player_id
            )
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
    bbe_selected = pyqtSignal(float, float, int)   # ev, launch_angle, player_id

    COLS = [
        ("Date",     "game_date"),
        ("Event",    "events"),
        ("EV",       "launch_speed"),
        ("LA°",      "launch_angle"),
        ("Dist",     "hit_distance_sc"),
        ("BB Type",  "bb_type"),
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

    def load_events(self, events: list, player_id: int):
        self._player_id = player_id
        self.setRowCount(0)
        for record in events:
            row = self.rowCount()
            self.insertRow(row)
            for col_idx, (_, key) in enumerate(self.COLS):
                val = record.get(key, "")
                if isinstance(val, float):
                    text = "—" if math.isnan(val) else f"{val:.1f}"
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
        if self._player_id is None:
            return
        try:
            ev  = float(self.item(row, 2).text())
            la  = float(self.item(row, 3).text())
            self.bbe_selected.emit(ev, la, self._player_id)
        except (ValueError, AttributeError):
            pass

    def clear_events(self):
        self._player_id = None
        self.setRowCount(0)


# ---------------------------------------------------------------------------
# Inner content widget (everything inside the sliding panel)
# ---------------------------------------------------------------------------
class _OverlayContent(QWidget):
    player_changed  = pyqtSignal(int)          # player_id
    bbe_selected    = pyqtSignal(float, float, int)  # ev, la, player_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #14141e; border-left: 1px solid #333345;")
        self._store: BBEDataStore | None = None
        self._filtered_players: list     = []
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

        self.team_cb = QComboBox()
        self.team_cb.setStyleSheet(
            "background: #2a2a3a; color: #eee; border: 1px solid #444; "
            "border-radius: 4px; font-size: 11px;"
        )
        self.team_cb.setMinimumWidth(70)
        self.team_cb.addItem("All Teams")
        self.team_cb.currentTextChanged.connect(self._apply_filter)
        hl.addWidget(self.team_cb, 1)
        root.addWidget(header)

        # ---- loading label (shown until data ready) ----
        self.loading_lbl = QLabel("Loading player data…")
        self.loading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_lbl.setStyleSheet("color: #888; font-size: 12px; padding: 20px;")
        root.addWidget(self.loading_lbl)

        # ---- main content (hidden until ready) ----
        self.content_frame = QWidget()
        self.content_frame.hide()
        cf_layout = QVBoxLayout(self.content_frame)
        cf_layout.setContentsMargins(0, 0, 0, 0)
        cf_layout.setSpacing(0)

        # Player list
        self.player_list_widget = QTableWidget(0, 3)
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
        self.player_list_widget.setFixedHeight(180)
        self.player_list_widget.setStyleSheet("""
            QTableWidget {
                background: #0e0e18;
                alternate-background-color: #16161f;
                color: #cccccc;
                gridline-color: #252530;
                font-size: 11px;
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
        self.player_list_widget.setAlternatingRowColors(True)
        self.player_list_widget.cellClicked.connect(self._on_player_clicked)
        cf_layout.addWidget(self.player_list_widget)

        # Summary
        self.summary = PlayerSummaryWidget()
        cf_layout.addWidget(self.summary)

        # BBE label
        bbe_hdr = QLabel("  BBE Events")
        bbe_hdr.setFixedHeight(22)
        bbe_hdr.setStyleSheet(
            "background: #1e1e2e; color: #aaa; font-size: 10px; "
            "border-top: 1px solid #333345; border-bottom: 1px solid #333345;"
        )
        cf_layout.addWidget(bbe_hdr)

        # BBE table
        self.bbe_table = BBETableWidget()
        self.bbe_table.bbe_selected.connect(self.bbe_selected)
        cf_layout.addWidget(self.bbe_table, 1)

        root.addWidget(self.content_frame, 1)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def set_data_store(self, store: BBEDataStore):
        self._store = store
        self.loading_lbl.hide()
        self.content_frame.show()

        # Populate team filter
        self.team_cb.blockSignals(True)
        self.team_cb.clear()
        self.team_cb.addItem("All Teams")
        for team in store.teams:
            self.team_cb.addItem(team)
        self.team_cb.blockSignals(False)

        self._apply_filter()

    def set_loading_message(self, msg: str):
        self.loading_lbl.setText(msg)

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #
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
        self._filtered_players = filtered
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

        lb_row = None
        if "player_id" in self._store.leaderboard.columns:
            match = self._store.leaderboard[self._store.leaderboard["player_id"] == pid]
            if not match.empty:
                lb_row = match.iloc[0]

        self.summary.load_player(name, team, pid, lb_row, self._store.percentiles)

        events = self._store.by_player.get(pid, [])
        self.bbe_table.load_events(events, pid)
        self.player_changed.emit(pid)


# ---------------------------------------------------------------------------
# Outer overlay shell with slide animation and toggle button
# ---------------------------------------------------------------------------
class PlayerBBEOverlay(QWidget):
    """
    Overlay panel parented directly to the UmpireView3D widget.
    Call attach(umpire_view) after construction, then position_overlay()
    whenever the parent resizes.
    """
    simulate_bbe = pyqtSignal(float, float, int)   # ev, launch_angle, player_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PlayerBBEOverlay")
        self._expanded  = False
        self._loader_thread: QThread | None = None

        self._build_ui()
        self._build_animation()

        # WA_NativeWindow gives the overlay its own OS compositor layer,
        # which renders above the QOpenGLWidget framebuffer regardless of
        # Qt Z-order. Without this the GL surface paints over us at the OS level.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        # Start collapsed — panel hidden so no content bleeds through
        self.panel.hide()
        self.panel.setFixedWidth(0)

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sliding content panel ----
        self.panel = _OverlayContent()
        self.panel.setFixedWidth(OVERLAY_WIDTH_COLLAPSED)
        self.panel.bbe_selected.connect(self.simulate_bbe)
        outer.addWidget(self.panel)

        # ---- toggle tab (always visible, sits on right edge of panel) ----
        self.toggle_btn = QPushButton("◀")
        self.toggle_btn.setFixedSize(24, 80)
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

    def _build_animation(self):
        self._anim = QPropertyAnimation(self.panel, b"minimumWidth")
        self._anim.setDuration(ANIM_DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def attach(self, umpire_view: QWidget):
        """Store reference to the umpire view for geometry tracking.
        The overlay is already parented to SplitView via __init__ — we just
        need the umpire_view reference so position_overlay() can align to it.
        """
        self._umpire_view = umpire_view
        # Reposition whenever the umpire view resizes
        _orig = umpire_view.resizeEvent
        def _on_resize(event):
            _orig(event)
            self.position_overlay()
        umpire_view.resizeEvent = _on_resize
        self.position_overlay()

    def position_overlay(self):
        """Position overlay over the umpire view rect within SplitView.
        Called on umpire_view resize and on toggle so geometry stays correct.
        """
        if not hasattr(self, '_umpire_view') or self.parent() is None:
            return
        # Map umpire_view top-left into SplitView coordinates
        par = self.parent()
        uv  = self._umpire_view
        if par is None or uv is None:
            return

        # X: anchor to right edge of SplitView (par.width() is reliable)
        # Y: map umpire_view top-left into SplitView coords to skip the
        #    wind widget and stats bar that sit above the views area
        # H: map umpire_view bottom into SplitView coords for exact height
        pw   = par.width()
        uv_top    = uv.mapTo(par, uv.rect().topLeft())
        uv_bottom = uv.mapTo(par, uv.rect().bottomLeft())
        y  = uv_top.y()
        uh = uv_bottom.y() - y
        # Clamp to sensible values in case mapTo fires before layout is done
        if uh <= 0:
            uh = par.height() - y

        tab_w   = 24
        panel_w = OVERLAY_WIDTH_EXPANDED if self._expanded else OVERLAY_WIDTH_COLLAPSED
        total_w = panel_w + tab_w
        x = pw - total_w
        print(f"[overlay] y={y} uh={uh} -> overlay=({x},{y},{total_w},{uh})")
        self.setGeometry(x, y, total_w, uh)
        self.panel.setFixedWidth(panel_w)
        self.raise_()

    def toggle(self):
        if self._expanded:
            self._collapse()
        else:
            self._expand()

    def load_data(self, bbe_csv: str, leaderboard_csv: str | None = None):
        """Start background data load.  Call once after attach()."""
        self.panel.set_loading_message("Loading player data…")
        worker = DataLoaderWorker(bbe_csv, leaderboard_csv)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._on_data_ready)
        worker.progress.connect(self.panel.set_loading_message)
        worker.error.connect(lambda msg: self.panel.set_loading_message(f"Error: {msg[:80]}"))
        thread.started.connect(worker.run)
        thread.start()
        self._loader_thread = thread
        self._loader_worker = worker   # keep reference

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _expand(self):
        self._expanded = True
        self.toggle_btn.setText("▶")
        self.panel.show()
        self._anim.setStartValue(0)
        self._anim.setEndValue(OVERLAY_WIDTH_EXPANDED)
        self._anim.start()
        self.position_overlay()

    def _collapse(self):
        self._expanded = False
        self.toggle_btn.setText("◀")
        self._anim.setStartValue(self.panel.width())
        self._anim.setEndValue(0)
        self._anim.start()

    def _on_anim_finished(self):
        if not self._expanded:
            self.panel.hide()
            self.panel.setFixedWidth(0)
        self.position_overlay()

    def _on_data_ready(self, store: BBEDataStore):
        self.panel.set_data_store(store)
        if self._loader_thread:
            self._loader_thread.quit()
