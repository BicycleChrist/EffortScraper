# ── Debug / verbose-output switch ──────────────────────────────────────
# Master toggle for the app's verbose stdout logging. The worker modules
# (odds / prophetx / polymarket / kalshi loaders, the league dump, the
# per-game and per-token loops, etc.) print large volumes to stdout during
# startup — hundreds of synchronous writes that hold the GIL and stalled
# the ticker by 100-400ms. With DEBUG = False we install a process-wide
# gate that drops those stdout prints; flip to True to restore the full
# diagnostic firehose.
#
# Only plain stdout print() is gated. Anything sent to a file/stream
# explicitly — print(..., file=sys.stderr) — passes through untouched, and
# exceptions / traceback.print_exc() (stderr) are never affected, so real
# errors still surface even with DEBUG off.
DEBUG = False

import builtins as _builtins
_real_print = _builtins.print  # kept so internal diagnostics (e.g. the loop-lag monitor) can bypass the gate
def _gated_print(*args, **kwargs):
    if DEBUG or kwargs.get("file") is not None:
        _real_print(*args, **kwargs)
_builtins.print = _gated_print
# ───────────────────────────────────────────────────────────────────────

import pathlib
from datetime import datetime, timezone
import aiohttp
import json
from PyQt6.QtCore import Qt, QObject, QEvent, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QRect, QRectF, QPointF, pyqtProperty, QThread, QUrl
from PyQt6.QtGui import QColor, QBrush, QPainter, QPen, QIcon, QFont, QFontMetrics, QLinearGradient, QRadialGradient, QPainterPath, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QHBoxLayout, QFrame, QSizePolicy, QGridLayout, QSplitter, QLineEdit,
    QInputDialog, QScrollArea
)
from propQuery import PropClient
from OddsAPIQuery import league_query, odds_query, scores_query, get_game_status
from Creds import SUPER_KEY
from marketKeys import *
from EffortOddsPropsWindow import PropsWindow
import pandas as pd
from GUIteamnewswidget import TeamNewsWidget
from GUItuneinwidget import TuneInWidget
from GUIbestlineswidget import *
from HistoricalOddsClient import *
from TTwindow import TableTennisGUI
from effortcalculator import CalculatorApp
from tickertape import TickerTape
from radio_widget import ShortWaveRadioWidget
from polymarketquery import fetch_and_process_markets
from prediction_markets_worker import PredictionMarketsWorker
from LiquidityWidget import ProphetXBrowser
from prophetx_async import ProphetXWorker
import feedparser
import traceback
import qasync
import asyncio
  # Use SUPER_KEY since ODDS_API_KEY is commented out
#TODO: MMA (Mixed Marital Arts) Markets ouput is nuked, gotta investigate that one
#TODO: Auto update cuts off last few market lines??
#TODO: change layout of stream-links controls from horizontal to vertical - giving more space to the listbox
#TODO: Explore/investigate threads(Zuck) API for team/player news from beat reporters
DEBUG_OUTLINES = False

MAJOR_PROP_LEAGUES = {
    "basketball_nba": NBA_MARKETS,
    "baseball_mlb": MLB_MARKETS,
    "icehockey_nhl": NHL_MARKETS,
    "football_nfl": NFL_MARKETS,
    "aussierules_afl": AFL_MARKETS,
    "soccer_usa_mls": SOCCER_MARKETS
}

REGULAR_MARKETS = {"spreads"} # "h2h", "spreads", "totals"

class ColoredTableItem(QTableWidgetItem):
    """Custom table item that stores game ID for color coordination"""
    def __init__(self, text, game_id):
        super().__init__(text)
        self.game_id = game_id




class LeagueTabData:
    """Manages data and display state for each league tab"""
    def __init__(self, league_name, sport_key):
        self.league_name = league_name
        self.sport_key = sport_key
        self.num_rows = 0
        self.num_cols = 0
        self.table_rows = []
        self.table_data = {}
        self.game_colors = {}
        self.current_color_index = 0
        self.bookmakers = []
        self.previous_data = {}
        self.game_status = {}
        # Betslip deep-links (from the Odds API includeLinks flag):
        #   cell_links[(row_label, bm_title)]    -> outcome betslip link (auto-populates a slip)
        #   bookmaker_links[(game_id, bm_title)] -> event-page link (fallback when no outcome link)
        # bookmaker_links MUST be keyed by game too: a book's event link differs
        # per game, so keying by title alone makes every game share the last one.
        self.cell_links = {}
        self.bookmaker_links = {}
        self.color_palette = [
            QColor(232, 240, 254),  # Sky Blue
            QColor(240, 247, 255),  # Ice Blue
            QColor(230, 255, 230),  # Mint
            QColor(240, 255, 240),  # Honeydew
            QColor(255, 240, 240),  # Misty Rose
            QColor(255, 245, 245),  # Lavender Blush
            QColor(245, 240, 255),  # Lavender
            QColor(240, 230, 255),  # Periwinkle
            QColor(255, 255, 240),  # Ivory
            QColor(255, 250, 240),  # Floral White
            QColor(240, 255, 255),  # Azure
            QColor(245, 255, 250),  # Mint Cream
            QColor(255, 245, 230),  # Peach
            QColor(245, 245, 245),  # White Smoke
            QColor(240, 248, 255),  # Alice Blue
            QColor(248, 248, 255),  # Ghost White
            QColor(255, 248, 220),  # Cornsilk
            QColor(240, 255, 244),  # Soft Mint
            QColor(255, 240, 245),  # Pink Snow
            QColor(245, 240, 240),  # Soft Pink
        ]
        self.table_widget = None
        self.last_update_time = None

    def create_table_widget(self):
        """Create and configure a new table widget for this league"""
        self.table_widget = QTableWidget()
        self.table_widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table_widget.updateGeometry()
        # Style the header
        header = self.table_widget.horizontalHeader()
        header.setStyleSheet("""
            QHeaderView::section {
                background-color: #2C3E50;
                color: white;
                padding: 4px;
                border: 1px solid #34495E;
            }
        """)
        return self.table_widget

    def get_game_color(self, game_id):
        """Get or assign a color for a specific game"""
        if game_id not in self.game_colors:
            self.game_colors[game_id] = self.color_palette[self.current_color_index]
            self.current_color_index = (self.current_color_index + 1) % len(self.color_palette)
        return self.game_colors[game_id]

    def to_dataframe(self):
        """Convert the current table data to a pandas DataFrame"""
        # Create a DataFrame with rows as indices and bookmakers as columns
        df = pd.DataFrame(index=self.table_rows, columns=self.bookmakers)

        # Fill the DataFrame with current odds values
        for row_label in self.table_rows:
            row_data = self.table_data.get(row_label, {})
            for bm in self.bookmakers:
                df.at[row_label, bm] = row_data.get(bm, "")

        # Add game_id and is_header columns for reference
        df['game_id'] = [self.table_data.get(row, {}).get('game_id', '') for row in self.table_rows]
        df['is_header'] = [self.table_data.get(row, {}).get('is_header', False) for row in self.table_rows]

        return df

    def update_from_dataframe(self, df):
        """Update table data from a pandas DataFrame"""
        # Update bookmakers list if needed
        bm_columns = [col for col in df.columns if col not in ['game_id', 'is_header']]
        for bm in bm_columns:
            if bm not in self.bookmakers:
                self.bookmakers.append(bm)

        # Update rows
        self.table_rows = df.index.tolist()

        # Update table data
        for row_label in self.table_rows:
            if row_label not in self.table_data:
                self.table_data[row_label] = {}

            # Set game_id and is_header
            self.table_data[row_label]['game_id'] = df.at[row_label, 'game_id']
            self.table_data[row_label]['is_header'] = df.at[row_label, 'is_header']

            # Set bookmaker data
            for bm in bm_columns:
                if pd.notna(df.at[row_label, bm]) and df.at[row_label, bm] != "":
                    self.table_data[row_label][bm] = df.at[row_label, bm]








#TODO: Make this work, spinbox not allowing for interval selection for update interval
class UpdateProgressIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.progress = 0

    def setProgress(self, value):
        self.progress = value
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw background circle
        painter.setPen(QPen(QColor("#e9ecef"), 2))
        painter.drawEllipse(2, 2, 16, 16)

        # Draw progress arc
        painter.setPen(QPen(QColor("#007bff"), 2))
        angle = int(-self.progress * 360)
        painter.drawArc(2, 2, 16, 16, 90 * 16, angle * 16)




class DataManager(QObject):
    """Manages data fetching and processing for odds data"""
    games_updated = pyqtSignal(list)
    odds_updated = pyqtSignal(dict, str)  # Added league_name parameter

    def __init__(self):
        super().__init__()
        self.sport_key = None
        self.prop_client = None
        self.league_map = {}

    async def fetch_leagues(self, session=None):
        """Fetch available leagues from the API"""
        return await league_query(session)

    async def fetch_odds(self, sport, region, markets, odds_format, date_format, session):
        """Fetch odds for a specific sport, region, and markets"""
        return await odds_query(sport, region, markets, odds_format, date_format, session)


# ── QueryList panel ───────────────────────────────────────────────────────────
# Flat ordered list of (display_label, sport_key) for the sport picker,
# restricted to sports that have GAME_MARKETS entries.
_QUERY_SPORT_LIST = [
    (label, key)
    for _cat, _leagues in SPORTS_MARKETS.items()
    for label, key in _leagues.items()
    if key in GAME_MARKETS
]

_QL_BG      = "#0b1520"
_QL_BG2     = "#101e2e"
_QL_BORDER  = "#1e3048"
_QL_HOVER   = "#1a2d42"
_QL_DIM     = "#4a6070"
_QL_MID     = "#8a9db5"
_QL_BRIGHT  = "#c8daf0"
_QL_ACCENT  = "#00c896"
_QL_F       = "8pt"
_QL_ROW_H   = 19
_QL_SCROLL_H = 4 * _QL_ROW_H + 3 * 2   # 4 visible rows + gaps = 82px

_QL_CB_STYLE = f"""
QCheckBox {{
    color:{_QL_MID}; background:transparent; padding:2px 2px; spacing:5px; font-size:{_QL_F};
}}
QCheckBox:hover {{ color:{_QL_BRIGHT}; }}
QCheckBox::indicator {{
    width:9px; height:9px; border:1px solid {_QL_DIM}; border-radius:1px; background:transparent;
}}
QCheckBox::indicator:checked {{
    background-color:{_QL_ACCENT}; border-color:{_QL_ACCENT};
}}
"""

_QL_BTN_STYLE = f"""
QPushButton {{
    background:{_QL_BG2}; color:{_QL_BRIGHT}; border:1px solid {_QL_BORDER};
    border-radius:1px; padding:0 3px; text-align:left; font-size:{_QL_F};
}}
QPushButton:hover {{ background:{_QL_HOVER}; border-color:#2a4060; }}
"""


class _QLDropPanel(QFrame):
    """Dropdown panel anchored beneath its toggle button, closes on outside click.

    Uses Qt.Popup (the same window type QMenu / QComboBox dropdowns use) rather
    than a separate Qt.Tool top-level window. On Wayland the compositor ignores
    move() on top-level Tool windows and places them centered, which made these
    menus float in the middle of the screen. A Qt.Popup maps to an xdg_popup that
    is anchored to its parent surface (and auto-flipped to stay on-screen), so
    popup_below() positions it correctly. Qt.Popup also dismisses itself on an
    outside click; the eventFilter below is kept as a harmless safety net.
    """
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setStyleSheet(f"QFrame{{background:{_QL_BG};border:1px solid {_QL_BORDER};}}")
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if not self.geometry().contains(event.globalPosition().toPoint()):
                self.hide()
        return False

    def popup_below(self, btn):
        # adjustSize() first so the popup geometry is final before it is anchored.
        self.adjustSize()
        gp = btn.mapToGlobal(QPoint(0, btn.height() + 1))
        self.move(gp)
        self.show()
        self.raise_()
        self.activateWindow()


from PyQt6.QtCore import QPoint

class _QLSportPanel(_QLDropPanel):
    def __init__(self, slot, parent=None):
        super().__init__(parent)
        self.slot = slot
        self.setFixedWidth(160)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(3)

        self._search = QLineEdit()
        self._search.setPlaceholderText("search…")
        self._search.setFixedHeight(18)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{_QL_BG2};color:{_QL_BRIGHT};"
            f"border:1px solid {_QL_BORDER};border-radius:1px;"
            f"padding:0 4px;font-size:{_QL_F};}}"
            f"QLineEdit:focus{{border-color:#2a5080;}}"
        )
        self._search.textChanged.connect(self._filter)
        outer.addWidget(self._search)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{_QL_BORDER};border:none;")
        outer.addWidget(rule)

        self._inner = QWidget()
        self._inner.setStyleSheet("background:transparent;")
        self._lay = QVBoxLayout(self._inner)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._inner)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{background:{_QL_BG};width:3px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_QL_DIM};border-radius:1px;min-height:12px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        outer.addWidget(scroll)
        self._populate("")

    def _populate(self, query):
        """Rebuild the button list from the filtered set so hidden rows don't
        leave gaps (setVisible doesn't reliably collapse slots inside a
        widgetResizable scroll area)."""
        # Drop everything (buttons + trailing stretch), then re-add matches.
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        q = query.strip().lower()
        for label, key in _QUERY_SPORT_LIST:
            if q and q not in label.lower():
                continue
            b = QPushButton(label)
            b.setFixedHeight(19)
            b.setStyleSheet(
                f"QPushButton{{background:transparent;color:{_QL_MID};border:none;"
                f"text-align:left;padding:0 6px;font-size:{_QL_F};}}"
                f"QPushButton:hover{{background:{_QL_HOVER};color:{_QL_BRIGHT};}}"
            )
            b.clicked.connect(lambda _, l=label, k=key: (self.slot.set_sport(l, k), self.hide()))
            self._lay.addWidget(b)
        self._lay.addStretch()

    def _filter(self, text):
        self._populate(text)

    def popup_below(self, btn):
        super().popup_below(btn)
        self._search.clear()
        self._search.setFocus()


class _QLMarketsPanel(_QLDropPanel):
    def __init__(self, slot, parent=None):
        super().__init__(parent)
        self.slot = slot
        self.setFixedWidth(160)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(3)

        self._search = QLineEdit()
        self._search.setPlaceholderText("search…")
        self._search.setFixedHeight(18)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{_QL_BG2};color:{_QL_BRIGHT};"
            f"border:1px solid {_QL_BORDER};border-radius:1px;"
            f"padding:0 4px;font-size:{_QL_F};}}"
            f"QLineEdit:focus{{border-color:#2a5080;}}"
        )
        self._search.textChanged.connect(self._filter)
        outer.addWidget(self._search)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background:{_QL_BORDER};border:none;")
        outer.addWidget(rule)

        self._cb_widget = QWidget()
        self._cb_widget.setStyleSheet("background:transparent;")
        self._cb_lay = QVBoxLayout(self._cb_widget)
        self._cb_lay.setContentsMargins(0, 0, 0, 0)
        self._cb_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidget(self._cb_widget)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{background:{_QL_BG};width:3px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_QL_DIM};border-radius:1px;min-height:12px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        outer.addWidget(scroll)
        self._sections: list = []
        self.rebuild()

    def rebuild(self):
        # Called when the panel opens (sport may have changed): reset to the full
        # unfiltered view. Blocking signals avoids re-triggering _filter via clear().
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._populate("")

    def _populate(self, query):
        """Rebuild the section/checkbox tree from the filtered set. Rebuilding
        (rather than setVisible) keeps hidden rows from leaving gaps inside the
        widgetResizable scroll area; a section header only shows when it has at
        least one matching market."""
        while self._cb_lay.count():
            item = self._cb_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._sections = []
        q = query.strip().lower()
        mmap = GAME_MARKETS.get(self.slot.sport_key,
                                {"GAME LINES": ["h2h", "spreads", "totals"],
                                 "ALT LINES": [], "SECONDARY": []})
        first = True
        for section, keys in mmap.items():
            keys = [k for k in keys
                    if not q
                    or q in GAME_MARKET_LABELS.get(k, k).lower()
                    or q in k.lower()]
            if not keys:
                continue
            if not first:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background:{_QL_BORDER};border:none;margin:2px 0;")
                self._cb_lay.addWidget(sep)
            first = False
            hdr = QLabel(section)
            hdr.setStyleSheet(
                f"color:{_QL_DIM};font-size:7pt;letter-spacing:2px;"
                f"background:transparent;padding:3px 2px 1px 2px;"
            )
            self._cb_lay.addWidget(hdr)
            rows = []
            for key in keys:
                cb = QCheckBox(GAME_MARKET_LABELS.get(key, key))
                cb.setChecked(key in self.slot.markets)
                cb.setStyleSheet(_QL_CB_STYLE)
                cb.toggled.connect(lambda chk, k=key: self._toggle(k, chk))
                self._cb_lay.addWidget(cb)
                rows.append((cb, key))
            self._sections.append((hdr, None, rows))
        self._cb_lay.addStretch()

    def _filter(self, text):
        self._populate(text)

    def popup_below(self, btn):
        super().popup_below(btn)
        self._search.setFocus()
        self._search.selectAll()

    def _toggle(self, key, checked):
        self.slot.markets.add(key) if checked else self.slot.markets.discard(key)
        self.slot.refresh_mkts()


class _QLRegionPanel(_QLDropPanel):
    def __init__(self, rw, parent=None):
        super().__init__(parent)
        self.rw = rw
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)
        hdr = QLabel("REGIONS")
        hdr.setStyleSheet(
            f"color:{_QL_DIM};font-size:7pt;letter-spacing:2px;"
            f"background:transparent;padding:0 2px 2px 2px;"
        )
        lay.addWidget(hdr)
        self._checks = {}
        for r in ["us", "us2", "eu", "au", "uk"]:
            cb = QCheckBox(r.upper())
            cb.setChecked(r in rw.selected)
            cb.setStyleSheet(_QL_CB_STYLE)
            cb.toggled.connect(lambda chk, reg=r: self._toggle(reg, chk))
            self._checks[r] = cb
            lay.addWidget(cb)
        self.setFixedWidth(110)

    def _toggle(self, region, checked):
        self.rw.selected.add(region) if checked else self.rw.selected.discard(region)
        self.rw.refresh()

    def sync(self):
        for r, cb in self._checks.items():
            cb.setChecked(r in self.rw.selected)


class _QLRegionWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected = {"us"}
        self._panel = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.btn = QPushButton()
        self.btn.setFixedHeight(_QL_ROW_H)
        self.btn.setStyleSheet(_QL_BTN_STYLE)
        self.btn.clicked.connect(self._toggle)
        lay.addWidget(self.btn)
        self.refresh()

    def refresh(self):
        s = "·".join(r.upper() for r in sorted(self.selected)) or "—"
        self.btn.setText(f"REGION  {s}  ▾")

    def _toggle(self):
        if not self._panel:
            self._panel = _QLRegionPanel(self, self.window())
        if self._panel.isVisible():
            self._panel.hide()
        else:
            self._panel.sync()
            self._panel.popup_below(self.btn)


class _QLSlot(QWidget):
    def __init__(self, qlist, parent=None):
        super().__init__(parent)
        self.qlist      = qlist
        self.sport_key  = "baseball_mlb"
        self.sport_label = "MLB"
        self.markets    = {"spreads", "totals"}

        self.setFixedHeight(_QL_ROW_H)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        self.sport_btn = QPushButton("MLB")
        self.sport_btn.setFixedWidth(64)
        self.sport_btn.setFixedHeight(_QL_ROW_H)
        self.sport_btn.setStyleSheet(_QL_BTN_STYLE)
        self.sport_btn.clicked.connect(self._open_sport)
        row.addWidget(self.sport_btn)

        self.mkts_btn = QPushButton()
        self.mkts_btn.setFixedWidth(118)
        self.mkts_btn.setFixedHeight(_QL_ROW_H)
        self.mkts_btn.setStyleSheet(_QL_BTN_STYLE)
        self.mkts_btn.clicked.connect(self._open_mkts)
        row.addWidget(self.mkts_btn)

        rm = QPushButton("×")
        rm.setFixedWidth(16)
        rm.setFixedHeight(_QL_ROW_H)
        rm.setStyleSheet(
            f"QPushButton{{background:transparent;color:{_QL_DIM};border:none;font-size:10pt;}}"
            f"QPushButton:hover{{color:#e05555;}}"
        )
        rm.clicked.connect(lambda: qlist.remove_slot(self))
        row.addWidget(rm)

        self._sp = None
        self._mp = None
        self.refresh_mkts()

    def set_sport(self, label, key):
        self.sport_label = label
        self.sport_key   = key
        fm = QFontMetrics(self.sport_btn.font())
        self.sport_btn.setText(
            fm.elidedText(label, Qt.TextElideMode.ElideRight, self.sport_btn.width() - 10)
        )
        defaults = GAME_MARKETS.get(key, {}).get("GAME LINES", [])
        self.markets = set(defaults[:2])
        if self._mp:
            self._mp.rebuild()
        self.refresh_mkts()

    def refresh_mkts(self):
        order = list(GAME_MARKET_LABELS.keys())
        parts = sorted(self.markets, key=lambda k: order.index(k) if k in order else 99)
        text = ("·".join(GAME_MARKET_LABELS.get(k, k) for k in parts) or "—") + " ▾"
        fm = QFontMetrics(self.mkts_btn.font())
        self.mkts_btn.setText(
            fm.elidedText(text, Qt.TextElideMode.ElideRight, self.mkts_btn.width() - 4)
        )

    def _open_sport(self):
        if not self._sp:
            self._sp = _QLSportPanel(self, self.window())
        self._sp.hide() if self._sp.isVisible() else self._sp.popup_below(self.sport_btn)

    def _open_mkts(self):
        if not self._mp:
            self._mp = _QLMarketsPanel(self, self.window())
        if self._mp.isVisible():
            self._mp.hide()
        else:
            self._mp.rebuild()
            self._mp.popup_below(self.mkts_btn)


class QueryList(QWidget):
    """Compact query-slot panel: replaces region pills + market buttons."""

    _ADD_STYLE = (
        f"QPushButton{{background:{_QL_BG2};color:{_QL_ACCENT};"
        f"border:1px solid {_QL_ACCENT};border-radius:2px;"
        f"padding:1px 6px;font-size:7pt;letter-spacing:1px;font-weight:bold;}}"
        f"QPushButton:hover{{background:{_QL_ACCENT};color:#000;}}"
    )

    # Thin border around just the query-slot box (the scroll area).
    # Teal accent matches the ADD button's border below it.
    _BORDER_COLOR = _QL_ACCENT

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(208)
        self.slots: list[_QLSlot] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Fixed-height scrollable slot area
        self._slot_area = QWidget()
        self._slot_area.setStyleSheet("background:transparent;")
        self._slot_lay = QVBoxLayout(self._slot_area)
        self._slot_lay.setContentsMargins(0, 0, 0, 0)
        self._slot_lay.setSpacing(2)
        self._slot_lay.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._slot_area)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(_QL_SCROLL_H)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            f"QScrollArea{{border:1px solid {self._BORDER_COLOR};border-radius:2px;background:transparent;}}"
            f"QScrollBar:vertical{{background:{_QL_BG};width:3px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_QL_DIM};border-radius:1px;min-height:12px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        outer.addWidget(self._scroll)

        self.add_btn = QPushButton("+ ADD")
        self.add_btn.setFixedHeight(16)
        self.add_btn.setStyleSheet(self._ADD_STYLE)
        self.add_btn.clicked.connect(self.add_slot)
        outer.addWidget(self.add_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_QL_BORDER};border:none;")
        outer.addWidget(sep)

        self.region = _QLRegionWidget()
        outer.addWidget(self.region)

        self.add_slot()

    def add_slot(self):
        s = _QLSlot(self, self._slot_area)
        self.slots.append(s)
        self._slot_lay.insertWidget(self._slot_lay.count() - 1, s)
        self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        )

    def remove_slot(self, slot):
        if slot in self.slots:
            self.slots.remove(slot)
            self._slot_lay.removeWidget(slot)
            slot.deleteLater()

    def get_queries(self):
        """Return list of (sport_key, sport_label, markets, regions) for non-empty slots."""
        region_set = self.region.selected.copy()
        return [
            (s.sport_key, s.sport_label, s.markets.copy(), region_set)
            for s in self.slots if s.markets
        ]




class ModernOddsWindow(QMainWindow):
    """Main window for displaying and managing odds data"""

    # PX auth bridge signals — see _setup_px_auth_bridge. Declared as
    # pyqtSignals so they can be emitted from the Playwright background
    # thread and received via QueuedConnection on the main thread (Qt's
    # rule: signals can only be cross-thread when both sides are QObjects
    # with a proper event loop on the receiver).
    px_otp_requested = pyqtSignal()
    px_token_refreshed = pyqtSignal()

    BUTTON_STYLE = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 1px solid #0056b3;
            padding: 5px 10px;
            margin-right: 5px;
            border-radius: 4px;
        }
        QPushButton:checked {
            background-color: #28a745;  /* Green */
            color: white;
            border: 1px solid #218838;
        }
        QPushButton:disabled {
            background-color: #e9ecef;
            color: #6c757d;
            border-color: #dee2e6;
        }
    """

    fetch_odds_button_style = """
    QPushButton {
        background-color: #dc9437;  /* Orange */
        color: white; /* text color */
        border: 1px solid #0056b3;
        padding: 5px 10px;
        margin-right: 5px;
        border-radius: 4px;
        width: 200px;
        font-size: 24px;
    }"""

    props_button_style = """
        QPushButton {
            background-color: #007bff;  /* Blue */
            color: white;
            border: 3px solid #0056b3;
            border-radius: 4px;
            width: 72px;
            height: 36px;
            font-size: 12px;
        }
        QPushButton:disabled {
            background-color: #909090;
        }
    """

    def __init__(self):
        super().__init__()
        self.region_selector = None
        self.timer = QTimer()
        self.data_manager = DataManager()
        self.leagues_loaded = False
        self.league_tabs = {}  # {league_name: LeagueTabData}
        self.current_league = None
        self.init_ui()
        self.connect_signals()

        self.icon_frame = 0
        self._icon_cache = {}  # frame index -> decoded QIcon; avoids re-reading PNG from disk every tick
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(False)
        self.icon_timer.timeout.connect(self.UpdateIcon)
        self.icon_timer.start(16)
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_game_statuses)
        self.status_timer.start(600000) # 10 minutes - makes a request to fetch scores

    # God speed AC
    def UpdateIcon(self):
        icon = self._icon_cache.get(self.icon_frame)
        if icon is None:
            framesdir = pathlib.Path(__file__).parent / "appicon_frames"
            next_icon = framesdir / f"frame{str(self.icon_frame).zfill(3)}.png"
            icon = QIcon(str(next_icon))
            self._icon_cache[self.icon_frame] = icon
        self.setWindowIcon(icon)
        self.icon_frame = ((self.icon_frame + 1) % 200)
        #print(next_icon)


    def init_ui(self):
        """Initialize the user interface components"""
        self.setWindowTitle("Effort Odds")
        self.setGeometry(100, 100, 800, 600)
        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.layout = QVBoxLayout(main_widget)
        self.layout.setSpacing(1)  # Minimize spacing between all main layout elements
        self.layout.setContentsMargins(5, 5, 5, 5)  # Tight margins

        # --------- TOP SECTION ---------
        # league_selector kept headless — still used by populate_leagues / handle_league_change
        self.league_selector = QComboBox()

        league_layout = QHBoxLayout()
        league_layout.setContentsMargins(0, 0, 0, 0)
        league_layout.setSpacing(4)
        self.query_list = QueryList()
        league_layout.addWidget(self.query_list, 0, Qt.AlignmentFlag.AlignBottom)

        # Create toggle buttons
        self.stream_toggle_button = QPushButton("Show Streaming Links ▼")
        self.stream_toggle_button.setCheckable(True)
        self.stream_toggle_button.setChecked(False)
        self.stream_toggle_button.clicked.connect(self.toggle_streaming_links)
        self.stream_toggle_button.setFixedWidth(150)
        self.stream_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)

        # Create news toggle button
        self.news_toggle_button = QPushButton("Show Injury News ▼")
        self.news_toggle_button.setCheckable(True)
        self.news_toggle_button.setChecked(False)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        self.news_toggle_button.setFixedWidth(150)
        self.news_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)

        # Create historical odds toggle button
        self.history_toggle_button = QPushButton("Show Historical Odds ▼")
        self.history_toggle_button.setCheckable(True)
        self.history_toggle_button.setChecked(False)
        self.history_toggle_button.clicked.connect(self.toggle_historical_odds)
        self.history_toggle_button.setFixedWidth(150)
        self.history_toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:checked {
                background-color: #34495E;
            }
        """)


        # Create a dedicated container for the right side elements
        right_side_container = QWidget()
        right_side_layout = QVBoxLayout(right_side_container)
        right_side_layout.setContentsMargins(0, 0, 0, 0)  # No margins
        right_side_layout.setSpacing(2)  # Minimal spacing between buttons and widget

        # Create a horizontal layout for the buttons — stretch pushes them to the right
        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(self.stream_toggle_button)
        buttons_layout.addWidget(self.news_toggle_button)
        buttons_layout.addWidget(self.history_toggle_button)  # Add historical odds toggle button
        buttons_layout.setSpacing(4)  # Small spacing between buttons


        # ---- Calculator button ----
        self.calc_button = QPushButton("Calculator   🧮🎲")# ⚙
        self.calc_button.setFixedWidth(150)
        self.calc_button.setCheckable(False)
        self.calc_button.clicked.connect(self.handle_calc_button)
        self.calc_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 3px;
                font-size: 9pt;
            }
            QPushButton:hover {
                background-color: #34495E;
            }
        """)
        buttons_layout.addWidget(self.calc_button)

        # ---- Radio button ----
        self.radio_button = QPushButton("Radio  📻")
        self.radio_button.setFixedWidth(90)
        self.radio_button.clicked.connect(self.handle_radio_button)
        self.radio_button.setStyleSheet("""
            QPushButton {
                background-color: #0a1520;
                color: #00d4d4;
                border: 1px solid #00d4d4;
                border-left: 2px solid #ff0080;
                padding: 4px;
                border-radius: 0px;
                font-size: 9pt;
                font-family: 'Consolas', 'Courier New', monospace;
            }
            QPushButton:hover {
                background-color: #102030;
                color: #ffffff;
                border: 1px solid #00ffff;
            }
        """)
        buttons_layout.addWidget(self.radio_button)
        self.radio_window = None



        # Add the buttons layout to the right side layout
        right_side_layout.addLayout(buttons_layout)

        # right_side_container gets stretch=1 so ticker fills the remaining width
        league_layout.addWidget(right_side_container, 1)
        # NOTE: self.layout.addLayout(league_layout) is deferred until ticker_section
        # is built so it can be parented into right_side_layout before we commit.

        # --------- TICKER + CONTROLS SECTION ---------
        ticker_section = QWidget()
        ticker_section_layout = QVBoxLayout(ticker_section)
        ticker_section_layout.setContentsMargins(0, 0, 0, 0)
        ticker_section_layout.setSpacing(2)
        if DEBUG_OUTLINES: ticker_section.setStyleSheet(""" QWidget { border: 4px solid #00FFFF; } """);

        # Choose transition style: "flip_card" or "split_reveal"
        self.ticker_tape = TickerTape(transition_style="flip_card")
        ticker_section_layout.addWidget(self.ticker_tape)

        # Initialize prediction markets worker for ticker
        self.prediction_markets_worker = PredictionMarketsWorker()
        self.prediction_markets_worker.data_ready.connect(self.ticker_tape.add_prediction_markets)
        self.prediction_markets_worker.error_occurred.connect(self.handle_prediction_markets_error)
        self.prediction_markets_worker.status_update.connect(self.handle_prediction_markets_status)
        # Delay prediction markets to let RSS feeds fetch first (they're much faster).
        # Pushed to 8s as part of startup staggering so the Polymarket scrape doesn't
        # land in the same window as the news fetch (6s) — see startup-stagger notes.
        QTimer.singleShot(8000, self.prediction_markets_worker.start)

        # Action buttons row (Fetch Odds, Props, TT) — no splitter needed now that
        # streaming widget is TuneInWidget — a self-contained floating dropdown.
        controls_container = QWidget()
        if DEBUG_OUTLINES: controls_container.setStyleSheet(""" QWidget { border: 4px solid #00FFFF; } """);
        controls_layout = QVBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(2)

        action_buttons_widget = QWidget()
        if DEBUG_OUTLINES: action_buttons_widget.setStyleSheet(""" QWidget { border: 4px solid #00FF00; } """);
        action_buttons_layout = QHBoxLayout(action_buttons_widget)
        action_buttons_layout.setContentsMargins(0, 0, 0, 0)
        action_buttons_layout.setSpacing(4)

        self.fetch_odds_button = QPushButton("Fetch Odds 🎰")
        self.fetch_odds_button.setStyleSheet(self.fetch_odds_button_style)
        action_buttons_layout.addWidget(self.fetch_odds_button)

        self.props_button = QPushButton("Props ➣➣")
        self.props_button.setObjectName("market_props")
        self.props_button.setEnabled(False)
        self.props_button.setStyleSheet(self.props_button_style)
        action_buttons_layout.addWidget(self.props_button)

        self.tt_button = QPushButton("TT🏓")
        self.tt_button.setObjectName("market_tt")
        self.tt_button.setStyleSheet(self.props_button_style)
        action_buttons_layout.addWidget(self.tt_button)

        self.props_availability_label = QLabel("No Props available for this league")
        self.props_availability_label.setStyleSheet("color: #6c757d; font-style: italic;")
        self.props_availability_label.setVisible(False)
        action_buttons_layout.addWidget(self.props_availability_label)

        action_buttons_layout.addStretch()
        controls_layout.addWidget(action_buttons_widget)

        # TuneInWidget is itself the floating dropdown — no wrapper needed
        self.tune_in_widget = TuneInWidget(self.stream_toggle_button)

        ticker_section_layout.addWidget(controls_container)

        # Now that ticker_section is complete, nest it in right_side_layout so it
        # sits to the RIGHT of query_list in the same horizontal row.
        right_side_layout.addWidget(ticker_section, 1)
        self.layout.addLayout(league_layout)

        # --------- ODDS SECTION ---------
        # Tab widget for different leagues
        self.tab_widget = QTabWidget()

        # Odds-table filter bar. Rather than consuming a row above the table or
        # riding the tab bar, it's overlaid onto the current table's horizontal
        # header, hovering at the right edge of the "Market/Outcome" section.
        # Its geometry is kept in sync by _reposition_search_bar(), driven by
        # header resize/scroll signals and tab changes (see _attach_search_to_header).
        self._search_target_width = 240   # desired bar width inside section 0
        self._wired_headers = set()       # headers we've already hooked up
        self._current_header = None       # header the bar is currently parented to
        self.search_bar = QLineEdit(self.tab_widget)
        self.search_bar.setPlaceholderText("Filter odds table…")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.setStyleSheet("""
            QLineEdit {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 9pt;
                color: #495057;
            }
            QLineEdit:focus {
                border-color: #007bff;
                background-color: white;
                outline: 0;
            }
        """)
        self.search_bar.hide()  # shown once a table exists to host it

        # Create the team news widget first
        self.team_news_widget = TeamNewsWidget()
        self.team_news_widget.setVisible(False)  # Hidden by default

        # Set the news widget reference for the ticker tape
        self.ticker_tape.news_widget = self.team_news_widget

        # Connect to NewsWorker signal to avoid polling
        if hasattr(self.team_news_widget, 'worker'):
            self.team_news_widget.worker.news_fetched.connect(self.ticker_tape.on_news_ready)

        # Create news container and add the team news widget
        self.news_container = QWidget()
        news_container_layout = QVBoxLayout(self.news_container)
        news_container_layout.setContentsMargins(0, 0, 0, 0)  # Zero margins
        news_container_layout.setSpacing(0)  # Zero spacing
        news_container_layout.addWidget(self.team_news_widget)

        # Set initial state for the news container
        self.news_container.setVisible(False)  # Hide container initially

        # Create the best lines container
        self.best_lines_container = QWidget()
        best_lines_layout = QVBoxLayout(self.best_lines_container)
        best_lines_layout.setContentsMargins(0, 0, 0, 0)

        # Create header layout with label and buttons
        best_lines_header_layout = QHBoxLayout()

        # Add the header label
        best_lines_header = QLabel("Best Lines ⮟")
        best_lines_header.setStyleSheet("font-weight: bold; font-size: 14px; color: #7bd419")
        best_lines_header_layout.addWidget(best_lines_header)

        # Add spacer to push buttons to the right
        best_lines_header_layout.addStretch(1)

        # Add refresh button for splits data
        self.splits_refresh_button = QPushButton("↻")
        self.splits_refresh_button.setToolTip("Refresh Betting Splits Data")
        self.splits_refresh_button.setStyleSheet("""
            QPushButton {
                background-color: #2C3E50;
                color: white;
                border: none;
                padding: 2px 4px;
                border-radius: 3px;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #34495E;
            }
        """)
        self.splits_refresh_button.setFixedWidth(25)
        self.splits_refresh_button.clicked.connect(self.refresh_splits_data)
        best_lines_header_layout.addWidget(self.splits_refresh_button)

        # Create the best lines widget
        self.best_lines_widget = BestLinesWidget()

        def UpdateBestLinesHeader():
            best_lines_header.setText("Splits ⮟" if self.best_lines_widget.show_splits else "Best Lines ⮟")

        # Add the toggle button from the BestLinesWidget
        best_lines_header_layout.addWidget(self.best_lines_widget.toggle_button)
        self.best_lines_widget.toggle_button.clicked.connect(UpdateBestLinesHeader)

        # Add the header layout and the widget to the container
        best_lines_layout.addLayout(best_lines_header_layout)
        best_lines_layout.addWidget(self.best_lines_widget)

        # Create the historical odds container
        self.historical_odds_container = QWidget()
        historical_odds_layout = QVBoxLayout(self.historical_odds_container)
        historical_odds_layout.setContentsMargins(0, 0, 0, 0)

        # Create the historical odds widget
        self.historical_odds_widget = HistoricalOddsWidget(SUPER_KEY, 10)
        historical_odds_layout.addWidget(self.historical_odds_widget)

        # Initially hide the historical container (important!)
        self.historical_odds_container.setVisible(False)
        self.historical_odds_widget.setVisible(False)

        # First, create a horizontal splitter for the bottom section
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        # Disable opaque resize for smooth dragging with news widget
        self.horizontal_splitter.setOpaqueResize(False) # massive UI lag on resize without this

        # Add the news container to the left side of horizontal splitter
        self.horizontal_splitter.addWidget(self.news_container)

        # Add the best lines container to the middle of horizontal splitter
        self.horizontal_splitter.addWidget(self.best_lines_container)

        # Add the historical odds container to the right side of horizontal splitter
        self.horizontal_splitter.addWidget(self.historical_odds_container)

        # Create the compact liquidity widget (ProphetX order book browser)
        self.liquidity_widget = ProphetXBrowser(compact_mode=True)
        self.liquidity_widget.setMaximumWidth(400)  # Constrain max width to keep it compact
        self.liquidity_widget.setMinimumWidth(250)  # Allow it to be narrower

        # Bridge the Playwright-thread refresh path to the Qt main thread:
        #   - OTP requests pop a QInputDialog instead of typing into the
        #     (now-hidden) Chromium window.
        #   - Token-refresh notifications auto-refire the bet slip's
        #     wallet/positions workers so the labels repopulate without
        #     a manual ↻ click.
        self._setup_px_auth_bridge()

        # Initialize ProphetX worker for async data updates
        # Refresh interval: 20 seconds (more frequent than main odds due to live orderbook changes)
        self.prophetx_worker = ProphetXWorker(refresh_interval=20)
        self.prophetx_worker_thread = QThread()
        self.prophetx_worker.moveToThread(self.prophetx_worker_thread)

        # Connect worker signals
        self.prophetx_worker.data_ready.connect(self.liquidity_widget.updateEventMarkets)
        self.prophetx_worker.error_occurred.connect(self.handle_prophetx_error)
        self.prophetx_worker.status_update.connect(self.handle_prophetx_status)
        self.prophetx_worker.fetch_requested.connect(self.fetch_prophetx_event)

        # Connect widget's event selection to worker
        self.liquidity_widget.event_selected.connect(self.on_prophetx_event_selected)

        # Connect widget's refresh all request to trigger initial fresh fetch
        self.liquidity_widget.refresh_all_requested.connect(self.on_prophetx_refresh_all_requested)

        # Start worker thread (this will start the periodic refresh timer)
        self.prophetx_worker_thread.started.connect(self.prophetx_worker.start)
        self.prophetx_worker_thread.start()

        # Start the Novig geo_tx HTTP listener. The Tampermonkey
        # userscript in the user's real Firefox POSTs every fresh
        # geolocationTransactionId it sees here, so single-bet NV
        # placements always have a current token without manual paste.
        #
        # We also kick off a one-shot headless refresh in a background
        # daemon thread: drives a snapshot Firefox to novig.com, places
        # a 10-COIN bet, captures the fresh PREWAGER tx. Runs concurrent
        # with the rest of EffortOdds startup so the UI is interactive
        # immediately; takes ~25-30s wall-time. Best-effort — if it
        # fails (no userscript, no Firefox profile, network down) the cache
        # is empty (we purge it below), so the first place's 400-retry path
        # drives the refresh on demand instead.
        try:
            from NovigClient import geo_harvester
            geo_harvester.start_geo_listener()
            # Drop any token persisted from a previous run before anything can
            # read it. A PREWAGER geo_tx is session-bound, so a carried-over
            # token is always stale and /orders rejects it. Purging up front
            # means the worst case is one 400 → refresh on first place; the
            # startup refresh below normally mints a fresh one well before then.
            geo_harvester.clear_geo_cache()

            def _bg_refresh():
                import asyncio as _asyncio
                try:
                    # place_bet=False: stop at geolocation, don't place a COIN
                    # bet — leaves the harvested token unconsumed for the real
                    # bet to spend (PREWAGER model).
                    ok = _asyncio.run(
                        geo_harvester.refresh_geo_tx(
                            headless=True, timeout_s=30.0, place_bet=False))
                    print(f"[novig] startup geo refresh: "
                          f"{'OK' if ok else 'FAIL'}")
                except Exception as ex:
                    print(f"[novig] startup geo refresh crashed: {ex}")

            # Startup staggering: the headless Firefox launch is the single
            # biggest startup stall (~0.5s of GIL/subprocess contention). It's
            # a 25-30s best-effort job with on-disk fallback, so nothing needs
            # it in the first few seconds. Defer the thread start by 4s so the
            # window paints and the ticker starts scrolling before Firefox
            # spawns. The HTTP listener above stays immediate (it's cheap).
            import threading as _threading
            def _start_geo_refresh_thread():
                _threading.Thread(target=_bg_refresh,
                                  name="novig-startup-geo-refresh",
                                  daemon=True).start()
            # [PERF-DIAG] Temporarily disabled while triaging the OTHER startup
            # stutters. The in-process Selenium geo-harvest hogs the GIL for
            # ~11-30s and masks every other offender in the watchdog dumps
            # (confirmed root cause — see PERF_DIAGNOSTICS.md). The listener +
            # cache above stay live, so tokens from the real everyday Firefox
            # still flow; only the headless Selenium drive is skipped. Worst
            # case: first Novig single-bet hits the 400 → on-demand refresh path.
            # Flip back to True (or delete this gate) to restore startup pre-mint.
            DIAG_STARTUP_GEO_REFRESH = False
            if DIAG_STARTUP_GEO_REFRESH:
                QTimer.singleShot(4000, _start_geo_refresh_thread)
            else:
                print("[novig] startup geo refresh DISABLED (DIAG) — "
                      "on-demand refresh still active on first 400")
        except Exception as e:
            print(f"[novig] couldn't start geo listener: {e}")

        # Create horizontal splitter for odds table + liquidity widget (side-by-side)
        self.odds_liquidity_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.odds_liquidity_splitter.setOpaqueResize(False)
        self.odds_liquidity_splitter.addWidget(self.tab_widget)  # Odds table on left
        self.odds_liquidity_splitter.addWidget(self.liquidity_widget)  # Liquidity widget on right
        self.odds_liquidity_splitter.setSizes([850, 150])  # ~85% odds table, ~15% liquidity widget

        # Now create the vertical splitter with odds+liquidity on top and horizontal splitter on bottom
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        # CRITICAL FIX: Disable opaque resize to prevent expensive repaints during drag
        self.vertical_splitter.setOpaqueResize(False)
        self.vertical_splitter.addWidget(self.odds_liquidity_splitter)  # Changed from self.tab_widget
        self.vertical_splitter.addWidget(self.horizontal_splitter)

        # Set initial sizes for vertical splitter to show more of the tab widget
        self.vertical_splitter.setSizes([400, 200])

        # Add the vertical splitter to the main layout
        self.layout.addWidget(self.vertical_splitter, 1)  # The 1 gives it stretch

        # --------- AUTO-UPDATE CONTROLS ---------
        # Auto-update controls
        update_controls_layout = QHBoxLayout()

        self.auto_update_check = QCheckBox("Auto-Update Odds")
        update_controls_layout.addWidget(self.auto_update_check)

        self.update_interval = QSpinBox()
        self.update_interval.setRange(1, 60)
        self.update_interval.setSuffix(" min")
        self.update_interval.setValue(30)
        self.update_interval.setEnabled(True)
        update_controls_layout.addWidget(QLabel("Update Interval:"))
        update_controls_layout.addWidget(self.update_interval)

        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.status_frame_layout = QHBoxLayout(self.status_frame)

        self.last_update_label = QLabel("Last Update: Never")
        self.last_update_label.setStyleSheet("color: #6c757d;")

        self.update_status = QLabel("")
        self.update_status.setStyleSheet("""
            QLabel {
                padding: 5px;
                border-radius: 3px;
                background-color: #f8f9fa;
            }
        """)

        # Progress indicator
        self.progress_indicator = UpdateProgressIndicator()
        self.status_frame_layout.addWidget(self.progress_indicator)
        self.status_frame_layout.addWidget(self.last_update_label)
        self.status_frame_layout.addWidget(self.update_status)

        # Add progress bar (hidden by default)
        self.progress = QProgressBar()
        self.progress.setVisible(False)  # Hidden by default
        self.progress.setMaximumHeight(15)  # Make it more compact
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #dee2e6;
                border-radius: 3px;
                text-align: center;
                font-size: 9pt;
            }
            QProgressBar::chunk {
                background-color: #007bff;
                border-radius: 2px;
            }
        """)
        self.status_frame_layout.addWidget(self.progress)

        update_controls_layout.addWidget(self.status_frame)
        update_controls_layout.addStretch()

        self.layout.addLayout(update_controls_layout)


    def handle_league_change(self):
        """Handle league selection changes"""

        # Ensure self.props_button exists before proceeding
        if not hasattr(self, "props_button"):
            print("Warning: props_button does not exist yet. Skipping handle_league_change.")
            return

        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)

        has_props = sport_key in MAJOR_PROP_MARKETS  # Check if this league has props
        self.props_button.setEnabled(has_props)
        self.props_availability_label.setVisible(not has_props)

        if hasattr(self, 'team_news_widget'):
            self.team_news_widget.handle_league_change(sport_key)

        # Update splits data when league changes
        if hasattr(self, 'best_lines_widget'):
            self.best_lines_widget.set_sport(sport_key)

        # Historical odds widget is self-contained and handles its own sport selection

        return

    def handle_props_button(self):
        """Handle Props button click to open PropsWindow."""
        selected_league = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(selected_league)

        print(f"Props button clicked. League: {selected_league}, Sport Key: {sport_key}")  # Debug print

        if sport_key in MAJOR_PROP_MARKETS:  # Ensure props exist for this league
            print("Props are available for this league. Creating PropsWindow...")  # Debug print

            # If there's an existing window, properly destroy it
            if hasattr(self, "props_window") and self.props_window is not None:
                try:
                    self.props_window.close()
                    self.props_window.deleteLater()  # Schedule for Qt deletion
                    self.props_window = None
                except Exception as e:
                    print(f"Error cleaning up old props window: {e}")

            # Create a completely new instance with no reference to old data
            self.props_window = PropsWindow(sport_key, selected_league)
            self.props_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # Qt will delete the widget when closed
            self.props_window.show()
        else:
            print("No props available for this league.")  # Debug print

    # overriding inherited method for custom keybinds
    def keyPressEvent(self, a0):
        self.clearFocus()
        # “1” → Props (already existing)
        if a0.key() == Qt.Key.Key_1:
            if not self.props_button.isEnabled():
                return
            self.handle_props_button()
            return

        # “2” → Calculator
        if a0.key() == Qt.Key.Key_2:
            self.handle_calc_button()
            return

        if a0.key() == Qt.Key.Key_R:
            print("refreshing")
            self.status_timer.setSingleShot(True)
            self.status_timer.setInterval(0)
            return

        super().keyPressEvent(a0)

    def connect_signals(self):
        """Connect UI signals to their respective slots"""
        self.league_selector.currentTextChanged.connect(self.handle_league_change)
        self.fetch_odds_button.clicked.connect(self.refresh_data)
        self.auto_update_check.stateChanged.connect(self.toggle_auto_update)
        self.update_interval.valueChanged.connect(self.update_timer_interval)
        self.data_manager.odds_updated.connect(self.display_odds)
        self.tab_widget.currentChanged.connect(self.handle_tab_change)
        self.timer.timeout.connect(self.refresh_data)
        self.props_button.clicked.connect(self.handle_props_button)
        self.news_toggle_button.clicked.connect(self.toggle_news_feed)
        self.tt_button.clicked.connect(self.handle_tt_button)
        self.search_bar.textChanged.connect(self.filter_table)



    def RestartTimer(self):
        interval_ms = self.update_interval.value() * 60 * 1000
        self.timer.start(interval_ms)
        return interval_ms

    def update_region(self):
        selected_region = self.region_selector.currentText()
        print(f"Selected bookmaker region: {selected_region}")
        # Modify your odds fetching logic based on selected region here

    def update_status_text(self):
        """Update the status text showing time until next update"""
        if not self.auto_update_check.isChecked():
            self.update_status.setText("")
            self.progress_indicator.setProgress(0)
            return

        remaining_time = self.timer.remainingTime() // 1000  # Convert to milliseconds to seconds
        total_time = self.update_interval.value() * 60  # Total time in seconds
        progress = 1 - (remaining_time / total_time)  # Calculate progress (0 to 1)

        minutes = remaining_time // 60
        seconds = remaining_time % 60

        # Color coding based on remaining time
        if remaining_time < 60:  # Less than 1 minute
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
        elif remaining_time < 180:  # Less than 3 minutes
            self.update_status.setStyleSheet("background-color: #ffc107; color: #000;")
        else:
            self.update_status.setStyleSheet("background-color: #28a745; color: white;")

        self.update_status.setText(f"Next update in: {minutes}m {seconds}s")
        self.progress_indicator.setProgress(progress)




    async def initialize(self):
        """Initialize the window with league data"""
        await self.populate_leagues()
        self.leagues_loaded = True

    async def populate_leagues(self, default_league="MLB"):
        """Fetch and populate leagues in the dropdown"""
        # Create a single session for the league fetch
        async with aiohttp.ClientSession() as session:
            leagues = await self.data_manager.fetch_leagues(session)

        print("Fetched leagues:", leagues)
        self.league_selector.clear()
        self.data_manager.league_map.clear()

        for sport_category, league_list in leagues.items():
            for league in league_list:
                self.league_selector.addItem(league['title'])
                self.data_manager.league_map[league['title']] = league['key']

        self.league_selector.setCurrentText(default_league)

        # Call handle_league_change after populating
        if self.league_selector.count() > 0:
            self.handle_league_change()

    def handle_tab_change(self, index):
        """Handle tab switching events, properly sync best lines data and main table data"""
        if index >= 0:
            self.current_league = self.tab_widget.tabText(index)
            # Extract the league name without the market info
            if "(" in self.current_league:
                base_league = self.current_league.split(" (")[0]
                self.league_selector.blockSignals(True)
                self.league_selector.setCurrentText(base_league)
                self.league_selector.blockSignals(False)
                # Manually sync props button / team news since signal was blocked
                self.handle_league_change()

            # UPDATE BEST LINES FOR CURRENT TAB - defer to avoid blocking tab switch
            if hasattr(self, 'best_lines_widget') and self.current_league in self.league_tabs:
                tab_data = self.league_tabs[self.current_league]
                if hasattr(tab_data, 'consolidated_odds_data'):
                    # Use QTimer to defer the update so tab switching is instant
                    QTimer.singleShot(0, lambda: self.best_lines_widget.update_display(tab_data.consolidated_odds_data))

            # Clear search bar and reset table display when switching tabs
            if hasattr(self, 'search_bar'):
                self.search_bar.clear()
                # Reset table display to show all rows
                current_table = self.tab_widget.currentWidget()
                if current_table and isinstance(current_table, QTableWidget):
                    for row in range(current_table.rowCount()):
                        current_table.setRowHidden(row, False)
                # Move the filter bar onto the now-current tab's header.
                self._attach_search_to_header(current_table)

    def create_league_tab(self, league_name, sport_key, selected_markets=None):
        """Create a new tab for a league with specific markets"""
        # Create a unique tab identifier based on league name and selected markets
        markets_str = "+".join(sorted(selected_markets)) if selected_markets else "default"
        tab_id = f"{league_name} ({markets_str})"

        if tab_id not in self.league_tabs:
            tab_data = LeagueTabData(league_name, sport_key)
            table_widget = tab_data.create_table_widget()

            # Connect selection signal for the new table
            table_widget.itemSelectionChanged.connect(self.on_market_selection_changed)
            # Double-click a cell to open that book's betslip deep-link
            table_widget.cellDoubleClicked.connect(self.on_odds_cell_double_clicked)

            self.tab_widget.addTab(table_widget, tab_id)
            self.league_tabs[tab_id] = tab_data
            self.current_league = tab_id
            self.tab_widget.setCurrentIndex(self.tab_widget.count() - 1)
            # Dock the filter bar onto the new table's header.
            self._attach_search_to_header(table_widget)
        return self.league_tabs[tab_id]


    # Proper formatting for 3-way markets (likely redundant, but im tilt)
    def format_market_label(self, market_key, outcome):
        """Format the market label based on market type and outcome"""
        if market_key == 'h2h':
            return f"Moneyline: {outcome['name']}"
        elif market_key in ('h2h_3way', 'h2h_3_way'):
            return f"3-Way Moneyline: {outcome['name']}"
        elif market_key == 'spreads':
            return f"Spread: {outcome['name']} {outcome.get('point', '')}"
        elif market_key == 'totals':
            return f"Total {outcome['name']} {outcome.get('point', '')}"
        return f"{market_key}: {outcome['name']}"



    def format_price(self, outcome):
        """Format the price display including point if available"""
        price = str(outcome.get('price', ''))
        if 'point' in outcome:
            price += f" ({outcome['point']})"
        return price

    def display_odds(self, odds: dict, league_name: str):
        """Update odds display efficiently by only processing changed data"""
        if not odds or 'bookmakers' not in odds:
            return

        tab_data = self.league_tabs.get(league_name)
        if not tab_data:
            return

        game_id = odds.get('id', 'unknown_game')
        home_team = odds.get('home_team', 'Unknown')
        away_team = odds.get('away_team', 'Unknown')

        # Collect bookmaker names and event-page links (fallback betslip target)
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            if bm_title not in tab_data.bookmakers:
                tab_data.bookmakers.append(bm_title)
            if bm.get('link'):
                tab_data.bookmaker_links[(game_id, bm_title)] = bm['link']

        # Add a header row for the game if it doesn't exist
        game_header = f"Game: {home_team} vs {away_team}"
        if game_header not in tab_data.table_rows:
            tab_data.table_rows.append(game_header)
            tab_data.table_data[game_header] = {'is_header': True, 'game_id': game_id}
            tab_data.num_rows += 1

        # Process markets and track changes
        for bm in odds['bookmakers']:
            bm_title = bm['title']
            for market in bm['markets']:
                market_key = market['key']
                for outcome in market['outcomes']:
                    unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"

                    # Add new row if needed
                    if unique_label not in tab_data.table_rows:
                        tab_data.table_rows.append(unique_label)
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                        tab_data.num_rows += 1

                    # Update price only if changed
                    price = self.format_price(outcome)
                    if unique_label not in tab_data.table_data:
                        tab_data.table_data[unique_label] = {'game_id': game_id}
                    tab_data.table_data[unique_label][bm_title] = price

                    # Stash the outcome's betslip deep-link for this cell (may be None)
                    if outcome.get('link'):
                        tab_data.cell_links[(unique_label, bm_title)] = outcome['link']

        self.update_table_display(tab_data)




    def update_table_display(self, tab_data: LeagueTabData):
        """Update table display with improved price change highlighting and live status"""
        table = tab_data.table_widget
        current_rows = table.rowCount()
        current_cols = table.columnCount()

        # Update table structure if needed
        expected_cols = len(tab_data.bookmakers) + 1
        if current_cols != expected_cols:
            table.setColumnCount(expected_cols)
            table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
            # Left-align the first header label so the docked filter bar (which
            # sits at the right of this section) never covers a centered label.
            hdr0 = table.horizontalHeaderItem(0)
            if hdr0 is not None:
                hdr0.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        expected_rows = len(tab_data.table_rows)
        if current_rows != expected_rows:
            table.setRowCount(expected_rows)

        needs_resize = False

        for row_idx, row_label in enumerate(tab_data.table_rows):
            row_data = tab_data.table_data[row_label]
            game_id = row_data['game_id']

            # Check if this is a live game for special coloring
            status_info = row_data.get('status_info', {})
            is_live = status_info.get('is_live', False)

            # Choose color based on live status
            if is_live:
                color = QColor(220, 53, 69, 120)  # Semi-transparent red for live games
            else:
                color = tab_data.get_game_color(game_id)  # Normal game colors

            # Create or update row header if needed
            header_item = table.item(row_idx, 0)
            if not header_item:
                header_item = ColoredTableItem(row_label, game_id)
                table.setItem(row_idx, 0, header_item)
                needs_resize = True

            # Apply header styling with live game highlighting
            if row_data.get('is_header'):
                font = QFont()
                font.setBold(True)
                header_item.setFont(font)
                header_item.setBackground(color)

                # Use white text for live games for better contrast, black for others
                if is_live:
                    header_item.setForeground(QColor('white'))
                else:
                    header_item.setForeground(QColor('black'))
            else:
                market_color = QColor(color)
                market_color.setAlpha(230)
                header_item.setBackground(market_color)
                header_item.setForeground(QColor('black'))

            # Update bookmaker columns
            for col_idx, bm in enumerate(tab_data.bookmakers, 1):
                current_value = row_data.get(bm, "")
                previous_value = tab_data.previous_data.get((row_label, bm))

                item = table.item(row_idx, col_idx)
                if not item:
                    item = ColoredTableItem(current_value, game_id)
                    table.setItem(row_idx, col_idx, item)
                    needs_resize = True

                # Attach the betslip deep-link for double-click handling. Prefer
                # the outcome-specific link (auto-populates a slip); fall back to
                # the bookmaker's event page (keyed per game) otherwise.
                link = (tab_data.cell_links.get((row_label, bm))
                        or tab_data.bookmaker_links.get((game_id, bm)))
                item.setData(Qt.ItemDataRole.UserRole, link)
                if link and current_value:
                    item.setToolTip("Double-click to open betslip")

                # Only update if value has changed
                if current_value != previous_value and previous_value is not None:
                    item.setText(current_value)

                    # Parse the odds values for comparison
                    try:
                        current_odds = float(current_value.split()[0])
                        previous_odds = float(previous_value.split()[0])

                        # Better odds (higher value) = green, worse odds = red
                        if current_odds > previous_odds:
                            highlight_color = QColor(0, 200, 0, 180)  # Semi-transparent green
                        else:
                            highlight_color = QColor(200, 0, 0, 180)  # Semi-transparent red

                        item.setBackground(highlight_color)
                        item.setForeground(QColor('black'))  # Keep text black for readability

                        # Reset background after 5 seconds
                        market_color = QColor(color)
                        market_color.setAlpha(230)
                        QTimer.singleShot(5000, lambda i=item, c=market_color: (
                            i.setBackground(c),
                            i.setForeground(QColor('black'))
                        ))
                    except (ValueError, IndexError):
                        # If we can't parse the odds, just update without highlighting
                        item.setText(current_value)

                    tab_data.previous_data[(row_label, bm)] = current_value
                    needs_resize = True
                elif current_value != previous_value:
                    # First time seeing this value
                    item.setText(current_value)
                    tab_data.previous_data[(row_label, bm)] = current_value

                # Maintain consistent background and text color when not highlighted
                if not row_data.get('is_header') and item.background().color().alpha() != 180:  # Don't override highlight
                    market_color = QColor(color)
                    market_color.setAlpha(230)
                    item.setBackground(market_color)
                    item.setForeground(QColor('black'))

        # Only resize if needed
        if needs_resize:
            table.resizeColumnsToContents()
            table.resizeRowsToContents()

        # Guarantee section 0 is wide enough for the label + docked filter bar,
        # then snap the bar into place (covers tabs whose contents are narrow,
        # e.g. KBO team names, where resizeColumnsToContents would shrink it).
        self._ensure_market_column_width(table)
        if table is self.tab_widget.currentWidget():
            self._reposition_search_bar()


    def _ensure_market_column_width(self, table):
        """Widen the 'Market/Outcome' column if needed so the left-anchored
        label and the right-docked filter bar both fit without overlapping."""
        if not isinstance(table, QTableWidget) or table.columnCount() == 0:
            return
        header = table.horizontalHeader()
        # Keep the label hugging the left edge so it stays clear of the bar.
        hdr0 = table.horizontalHeaderItem(0)
        if hdr0 is not None:
            hdr0.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # label reserve + gap + bar width + left/right margins
        min_w = self._market_label_reserve(header) + 24 + self._search_target_width + 12
        if table.columnWidth(0) < min_w:
            table.setColumnWidth(0, int(min_w))


    def toggle_auto_update(self):
        """Enable or disable auto-update functionality"""
        if self.auto_update_check.isChecked():
           self.update_interval.setEnabled(True)
           interval_ms = self.RestartTimer()
           print(f"next update in: {int(interval_ms/1000)} seconds")
        else:
            self.timer.stop()
            print("timer stopped")
        # self.update_status_text() # crashes

    @qasync.asyncSlot()
    async def refresh_splits_data(self):
        """The actual async refresh method"""
        self.splits_refresh_button.setEnabled(False)
        self.splits_refresh_button.setText("⟳")

        try:
            result = await self.best_lines_widget.refresh_splits_data()
            if result:
                # Show success indicator briefly
                self.splits_refresh_button.setText("✓")
                QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))
            else:
                # Show error indicator briefly
                self.splits_refresh_button.setText("✗")
                QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))
        except Exception as e:
            print(f"Error refreshing splits data: {e}")

            traceback.print_exc()
            self.splits_refresh_button.setText("✗")
            QTimer.singleShot(1500, lambda: self.splits_refresh_button.setText("↻"))

        self.splits_refresh_button.setEnabled(True)






    def update_timer_interval(self):
        """Update the timer interval when spinbox value changes"""
        if self.auto_update_check.isChecked():
            interval_ms = self.update_interval.value() * 60 * 1000
            self.timer.start(interval_ms)
            print(f"timer interval updated: {interval_ms}")
        # self.update_status_text() # crashes

    def toggle_streaming_links(self):
        """Toggle the floating streaming-links dropdown."""
        if self.tune_in_widget.isVisible():
            self.tune_in_widget.hide()
        else:
            self.tune_in_widget.popup_below(self.stream_toggle_button)
            self.stream_toggle_button.setChecked(True)
            self.stream_toggle_button.setText("Hide Streaming Links ▲")


    def toggle_news_feed(self):
        """Toggle visibility of the news feed widget with optimized spacing"""
        visible = self.news_toggle_button.isChecked()

        # Defer the heavy layout work to avoid blocking during Polymarket fetching
        def do_layout_work():
            # Make the widget visible first (needed for proper layout calculations)
            self.news_container.setVisible(visible)
            self.team_news_widget.setVisible(visible)

            # Update button text and adjust container size
            if visible:
                self.news_toggle_button.setText("Hide Injury News ▲")

                # Calculate exact height for 3 articles
                article_height = 85
                container_height = (article_height * 3)
                self.news_container.setMinimumHeight(container_height)

                # KEY FIX: Set negative top margin on progress bar to pull it upward
                prog_margins = self.progress.contentsMargins()
                prog_margins.setTop(-100)  # Adjust this value as needed
                self.progress.setContentsMargins(prog_margins)

                # Set minimal spacing in main layout
                self.layout.setSpacing(0)
            else:
                self.news_toggle_button.setText("Show Injury News ▼")

                # Reset progress bar margins to normal
                prog_margins = self.progress.contentsMargins()
                prog_margins.setTop(0)
                self.progress.setContentsMargins(prog_margins)

                # Reset layout spacing
                self.layout.setSpacing(0)

            # Force update to apply changes
            self.update()

        # Update button text immediately for responsive feedback
        if visible:
            self.news_toggle_button.setText("Hide Injury News ▲")
        else:
            self.news_toggle_button.setText("Show Injury News ▼")

        # Defer the heavy layout work by 1ms to avoid blocking
        QTimer.singleShot(1, do_layout_work)


    def toggle_historical_odds(self):
        """Toggle visibility of the historical odds widget"""
        visible = self.history_toggle_button.isChecked()

        # Make the historical odds container visible/invisible based on toggle state
        self.historical_odds_container.setVisible(visible)

        # Update button text
        if visible:
            self.history_toggle_button.setText("Hide Historical Odds ▲")

            # Historical odds widget handles its own sport selection
            # No need to load events here - widget is self-contained
        else:
            self.history_toggle_button.setText("Show Historical Odds ▼")

            # If we're hiding, cancel any running data loads
            if hasattr(self.historical_odds_widget, '_load_task') and self.historical_odds_widget._load_task:
                try:
                    self.historical_odds_widget._load_task.cancel()
                except:
                    pass

        # Force update to apply changes
        # Make sure both are visible
        self.historical_odds_container.setVisible(visible)
        self.historical_odds_widget.setVisible(visible)

        # Force a layout update
        self.historical_odds_container.updateGeometry()
        self.historical_odds_widget.updateGeometry()
        QTimer.singleShot(10, self.update)


    def update_best_lines_display(self):
        """Update the best lines widget with the latest consolidated raw API data."""
        if not self.best_lines_widget:
            print("Best lines widget not initialized. Skipping update.")
            return

        # Use the consolidated raw data if available
        if hasattr(self, 'consolidated_odds_data') and self.consolidated_odds_data:
            self.best_lines = self.best_lines_widget.update_display(self.consolidated_odds_data)
        else:
            print("No consolidated odds data available for best lines calculation.")


    def on_odds_cell_double_clicked(self, row, col):
        """Double-click an odds cell to open the bookmaker's betslip deep-link."""
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        # Column 0 is the market/outcome label, not a bookmaker price.
        if col < 1:
            return
        item = table.item(row, col)
        if item is None:
            return
        link = item.data(Qt.ItemDataRole.UserRole)
        if link:
            QDesktopServices.openUrl(QUrl(link))
        else:
            self.statusBar().showMessage("No betslip link available for this cell", 3000)

    def on_market_selection_changed(self):
        """Handle market selection in the odds table"""
        table = self.tab_widget.currentWidget()
        if not table or not isinstance(table, QTableWidget):
            return

        current_row = table.currentRow()
        if current_row < 0:
            return

        # Get event and market info from the selected row
        header_item = table.item(current_row, 0)
        if not header_item:
            return

        row_label = header_item.text()
        market_type = ""

        # Skip header rows
        if "Game:" in row_label:
            return

        # Try to determine market type from the row label
        if "Moneyline" in row_label:
            market_type = "h2h"
        elif "Spread" in row_label:
            market_type = "spreads"
        elif "Total" in row_label:
            market_type = "totals"
        else:
            # If we can't determine, don't update
            return

        # Find the game ID from the item
        if not hasattr(header_item, 'game_id'): return;
        game_id = header_item.game_id

        # Get league and sport info
        league_name = self.league_selector.currentText()
        sport_key = self.data_manager.league_map.get(league_name)

        game_text = header_item.text().split('|', maxsplit=1)[0].strip()
        (home_team, away_team) = [text.strip() for text in game_text.split(' vs ', maxsplit=1)]

        # Only update the historical odds widget if it's visible
        if self.historical_odds_container.isVisible() and hasattr(self, 'historical_odds_widget'):
            self.historical_odds_widget.set_market(sport_key, game_id, market_type, home_team, away_team)



    def handle_tt_button(self):
        """Handle Table Tennis button click to open TableTennisGUI."""
        print("Table Tennis button clicked.")

        # If there's an existing window, properly destroy it
        if hasattr(self, "tt_window") and self.tt_window is not None:
            try:
                self.tt_window.close()
                self.tt_window.deleteLater()  # Schedule for Qt deletion
                self.tt_window = None
            except Exception as e:
                print(f"Error cleaning up old TT window: {e}")

        # Create a completely new instance
        self.tt_window = TableTennisGUI()
        self.tt_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # Qt will delete the widget when closed
        self.tt_window.show()




    @qasync.asyncSlot()
    # This function might just be too fucking much
    async def refresh_data(self):
        """Fetch and update odds data asynchronously using pandas for comparison"""
        if not self.leagues_loaded:
            print("Leagues not loaded yet. Please wait.")
            return

        # Show and reset progress bar
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.fetch_odds_button.setEnabled(False)

        # Update last update time
        current_time = datetime.now().strftime("%H:%M:%S")
        self.last_update_label.setText(f"Last Update: {current_time}")

        # Update status during refresh
        self.update_status.setStyleSheet("background-color: #007bff; color: white;")
        self.update_status.setText("Updating odds...")

        try:
            queries = self.query_list.get_queries()
            if not queries:
                self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
                self.update_status.setText("No queries configured")
                return

            total_slots = len(queries)
            changes = {}

            async with aiohttp.ClientSession() as session:
                for slot_idx, (sport_key, league_name, current_markets, region_set) in enumerate(queries):
                    region_str = ",".join(sorted(region_set)) if region_set else "us"
                    slot_base = slot_idx / total_slots

                    tab_data = self.create_league_tab(league_name, sport_key, current_markets)
                    current_df = tab_data.to_dataframe() if tab_data.table_rows else None

                    self.data_manager.sport_key = sport_key
                    self.data_manager.prop_client = PropClient(sport_key)

                    scores_data = await scores_query(sport_key, session=session)

                    games = await self.data_manager.prop_client.get_games(session)
                    print(f"[{league_name}] Fetched {len(games) if isinstance(games, list) else 0} games")

                    if not isinstance(games, list):
                        print(f"[{league_name}] Unexpected response from get_games(): {games}")
                        continue

                    total_games = len(games)
                    new_table_rows = []
                    new_table_data = {}
                    bookmakers_seen = set()
                    consolidated_odds_data = {'bookmakers': []}
                    bookmakers_map = {}

                    for index, game in enumerate(games):
                        game_id = game.get('id', '')
                        if not game_id:
                            continue

                        slot_progress = (index + 1) / total_games if total_games else 1
                        self.progress.setValue(int((slot_base + slot_progress / total_slots) * 100))

                        if index > 0:
                            await asyncio.sleep(0.034)

                        odds = await self.data_manager.prop_client.get_event_odds(
                            session, game_id, current_markets,
                            region=region_str, include_links=True, include_sids=True,
                        )

                        if odds is None:
                            continue

                        home_team = odds.get('home_team', 'Unknown')
                        away_team = odds.get('away_team', 'Unknown')

                        status_result = get_game_status(odds, scores_data)
                        if status_result is None:
                            continue
                        status_text, is_live, scores_text = status_result

                        if not hasattr(tab_data, 'game_status'):
                            tab_data.game_status = {}
                        tab_data.game_status[game_id] = {
                            'text': status_text, 'is_live': is_live, 'scores_text': scores_text
                        }

                        game_header = f"Game: {home_team} vs {away_team} [{status_text}]"
                        if scores_text:
                            game_header += f" - {scores_text}"
                        new_table_rows.append(game_header)
                        new_table_data[game_header] = {
                            'is_header': True, 'game_id': game_id,
                            'status_info': tab_data.game_status[game_id]
                        }

                        for bm in odds.get('bookmakers', []):
                            bm_title = bm['title']
                            bookmakers_seen.add(bm_title)
                            if bm.get('link'):
                                tab_data.bookmaker_links[(game_id, bm_title)] = bm['link']
                            if bm_title not in bookmakers_map:
                                bookmakers_map[bm_title] = {'title': bm_title, 'markets': []}
                                consolidated_odds_data['bookmakers'].append(bookmakers_map[bm_title])
                            for market in bm.get('markets', []):
                                market_key = market['key']
                                for outcome in market.get('outcomes', []):
                                    outcome['game_id'] = game_id
                                    outcome['game_name'] = f"{home_team} vs {away_team}"
                                bookmakers_map[bm_title]['markets'].append(market)
                                for outcome in market.get('outcomes', []):
                                    unique_label = f"{home_team} vs {away_team} | {self.format_market_label(market_key, outcome)}"
                                    if unique_label not in new_table_rows:
                                        new_table_rows.append(unique_label)
                                        new_table_data[unique_label] = {'game_id': game_id}
                                    new_table_data[unique_label][bm_title] = self.format_price(outcome)
                                    if outcome.get('link'):
                                        tab_data.cell_links[(unique_label, bm_title)] = outcome['link']

                    self.consolidated_odds_data = consolidated_odds_data
                    tab_data.consolidated_odds_data = consolidated_odds_data

                    new_df = pd.DataFrame(index=new_table_rows, columns=list(bookmakers_seen))
                    for row_label in new_table_rows:
                        row_data = new_table_data.get(row_label, {})
                        for bm in bookmakers_seen:
                            new_df.at[row_label, bm] = row_data.get(bm, "")
                    new_df['game_id'] = [new_table_data.get(r, {}).get('game_id', '') for r in new_table_rows]
                    new_df['is_header'] = [new_table_data.get(r, {}).get('is_header', False) for r in new_table_rows]

                    slot_changes = {}
                    if current_df is not None:
                        for row in new_df.index:
                            if row in current_df.index:
                                for bm in bookmakers_seen:
                                    if bm in current_df.columns:
                                        old_val = current_df.at[row, bm]
                                        new_val = new_df.at[row, bm]
                                        if old_val != new_val and old_val != "" and new_val != "":
                                            slot_changes.setdefault(row, {})[bm] = (old_val, new_val)
                    changes.update(slot_changes)

                    tab_data.bookmakers  = list(bookmakers_seen)
                    tab_data.table_rows  = new_table_rows
                    tab_data.table_data  = new_table_data
                    self.update_table_with_changes(tab_data, slot_changes)

                    if hasattr(self, 'best_lines_widget') and self.best_lines_widget:
                        try:
                            self.best_lines = self.best_lines_widget.update_display(consolidated_odds_data)
                        except Exception as e:
                            print(f"Error updating best lines widget: {e}")
                            traceback.print_exc()

            self.progress.setValue(100)
            if self.auto_update_check.isChecked():
                self.update_status_text()
                self.RestartTimer()
            else:
                self.update_status.setStyleSheet("background-color: #28a745; color: white;")
                self.update_status.setText("Update complete")
            if changes:
                print(f"Total lines changed: {sum(len(v) for v in changes.values())}")

        except aiohttp.ClientError as e:
            print(f"Network error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("Network error occurred")
        except Exception as e:
            print(f"Error: {e}")
            self.update_status.setStyleSheet("background-color: #dc3545; color: white;")
            self.update_status.setText("An error occurred")

            traceback.print_exc()
        finally:
            self.fetch_odds_button.setEnabled(True)
            # Hide and reset progress bar
            QTimer.singleShot(2000, lambda: (
                self.progress.setValue(0),
                self.progress.setVisible(False)
            ))
            # Reset error messages after a delay
            if not self.auto_update_check.isChecked():
                QTimer.singleShot(5000, lambda: self.update_status.setText(""))


    # These two functions are for live game indication
    # This class if getting massive oh no
    def _update_game_statuses(self):
        """Wrapper for async status updates"""
        asyncio.create_task(self.update_game_statuses())

    async def update_game_statuses(self):
        """Update game statuses periodically"""
        try:
            # Create a single session for all status updates
            async with aiohttp.ClientSession() as session:
                for tab_id, tab_data in self.league_tabs.items():
                    if not tab_data.game_status:
                        continue

                    # Fetch fresh scores data - only retrieve live/upcoming games
                    scores_data = await scores_query(tab_data.sport_key, days_from=None, session=session)
                    status_changed = False
                    print("-1 credit")

                    # Update each game's status
                    for game_id, current_status in tab_data.game_status.items():
                        matching_games = [score_game for score_game in scores_data if score_game.get('id') == game_id]
                        if len(matching_games) == 0: continue;
                        game_data = matching_games[0]

                        old_status = current_status.get('text', '')
                        old_scores = current_status.get('scores_text', '')

                        # Create game data for status check
                        #game_data = {'id': game_id, 'commence_time': ''}  # commence_time will be ignored with scores_data
                        #status_text, is_live, scores_text = get_game_status(game_data, scores_data)

                        # always do time-based calc because most leagues won't return scores for live games
                        time_diff = (datetime.now(timezone.utc) - datetime.fromisoformat(game_data["commence_time"])).total_seconds()

                        if time_diff < -1800:  # More than 30 min before
                            status_text, is_live, scores_text = "Pre-Game", False, "";
                        elif time_diff < 0:  # Less than 30 min before
                            status_text, is_live, scores_text = "Starting Soon", False, "";
                        elif time_diff < 14400:  # Less than 4 hours after (likely live)
                            status_text, is_live, scores_text = "🔴LIVE", True, "";
                        else:
                            continue

                        if status_text != old_status or scores_text != old_scores:
                            tab_data.game_status[game_id] = {
                                'text': status_text,
                                'is_live': is_live,
                                'scores_text': scores_text
                            }
                            status_changed = True

                    # Update table if statuses changed
                    if status_changed:
                        # Update row labels with new status
                        for i, row_label in enumerate(tab_data.table_rows):
                            if 'Game:' in row_label and '[' in row_label:
                                row_data = tab_data.table_data[row_label]
                                game_id = row_data.get('game_id')
                                if game_id in tab_data.game_status:
                                    # Rebuild header with new status
                                    teams_part = row_label.split('[')[0].strip()
                                    status_info = tab_data.game_status[game_id]

                                    new_row_label = f"{teams_part} [{status_info['text']}]"
                                    if status_info.get('scores_text'):
                                        new_row_label += f" - {status_info['scores_text']}"

                                    # Update the data structures
                                    tab_data.table_rows[i] = new_row_label
                                    tab_data.table_data[new_row_label] = tab_data.table_data.pop(row_label)
                                    tab_data.table_data[new_row_label]['status_info'] = status_info

                        self.update_table_display(tab_data)

        except Exception as e:
            print(f"Error updating game statuses: {e}")

    def update_table_with_changes(self, tab_data, changes):
         """Update the table with efficient display of odds changes"""
         table = tab_data.table_widget

         # Count the total number of changed cells
         total_changes = sum(len(changes_dict) for changes_dict in changes.values())

         # Update the changes counter label
         if hasattr(self, 'changes_counter_label'):
             if total_changes > 0:
                 self.changes_counter_label.setText(f"Lines Changed: {total_changes}")
                 self.changes_counter_label.setStyleSheet("color: #dc3545; font-weight: bold;")

                 # Reset the color after 5 seconds
                 QTimer.singleShot(5000, lambda: self.changes_counter_label.setStyleSheet("color: #6c757d;"))
             else:
                 self.changes_counter_label.setText("No Changes")

         # Update table structure if needed
         expected_cols = len(tab_data.bookmakers) + 1
         if table.columnCount() != expected_cols:
             table.setColumnCount(expected_cols)
             table.setHorizontalHeaderLabels(["Market/Outcome"] + tab_data.bookmakers)
             # Left-align the first header label so the docked filter bar (which
             # sits at the right of this section) never covers a centered label.
             hdr0 = table.horizontalHeaderItem(0)
             if hdr0 is not None:
                 hdr0.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

         expected_rows = len(tab_data.table_rows)
         if table.rowCount() != expected_rows:
             table.setRowCount(expected_rows)

         # Update all rows
         for row_idx, row_label in enumerate(tab_data.table_rows):
             row_data = tab_data.table_data[row_label]
             game_id = row_data['game_id']
             color = tab_data.get_game_color(game_id)

             # Create or update row header
             header_item = table.item(row_idx, 0)
             if not header_item:
                 header_item = ColoredTableItem(row_label, game_id)
                 table.setItem(row_idx, 0, header_item)
             else:
                 header_item.setText(row_label)

             # Apply header styling
             if row_data.get('is_header'):
                 font = QFont()
                 font.setBold(True)
                 header_item.setFont(font)
                 header_item.setBackground(color)
                 header_item.setForeground(QColor('black'))
             else:
                 market_color = QColor(color)
                 market_color.setAlpha(230)
                 header_item.setBackground(market_color)
                 header_item.setForeground(QColor('black'))

             # Update bookmaker columns
             for col_idx, bm in enumerate(tab_data.bookmakers, 1):
                 current_value = row_data.get(bm, "")

                 item = table.item(row_idx, col_idx)
                 if not item:
                     item = ColoredTableItem(current_value, game_id)
                     table.setItem(row_idx, col_idx, item)
                 else:
                     item.setText(current_value)

                 # Attach the betslip deep-link (outcome link, else event page per game)
                 link = (tab_data.cell_links.get((row_label, bm))
                         or tab_data.bookmaker_links.get((game_id, bm)))
                 item.setData(Qt.ItemDataRole.UserRole, link)
                 if link and current_value:
                     item.setToolTip("Double-click to open betslip")

                 # Check if this cell has changed
                 if row_label in changes and bm in changes[row_label]:
                     old_value, new_value = changes[row_label][bm]

                     # Parse the odds values for comparison
                     try:
                         current_odds = float(new_value.split()[0])
                         previous_odds = float(old_value.split()[0])

                         # Better odds (higher value) = green, worse odds = red
                         if current_odds > previous_odds:
                             highlight_color = QColor(0, 200, 0, 180)  # Semi-transparent green
                         else:
                             highlight_color = QColor(200, 0, 0, 180)  # Semi-transparent red

                         item.setBackground(highlight_color)
                         item.setForeground(QColor('black'))  # Keep text black for readability

                         # Reset background after 5 seconds
                         market_color = QColor(color)
                         market_color.setAlpha(230)
                         QTimer.singleShot(5000, lambda i=item, c=market_color: (
                             i.setBackground(c),
                             i.setForeground(QColor('black'))
                         ))
                     except (ValueError, IndexError):
                         # If we can't parse the odds, just update without highlighting
                         pass
                 else:
                     # No change, maintain consistent background and text color
                     if not row_data.get('is_header'):
                         market_color = QColor(color)
                         market_color.setAlpha(230)
                         item.setBackground(market_color)
                         item.setForeground(QColor('black'))

         # Resize the table
         table.resizeColumnsToContents()
         table.resizeRowsToContents()

    def handle_calc_button(self):
        """Show the odds-converter/calculator."""
        # If it's already open, close & re-open to reset state
        if hasattr(self, "calc_window") and self.calc_window:
            try:
                self.calc_window.close()
            except:
                pass
        self.calc_window = CalculatorApp()
        self.calc_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.calc_window.show()

    def handle_radio_button(self):
        """Show the radio scanner widget."""
        try:
            if self.radio_window and self.radio_window.isVisible():
                self.radio_window.raise_()
                self.radio_window.activateWindow()
                return
        except RuntimeError:
            # C++ object was deleted, clear the reference
            self.radio_window = None
        self.radio_window = ShortWaveRadioWidget()
        self.radio_window.setWindowTitle("// RADIO SCANNER")
        self.radio_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.radio_window.show()

    def filter_table(self):
        """Filter the current table based on search term"""
        search_term = self.search_bar.text().lower().strip()
        current_table = self.tab_widget.currentWidget()

        if not current_table or not isinstance(current_table, QTableWidget):
            return

        # Show all rows if search is empty
        if not search_term:
            for row in range(current_table.rowCount()):
                current_table.setRowHidden(row, False)
            return

        # Filter rows based on search term
        for row in range(current_table.rowCount()):
            # Get the market/outcome text (first column)
            header_item = current_table.item(row, 0)
            if not header_item:
                current_table.setRowHidden(row, True)
                continue

            row_text = header_item.text().lower()

            # Also check bookmaker odds in other columns
            match_found = search_term in row_text

            if not match_found:
                # Check odds values in bookmaker columns
                for col in range(1, current_table.columnCount()):
                    item = current_table.item(row, col)
                    if item and item.text():
                        if search_term in item.text().lower():
                            match_found = True
                            break

            # Show/hide row based on match
            current_table.setRowHidden(row, not match_found)

    def _attach_search_to_header(self, table=None):
        """Parent the filter bar onto the given (or current) table's header and
        keep it positioned over the 'Market/Outcome' section. Hooks the header's
        resize/scroll signals once so the overlay tracks the section geometry."""
        if not hasattr(self, 'search_bar'):
            return
        if table is None:
            table = self.tab_widget.currentWidget()
        if not isinstance(table, QTableWidget):
            # No real table to host the bar (e.g. empty/placeholder tab)
            self._current_header = None
            self.search_bar.hide()
            return

        header = table.horizontalHeader()
        self._current_header = header
        if self.search_bar.parent() is not header:
            self.search_bar.setParent(header)

        # Wire each header exactly once so we don't stack duplicate connections.
        if header not in self._wired_headers:
            header.sectionResized.connect(self._reposition_search_bar)
            header.geometriesChanged.connect(self._reposition_search_bar)
            header.installEventFilter(self)
            sb = table.horizontalScrollBar()
            if sb is not None:
                sb.valueChanged.connect(self._reposition_search_bar)
            self._wired_headers.add(header)

        self.search_bar.show()
        self._reposition_search_bar()

    def _market_label_reserve(self, header):
        """Pixel width to reserve at the left of section 0 for the
        'Market/Outcome' label so the filter bar never overlaps it."""
        fm = QFontMetrics(header.font())
        # +16 covers the header's 4px stylesheet padding on each side plus slack.
        return fm.horizontalAdvance("Market/Outcome") + 16

    def _reposition_search_bar(self, *args):
        """Right-align the filter bar inside header section 0, but never let it
        intrude on the left-anchored 'Market/Outcome' label. If the section is
        too narrow to hold both, the bar hides rather than covering the label."""
        header = self._current_header
        if header is None or self.search_bar.parent() is not header:
            return
        if header.count() <= 0:
            return

        section_x = header.sectionViewportPosition(0)
        section_w = header.sectionSize(0)
        margin = 6
        gap = 24  # breathing room between label and bar

        right_edge = section_x + section_w - margin
        left_edge = section_x + self._market_label_reserve(header) + gap
        avail = right_edge - left_edge

        # Not enough room for a usable bar without covering the label → hide it.
        if avail < 60:
            self.search_bar.hide()
            return

        bar_w = min(self._search_target_width, avail)
        bar_h = max(16, header.height() - 6)
        bar_x = right_edge - bar_w
        bar_y = (header.height() - bar_h) // 2
        self.search_bar.setGeometry(bar_x, bar_y, bar_w, bar_h)
        self.search_bar.show()
        self.search_bar.raise_()

    def eventFilter(self, obj, event):
        # Reposition the filter bar when its host header is resized.
        if event.type() == QEvent.Type.Resize and obj in self._wired_headers:
            self._reposition_search_bar()
        return super().eventFilter(obj, event)

    def handle_prediction_markets_error(self, error_message):
        """Handle errors from prediction markets worker"""
        print(f"Prediction markets error: {error_message}")

    def handle_prediction_markets_status(self, status_message):
        """Handle status updates from prediction markets worker"""
        print(f"Prediction markets status: {status_message}")

    def _setup_px_auth_bridge(self) -> None:
        """Wire the cross-thread bridge between the Playwright refresh
        (runs on a daemon thread) and the Qt main thread:

        - OTP requests from the headless browser pop a QInputDialog so
          the user types the SMS code into an in-app modal instead of
          having to spot the hidden Chromium window.
        - On successful token refresh, the bet slip's wallet/positions
          workers re-fire automatically so the labels reflect the new
          token without a manual ↻ click.
        """
        import threading
        import ProphetXQuery

        # Result holder for cross-thread OTP request/response.
        self._px_otp_event = threading.Event()
        self._px_otp_result = None

        # Signals are declared as pyqtSignal class members on
        # ModernOddsWindow; wire them to the main-thread slots via
        # QueuedConnection so emit() from the PW thread is safe.
        self.px_token_refreshed.connect(
            self._on_px_token_refreshed,
            Qt.ConnectionType.QueuedConnection)
        self.px_otp_requested.connect(
            self._on_px_otp_requested,
            Qt.ConnectionType.QueuedConnection)

        def otp_provider():
            self._px_otp_result = None
            self._px_otp_event.clear()
            self.px_otp_requested.emit()
            # Block the PW thread for up to 3 min while the user types.
            self._px_otp_event.wait(timeout=180)
            return self._px_otp_result

        def on_refresh():
            try:
                self.px_token_refreshed.emit()
            except Exception:
                pass

        ProphetXQuery.set_otp_provider(otp_provider)
        ProphetXQuery.on_token_refresh(on_refresh)
        print("[px-bridge] OTP provider + token-refresh listener registered")

    def _on_px_otp_requested(self) -> None:
        """Main-thread slot: pop a QInputDialog for the ProphetX SMS
        code, stash the result, and release the PW thread waiting on
        _px_otp_event."""
        code, ok = QInputDialog.getText(
            self, "ProphetX SMS OTP",
            "Enter the SMS code from ProphetX to complete re-authentication:",
            QLineEdit.EchoMode.Normal)
        self._px_otp_result = code.strip() if (ok and code) else None
        self._px_otp_event.set()

    def _on_px_token_refreshed(self) -> None:
        """Main-thread slot: re-fire wallet + positions workers so the
        bet slip labels repopulate immediately when a fresh token lands."""
        try:
            slip = self.liquidity_widget.orderbook.bet_slip
            slip.refresh_wallet()
        except Exception as e:
            print(f"[px-bridge] auto-refresh failed: {e}")

    def on_prophetx_event_selected(self, event_id: int):
        """Handle event selection from liquidity widget - trigger async refresh"""
        print(f"ProphetX event selected: {event_id}")
        # Update the worker's current event (this will trigger immediate fetch via signal)
        self.prophetx_worker.set_event(event_id)

    @qasync.asyncSlot()
    async def on_prophetx_refresh_all_requested(self):
        """
        Handle request for fresh ProphetX data on startup.
        Triggers a full async scrape of all ProphetX markets.
        """
        print("ProphetX refresh all requested - starting fresh scrape...")
        self.liquidity_widget.showLoading("Scraping ProphetX markets...")

        try:
            # Import the async scraping function
            from prophetx_async import FetchAllEventsAsync

            # Fetch all events fresh (30s hard cap so a stuck request can't pin the spinner)
            all_markets = await asyncio.wait_for(
                FetchAllEventsAsync(save_combined=True), timeout=30
            )

            if all_markets:
                # Feed the fresh dump through the widget's gated startup
                # coordinator. It holds the single event-list paint AND the
                # loading overlay until the Novig event list + match map + NV
                # dump are all in, then paints once (already combined) and
                # auto-opens the top event itself. Painting / auto-selecting /
                # revealing from here directly (the old path) bypassed that
                # gate and is exactly what produced the PX-only-then-matched
                # two-stage load.
                self.liquidity_widget.load_fresh_prophetx_dump(all_markets)
                print(f"ProphetX fresh scrape complete: {len(all_markets)} events")
            else:
                print("ProphetX fresh scrape returned no data")
                self.liquidity_widget.hideLoading()

        except asyncio.TimeoutError:
            print("ProphetX refresh timed out after 30s")
            self.liquidity_widget.hideLoading()

        except ImportError as e:
            # FetchAllEventsAsync might not exist — leave the event list
            # populated from stale data but don't auto-open any event's
            # orderbook (would show post-game leftover orders).
            print(f"FetchAllEventsAsync not available ({e}), using stale data...")
            self.liquidity_widget.hideLoading()

        except Exception as e:
            print(f"Error during ProphetX refresh: {e}")
            traceback.print_exc()
            self.liquidity_widget.hideLoading()

    async def _get_prophetx_session(self):
        """Lazily build a long-lived aiohttp session for the periodic
        ProphetX refresh path. Reusing one session avoids paying
        TCP+TLS handshake every 20s. Must be called from the qasync loop."""
        sess = getattr(self, "_prophetx_session", None)
        if sess is None or sess.closed:
            from prophetx_async import REQUEST_TIMEOUT
            self._prophetx_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._prophetx_session

    @qasync.asyncSlot(int)
    async def fetch_prophetx_event(self, event_id: int):
        """Fetch ProphetX event data asynchronously in main thread"""
        try:
            self.prophetx_worker.status_update.emit(f"Fetching ProphetX event {event_id}...")

            session = await self._get_prophetx_session()
            from prophetx_async import GetEventMarketsAsync, precompute_market_caches
            markets_data = await GetEventMarketsAsync(session, event_id)

            if markets_data:
                # Offload per-market liquidity sum + content-signature
                # walk to a worker thread so the qasync loop stays
                # responsive while the ~20-40ms of pure-Python order
                # iteration runs.
                loop = asyncio.get_running_loop()
                markets_data = await loop.run_in_executor(
                    None, precompute_market_caches, markets_data)

                self.prophetx_worker.data_ready.emit(markets_data)
                num_markets = len(markets_data.get('data', {}).get('markets', []))
                self.prophetx_worker.status_update.emit(f"Updated {num_markets} markets")
            else:
                self.prophetx_worker.error_occurred.emit(f"No data for event {event_id}")

        except Exception as e:
            self.prophetx_worker.error_occurred.emit(f"Error fetching event: {e}")

    def handle_prophetx_error(self, error_message):
        """Handle errors from ProphetX worker"""
        print(f"ProphetX error: {error_message}")
        # Hide loading on error so user isn't stuck with loading animation
        if hasattr(self, 'liquidity_widget'):
            self.liquidity_widget.hideLoading()

    def handle_prophetx_status(self, status_message):
        """Handle status updates from ProphetX worker"""
        print(f"ProphetX status: {status_message}")

    def closeEvent(self, event):
        """Clean up when the application is closing"""
        print("Application closing, cleaning up background operations...")

        # Stop the prediction markets worker
        if hasattr(self, 'prediction_markets_worker'):
            print("Stopping prediction markets worker...")
            self.prediction_markets_worker.stop()

        # Stop ProphetX worker
        if hasattr(self, 'prophetx_worker'):
            print("Stopping ProphetX worker...")
            self.prophetx_worker.running = False
            self.prophetx_worker_thread.quit()
            if not self.prophetx_worker_thread.wait(2000):
                print("ProphetX worker didn't stop gracefully, terminating...")
                self.prophetx_worker_thread.terminate()
                self.prophetx_worker_thread.wait(1000)

        # Stop team news widget worker
        if hasattr(self, 'team_news_widget') and hasattr(self.team_news_widget, 'worker_thread'):
            print("Stopping team news worker...")
            self.team_news_widget.worker.running = False
            self.team_news_widget.worker_thread.quit()
            if not self.team_news_widget.worker_thread.wait(2000):
                print("Team news worker didn't stop gracefully, terminating...")
                self.team_news_widget.worker_thread.terminate()
                self.team_news_widget.worker_thread.wait(1000)

        # Stop wallet/positions workers inside the LiquidityWidget's
        # bet slip — they spawn QThreads owned by BetSlipDrawer; killing
        # the parent without quit()+wait() triggers Qt's "Timers cannot
        # be stopped from another thread" warning on shutdown.
        slip = getattr(getattr(self.liquidity_widget, "orderbook", None),
                       "bet_slip", None)
        if slip is not None:
            print("Stopping wallet workers...")
            try:
                slip.cleanup()
            except Exception as e:
                print(f"  bet_slip cleanup failed: {e}")

        # Stop any update timers
        if hasattr(self, 'update_timer') and self.update_timer.isActive():
            self.update_timer.stop()

        # Close the persistent ProphetX aiohttp session (fire-and-forget;
        # the loop is closing anyway, but this avoids a "Unclosed client
        # session" warning on shutdown).
        sess = getattr(self, "_prophetx_session", None)
        if sess is not None and not sess.closed:
            try:
                asyncio.get_event_loop().create_task(sess.close())
            except Exception:
                pass

        print("Cleanup complete")
        event.accept()



def main():
    app = QApplication([])

    # Create qasync event loop FIRST, before any async operations
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ModernOddsWindow()

    # Render the UI immediately. Async initialization (populate_leagues
    # over the network, etc.) runs as a task once the loop starts — the
    # league selector will fill in after the first frame instead of
    # blocking window.show() behind a network round-trip.
    window.show()
    loop.create_task(window.initialize())

    # ╔══ [PERF-DIAG] Temporary UI-stutter instrumentation ════════════════╗
    # Catalogued in OddsAPI/PERF_DIAGNOSTICS.md. Grep "[PERF-DIAG]" to find
    # every diagnostic block in the codebase; ALL of them are deletable once
    # the ticker stutter is root-caused and fixed.
    #
    # (1) event-loop lag monitor — reports *that* the shared qasync loop
    #     stalled, but only after it unblocks (the offending call is already
    #     off the stack by the time this logs). Good for spotting frequency.
    import time as _time
    async def _loop_lag_monitor(threshold_ms=100, interval=0.05):
        while True:
            t0 = _time.perf_counter()
            await asyncio.sleep(interval)
            late_ms = (_time.perf_counter() - t0 - interval) * 1000
            if late_ms > threshold_ms:
                # bypass the DEBUG gate so lag readings show regardless
                _real_print(f"[loop-lag] blocked {late_ms:.0f}ms @ {_time.strftime('%H:%M:%S')}")
    loop.create_task(_loop_lag_monitor())

    # (2) main-loop watchdog — reports *where* the loop stalled. A heartbeat
    #     coroutine refreshes a timestamp every 20ms while the loop is healthy;
    #     an independent OS thread (NOT a QThread, so it keeps running while the
    #     main thread is parked) watches that timestamp and, the moment it goes
    #     stale past the threshold, dumps every thread's Python stack mid-stall.
    #       • MainThread deep in app code (e.g. resizeRowsToContents / pandas
    #         .at fill)            → synchronous main-loop burst   (Tier-1 fix)
    #       • MainThread idle in select/poll while a worker thread is busy in
    #         json/parse           → GIL contention from that worker (Tier-3 fix)
    #     faulthandler is C-level and dumps all threads regardless of who holds
    #     the GIL (CPython releases it every ~5ms, so this thread gets scheduled).
    import faulthandler as _faulthandler
    import threading as _threading
    import sys as _sys
    # Stacks go to a DEDICATED file (not stderr) so they're always captured no
    # matter how the app is launched — no 2>&1 needed. faulthandler writes via
    # the raw fd so it lands on disk immediately; line-buffered text mode keeps
    # the headline ordered with it. Read OddsAPI/stutter_dumps.txt after a run.
    _wd_path = pathlib.Path(__file__).parent / "stutter_dumps.txt"
    _wd_file = open(_wd_path, "a", buffering=1)
    _real_print(f"[watchdog] stall stacks → {_wd_path}")
    _wd = {"beat": _time.monotonic()}            # dict cell: shared, no nonlocal
    async def _wd_heartbeat(interval=0.02):
        while True:
            _wd["beat"] = _time.monotonic()
            await asyncio.sleep(interval)
    def _wd_watch(threshold=0.12, poll=0.02):
        dumped = False                           # de-dupe: one dump per stall
        while True:
            _time.sleep(poll)
            lag = _time.monotonic() - _wd["beat"]
            if lag > threshold:
                if not dumped:
                    _real_print(
                        f"\n[watchdog] main loop blocked {lag*1000:.0f}ms @ "
                        f"{_time.strftime('%H:%M:%S')} — all-thread stacks below:",
                        file=_wd_file, flush=True)
                    _faulthandler.dump_traceback(file=_wd_file, all_threads=True)
                    _wd_file.flush()
                    # Brief echo to stdout so it's obvious in the console too.
                    _real_print(f"[watchdog] {lag*1000:.0f}ms stall — stack written "
                                f"to stutter_dumps.txt")
                    dumped = True
            else:
                dumped = False
    loop.create_task(_wd_heartbeat())
    _threading.Thread(target=_wd_watch, daemon=True, name="loop-watchdog").start()
    # ╚════════════════════════════════════════════════════════════════════╝

    if DEBUG:
        app.dumpObjectTree()
        app.dumpObjectInfo()

    # Run the event loop (handles both Qt and asyncio)
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
