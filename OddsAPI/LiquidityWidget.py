#!/usr/bin/env python3
"""
ProphetX Order Book Widget
Professional PyQt6 widget for viewing ProphetX exchange order books
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QSplitter, QFrame, QComboBox, QHeaderView, QGraphicsOpacityEffect,
    QStackedWidget, QPushButton, QProgressBar, QTreeWidget, QTreeWidgetItem,
    QStyledItemDelegate, QDoubleSpinBox, QScrollArea
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve, QRectF, QPointF, QThread, QSize, QObject, QThreadPool, QRunnable
from PyQt6.QtGui import QFont, QColor, QPalette, QPainter, QPen, QBrush, QLinearGradient, QRadialGradient, QPainterPath, QFontMetrics
import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional
import ProphetXQuery

#TODO: Remarkable how well this widget works given how weak this code is. Massive refactor required.
# Delicate, careful refactor that is.


# [PX]/[NV]/[PX/NV] prefix tokens for an event's dropdown label, keyed by the
# source class returned by ProphetXBrowser._event_source_for.
_SOURCE_BADGES = {"BOTH": "[PX/NV] ", "PX": "[PX] ", "NV": "[NV] "}


def _action_button_qss(fs: int, compact: bool) -> str:
    """Shared mint-green primary-action button stylesheet (Place / Run)."""
    vpad, hpad = (3, 10) if compact else (6, 16)
    return f"""
        QPushButton {{
            background-color: #1f6f5e; color: #e8fff8;
            border: 1px solid #5eead4; border-radius: 4px;
            padding: {vpad}px {hpad}px;
            font-size: {fs}px; font-weight: 600;
        }}
        QPushButton:hover {{ background-color: #2a8c76; }}
        QPushButton:disabled {{
            background-color: #1a1d24; color: #555b66;
            border: 1px solid #2a2d34;
        }}
    """


# ============================================================================
# Wallet snapshot data classes + workers
# ----------------------------------------------------------------------------
# The bet slip displays current PX/NV balances + open positions. We pull this
# in two cadences:
#   - balance: once per session (cheap, rarely changes outside of placements)
#   - positions: re-fetched after each placement + on user ↻ click
#
# The actual API calls live in ProphetXQuery (PX) and NovigClient (NV). These
# workers run them on a fresh asyncio loop inside a QThread and emit the
# normalized result back to the BetSlipDrawer.
# ============================================================================

@dataclass
class Position:
    """One open wager, normalized across PX and NV."""
    src: str               # "PX" or "NV"
    event: str
    market: str
    side: str
    odds: str              # american-format display string
    stake: float           # dollars at risk
    matched: float
    status: str            # raw status from the API


@dataclass
class WalletSnapshot:
    """Unified balance + positions snapshot. Either side may be partially
    populated (one source failing doesn't poison the other)."""
    px_balance: Optional[float] = None
    px_withdrawable: Optional[float] = None
    px_exposure: Optional[float] = None
    px_positions: List[Position] = field(default_factory=list)
    px_error: Optional[str] = None

    nv_balance: Optional[float] = None
    nv_coin_balance: Optional[float] = None
    nv_bonus_balance: Optional[float] = None
    nv_positions: List[Position] = field(default_factory=list)
    nv_error: Optional[str] = None


def _normalize_px_wager(w: dict) -> Position:
    sport_event = (w.get("sportEvent") or {}).get("name") or ""
    market = (w.get("market") or {}).get("name") or w.get("marketLineName") or ""
    mtype = (w.get("market") or {}).get("type") or ""
    label = f"{market} ({mtype})" if mtype else market
    outcome = (w.get("outcome") or {}).get("name") or ""
    line = w.get("displayLine") or w.get("line") or ""
    side = outcome
    if line and str(line) not in ("0.0", "0", ""):
        side = f"{outcome} {line}"
    try:
        a = int(w.get("odds") or 0)
        odds_disp = w.get("displayOdds") or (f"+{a}" if a > 0 else str(a))
    except (TypeError, ValueError):
        odds_disp = w.get("displayOdds") or ""
    return Position(
        src="PX",
        event=sport_event,
        market=label,
        side=side,
        odds=odds_disp,
        stake=float(w.get("stake") or 0),
        matched=float(w.get("matchedStake") or 0),
        status=w.get("matchingStatus") or w.get("status") or "",
    )


def _normalize_px_parlay(p: dict) -> Position:
    legs = p.get("legs") or []
    n_legs = len(legs)
    titles = [leg.get("sportEventName") for leg in legs
              if leg.get("sportEventName")]
    if titles:
        seen = set()
        uniq = [t for t in titles if not (t in seen or seen.add(t))]
        event_desc = " • ".join(uniq[:2]) + (" …" if len(uniq) > 2 else "")
    else:
        event_desc = "Parlay"
    leg_summary = []
    for leg in legs[:3]:
        mkt = (leg.get("market") or {}).get("name") or ""
        sel = (leg.get("selection") or {}).get("name") or ""
        if mkt or sel:
            leg_summary.append(f"{mkt}: {sel}".strip(": "))
    side = " / ".join(leg_summary) + (" …" if n_legs > 3 else "")
    # Parlay endpoint encodes odds as american * 100 — divide if too large.
    raw = p.get("confirmedOdds") or p.get("requestedOdds") or 0
    try:
        v = int(raw)
        if abs(v) > 10000:
            v = round(v / 100)
        odds_disp = f"+{v}" if v > 0 else str(v)
    except (TypeError, ValueError):
        odds_disp = str(raw)
    stake = float(p.get("confirmedStake") or p.get("requestedStake") or 0)
    return Position(
        src="PX",
        event=event_desc,
        market=f"{n_legs}-leg parlay",
        side=side,
        odds=odds_disp,
        stake=stake,
        matched=stake,
        status=p.get("settlementStatus") or p.get("status") or "",
    )


def _normalize_nv_order(o: dict) -> Position:
    from NovigClient import nv_decimal_to_american
    market = o.get("market") or {}
    outcome = o.get("outcome") or {}
    event = (market.get("event") or {}) if isinstance(market, dict) else {}
    event_desc = event.get("description") or ""
    mtype = market.get("type") or ""
    market_label = (market.get("name") or
                    (market.get("market_detail") or {}).get("name") or
                    mtype or "")
    side = outcome.get("description") or ""
    competitor = outcome.get("competitor") or {}
    if competitor.get("name"):
        side = competitor["name"]
    strike = market.get("strike")
    if strike and float(strike) != 0:
        side = f"{side} {strike}".strip()
    qty = float(o.get("qty") or 0)
    orig = float(o.get("originalQty") or 0)
    return Position(
        src="NV",
        event=event_desc,
        market=market_label,
        side=side,
        odds=nv_decimal_to_american(o.get("price")),
        stake=orig,
        matched=orig - qty if qty <= orig else orig,
        status=o.get("status") or "",
    )


def _emit_snapshot(worker: "QThread", snap: "WalletSnapshot", coro) -> None:
    """Run a wallet/positions coroutine on a fresh loop with a hard timeout,
    backfilling any uncaught error onto both exchange slots, then emit the
    snapshot. Shared by the balance + positions workers."""
    try:
        asyncio.run(asyncio.wait_for(coro, timeout=15))
    except Exception as e:
        snap.px_error = snap.px_error or str(e)
        snap.nv_error = snap.nv_error or str(e)
    worker.snapshot_ready.emit(snap)


class WalletBalanceWorker(QThread):
    """One-shot balance fetch (PX + NV). Emits a WalletSnapshot with only
    the balance fields populated; positions are left empty so the caller
    can merge in a positions snapshot independently."""

    snapshot_ready = pyqtSignal(object)

    def run(self) -> None:
        snap = WalletSnapshot()

        async def _go():
            import aiohttp
            try:
                from ProphetXQuery import fetch_px_wallet
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    wallet = await fetch_px_wallet(session)
                snap.px_balance = float(wallet.get("totalBalance") or
                                        wallet.get("balance") or 0)
                snap.px_withdrawable = float(wallet.get("withdrawableCash") or 0)
                snap.px_exposure = float(wallet.get("exposureCredit") or 0)
            except Exception as e:
                snap.px_error = str(e)

            try:
                from NovigClient import fetch_nv_balance
                from Creds import NOVIG_USER_ID
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    bal = await fetch_nv_balance(session, NOVIG_USER_ID)
                snap.nv_balance = bal.get("cash")
                snap.nv_coin_balance = bal.get("coin")
                snap.nv_bonus_balance = bal.get("bonus")
                if bal.get("error"):
                    snap.nv_error = bal["error"]
            except Exception as e:
                snap.nv_error = str(e)

        _emit_snapshot(self, snap, _go())


class OpenPositionsWorker(QThread):
    """Refreshable open-positions fetch (PX singles + parlays + NV).
    Emits a WalletSnapshot with only the positions / error fields set;
    balance fields stay None so callers know to preserve their cached
    balance snapshot rather than overwriting it."""

    snapshot_ready = pyqtSignal(object)

    def run(self) -> None:
        snap = WalletSnapshot()

        async def _go():
            import aiohttp
            try:
                from ProphetXQuery import (fetch_px_open_wagers,
                                           fetch_px_open_parlays)
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    wagers, parlays = await asyncio.gather(
                        fetch_px_open_wagers(session),
                        fetch_px_open_parlays(session),
                        return_exceptions=True,
                    )
                singles = []
                parlay_rows = []
                if isinstance(wagers, Exception):
                    snap.px_error = f"wagers: {wagers}"
                else:
                    singles = [_normalize_px_wager(w) for w in wagers]
                if isinstance(parlays, Exception):
                    snap.px_error = snap.px_error or f"parlays: {parlays}"
                else:
                    parlay_rows = [_normalize_px_parlay(p) for p in parlays]
                snap.px_positions = singles + parlay_rows
            except Exception as e:
                snap.px_error = str(e)

            try:
                from NovigClient import fetch_nv_open_positions
                from Creds import NOVIG_TRADER_ID, NOVIG_AUTH_ID
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    rows = await fetch_nv_open_positions(
                        session, NOVIG_TRADER_ID, NOVIG_AUTH_ID)
                snap.nv_positions = [_normalize_nv_order(o) for o in rows]
            except Exception as e:
                snap.nv_error = str(e)

        _emit_snapshot(self, snap, _go())


class NovigDumpWorker(QThread):
    """QThread wrapper around `novig_async.FetchAllLeaguesAsync`.

    Fired by ProphetXBrowser on init when no fresh on-disk dump exists,
    so the match map between ProphetX and Novig events is populated
    before the user starts clicking markets. The scrape runs on a fresh
    asyncio loop inside this thread (separate from the main qasync loop)
    and writes the dump to disk; emits the captured event count when done.

    Failures are reported via the failure signal — the widget keeps
    working as PX-only when this errors out.
    """

    dump_ready = pyqtSignal(int)   # number of events captured
    dump_failed = pyqtSignal(str)  # short error string

    def run(self) -> None:
        # Use the async path (novig_async.FetchAllLeaguesAsync) — runs all
        # league listings + per-event market fetches concurrently via one
        # aiohttp session. The QThread hosts a fresh asyncio loop just for
        # this scrape, so the main qasync loop is untouched.
        try:
            import asyncio
            from novig_async import FetchAllLeaguesAsync
        except Exception as e:
            self.dump_failed.emit(f"import: {e}")
            return
        try:
            dump = asyncio.run(FetchAllLeaguesAsync(
                save=True, progress=False))
            self.dump_ready.emit(len(dump))
        except Exception as e:
            self.dump_failed.emit(str(e))


class NovigMarketBookWorker(QThread):
    """Async fetcher for Novig /book/batch.

    Given a list of Novig market UUIDs, fetches their orderbook ladders
    in CASH currency on a background thread and emits the result as a
    {market_id: book_entry} dict. Used by ProphetXBrowser to refresh
    Novig book depth on event/market selection without blocking the UI.

    Failures are non-fatal — an empty dict is emitted so the widget can
    fall back to dump-level top-of-book pricing.
    """

    # Signal payload: (prophetx_event_id, {novig_market_id: book_entry})
    # prophetx_event_id is the *requesting* event so callers can ignore
    # stale results if the user has moved on.
    books_ready = pyqtSignal(str, dict)

    def __init__(self, prophetx_event_id: str,
                 novig_market_ids: List[str], parent=None):
        super().__init__(parent)
        self.prophetx_event_id = prophetx_event_id
        self.novig_market_ids = list(novig_market_ids)

    def run(self) -> None:
        if not self.novig_market_ids:
            self.books_ready.emit(self.prophetx_event_id, {})
            return
        # Lazy import keeps PyQt-only consumers from pulling NovigClient
        # at module load time.
        try:
            from NovigClient import NovigClient, NovigError
        except Exception:
            self.books_ready.emit(self.prophetx_event_id, {})
            return

        books: Dict[str, dict] = {}
        try:
            client = NovigClient()
        except Exception:
            self.books_ready.emit(self.prophetx_event_id, {})
            return

        # Batch in chunks of 20 (server-friendly + matches the dump scraper).
        for i in range(0, len(self.novig_market_ids), 20):
            batch = self.novig_market_ids[i:i + 20]
            try:
                resp = client.get_market_books(batch, currency="CASH")
            except NovigError:
                continue
            except Exception:
                continue
            for b in resp:
                mid = (b.get("market") or {}).get("id")
                if mid:
                    books[mid] = b
        self.books_ready.emit(self.prophetx_event_id, books)


class NovigEventsWorker(QThread):
    """Live event-list fetch so Novig events show in the dropdown alongside
    ProphetX. Uses NovigQueries.list_events (visible_only=True → only events
    actually live on the site, no phantom pre-listings) and shapes each into
    the same {event_metadata, data:{markets}} dict all_events uses. Markets are
    left empty here — they're fetched live when the event is opened."""

    events_ready = pyqtSignal(dict)  # {event_id: dump-style entry}

    def run(self) -> None:
        try:
            from NovigClient import (NovigClient, NovigQueries,
                                     _event_to_dump_entries)
            q = NovigQueries(NovigClient())
            nodes = []
            nodes += q.list_events(league=None,
                                   status_in=("OPEN_PREGAME",), limit=1000)
            nodes += q.list_events(league=None,
                                   status_in=("OPEN_INGAME",), limit=500)
        except Exception as e:
            print(f"[LiquidityWidget] Novig event list fetch failed: {e!r}")
            self.events_ready.emit({})
            return
        out: dict = {}
        for node in nodes:
            try:
                for eid, entry in _event_to_dump_entries(node):
                    if eid:
                        entry["_exchange"] = "NV"
                        out[str(eid)] = entry
            except Exception:
                continue
        self.events_ready.emit(out)


class NovigEventMarketsWorker(QThread):
    """Live single-event market fetch for an NV-only event: get_event_markets
    (only_available=True → OPEN markets with liquidity/consensus) + /book/batch
    depth, normalized to a NormalizedEvent. Mirrors how a ProphetX event pulls
    fresh markets on selection, but for the Novig side."""

    markets_ready = pyqtSignal(str, object)  # (event_id, NormalizedEvent|None)

    def __init__(self, event_id: str, parent=None):
        super().__init__(parent)
        self.event_id = str(event_id)

    def run(self) -> None:
        try:
            from NovigClient import NovigClient, NovigQueries
            import exchange_market_keys as emk
            client = NovigClient()
            q = NovigQueries(client)
            node = q.get_event_markets(self.event_id, only_available=True)
            if not node:
                self.markets_ready.emit(self.event_id, None)
                return
            # Live book depth. get_market_books auto-chunks, so passing every
            # market id is safe even for huge slates (e.g. NBA finals).
            flat = NovigQueries.flatten_markets(node)
            ids = [m.get("id") for m in flat if m.get("id")]
            books_list = client.get_market_books(ids, currency="CASH") if ids else []
            books = {(b.get("market") or {}).get("id"): b for b in books_list}
            nev = emk.from_novig_event(node, books=books, currency="CASH")
        except Exception as e:
            print(f"[LiquidityWidget] Novig event markets fetch failed "
                  f"for {self.event_id}: {e!r}")
            self.markets_ready.emit(self.event_id, None)
            return
        self.markets_ready.emit(self.event_id, nev)


class MatchMapWorker(QThread):
    """Builds the ProphetX↔Novig event match map off the main thread.

    The load (disk JSON of the ~650-event Novig dump) + normalize + match
    (28 PX × 650 NV events) is ~135ms of disk I/O plus ~300-400ms of pure-
    Python cross-matching — a GIL-bound burst that stalled the ticker by
    ~270-570ms when it ran on the qasync loop inside _loadNovigMatchMap.

    The worker reproduces exactly that compute and emits the resulting
    pairs list. The caller (ProphetXBrowser._onMatchMapReady) does the only
    main-thread-bound part: stamping self._novig_event_map and re-rendering
    the current market. px_data is passed in (a snapshot of self.all_events)
    so the worker never touches live widget state.

    Emits an empty list on any failure → widget stays PX-only, same as the
    old inline except-branch.
    """

    map_ready = pyqtSignal(list)  # list[EventPair]

    def __init__(self, px_data: dict, parent=None):
        super().__init__(parent)
        # Shallow copy so a concurrent refresh re-assigning self.all_events
        # can't mutate what we're iterating. The event dicts themselves are
        # read-only here.
        self._px_data = dict(px_data) if px_data else {}

    def run(self) -> None:
        try:
            from NovigClient import NovigQueries
            import exchange_market_keys as emk
        except Exception:
            self.map_ready.emit([])
            return

        try:
            # Slim metadata-only index (~0.5MB / ~2ms) instead of the full
            # ~48MB / ~122ms dump: event-level pairing only needs identity, so
            # the markets are hydrated lazily per-event when the user opens one
            # (ProphetXBrowser._lookupCurrentMarketPair). This is THE fix for
            # the startup ticker stutter — the 122ms GIL-held parse is gone.
            nv_data = NovigQueries.load_events_index()
            if not nv_data:
                self.map_ready.emit([])
                return

            px_data = self._px_data
            if not px_data:
                dump_dir = Path.cwd() / "prophetx_dumps"
                if dump_dir.exists():
                    files = sorted(dump_dir.glob("all_markets_combined_*.json"),
                                   key=lambda p: p.stat().st_mtime)
                    if files:
                        try:
                            px_data = json.loads(files[-1].read_text())
                        except Exception:
                            px_data = {}
            if not px_data:
                self.map_ready.emit([])
                return

            # populate_markets=False on BOTH sides: event-level pairing only
            # reads identity fields, and the eager PX market/order normalize
            # here was a ~100ms+ pure-Python GIL burn starving the UI loop.
            # hydrate_event_pair_markets fills both sides in for the one
            # event the user actually opens.
            px_events = emk.load_prophetx_normalized_events(
                px_data, league_filter=None, populate_markets=False)
            nv_events = emk.load_novig_normalized_events(
                nv_data, league_filter=None, currency="CASH")
            # populate_markets=False: nv_events have no markets (index is
            # metadata-only); pair on identity now, hydrate markets on demand.
            pairs = emk.match_events(px_events, nv_events,
                                     populate_markets=False)
            self.map_ready.emit(pairs)
        except Exception as e:
            import traceback
            print(f"[LiquidityWidget] Novig match-map build failed: {e!r}")
            traceback.print_exc()
            self.map_ready.emit([])


class OrderBookLoadingOverlay(QWidget):
    """
    Animated loading overlay for orderbook widget.
    Features a sophisticated scanning/pulsing effect with particle-like elements.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("background: transparent;")

        # Animation state
        self.scan_offset = 0.0
        self.pulse_phase = 0.0
        self.particle_phase = 0.0
        self.glow_intensity = 0.0

        # Fake orderbook rows for skeleton effect
        self.skeleton_rows = 12

        # Timer for animation (~60fps)
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._updateAnimation)

        # Status text
        self.status_text = "Fetching live orderbook..."

    def start(self, status_text: str = "Fetching live orderbook..."):
        """Start the loading animation"""
        self.status_text = status_text
        self.scan_offset = 0.0
        self.pulse_phase = 0.0
        self.show()
        self.raise_()
        self.animation_timer.start(16)  # ~60fps

    def stop(self):
        """Stop the loading animation"""
        self.animation_timer.stop()
        self.hide()

    def _updateAnimation(self):
        """Update animation state each frame"""
        self.scan_offset = (self.scan_offset + 3) % (self.height() + 100)
        self.pulse_phase = (self.pulse_phase + 0.08) % (2 * math.pi)
        self.particle_phase = (self.particle_phase + 0.03) % (2 * math.pi)
        self.glow_intensity = 0.5 + 0.5 * math.sin(self.pulse_phase)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()

        # Semi-transparent dark background
        painter.fillRect(self.rect(), QColor(13, 15, 20, 230))

        # Draw skeleton orderbook rows
        self._drawSkeletonRows(painter, w, h)

        # Draw scanning line effect
        self._drawScanLine(painter, w, h)

        # Draw central glow orb
        self._drawGlowOrb(painter, w, h)

        # Draw status text
        self._drawStatusText(painter, w, h)

        # Draw floating particles
        self._drawParticles(painter, w, h)

    def _drawSkeletonRows(self, painter: QPainter, w: int, h: int):
        """Draw faint skeleton orderbook rows that pulse"""
        row_height = 28
        start_y = 60  # Below header area

        for i in range(self.skeleton_rows):
            y = start_y + i * row_height
            if y > h - 40:
                break

            # Alternating bid/ask colors with pulse
            phase_offset = i * 0.3
            alpha = int(20 + 15 * math.sin(self.pulse_phase + phase_offset))

            if i < self.skeleton_rows // 2:
                # Ask side (red tint)
                color = QColor(248, 113, 113, alpha)
            else:
                # Bid side (green tint)
                color = QColor(52, 211, 153, alpha)

            # Draw row background
            painter.fillRect(10, y, w - 20, row_height - 2, color)

            # Draw fake content bars
            bar_alpha = int(30 + 20 * math.sin(self.pulse_phase + phase_offset + 0.5))
            painter.fillRect(15, y + 8, 50, 12, QColor(255, 255, 255, bar_alpha))
            painter.fillRect(75, y + 8, 80, 12, QColor(255, 255, 255, bar_alpha))
            painter.fillRect(w - 70, y + 8, 50, 12, QColor(255, 255, 255, bar_alpha))

    def _drawScanLine(self, painter: QPainter, w: int, h: int):
        """Draw animated scanning line that sweeps down"""
        scan_y = self.scan_offset

        # Create gradient for scan line
        gradient = QLinearGradient(0, scan_y - 50, 0, scan_y + 50)
        gradient.setColorAt(0.0, QColor(74, 158, 255, 0))
        gradient.setColorAt(0.4, QColor(74, 158, 255, 100))
        gradient.setColorAt(0.5, QColor(74, 158, 255, 200))
        gradient.setColorAt(0.6, QColor(74, 158, 255, 100))
        gradient.setColorAt(1.0, QColor(74, 158, 255, 0))

        painter.fillRect(0, int(scan_y - 50), w, 100, gradient)

        # Bright center line
        painter.setPen(QPen(QColor(74, 158, 255, 255), 2))
        painter.drawLine(0, int(scan_y), w, int(scan_y))

    def _drawGlowOrb(self, painter: QPainter, w: int, h: int):
        """Draw central pulsing glow orb"""
        center_x, center_y = w // 2, h // 2

        # Outer glow
        outer_radius = 60 + 20 * self.glow_intensity
        gradient = QRadialGradient(center_x, center_y, outer_radius)
        gradient.setColorAt(0.0, QColor(74, 158, 255, int(80 * self.glow_intensity)))
        gradient.setColorAt(0.5, QColor(74, 158, 255, int(40 * self.glow_intensity)))
        gradient.setColorAt(1.0, QColor(74, 158, 255, 0))

        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(center_x, center_y), outer_radius, outer_radius)

        # Inner bright core
        inner_radius = 8 + 4 * self.glow_intensity
        gradient2 = QRadialGradient(center_x, center_y, inner_radius)
        gradient2.setColorAt(0.0, QColor(255, 255, 255, 255))
        gradient2.setColorAt(0.5, QColor(74, 158, 255, 200))
        gradient2.setColorAt(1.0, QColor(74, 158, 255, 0))

        painter.setBrush(gradient2)
        painter.drawEllipse(QPointF(center_x, center_y), inner_radius, inner_radius)

        # Rotating arc around the orb
        arc_radius = 30 + 10 * self.glow_intensity
        painter.setPen(QPen(QColor(74, 158, 255, 180), 3))
        arc_angle = int(self.pulse_phase * 180 / math.pi * 2) % 360
        painter.drawArc(
            int(center_x - arc_radius), int(center_y - arc_radius),
            int(arc_radius * 2), int(arc_radius * 2),
            arc_angle * 16, 90 * 16
        )

    def _drawStatusText(self, painter: QPainter, w: int, h: int):
        """Draw status text below the glow orb"""
        font = QFont("SF Mono", 11, QFont.Weight.DemiBold)
        painter.setFont(font)

        # Pulsing text alpha
        text_alpha = int(180 + 75 * math.sin(self.pulse_phase))
        painter.setPen(QColor(200, 210, 220, text_alpha))

        text_rect = QRectF(0, h // 2 + 50, w, 30)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self.status_text)

        # Animated dots
        dots = "." * (1 + int(self.pulse_phase / (math.pi / 2)) % 4)
        dots_rect = QRectF(0, h // 2 + 75, w, 20)
        painter.drawText(dots_rect, Qt.AlignmentFlag.AlignCenter, dots)

    def _drawParticles(self, painter: QPainter, w: int, h: int):
        """Draw floating particle effects"""
        center_x, center_y = w // 2, h // 2
        num_particles = 8

        for i in range(num_particles):
            angle = (2 * math.pi * i / num_particles) + self.particle_phase
            radius = 80 + 20 * math.sin(self.particle_phase * 2 + i)

            px = center_x + radius * math.cos(angle)
            py = center_y + radius * math.sin(angle)

            # Particle glow
            particle_alpha = int(100 + 50 * math.sin(self.particle_phase + i * 0.5))
            gradient = QRadialGradient(px, py, 8)
            gradient.setColorAt(0.0, QColor(74, 158, 255, particle_alpha))
            gradient.setColorAt(1.0, QColor(74, 158, 255, 0))

            painter.setBrush(gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(px, py), 8, 8)


class BetSlipDrawer(QWidget):
    """Collapsible bet slip pinned to the bottom of SGPScannerPanel.

    Collapsed state: a thin always-visible header bar showing the leg
    count plus the running stake → payout summary. Click expands upward
    (eating into the EV results table above) to reveal each leg, a
    shared per-leg wager input, the Place button, and a status strip.

    Owns the slip state but not the placement coroutine: clicking Place
    emits `place_requested(legs, wager)` so the parent panel can drive
    the async re-quote + execute flow with whatever session/clients it
    already has wired up.

    Each leg is a dict with at least: src ("PX"|"NV"), key (unique
    identifier for dedup + removal), label (display string),
    odds (american), edge_pct (float), and raw (the original EV row
    dict, used by the parent panel at place time).
    """

    leg_added = pyqtSignal(str)         # leg key
    leg_removed = pyqtSignal(str)       # leg key
    place_requested = pyqtSignal(list, float)  # (legs, per_leg_wager)
    # Emitted when the wallet snapshot worker finishes — lets callers
    # (OrderBookWidget) chain a re-render after a placement-triggered
    # refresh if they want to.
    wallet_refreshed = pyqtSignal(object)

    DEFAULT_PER_LEG_WAGER = 10.00

    _SCROLL_QSS = """
        QScrollArea {
            background-color: #0d0f14;
            border: 1px solid #2a2d34;
            border-radius: 3px;
        }
    """

    def __init__(self, parent=None, compact_mode: bool = False):
        super().__init__(parent)
        self.compact_mode = compact_mode
        self._legs: list[dict] = []
        self._expanded = False
        # Live wallet snapshot — merged from a one-shot balance fetch + a
        # refreshable positions fetch. Placement gating reads PX/NV
        # balances off this; None means "not loaded yet".
        self._snapshot = WalletSnapshot()
        self._balance_loaded = False
        self._balance_worker = None
        self._positions_worker = None
        self._positions_expanded = False
        self._initUI()
        self._refreshHeader()
        # Stagger initial fetches so the event loop / parent wiring is
        # settled first. Balance + positions fire in parallel.
        QTimer.singleShot(100, self.fetch_balance)
        QTimer.singleShot(120, self.refresh_positions)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _initUI(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        fs = 9 if self.compact_mode else 11

        # --- Header strip (always visible) ---
        self.header = QPushButton()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.clicked.connect(self._toggleExpanded)
        self.header.setStyleSheet(f"""
            QPushButton {{
                background-color: #14181f;
                color: #e8e9ed;
                border: none;
                border-top: 1px solid #2a2d34;
                border-bottom: 1px solid #2a2d34;
                padding: {4 if self.compact_mode else 7}px 10px;
                font-size: {fs}px;
                text-align: left;
            }}
            QPushButton:hover {{ background-color: #1a1f28; }}
        """)
        outer.addWidget(self.header)

        # --- Expanded body (hidden when collapsed) ---
        self.body = QWidget()
        self.body.setVisible(False)
        body_layout = QVBoxLayout(self.body)
        pad = 4 if self.compact_mode else 8
        body_layout.setContentsMargins(pad, pad, pad, pad)
        body_layout.setSpacing(pad)

        # Leg list — scrollable so 10+ legs don't blow up the height.
        self.legs_container = QWidget()
        self.legs_layout = QVBoxLayout(self.legs_container)
        self.legs_layout.setContentsMargins(0, 0, 0, 0)
        self.legs_layout.setSpacing(2)
        self.legs_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.legs_container)
        scroll.setMaximumHeight(120 if self.compact_mode else 180)
        scroll.setStyleSheet(self._SCROLL_QSS)
        body_layout.addWidget(scroll)

        # Controls row: stake input + place button.
        controls = QHBoxLayout()
        controls.setSpacing(6)
        stake_label = QLabel("Wager / leg:")
        stake_label.setStyleSheet(f"color: #8a92a3; font-size: {fs}px;")
        controls.addWidget(stake_label)

        self.wager_spin = QDoubleSpinBox()
        # No hard cap on per-leg wager — the only real ceiling is the
        # liquidity available at the targeted price, which is enforced
        # downstream in _placePXWager / _placeNVWager via
        # `actual_stake = min(wager, avail)`. Qt's QDoubleSpinBox needs a
        # finite max (it can't represent inf), so we set a value large
        # enough never to be hit by a realistic edit.
        self.wager_spin.setRange(0.01, 1e9)
        self.wager_spin.setSingleStep(0.10)
        self.wager_spin.setDecimals(2)
        self.wager_spin.setValue(self.DEFAULT_PER_LEG_WAGER)
        self.wager_spin.setPrefix("$")
        # Don't fire valueChanged on every keystroke — only on commit
        # (Enter / focus loss / spin click). Avoids partial values like
        # 22 firing the header refresh while the user is mid-typing 2.22.
        self.wager_spin.setKeyboardTracking(False)
        self.wager_spin.setFixedWidth(100)
        self.wager_spin.valueChanged.connect(self._refreshHeader)
        self.wager_spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background-color: #0d0f14;
                border: 1px solid #2a2d34;
                border-radius: 3px;
                color: #e8e9ed;
                padding: 2px 4px;
                font-size: {fs}px;
            }}
        """)
        controls.addWidget(self.wager_spin)
        controls.addStretch()

        self.place_btn = QPushButton("Place All")
        self.place_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.place_btn.clicked.connect(self._onPlaceClicked)
        self.place_btn.setStyleSheet(_action_button_qss(fs, self.compact_mode))
        controls.addWidget(self.place_btn)
        body_layout.addLayout(controls)

        # --- Wallet balance strip ---
        wallet_row = QHBoxLayout()
        wallet_row.setSpacing(6)
        self.wallet_label = QLabel("PX: …   •   NV: …")
        self.wallet_label.setTextFormat(Qt.TextFormat.RichText)
        self.wallet_label.setStyleSheet(
            f"color: #e8e9ed; font-size: {fs}px;")
        wallet_row.addWidget(self.wallet_label, 1)

        self.wallet_refresh_btn = QPushButton("↻")
        self.wallet_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wallet_refresh_btn.setToolTip("Refresh PX & NV balance / positions")
        self.wallet_refresh_btn.setFixedWidth(24)
        self.wallet_refresh_btn.clicked.connect(self.refresh_wallet)
        self.wallet_refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #1a1d24; color: #8a92a3;
                border: 1px solid #2a2d34; border-radius: 3px;
                padding: 1px 4px; font-size: {fs}px;
            }}
            QPushButton:hover {{ color: #e8e9ed; }}
        """)
        wallet_row.addWidget(self.wallet_refresh_btn)
        body_layout.addLayout(wallet_row)

        # --- Open-positions disclosure ---
        self.positions_toggle = QPushButton("▸  Open positions (0)")
        self.positions_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.positions_toggle.clicked.connect(self._togglePositions)
        self.positions_toggle.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent; color: #8a92a3;
                border: none; padding: 2px 0px;
                font-size: {fs - 1}px; text-align: left;
            }}
            QPushButton:hover {{ color: #e8e9ed; }}
        """)
        body_layout.addWidget(self.positions_toggle)

        self.positions_container = QWidget()
        self.positions_layout = QVBoxLayout(self.positions_container)
        self.positions_layout.setContentsMargins(0, 0, 0, 0)
        self.positions_layout.setSpacing(2)
        self._positions_scroll = QScrollArea()
        self._positions_scroll.setWidgetResizable(True)
        self._positions_scroll.setWidget(self.positions_container)
        self._positions_scroll.setMaximumHeight(
            120 if self.compact_mode else 180)
        self._positions_scroll.setStyleSheet(self._SCROLL_QSS)
        self._positions_scroll.setVisible(False)
        body_layout.addWidget(self._positions_scroll)

        # --- Status strip (placement warnings) ---
        self.status_label = QLabel()
        self.status_label.setStyleSheet(
            f"color: #8a92a3; font-size: {fs - 1}px;")
        self.status_label.setWordWrap(True)
        body_layout.addWidget(self.status_label)

        outer.addWidget(self.body)
        self._refreshStatus()

    # ------------------------------------------------------------------
    # Leg management — called by SGPScannerPanel on row click.
    # ------------------------------------------------------------------
    def has_leg(self, key: str) -> bool:
        return any(leg["key"] == key for leg in self._legs)

    def add_leg(self, leg: dict) -> None:
        if self.has_leg(leg["key"]):
            return
        self._legs.append(leg)
        self._rebuildLegRows()
        self._refreshHeader()
        self.leg_added.emit(leg["key"])

    def remove_leg(self, key: str) -> None:
        before = len(self._legs)
        self._legs = [leg for leg in self._legs if leg["key"] != key]
        if len(self._legs) != before:
            self._rebuildLegRows()
            self._refreshHeader()
            self.leg_removed.emit(key)

    def clear(self) -> None:
        removed_keys = [leg["key"] for leg in self._legs]
        self._legs = []
        self._rebuildLegRows()
        self._refreshHeader()
        for k in removed_keys:
            self.leg_removed.emit(k)

    def legs(self) -> list:
        return list(self._legs)

    def mark_leg_status(self, key: str, status: str,
                        success: bool = True) -> None:
        """Update the status label of a specific leg row in the slip."""
        for leg in self._legs:
            if leg["key"] == key:
                leg["status"] = status
                leg["status_ok"] = success
                self._rebuildLegRows()
                return

    def record_placement(self, dollars: float, src: str = "") -> None:
        """Optimistically decrement the cached balance for `src` after a
        leg places. Authoritative state arrives shortly via the next
        wallet refresh (which callers should trigger after _runPlacements
        completes); this just keeps the gating reactive in the gap."""
        if not self._balance_loaded:
            return
        if src == "PX" and self._snapshot.px_balance is not None:
            self._snapshot.px_balance = max(
                0.0, self._snapshot.px_balance - dollars)
        elif src == "NV" and self._snapshot.nv_balance is not None:
            self._snapshot.nv_balance = max(
                0.0, self._snapshot.nv_balance - dollars)
        self._refreshWalletLabel()
        self._refreshHeader()

    # ------------------------------------------------------------------
    # Wallet + positions
    # ------------------------------------------------------------------
    def fetch_balance(self) -> None:
        """One-shot balance fetch. Wired to fire once on construction.
        Manual refresh comes through refresh_wallet() which fires both
        workers (used by the ↻ button so the user can force-reload if
        they paste a new token mid-session)."""
        if (self._balance_worker is not None
                and self._balance_worker.isRunning()):
            return
        if hasattr(self, "wallet_label") and not self._balance_loaded:
            self.wallet_label.setText("PX: …   •   NV: …")
        self._balance_worker = WalletBalanceWorker(self)
        self._balance_worker.snapshot_ready.connect(self._onBalanceReady)
        self._balance_worker.start()

    def refresh_positions(self) -> None:
        """Re-fetch open positions on both exchanges. Called on init, on
        ↻ click, and after every placement so freshly-placed bets appear
        in the slip's open-positions list."""
        if (self._positions_worker is not None
                and self._positions_worker.isRunning()):
            return
        self._positions_worker = OpenPositionsWorker(self)
        self._positions_worker.snapshot_ready.connect(self._onPositionsReady)
        self._positions_worker.start()

    def refresh_wallet(self) -> None:
        """Force-refresh both balance and positions. Bound to the ↻
        button — typically used after the user repastes a fresh
        localStorage.auth or wants to manually re-sync. Drops the
        in-memory token cache too so a re-pasted token in
        prophetx_auth_state.json gets picked up."""
        try:
            from ProphetXQuery import reload_state_from_disk
            reload_state_from_disk()
        except Exception:
            pass
        self.fetch_balance()
        self.refresh_positions()

    def cleanup(self) -> None:
        """Stop any in-flight wallet workers. Called by the parent on
        app close so QThreads finish before their owning widget is
        destroyed (otherwise Qt emits 'Timers cannot be stopped from
        another thread' warnings on shutdown)."""
        for attr in ("_balance_worker", "_positions_worker"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                w.quit()
                w.wait(2000)

    def _onBalanceReady(self, snap) -> None:
        # Merge balance fields onto the cached snapshot — preserve
        # whatever positions already arrived from the other worker.
        self._snapshot.px_balance = snap.px_balance
        self._snapshot.px_withdrawable = snap.px_withdrawable
        self._snapshot.px_exposure = snap.px_exposure
        self._snapshot.nv_balance = snap.nv_balance
        self._snapshot.nv_coin_balance = snap.nv_coin_balance
        self._snapshot.nv_bonus_balance = snap.nv_bonus_balance
        # Overwrite (don't OR) so a successful refresh after a prior
        # failure clears the stale "PX err" / "NV err" label. The old
        # gated assignment kept the error sticky forever.
        self._snapshot.px_error = snap.px_error
        self._snapshot.nv_error = snap.nv_error
        self._balance_loaded = True
        self._refreshWalletLabel()
        self._refreshStatus()
        self._refreshHeader()
        self.wallet_refreshed.emit(self._snapshot)

    def _onPositionsReady(self, snap) -> None:
        # Merge positions onto the cached snapshot.
        self._snapshot.px_positions = snap.px_positions
        self._snapshot.nv_positions = snap.nv_positions
        # A successful positions fetch with no error proves the token is
        # working, so clear any stale balance-side error too — otherwise
        # the label stays red even after the next balance fetch succeeds.
        # If positions itself errored, surface that (but don't stomp an
        # existing balance error with a positions-side error of equal
        # priority).
        if snap.px_error and not self._snapshot.px_error:
            self._snapshot.px_error = snap.px_error
        elif not snap.px_error and snap.px_positions is not None:
            self._snapshot.px_error = None
        if snap.nv_error and not self._snapshot.nv_error:
            self._snapshot.nv_error = snap.nv_error
        elif not snap.nv_error and snap.nv_positions is not None:
            self._snapshot.nv_error = None
        self._refreshPositionsToggle()
        self._rebuildPositionsRows()
        self._refreshStatus()
        self.wallet_refreshed.emit(self._snapshot)

    @staticmethod
    def _fmt_money(x) -> str:
        if x is None:
            return "—"
        try:
            return f"${float(x):,.2f}"
        except (TypeError, ValueError):
            return "—"

    def _refreshWalletLabel(self) -> None:
        if not hasattr(self, "wallet_label"):
            return
        snap = self._snapshot
        if not self._balance_loaded:
            self.wallet_label.setText("PX: …   •   NV: …")
            return
        px_part = (f"<span style='color:#f87171;'>PX err</span>"
                   if snap.px_error
                   else f"<b>PX:</b> {self._fmt_money(snap.px_balance)}")
        nv_part = (f"<span style='color:#f87171;'>NV err</span>"
                   if snap.nv_error
                   else f"<b>NV:</b> {self._fmt_money(snap.nv_balance)}")
        self.wallet_label.setText(
            f"{px_part} &nbsp;•&nbsp; {nv_part}")
        tip_parts = []
        if not snap.px_error and snap.px_balance is not None:
            tip_parts.append(
                f"ProphetX — withdrawable {self._fmt_money(snap.px_withdrawable)}"
                f", exposure {self._fmt_money(snap.px_exposure)}")
        if snap.px_error:
            tip_parts.append(f"ProphetX error: {snap.px_error}")
        if not snap.nv_error and snap.nv_balance is not None:
            extras = []
            if snap.nv_coin_balance:
                extras.append(f"coin {snap.nv_coin_balance:.0f}")
            if snap.nv_bonus_balance:
                extras.append(f"bonus {snap.nv_bonus_balance:.2f}")
            tail = f" ({', '.join(extras)})" if extras else ""
            tip_parts.append(f"Novig — cash {self._fmt_money(snap.nv_balance)}{tail}")
        if snap.nv_error:
            tip_parts.append(f"Novig error: {snap.nv_error}")
        self.wallet_label.setToolTip("\n".join(tip_parts))

    def _refreshPositionsToggle(self) -> None:
        if not hasattr(self, "positions_toggle"):
            return
        n_px = len(self._snapshot.px_positions) if self._snapshot else 0
        n_nv = len(self._snapshot.nv_positions) if self._snapshot else 0
        total = n_px + n_nv
        chev = "▾" if self._positions_expanded else "▸"
        self.positions_toggle.setText(
            f"{chev}  Open positions ({total})  —  PX {n_px} • NV {n_nv}")

    def _togglePositions(self) -> None:
        self._positions_expanded = not self._positions_expanded
        if hasattr(self, "_positions_scroll"):
            self._positions_scroll.setVisible(self._positions_expanded)
        self._refreshPositionsToggle()

    def _rebuildPositionsRows(self) -> None:
        if not hasattr(self, "positions_layout"):
            return
        # Clear existing rows.
        while self.positions_layout.count():
            item = self.positions_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        fs = 8 if self.compact_mode else 10
        positions = list(self._snapshot.px_positions) + list(
            self._snapshot.nv_positions)
        if not positions:
            empty = QLabel("  No open positions.")
            empty.setStyleSheet(
                f"color: #555b66; font-size: {fs}px; padding: 4px;")
            self.positions_layout.addWidget(empty)
            return
        for pos in positions:
            self.positions_layout.addWidget(self._buildPositionRow(pos, fs))
        self.positions_layout.addStretch()

    def _buildPositionRow(self, pos, fs: int) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(6)

        tag = QLabel(pos.src)
        tag.setFixedWidth(22)
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if pos.src == "PX":
            tag.setStyleSheet(
                f"color: #5eead4; font-weight: 700; font-size: {fs}px;")
        else:  # NV — Novig blue text on black chip
            tag.setStyleSheet(
                f"color: #3b82f6; background-color: #000000; "
                f"font-weight: 700; font-size: {fs}px; "
                f"border-radius: 3px; padding: 1px 2px;")
        layout.addWidget(tag)

        # event / market / side block (truncate-tolerant)
        title_text = pos.event or "—"
        sub_text = f"{pos.market} • {pos.side}".strip(" •")
        text = QLabel(f"<b>{title_text}</b><br>"
                      f"<span style='color:#8a92a3;'>{sub_text}</span>")
        text.setStyleSheet(f"color: #e8e9ed; font-size: {fs}px;")
        text.setWordWrap(True)
        layout.addWidget(text, 1)

        odds_lbl = QLabel(pos.odds or "")
        odds_lbl.setStyleSheet(
            f"color: #c8d0dc; font-size: {fs}px; font-weight: 600;")
        odds_lbl.setFixedWidth(40)
        odds_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(odds_lbl)

        stake_lbl = QLabel(self._fmt_money(pos.stake))
        stake_lbl.setStyleSheet(
            f"color: #8a92a3; font-size: {fs}px;")
        stake_lbl.setFixedWidth(56)
        stake_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(stake_lbl)

        row.setStyleSheet(
            "QWidget { background-color: #14181f; border-radius: 2px; }")
        return row

    def _stakeTotalsBySource(self) -> dict:
        """Sum the current slip's projected stake (wager * leg_count) per
        source. Used by the placement gate so PX legs can't blow PX
        balance even if NV has plenty."""
        wager = self.wager_spin.value() if hasattr(self, "wager_spin") else 0
        out = {"PX": 0.0, "NV": 0.0}
        for leg in self._legs:
            src = leg.get("src")
            if src in out:
                out[src] += wager
        return out

    # ------------------------------------------------------------------
    # Visual rebuild — cheap, slip rarely exceeds ~10 entries.
    # ------------------------------------------------------------------
    def _rebuildLegRows(self) -> None:
        # Strip everything before the trailing stretch.
        while self.legs_layout.count() > 1:
            item = self.legs_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        fs = 9 if self.compact_mode else 11
        for leg in self._legs:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 2, 4, 2)
            rl.setSpacing(6)

            src = leg.get("src", "?")
            src_color = "#5eead4" if src == "PX" else "#3b82f6"
            src_lbl = QLabel(src)
            src_lbl.setStyleSheet(
                f"color: {src_color}; font-weight: 700; font-size: {fs}px;")
            src_lbl.setFixedWidth(22)
            rl.addWidget(src_lbl)

            desc = QLabel(leg.get("label", ""))
            desc.setStyleSheet(f"color: #e8e9ed; font-size: {fs}px;")
            rl.addWidget(desc, 1)

            odds = leg.get("odds")
            odds_text = (f"+{odds}" if isinstance(odds, (int, float))
                         and odds > 0 else str(odds) if odds is not None
                         else "--")
            odds_lbl = QLabel(odds_text)
            odds_lbl.setStyleSheet(
                f"color: #fbbf24; font-size: {fs}px; font-weight: 600;")
            rl.addWidget(odds_lbl)

            status_text = leg.get("status", "")
            if status_text:
                status_ok = leg.get("status_ok", True)
                status_color = "#34d399" if status_ok else "#f87171"
                stat = QLabel(status_text)
                stat.setStyleSheet(
                    f"color: {status_color}; font-size: {fs - 1}px;")
                rl.addWidget(stat)

            remove = QPushButton("✕")
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setFixedWidth(20)
            remove.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent; color: #8a92a3;
                    border: none; font-size: {fs}px;
                }}
                QPushButton:hover {{ color: #f87171; }}
            """)
            key = leg["key"]
            remove.clicked.connect(lambda _checked, k=key: self.remove_leg(k))
            rl.addWidget(remove)

            row.setStyleSheet(
                "QWidget { background-color: #14181f; border-radius: 2px; }")
            self.legs_layout.insertWidget(self.legs_layout.count() - 1, row)

    def _refreshHeader(self) -> None:
        n = len(self._legs)
        wager = self.wager_spin.value() if hasattr(self, "wager_spin") else 0
        total_stake = wager * n
        # Naive payout estimate — sum of (decimal_odds * wager) per leg.
        payout = 0.0
        for leg in self._legs:
            odds = leg.get("odds")
            try:
                a = int(odds)
                dec = 1 + a / 100 if a > 0 else 1 + 100 / abs(a)
                payout += dec * wager
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        chev = "▼" if self._expanded else "▲"
        self.header.setText(
            f"  {chev}  Bet Slip ({n})   "
            f"${total_stake:,.2f} stake → ${payout:,.2f} payout")
        if hasattr(self, "place_btn"):
            self.place_btn.setEnabled(n > 0 and total_stake > 0
                                      and self._canCoverStake())
        self._refreshStatus()

    def _canCoverStake(self) -> bool:
        """True iff every per-source stake fits the corresponding cached
        balance. When the snapshot hasn't loaded yet we allow placement
        — the worker takes ~1s and we don't want to lock the user out on
        startup; the server will still reject if truly underfunded."""
        if not self._balance_loaded:
            return True
        totals = self._stakeTotalsBySource()
        if totals["PX"] > 0:
            bal = self._snapshot.px_balance
            if bal is not None and totals["PX"] > bal + 1e-6:
                return False
        if totals["NV"] > 0:
            bal = self._snapshot.nv_balance
            if bal is not None and totals["NV"] > bal + 1e-6:
                return False
        return True

    def _refreshStatus(self) -> None:
        if not hasattr(self, "status_label"):
            return
        if not self._balance_loaded:
            self.status_label.setText("Loading balances…")
            return
        warnings = []
        totals = self._stakeTotalsBySource()
        snap = self._snapshot
        if (totals["PX"] > 0 and snap.px_balance is not None
                and totals["PX"] > snap.px_balance + 1e-6):
            warnings.append(
                f"⚠ PX stake ${totals['PX']:.2f} exceeds "
                f"balance ${snap.px_balance:.2f}")
        if (totals["NV"] > 0 and snap.nv_balance is not None
                and totals["NV"] > snap.nv_balance + 1e-6):
            warnings.append(
                f"⚠ NV stake ${totals['NV']:.2f} exceeds "
                f"balance ${snap.nv_balance:.2f}")
        if warnings:
            self.status_label.setText(" • ".join(warnings))
        else:
            self.status_label.setText("")

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def _toggleExpanded(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._refreshHeader()

    def expand(self) -> None:
        if not self._expanded:
            self._toggleExpanded()

    def _onPlaceClicked(self) -> None:
        if not self._legs:
            return
        wager = self.wager_spin.value()
        # Final balance check — _refreshHeader already disables the
        # button when this fails, but recheck in case the snapshot
        # changed in between.
        if not self._canCoverStake():
            return
        self.place_btn.setEnabled(False)
        self.place_requested.emit(list(self._legs), float(wager))


class SGPScannerPanel(QWidget):
    """SGP +EV scanner — shown in place of the order book table when the
    header's EV-scan toggle is active.

    Per-event scope: the user ticks MLB events, hits Run, and both exchange
    scanners run per event as asyncio tasks on the host qasync loop —
    ProphetX (`ProphetXQuery.SGPScanner`) and Novig
    (`novig_async.scan_sgp_implications_async`), concurrently within each
    event. Only +EV rows surface: parlays whose SGP price beats the
    standalone implication-floor leg.

    The panel owns no threads — every scan is a coroutine. ProphetXBrowser
    repopulates it via set_events() each time the toggle opens it.
    """

    # Novig prop-type token -> short label for the parlay-legs column.
    _TYPE_ABBR = {
        "HOME_RUNS": "HR", "RBIS": "RBI", "RUNS": "Run", "HITS": "Hit",
    }

    # Emitted when a result row is clicked. Payload is a leg dict ready
    # for the bet slip (src, key, label, odds, edge_pct, raw, bet_kind,
    # status). OrderBookWidget hooks this to manage the shared slip.
    leg_picked = pyqtSignal(dict)

    # Fires when the user clicks Run — slip owner uses this to drop any
    # legs that came from previous scan results (since savedParlayId
    # quote IDs expire and the table row indexes are about to reset).
    scan_started = pyqtSignal()

    def __init__(self, parent=None, compact_mode=False):
        super().__init__(parent)
        self.compact_mode = compact_mode
        self._novig_map: Dict[str, object] = {}
        self._scanning = False
        self._cascading = False   # re-entrancy guard for tree check cascade
        self.initUI()

    def initUI(self):
        outer = QVBoxLayout(self)
        pad = 4 if self.compact_mode else 8
        outer.setContentsMargins(pad, pad, pad, pad)
        outer.setSpacing(pad)
        fs = 9 if self.compact_mode else 12

        # --- Event selection tree (league -> events, checkable) ---
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMaximumHeight(140 if self.compact_mode else 220)
        self.tree.itemChanged.connect(self._onItemChanged)
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: #0d0f14;
                border: 1px solid #2a2d34;
                border-radius: 4px;
                color: #e8e9ed;
                font-size: {fs}px;
                outline: none;
            }}
            QTreeWidget::item {{ padding: 3px; }}
            QTreeWidget::item:hover {{ background-color: #1a1d24; }}
        """)
        outer.addWidget(self.tree)

        # --- Controls row: Run button + status text ---
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.run_btn = QPushButton("Run EV Scan")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.clicked.connect(self._onRunClicked)
        self.run_btn.setStyleSheet(_action_button_qss(fs, self.compact_mode))
        controls.addWidget(self.run_btn)
        controls.addStretch()
        self.status_label = QLabel("Select events to scan")
        self.status_label.setStyleSheet(f"color: #8a92a3; font-size: {fs}px;")
        controls.addWidget(self.status_label)
        outer.addLayout(controls)

        # --- Progress bar (per-event) ---
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6 if self.compact_mode else 10)
        self.progress.setStyleSheet("""
            QProgressBar {
                background-color: #1a1d24; border: none; border-radius: 3px;
            }
            QProgressBar::chunk {
                background-color: #5eead4; border-radius: 3px;
            }
        """)
        outer.addWidget(self.progress)

        # --- Results table ---
        self.results = QTableWidget()
        self.results.setColumnCount(6)
        self.results.setHorizontalHeaderLabels(
            ["SRC", "PLAYER", "PARLAY LEGS", "SINGLE", "SGP", "EDGE"])
        self.results.verticalHeader().setVisible(False)
        self.results.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.results.setShowGrid(False)
        self.results.setAlternatingRowColors(False)
        hdr = self.results.horizontalHeader()
        for col, mode in (
            (0, QHeaderView.ResizeMode.ResizeToContents),
            (1, QHeaderView.ResizeMode.ResizeToContents),
            (2, QHeaderView.ResizeMode.Stretch),
            (3, QHeaderView.ResizeMode.ResizeToContents),
            (4, QHeaderView.ResizeMode.ResizeToContents),
            (5, QHeaderView.ResizeMode.ResizeToContents),
        ):
            hdr.setSectionResizeMode(col, mode)
        self.results.verticalHeader().setDefaultSectionSize(
            22 if self.compact_mode else 30)
        tfs = 9 if self.compact_mode else 12
        self.results.setStyleSheet(f"""
            QTableWidget {{
                background-color: #0d0f14; border: 1px solid #2a2d34;
                border-radius: 4px; color: #e8e9ed;
                gridline-color: transparent; font-size: {tfs}px;
            }}
            QTableWidget::item {{ padding: 2px 6px; }}
            QHeaderView::section {{
                background-color: #1a1d24; color: #9ca3af;
                padding: 4px; border: none; font-size: {max(tfs - 1, 8)}px;
                font-weight: 600; letter-spacing: 0.5px;
            }}
        """)
        outer.addWidget(self.results, 1)

        # Row registry — keyed by stable row key. Used by OrderBookWidget
        # (the slip owner one level up) to look up payloads at place time
        # and to repaint row highlights when the slip state changes.
        self._row_payloads: Dict[str, tuple] = {}

        # Canonical, edge-sorted result store. Each entry is
        # (key, src, row, edge). The table is rebuilt from this list on
        # every add so newly-scanned rows slot into the right rank as the
        # scan progresses. Slipped keys are tracked here so the mint tint
        # survives a rebuild (the slip owner one level up drives them via
        # set_row_slipped()).
        self._results_rows: List[tuple] = []
        self._slipped_keys: set = set()

        # Wire row clicks → emit leg-ready dict for the slip owner
        self.results.cellClicked.connect(self._onResultRowClicked)

    # ------------------------------------------------------------------
    # Population — called by ProphetXBrowser when the toggle opens.
    # ------------------------------------------------------------------
    def set_events(self, mlb_events: list, novig_map: dict) -> None:
        """Rebuild the event tree.

        `mlb_events` is a list of (event_id, event_dump_dict).
        `novig_map` is ProphetXBrowser._novig_event_map
        ({str(px_event_id): EventPair}), used to resolve each event's Novig
        UUID so the Novig scan can run for it.
        """
        self._novig_map = novig_map or {}
        self._cascading = True
        self.tree.clear()

        league_item = QTreeWidgetItem(self.tree, ["MLB"])
        league_item.setFlags(league_item.flags()
                             | Qt.ItemFlag.ItemIsUserCheckable)
        league_item.setCheckState(0, Qt.CheckState.Unchecked)

        def _start(pair):
            return ((pair[1].get("event_metadata") or {}).get("startTime")
                    or "")

        for eid, ev in sorted(mlb_events, key=_start):
            meta = ev.get("event_metadata") or {}
            name = meta.get("name") or str(eid)
            child = QTreeWidgetItem(league_item, [name])
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, Qt.CheckState.Unchecked)
            pair = self._novig_map.get(str(eid))
            nv_uuid = pair.event_b.source_event_id if pair else None
            child.setData(0, Qt.ItemDataRole.UserRole, {
                "px_id": eid, "name": name, "nv_uuid": nv_uuid,
            })
            if not nv_uuid:
                child.setForeground(0, QColor(138, 146, 163))
                child.setToolTip(0, "No Novig match — ProphetX scan only")

        league_item.setExpanded(True)
        self._cascading = False
        n = league_item.childCount()
        self.status_label.setText(
            f"{n} MLB event(s) available" if n else "No MLB events loaded")

    # ------------------------------------------------------------------
    # Tree check cascade
    # ------------------------------------------------------------------
    def _onItemChanged(self, item: QTreeWidgetItem, col: int) -> None:
        if self._cascading:
            return
        self._cascading = True
        try:
            if item.parent() is None:
                # League row toggled -> apply to every event under it.
                st = item.checkState(0)
                if st != Qt.CheckState.PartiallyChecked:
                    for i in range(item.childCount()):
                        item.child(i).setCheckState(0, st)
            else:
                # Event toggled -> reflect the aggregate on the league row.
                league = item.parent()
                states = [league.child(i).checkState(0)
                          for i in range(league.childCount())]
                if states and all(s == Qt.CheckState.Checked
                                  for s in states):
                    league.setCheckState(0, Qt.CheckState.Checked)
                elif all(s == Qt.CheckState.Unchecked for s in states):
                    league.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    league.setCheckState(0, Qt.CheckState.PartiallyChecked)
        finally:
            self._cascading = False

    def _selectedEvents(self) -> list:
        out = []
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            league = root.child(i)
            for j in range(league.childCount()):
                child = league.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    data = child.data(0, Qt.ItemDataRole.UserRole)
                    if data:
                        out.append(data)
        return out

    # ------------------------------------------------------------------
    # Scan orchestration (async, on the host qasync loop — no threads)
    # ------------------------------------------------------------------
    def _onRunClicked(self) -> None:
        if self._scanning:
            return
        try:
            asyncio.ensure_future(self._runScan())
        except RuntimeError as e:
            self.status_label.setText(f"Cannot start scan: {e}")

    async def _runScan(self) -> None:
        selected = self._selectedEvents()
        if not selected:
            self.status_label.setText("No events selected")
            return
        self._scanning = True
        self.run_btn.setEnabled(False)
        self.tree.setEnabled(False)
        self.results.setRowCount(0)
        # Drop the row registry — a fresh scan invalidates both the row
        # indexes and the underlying quote (savedParlayId TTL expires
        # server-side too). OrderBookWidget will clear its slip in
        # response to the scan_started signal.
        self._row_payloads.clear()
        self._results_rows.clear()
        self._slipped_keys.clear()
        self.scan_started.emit()
        self.progress.setVisible(True)
        # Scale by 1000 so the bar advances smoothly *within* an event —
        # driven by the per-quote Novig progress callback — rather than
        # jumping once per finished event.
        self._progress_scale = 1000
        self.progress.setMaximum(len(selected) * self._progress_scale)
        self.progress.setValue(0)
        found = 0
        try:
            for i, ev in enumerate(selected):
                self.status_label.setText(
                    f"Scanning {ev['name']}  ({i + 1}/{len(selected)})")
                rows = await self._scanEvent(ev, i + 1, len(selected))
                for src, row in rows:
                    self._addRow(src, row)
                found += len(rows)
                self.progress.setValue((i + 1) * self._progress_scale)
            self.status_label.setText(
                f"Done — {found} +EV play(s) across "
                f"{len(selected)} event(s)")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.status_label.setText(f"Scan error: {e}")
        finally:
            self._scanning = False
            self.run_btn.setEnabled(True)
            self.tree.setEnabled(True)

    async def _scanEvent(self, ev: dict, idx: int, total: int) -> list:
        """Run the per-event scanner(s). Returns a list of (source, row)
        tuples. A failure on one exchange is logged and does not abort
        the others.

        NV (Novig) disabled 2026-06-27: Novig killed parlay pricing — the
        /parlay/request pricer now 400s "Cannot price parlay" for every
        2-leg combo (verified live on priced pregame legs), so the SGP
        implication scan can never surface a row. PX still works, so only
        PX is dispatched. To revive if Novig restores parlays, restore the
        gather over _scanNV below. See memory: project_novig_parlay_pricer_400.
        """
        px_rows = await asyncio.gather(
            self._scanPX(ev),
            return_exceptions=True,
        )
        px_rows = px_rows[0]
        out = []
        if isinstance(px_rows, Exception):
            print(f"[sgp-panel] PX scan failed for {ev['name']}: "
                  f"{px_rows!r}")
        else:
            out += [("PX", r) for r in px_rows]
        # --- NV scan disabled (see docstring) ---
        # nv_rows = await self._scanNV(ev, idx, total)
        # if isinstance(nv_rows, Exception):
        #     print(f"[sgp-panel] NV scan failed for {ev['name']}: "
        #           f"{nv_rows!r}")
        # else:
        #     out += [("NV", r) for r in nv_rows]
        return out

    async def _scanPX(self, ev: dict) -> list:
        from ProphetXQuery import SGPScanner
        try:
            scanner = SGPScanner(concurrency=4)
        except FileNotFoundError:
            raise RuntimeError("ProphetX session file missing")
        rows = await scanner.scan(event_ids=[ev["px_id"]])
        return [r for r in rows if (r.get("edge_decimal") or 0) > 0]

    async def _scanNV(self, ev: dict, idx: int, total: int) -> list:
        nv_uuid = ev.get("nv_uuid")
        if not nv_uuid:
            return []   # no Novig match for this event — PX-only
        import aiohttp
        from novig_async import (scan_sgp_implications_async,
                                 REQUEST_TIMEOUT)

        def _prog(done: int, tot: int) -> None:
            if tot:
                self.status_label.setText(
                    f"Scanning {ev['name']}  ({idx}/{total}) "
                    f"— NV {done}/{tot}")
                # Advance the bar fractionally through this event so it
                # tracks quote combinations checked, not whole events.
                # idx is 1-based, so (idx-1) events are already complete.
                scale = getattr(self, "_progress_scale", 1000)
                self.progress.setValue(
                    int((idx - 1 + done / tot) * scale))

        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            rows = await scan_sgp_implications_async(
                session, nv_uuid, concurrency=4, progress_cb=_prog)
        return [r for r in rows if r.get("mispriced")]

    # ------------------------------------------------------------------
    # Result rows
    # ------------------------------------------------------------------
    @staticmethod
    def _rowKey(src: str, row: dict) -> str:
        """Stable identifier for a result row across click toggles."""
        if src == "PX":
            return (f"PX|{row.get('event_id')}|{row.get('player')}|"
                    f"{row.get('chain')}")
        return (f"NV|{row.get('player')}|{row.get('dominant_type')}|"
                f"{row.get('dominant_side')}|{row.get('implied_type')}|"
                f"{row.get('implied_side')}|"
                f"{row.get('dominant_outcome_id')}|"
                f"{row.get('implied_outcome_id')}")

    @staticmethod
    def _rowEdge(src: str, row: dict) -> float:
        """Numeric edge % used for sorting and the EDGE column."""
        if src == "PX":
            return float(row.get("edge_pct") or 0.0)
        return SGPScannerPanel._nvEdgePct(row)

    def _addRow(self, src: str, row: dict) -> None:
        # Append to the canonical store, then rebuild the table sorted by
        # edge so this row lands in rank. De-dupe on key in case the same
        # play is emitted twice (keeps the latest payload).
        key = self._rowKey(src, row)
        edge = self._rowEdge(src, row)
        self._results_rows = [e for e in self._results_rows if e[0] != key]
        self._results_rows.append((key, src, row, edge))
        self._rebuildResults()

    def _rebuildResults(self) -> None:
        """Repopulate the results table from `_results_rows`, sorted by
        edge descending. Rebuilds `_row_payloads` (the click/slip index)
        and re-applies the slip tint for any rows still in the slip."""
        # Stable sort: equal-edge rows keep insertion order.
        self._results_rows.sort(key=lambda e: e[3], reverse=True)
        self.results.setRowCount(0)
        self._row_payloads.clear()
        for r, (key, src, row, edge) in enumerate(self._results_rows):
            self.results.insertRow(r)
            self._row_payloads[key] = (src, row, r)
            self._renderRow(r, src, row, edge)
            if key in self._slipped_keys:
                item = self.results.item(r, 0)
                if item is not None:
                    item.setBackground(QColor(94, 234, 212, 60))

    def _renderRow(self, r: int, src: str, row: dict, edge: float) -> None:
        """Fill the six cells of table row `r` for one result."""
        if src == "PX":
            player = row.get("player", "")
            legs = row.get("chain", "")
            single = self._fmtOdds(row.get("hr_odds"))
            sgp = self._fmtOdds(row.get("sgp_odds"))
            src_color = QColor(94, 234, 212)     # ProphetX mint
        else:
            player = row.get("player", "")
            legs = self._nvLegs(row)
            single = row.get("american_dominant", "") or "--"
            sgp = row.get("american_combined", "") or "--"
            src_color = QColor(59, 130, 246)     # Novig blue

        values = [src, player, legs, single, sgp, f"{edge:+.1f}%"]
        for c, val in enumerate(values):
            item = QTableWidgetItem(str(val))
            if c in (3, 4, 5):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            else:
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                      | Qt.AlignmentFlag.AlignVCenter)
            if c == 0:
                item.setForeground(src_color)
            elif c == 5:
                item.setForeground(QColor(251, 191, 36) if edge > 0
                                   else QColor(156, 163, 175))
            self.results.setItem(r, c, item)

    def _nvLegs(self, row: dict) -> str:
        dom_t = self._TYPE_ABBR.get(row.get("dominant_type", ""),
                                    row.get("dominant_type", ""))
        imp_t = self._TYPE_ABBR.get(row.get("implied_type", ""),
                                    row.get("implied_type", ""))
        return (f"{dom_t} {row.get('dominant_side', '')} 0.5"
                f" + {imp_t} {row.get('implied_side', '')} 0.5")

    @staticmethod
    def _nvEdgePct(row: dict) -> float:
        """Edge % for a Novig row, mirroring the PX scanner's edge_pct:
        the parlay wins exactly when the dominant leg wins (dominant =>
        implied), so edge = how much more decimal the SGP pays. With
        implied probabilities dp/cp, decimal ratio = dp/cp."""
        dp = row.get("dominant_price")
        cp = row.get("combined_price")
        try:
            return (float(dp) / float(cp) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _fmtOdds(american) -> str:
        if american is None:
            return "--"
        try:
            a = int(american)
        except (TypeError, ValueError):
            return str(american)
        return f"+{a}" if a > 0 else str(a)

    # ------------------------------------------------------------------
    # Slip wiring: row click emits a ready-to-slip leg dict for the
    # OrderBookWidget owner to consume. Highlight maintenance is driven
    # by the owner via set_row_slipped().
    # ------------------------------------------------------------------
    def _slipLabelForRow(self, src: str, row: dict) -> tuple:
        if src == "PX":
            return (f"{row.get('player', '')} — {row.get('chain', '')}",
                    row.get("sgp_odds"))
        return (f"{row.get('player', '')} — {self._nvLegs(row)}",
                row.get("american_combined"))

    def _onResultRowClicked(self, table_row: int, _col: int) -> None:
        target = None
        for key, (src, row_dict, idx) in self._row_payloads.items():
            if idx == table_row:
                target = (key, src, row_dict)
                break
        if target is None:
            return
        key, src, row_dict = target
        label, odds = self._slipLabelForRow(src, row_dict)
        try:
            odds_int = int(odds) if odds is not None else None
        except (TypeError, ValueError):
            odds_int = None
        edge_pct = (float(row_dict.get("edge_pct") or 0.0)
                    if src == "PX"
                    else self._nvEdgePct(row_dict))
        leg = {
            "src": src,
            "key": key,
            "label": label,
            "odds": odds_int,
            "edge_pct": edge_pct,
            "raw": row_dict,
            "bet_kind": "sgp",
            "status": "",
        }
        self.leg_picked.emit(leg)

    def set_row_slipped(self, key: str, slipped: bool) -> None:
        """Called by the slip owner when a leg sourced from this panel
        enters or leaves the slip. Repaints the SRC-cell tint."""
        # Track the slip state so a re-sort rebuild can restore the tint.
        if slipped:
            self._slipped_keys.add(key)
        else:
            self._slipped_keys.discard(key)
        payload = self._row_payloads.get(key)
        if payload is None:
            return
        _src, _row, table_row = payload
        item = self.results.item(table_row, 0)
        if item is None:
            return
        if slipped:
            item.setBackground(QColor(94, 234, 212, 60))   # mint wash
        else:
            item.setBackground(QColor(0, 0, 0, 0))


class OddsBarDelegate(QStyledItemDelegate):
    """Paints the odds-with-liquidity-bar cell via QPainter, replacing
    the previous per-row QWidget + QHBoxLayout + QLabel + setStyleSheet
    pattern.

    The old pattern's dominant cost was the stylesheet engine
    re-parsing a multi-stop qlineargradient string for every row on
    every refresh — ~300-800µs per row, scaling to 15-40ms total for
    a ~50-row orderbook. A delegate's paint() does the same visual via
    plain QPainter calls in ~10-20µs per row, taking the table-render
    portion of a refresh from "blocks the qasync loop visibly" down to
    "below frame budget."

    Reads three custom data roles from the cell's QTableWidgetItem:
        ODDS_ROLE       — display string ("+164", "-190")
        BAR_WIDTH_ROLE  — 0–100, % of cell width the gradient fills
        SIDE_TYPE_ROLE  — "ask" or "bid", drives the color palette
    """

    ODDS_ROLE = Qt.ItemDataRole.UserRole + 1
    BAR_WIDTH_ROLE = Qt.ItemDataRole.UserRole + 2
    SIDE_TYPE_ROLE = Qt.ItemDataRole.UserRole + 3
    # Signed flash intensity for the per-tick price-change pulse:
    # >0 = odds ticked up (green wash), <0 = ticked down (red wash),
    # magnitude 0..1 is the current (decaying) alpha. Set/decayed by
    # OrderBookWidget._stepRowAnimations; absent/0 = no flash.
    FLASH_ROLE = Qt.ItemDataRole.UserRole + 4

    def __init__(self, parent=None, compact_mode: bool = False):
        super().__init__(parent)
        self.compact_mode = compact_mode
        # Build the font once; reused for every paint.
        font_size = 10 if compact_mode else 16
        self._font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        # Metrics for the base font (built once) drive the fast "does it
        # fit?" check; shrunk fonts are cached by point size so 4-digit
        # odds (e.g. -9900) auto-fit a narrow ODDS column instead of
        # clipping their last digit, without re-allocating per paint.
        self._base_pt = font_size
        self._fm = QFontMetrics(self._font)
        self._shrunk_fonts: dict = {}
        self._shrunk_fm: dict = {}
        # Color palette pre-built so paint() does no QColor allocation
        # in the hot path.
        self._ask_text = QColor("#f87171")
        self._bid_text = QColor("#34d399")
        self._ask_bar = QColor(248, 113, 113)
        self._bid_bar = QColor(52, 211, 153)
        self._transparent = QColor(13, 15, 20, 0)
        # Price-change flash hues (see FLASH_ROLE). Up = mint-green,
        # down = red; alpha is scaled per-paint by the decaying intensity.
        self._flash_up = QColor(74, 222, 128)
        self._flash_down = QColor(248, 113, 113)

    def _fontThatFits(self, text: str, avail_w: int):
        """Return the base font, or the largest shrunk variant that fits
        `text` within `avail_w` px. Fast path (the common 3-char odds)
        does a single cached-metrics check and returns the base font."""
        if avail_w <= 0 or self._fm.horizontalAdvance(text) <= avail_w:
            return self._font
        floor = max(7, self._base_pt - 6)
        for size in range(self._base_pt - 1, floor - 1, -1):
            fm = self._shrunk_fm.get(size)
            if fm is None:
                f = QFont(self._font)
                f.setPointSize(size)
                fm = QFontMetrics(f)
                self._shrunk_fonts[size] = f
                self._shrunk_fm[size] = fm
            if fm.horizontalAdvance(text) <= avail_w:
                return self._shrunk_fonts[size]
        return self._shrunk_fonts.get(floor, self._font)

    def paint(self, painter: QPainter, option, index) -> None:
        odds = index.data(self.ODDS_ROLE)
        if odds is None:
            # Not one of our cells (e.g. separator/empty state) — fall
            # through to default item rendering so text + background
            # set on the QTableWidgetItem still paint.
            super().paint(painter, option, index)
            return

        bar_width = index.data(self.BAR_WIDTH_ROLE) or 0
        side_type = index.data(self.SIDE_TYPE_ROLE) or 'bid'

        if side_type == 'ask':
            text_color = self._ask_text
            bar_color = self._ask_bar
        else:
            text_color = self._bid_text
            bar_color = self._bid_bar

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Honor any background brush set on the item (used by the
        # dual-source render path to tint rows by their source). We
        # fully override paint() so Qt's default item background draw
        # is skipped — restore it explicitly so PX/NV color cues survive.
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        if bg is not None:
            painter.fillRect(option.rect, QBrush(bg))

        # Mirror the stylesheet's padding: original had 8/4 margins
        # full-mode, 2/1 compact. Translate to inset on option.rect.
        inset_x, inset_y = (2, 1) if self.compact_mode else (4, 2)
        rect = option.rect.adjusted(inset_x, inset_y, -inset_x, -inset_y)

        if bar_width > 0:
            # Recreate the qlineargradient stops from the old stylesheet
            # exactly. Visual parity with the previous look.
            opacity = min(int(bar_width * 0.35), 35)
            edge_opacity = max(int(opacity * 0.6), 8)
            frac = bar_width / 100.0

            opaque = QColor(bar_color)
            opaque.setAlpha(opacity)
            edge = QColor(bar_color)
            edge.setAlpha(edge_opacity)

            grad = QLinearGradient(float(rect.left()), 0.0,
                                   float(rect.right()), 0.0)
            grad.setColorAt(0.0, opaque)
            mid_stop = max(0.0, min(1.0, frac * 0.7))
            end_stop = max(0.0, min(1.0, frac))
            grad.setColorAt(mid_stop, opaque)
            grad.setColorAt(end_stop, edge)
            # Hard cut to transparent — small epsilon so the gradient
            # engine doesn't smear past the bar end.
            cut = max(0.0, min(1.0, frac + 0.0001))
            grad.setColorAt(cut, self._transparent)
            grad.setColorAt(1.0, self._transparent)

            border = QColor(bar_color)
            border.setAlpha(min(opacity + 10, 45))
            painter.setBrush(QBrush(grad))
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(QRectF(rect), 4.0, 4.0)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        # Price-change flash: a brief directional wash over the cell that
        # decays to nothing. Painted over the bar but under the text so the
        # odds stay legible. Signed magnitude comes from FLASH_ROLE.
        flash = index.data(self.FLASH_ROLE) or 0.0
        if flash:
            wash = QColor(self._flash_up if flash > 0 else self._flash_down)
            wash.setAlpha(int(min(abs(flash), 1.0) * 90))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(wash))
            painter.drawRoundedRect(QRectF(rect), 4.0, 4.0)

        # Odds text, left-aligned with the original horizontal padding.
        # Auto-shrink the font when a long value (4-digit odds like -9900)
        # wouldn't otherwise fit, so the last digit never gets clipped.
        text_pad = 2 if self.compact_mode else 8
        text_rect = rect.adjusted(text_pad, 0, -text_pad, 0)
        odds_str = str(odds)
        painter.setPen(text_color)
        painter.setFont(self._fontThatFits(odds_str, text_rect.width()))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            odds_str,
        )
        painter.restore()


class _SectionBuildSignals(QObject):
    """Carries the cross-thread signal from the section-build worker back to
    OrderBookWidget._onSectionsReady on the main thread."""
    ready = pyqtSignal(int, object)   # (generation, result_dict)


class _SectionBuildRunnable(QRunnable):
    """Runs the pure-Python section-building off the main thread."""
    def __init__(self, gen: int, build_fn, signals: "_SectionBuildSignals"):
        super().__init__()
        self._gen = gen
        self._build_fn = build_fn
        self._signals = signals
        self.setAutoDelete(True)

    def run(self):
        try:
            result = self._build_fn()
            self._signals.ready.emit(self._gen, result)
        except Exception:
            pass  # superseded render just doesn't paint


class OrderBookWidget(QWidget):
    """
    Professional order book display widget for ProphetX markets.
    Shows bid/ask ladder with liquidity depth similar to Polymarket.
    """

    # Emitted when the header's EV-scan toggle flips (True = scanner shown).
    scannerToggled = pyqtSignal(bool)

    def __init__(self, parent=None, compact_mode=False):
        super().__init__(parent)
        self.current_market = None
        self.current_line = None
        self.compact_mode = compact_mode
        self.is_loading = False
        # Refresh animation state (see _emitOrderbookRows). _last_plan_sig
        # captures the structural shape of the last render; when a refresh
        # produces the same shape we update cells in place (no flash) and
        # animate the bars/odds instead of tearing the table down.
        self._last_plan_sig = None
        self._row_anim: dict = {}   # table_row -> animation state dict
        self.initUI()
        self._setupLoadingOverlay()
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)  # ~60fps while anything animates
        self._anim_timer.timeout.connect(self._stepRowAnimations)

    def _setupLoadingOverlay(self):
        """Setup the loading overlay widget"""
        self.loading_overlay = OrderBookLoadingOverlay(self)
        self.loading_overlay.hide()
        self.loading_overlay.setGeometry(self.rect())

    def resizeEvent(self, event):
        """Ensure loading overlay stays the right size"""
        super().resizeEvent(event)
        if hasattr(self, 'loading_overlay'):
            self.loading_overlay.setGeometry(self.rect())

    def showLoading(self, status_text: str = "Fetching live orderbook..."):
        """Show the loading overlay with animation"""
        # Don't drop the order book loading overlay over the scanner panel.
        if (getattr(self, "sgp_panel", None) is not None
                and self.sgp_panel.isVisible()):
            return
        self.is_loading = True
        self.loading_overlay.setGeometry(self.rect())
        self.loading_overlay.start(status_text)

    def hideLoading(self):
        """Hide the loading overlay"""
        self.is_loading = False
        self.loading_overlay.stop()

    @staticmethod
    def _scannerToggleStyle(compact: bool) -> str:
        """Stylesheet for the header EV-scan toggle button."""
        fs = 9 if compact else 12
        pad = "2px 6px" if compact else "5px 12px"
        return f"""
            QPushButton {{
                background-color: #1a1d24; color: #8a92a3;
                border: 1px solid #2a2d34; border-radius: 4px;
                padding: {pad}; font-size: {fs}px; font-weight: 600;
            }}
            QPushButton:hover {{
                border: 1px solid #5eead4; color: #c8d0dc;
            }}
            QPushButton:checked {{
                background-color: #1f6f5e; color: #e8fff8;
                border: 1px solid #5eead4;
            }}
        """

    def _on_scanner_toggle(self, checked: bool):
        """Header EV-scan toggle handler: swap the order book table region
        for the SGP scanner panel. The header bar itself stays put — only
        the content below it changes."""
        if hasattr(self, "orderbook_table"):
            self.orderbook_table.setVisible(not checked)
        if hasattr(self, "footer_label"):
            self.footer_label.setVisible(not checked)
        if hasattr(self, "sgp_panel"):
            self.sgp_panel.setVisible(checked)
        self.scannerToggled.emit(checked)

    def initUI(self):
        layout = QVBoxLayout(self)
        margins = 2 if self.compact_mode else 0
        layout.setContentsMargins(margins, margins, margins, margins)
        layout.setSpacing(0)

        # Header showing market info (compact in compact mode)
        if not self.compact_mode:
            header = self.createHeader()
            layout.addWidget(header)
        else:
            header = self.createCompactHeader()
            layout.addWidget(header)

        # Order book table (all lines displayed)
        self.orderbook_table = QTableWidget()

        # Paint the odds-bar column via a QStyledItemDelegate instead of
        # per-row QWidgets. The previous setCellWidget approach forced
        # the Qt stylesheet engine to re-parse a multi-stop gradient for
        # every row on every refresh, costing 15-40ms total and showing
        # up as a tickertape stutter. The delegate paints with QPainter
        # directly: ~10-20µs per row, the table-render portion of a
        # refresh drops below the frame budget.
        self._odds_delegate = OddsBarDelegate(self.orderbook_table,
                                              compact_mode=self.compact_mode)
        self.orderbook_table.setItemDelegateForColumn(1, self._odds_delegate)

        # In compact mode, hide some columns
        if self.compact_mode:
            self.orderbook_table.setColumnCount(3)
            self.orderbook_table.setHorizontalHeaderLabels([
                "SIDE", "ODDS", "LIQ"
            ])
        else:
            self.orderbook_table.setColumnCount(5)
            self.orderbook_table.setHorizontalHeaderLabels([
                "SELECTION", "ODDS", "LIQUIDITY", "CUMULATIVE", "% OF BOOK"
            ])

        # Styling
        self.orderbook_table.setAlternatingRowColors(False)
        self.orderbook_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.orderbook_table.setShowGrid(False)
        self.orderbook_table.verticalHeader().setVisible(False)
        self.orderbook_table.horizontalHeader().setStretchLastSection(True)

        # Set column widths. SIDE (col 0) is sized manually: full rebuilds in
        # _emitOrderbookRows run with the model's signals blocked, so a
        # ResizeToContents header never re-measures after the new content
        # lands — the column stayed at its stale width (truncated "AR…"
        # sides) until something else (e.g. the alt-lines toggle) forced a
        # re-layout. _fitSideColumn measures the real texts every render.
        header = self.orderbook_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.orderbook_table.setColumnWidth(0, 90)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        if not self.compact_mode:
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        # Font - smaller in compact mode with better font family
        font_size = 10 if self.compact_mode else 14
        book_font = QFont("SF Mono", font_size)
        book_font.setStyleHint(QFont.StyleHint.Monospace)
        book_font.setWeight(QFont.Weight.Medium)
        book_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
        self.orderbook_table.setFont(book_font)

        # Row height - needs to fit font + padding in compact mode
        row_height = 28 if self.compact_mode else 50
        self.orderbook_table.verticalHeader().setDefaultSectionSize(row_height)

        # Minimize spacing in compact mode
        if self.compact_mode:
            self.orderbook_table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
            self.orderbook_table.verticalHeader().setMinimumSectionSize(26)

        layout.addWidget(self.orderbook_table)

        # Footer with spread info — denser padding/type in compact mode.
        self.footer_label = QLabel()
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fpad, ffs = ("2px", "8px") if self.compact_mode else ("12px", "14px")
        self.footer_label.setStyleSheet(f"""
            QLabel {{
                background-color: #1a1d24;
                color: #8a92a3;
                padding: {fpad};
                font-size: {ffs};
                border-top: 1px solid #2a2d34;
            }}
        """)
        layout.addWidget(self.footer_label)

        # Styling - compact padding in compact mode
        padding = "4px 4px" if self.compact_mode else "12px"
        header_padding = "4px" if self.compact_mode else "12px"
        header_font_size = "9px" if self.compact_mode else "13px"

        self.setStyleSheet(f"""
            QTableWidget {{
                background-color: #0d0f14;
                border: none;
                color: #e8e9ed;
                gridline-color: transparent;
            }}
            QTableWidget::item {{
                padding: {padding};
                border: none;
                margin: 0px;
                background-color: rgba(26, 29, 36, 0.3);
            }}
            QHeaderView::section {{
                background-color: #1a1d24;
                color: #9ca3af;
                padding: {header_padding};
                border: none;
                font-weight: 600;
                font-size: {header_font_size};
                letter-spacing: 0.5px;
                text-transform: uppercase;
            }}
        """)

        # SGP +EV scanner panel — a sibling of the order book table,
        # created hidden. The header's EV-scan toggle swaps which one is
        # visible; the persistent header bar above is untouched in either
        # mode. Hidden widgets are skipped by the layout, so when the
        # scanner is off the panel takes no space and the book renders
        # exactly as before.
        self.sgp_panel = SGPScannerPanel(compact_mode=self.compact_mode)
        self.sgp_panel.setVisible(False)
        layout.addWidget(self.sgp_panel, 1)

        # --- Shared bet slip (pinned to bottom) ---
        # Slip lives at the OrderBookWidget level so it persists across the
        # orderbook ↔ EV-scan toggle and accepts legs from both surfaces.
        self.bet_slip = BetSlipDrawer(compact_mode=self.compact_mode)
        self.bet_slip.leg_removed.connect(self._onSlipLegRemoved)
        self.bet_slip.place_requested.connect(self._onSlipPlaceRequested)
        layout.addWidget(self.bet_slip)

        # Master leg registry across all sources. key -> leg dict. Keeps
        # row-highlight bookkeeping aware of which source originated a leg
        # so removals route to the right repaint method.
        self._slip_legs: Dict[str, dict] = {}

        # SGP scanner pushes ready-to-slip legs via leg_picked. Scan starts
        # drop scan-sourced legs (their quote IDs expire server-side).
        self.sgp_panel.leg_picked.connect(self._onLegPicked)
        self.sgp_panel.scan_started.connect(self._onScanStarted)

        # Orderbook taps: clicking a row stages a "take this resting offer"
        # leg. Registry mirrors the SGP panel's row-payload dict.
        self._orderbook_row_payloads: Dict[str, dict] = {}
        self.orderbook_table.cellClicked.connect(self._onOrderbookRowClicked)

        # Multi-strike (spread/total) ALT LINES section: collapsed by
        # default, toggled by clicking its header row. _alt_header_row is
        # stamped during render so the click handler can recognise it;
        # _last_render stashes the inputs so a toggle can re-render.
        self._alts_collapsed: bool = True
        self._alt_header_row: Optional[int] = None
        self._last_render = None  # ("dual", px, nv) | ("single", nm)
        self._render_gen = 0
        self._render_signals = _SectionBuildSignals(self)
        self._render_signals.ready.connect(self._onSectionsReady)

    def createHeader(self):
        """Create header widget showing current market name"""
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background-color: #1a1d24;
                border-bottom: 1px solid #2a2d34;
            }
            QLabel {
                color: #ffffff;
                padding: 10px;
            }
        """)

        layout = QHBoxLayout(header)
        self.market_title = QLabel("Select a market to view order book")
        self.market_title.setFont(QFont("Arial", 15, QFont.Weight.Bold))
        layout.addWidget(self.market_title)

        self.stake_label = QLabel()
        self.stake_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.stake_label.setStyleSheet("color: #8a92a3; font-size: 13px;")
        layout.addWidget(self.stake_label)

        # EV-scan toggle — sits at the far right, with the liquidity label
        # now between it and the market title / source legend.
        self.scanner_toggle = QPushButton("EV Scan")
        self.scanner_toggle.setCheckable(True)
        self.scanner_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scanner_toggle.setStyleSheet(self._scannerToggleStyle(False))
        self.scanner_toggle.toggled.connect(self._on_scanner_toggle)
        layout.addWidget(self.scanner_toggle)

        return header

    def createCompactHeader(self):
        """Create compact header for terminal integration"""
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background-color: #1a1d24;
                border-bottom: 1px solid #2a2d34;
            }
            QLabel {
                color: #ffffff;
                padding: 1px;
            }
        """)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(8)

        self.market_title = QLabel("Select market")
        self.market_title.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.market_title.setWordWrap(True)
        layout.addWidget(self.market_title)

        layout.addStretch()

        self.stake_label = QLabel()
        self.stake_label.setStyleSheet("color: #8a92a3; font-size: 9px; font-weight: bold;")
        self.stake_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.stake_label)

        # EV-scan toggle — far right; liquidity label now sits to its left.
        self.scanner_toggle = QPushButton("EV Scan")
        self.scanner_toggle.setCheckable(True)
        self.scanner_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scanner_toggle.setStyleSheet(self._scannerToggleStyle(True))
        self.scanner_toggle.toggled.connect(self._on_scanner_toggle)
        layout.addWidget(self.scanner_toggle)

        return header

    # ------------------------------------------------------------------
    # Source-agnostic render path. Consumes a NormalizedMarket from
    # exchange_market_keys.
    # ------------------------------------------------------------------

    # Brand-derived accent colors. Used by setNormalizedMarket to give
    # each book pane a visual identity matching its source's UI:
    #   ProphetX  -> mint green (their primary accent)
    #   Novig     -> blue       (their CASH "blue chip" convention)
    # COIN is API-testing only; not exposed in user-facing widget paths.
    SOURCE_ACCENT_COLOR = {
        "prophetx":    "#5eead4",
        "novig:CASH":  "#3b82f6",
    }
    SOURCE_BADGE_TEXT = {
        "prophetx":    "PROPHETX",
        "novig:CASH":  "NOVIG",
    }

    def setNormalizedMarket(self, nm) -> None:
        """Render the order book from a NormalizedMarket.

        Multi-strike layout: for Over/Under markets, Overs are rendered
        with strikes descending, Unders with strikes ascending, separated
        by a labeled divider. For team markets each line is rendered as
        its own block.

        Source identity is communicated via:
          - A 2px colored top border on the market title (accent bar)
          - A brand-colored badge prefixing the market name in rich text
        Neither touches table spacing or row heights.
        """
        self.current_market = nm
        self._last_render = ("single", nm)
        accent = self.SOURCE_ACCENT_COLOR.get(nm.source, "#8a92a3")
        badge = self.SOURCE_BADGE_TEXT.get(nm.source, nm.source.upper())

        if hasattr(self, "market_title"):
            # Rich-text title: colored brand badge + neutral market name
            title_html = (
                f'<span style="color:{accent}; font-weight:700; '
                f'letter-spacing:1px;">{badge}</span>'
                f'&nbsp;&nbsp;<span style="color:#e8e9ed;">{nm.market_name}</span>'
            )
            self.market_title.setText(title_html)
            self.market_title.setTextFormat(Qt.TextFormat.RichText)
            # Accent bar via top border on the title label. Padding
            # values match the existing header stylesheets (10px full,
            # 1px compact) so no layout shift.
            pad = "1px" if self.compact_mode else "10px"
            self.market_title.setStyleSheet(
                f"QLabel {{ color: #ffffff; padding: {pad}; "
                f"border-top: 2px solid {accent}; }}"
            )

        if hasattr(self, "stake_label"):
            self.stake_label.setText(
                f"Total Liquidity: ${nm.total_liquidity_usd:,.2f}")

        self.renderNormalizedOrderBook(nm)

    def _normalized_order_to_dict(self, side_label: str, abbreviated: str,
                                  order, source_line_id: str = "",
                                  source: str = "") -> Dict:
        """Adapt one NormalizedOrder into the dict shape renderOrderRow
        consumes. Also stamps placement metadata (`_place_*` keys) so the
        row-click handler can route a click into the bet slip without
        re-walking the NormalizedMarket tree."""
        american = order.american
        display_odds = f"+{american}" if american > 0 else str(american)
        raw = order.raw if hasattr(order, "raw") else None
        place_info: Dict = {
            "source": source,                  # "prophetx" / "novig:CASH"
            "american": int(american),
            "prob": float(order.prob or 0.0),
            "size_usd": float(order.size_usd or 0.0),
            "line_id": source_line_id,
        }
        if isinstance(raw, dict):
            # PX raw is the original PX order dict (carries lineID, odds,
            # value, displayName). NV raw is the ladder entry stamped
            # with _market_id / _outcome_id / _is_bid by the adapter.
            place_info["px_line_id"] = raw.get("lineID") or source_line_id
            place_info["nv_market_id"] = raw.get("_market_id")
            place_info["nv_outcome_id"] = raw.get("_outcome_id")
            place_info["nv_is_bid"] = raw.get("_is_bid")
        return {
            "displayName": side_label,
            "abbreviatedName": abbreviated,
            "displayOdds": display_odds,
            "value": float(order.size_usd or 0.0),
            "odds": int(american),
            "_place": place_info,
        }

    @staticmethod
    def _format_strike_short(strike: Optional[float]) -> str:
        if strike is None:
            return ""
        if strike == int(strike):
            return str(int(strike))
        return f"{strike}"

    # ------------------------------------------------------------------
    # Multi-strike (spread / total) grouping
    # ------------------------------------------------------------------
    # Spread + total markets bundle MANY strikes (8-10 alt totals / run
    # lines) into ONE market, each strike carrying two sides and a full
    # ladder. The default per-side flattening dumps 130-170 rows and
    # scatters the two halves of each line into opposite ends of the book.
    # These helpers regroup by LINE: the most-liquid strike becomes the
    # featured "main line" shown with real depth (best prices meeting in
    # the middle, like the moneyline); every other strike collapses to its
    # best price per side under a single ALT LINES section.
    MAIN_LINE_DEPTH = 4   # offers shown per side on the featured main line
    # Floor for the liquidity bar: any order that has real size behind it
    # gets at least this much width so a small-but-real order ($19) still
    # reads as a bar instead of a sliver hugging the odds text.
    MIN_BAR_WIDTH = 14

    def _gridFromLines(self, sources, is_over_under):
        """Build the per-strike grid used by the multi-strike renderer.

        Returns (per_strike, strike_liq) where:
          per_strike = {strike: [(side_label, [order_dict, ...best-first]), ...]}
          strike_liq = {strike: total_displayed_value}

        sources: list of (src_key, NormalizedMarket). src_key is "px"/"nv"
        for the dual view (stamped onto each row dict so the dual loop can
        tint it) or None for a single source. Sides are merged across
        sources by over/under type or canonical team symbol, so PX + NV
        depth for the same (line, side) lands together."""
        import exchange_market_keys as _emk
        _strip = re.compile(r"\s*[+-]?\d+(?:\.\d+)?\s*$")
        grid: dict = {}   # {strike: {side_key: {"label":.., "rows":[..]}}}
        for src_key, nm in sources:
            if nm is None:
                continue
            for ln in nm.lines:
                sd = grid.setdefault(ln.strike, {})
                for side_idx, s in enumerate(ln.sides):
                    if is_over_under:
                        if s.side_type == _emk.SIDE_OVER:
                            skey = "over"
                        elif s.side_type == _emk.SIDE_UNDER:
                            skey = "under"
                        else:
                            continue
                        ss = self._format_strike_short(ln.strike)
                        label = f"{skey} {ss}".strip() if ss else skey
                    else:
                        # Per-side strike from the label ("NYY -1.5" -> -1.5):
                        # a spread line's two sides carry opposite-signed
                        # strikes, so we can't reuse ln.strike for both.
                        side_strike = _emk._parse_strike(s.label or "")
                        if side_strike is None:
                            side_strike = ln.strike
                        bare = _strip.sub("", s.label or "").strip()
                        sym = _emk.team_to_symbol(bare)
                        if sym is not None:
                            skey = sym
                            team_sym = sym
                        elif bare:
                            # Individual sport: normalize to surname so
                            # "K. Pliskova" and "Karolina Pliskova" bucket together
                            surname = _emk._person_surname(bare)
                            skey = surname or bare
                            team_sym = bare
                        else:
                            # Pure numeric label (e.g. NV sends "+4.5"/"-4.5"):
                            # inject player name from event name using side order
                            _ev_parts: list = []
                            _ev = nm.event_name or ""
                            for _sep in (" at ", " vs ", " v "):
                                if _sep.lower() in _ev.lower():
                                    _i = _ev.lower().index(_sep.lower())
                                    _ev_parts = [_ev[:_i].strip(),
                                                 _ev[_i + len(_sep):].strip()]
                                    break
                            if _ev_parts:
                                _p = _ev_parts[min(side_idx, len(_ev_parts) - 1)]
                                _sn = _emk._person_surname(_p)
                                skey = _sn.lower() if _sn else f"side{side_idx}"
                                team_sym = _sn or _p
                            else:
                                skey = f"side{side_idx}"
                                team_sym = ""
                        ss = (self._format_strike_short(side_strike)
                              if side_strike is not None else "")
                        if (ss and side_strike is not None and side_strike > 0
                                and not ss.startswith("+")):
                            ss = f"+{ss}"
                        label = f"{team_sym} {ss}".strip() if ss else team_sym or skey
                    entry = sd.setdefault(skey, {"label": label, "rows": []})
                    for o in s.orders:
                        od = self._normalized_order_to_dict(
                            label, label, o,
                            source_line_id=ln.source_line_id,
                            source=nm.source)
                        # Skip dead quotes (Novig indicative top-of-book
                        # carries $0 size) so the main-line / alt selection
                        # ranks only takeable orders.
                        if od.get("value", 0) <= 0:
                            continue
                        if src_key is not None:
                            od["_source"] = src_key
                        entry["rows"].append(od)
        per_strike: dict = {}
        strike_liq: dict = {}
        for strike, sd in grid.items():
            if is_over_under:
                keyorder = [k for k in ("over", "under") if k in sd]
            else:
                keyorder = list(sd.keys())   # first-seen (favorite-ish) first
            sides = []
            liq = 0.0
            for k in keyorder:
                rows = sd[k]["rows"]
                # Best-paying offer first (max american) — same convention
                # the renderers use to find each side's touch.
                rows.sort(key=lambda od: od["odds"], reverse=True)
                liq += sum(o["value"] for o in rows)
                sides.append((sd[k]["label"], rows))
            per_strike[strike] = sides
            strike_liq[strike] = liq
        return per_strike, strike_liq

    def _composeMultiStrikeSections(self, per_strike, strike_liq,
                                    is_over_under, alts_collapsed=True):
        """Turn the per-strike grid into render sections: one MAIN LINE
        section (top MAIN_LINE_DEPTH offers per side PER SOURCE so both
        exchanges show, best prices meeting in the middle) followed by an
        ALT LINES section (every other strike collapsed to its best price
        per side). When alts_collapsed the alt section is header-only (its
        rows are hidden until the user clicks the header). Returns
        (sections, main_strike)."""
        strikes = [s for s in per_strike if s is not None]
        if not strikes:
            return [], None

        def _sources_at(s):
            return {od.get("_source")
                    for _lbl, rows in per_strike[s] for od in rows}

        # Main-line selection, in priority order:
        #   1. strikes BOTH exchanges quote — so the featured book is a true
        #      side-by-side PX vs NV comparison, not a single-source line;
        #   2. CLOSEST-TO-EVEN odds — what "the line" means (the book centres
        #      the primary spread/total near pick'em while alts get lopsided),
        #      measured by the tightest side's |american| (rows are best-first
        #      so rows[0] is each side's best price);
        #   3. total displayed liquidity, as a final tiebreak.
        def _tightness(s):
            vals = [abs(rows[0]["odds"]) for _lbl, rows in per_strike[s]
                    if rows]
            return min(vals) if vals else float("inf")
        def _both(s):
            srcs = _sources_at(s)
            return 0 if ("px" in srcs and "nv" in srcs) else 1
        main = min(strikes, key=lambda s: (_both(s), _tightness(s),
                                           -strike_liq.get(s, 0.0)))
        sections: list = []

        # --- main line: top-N PER SOURCE so a tighter exchange can't crowd
        # the other out of the featured book ---
        main_rows: list = []
        for idx, (_label, rows) in enumerate(per_strike[main]):
            by_src: dict = {}
            for od in rows:                       # rows are best-first
                by_src.setdefault(od.get("_source"), []).append(od)
            picked: list = []
            for _src, lst in by_src.items():
                picked.extend(lst[:self.MAIN_LINE_DEPTH])
            picked.sort(key=lambda od: od["odds"], reverse=True)   # best-first
            # First side's touch drops to the BOTTOM (nearest the spread);
            # the second side's touch stays at the TOP — the two best prices
            # meet in the middle, same idea as the moneyline.
            if idx == 0:
                picked = list(reversed(picked))
            main_rows.extend(picked)
        ss = self._format_strike_short(main if is_over_under else abs(main))
        main_label = f"MAIN LINE — {ss}" if ss else "MAIN LINE"
        sections.append((main_label, main_rows))

        # --- alt lines: best price per side, every other strike (desc) ---
        alt_strikes = sorted((s for s in strikes if s != main), key=lambda v: -v)
        alt_rows: list = []
        for s in alt_strikes:
            for _label, rows in per_strike[s]:
                if rows:
                    alt_rows.append(rows[0])   # best price per side
        if alt_strikes:
            if alts_collapsed:
                # Header-only; rows hidden. The chevron + count signal it's
                # tappable; _onOrderbookRowClicked toggles on the header row.
                hdr = f"▸ ALT LINES ({len(alt_strikes)})  •  tap to expand"
                sections.append((hdr, []))
            else:
                hdr = "▾ ALT LINES  •  tap to collapse"
                sections.append((hdr, alt_rows))
        return sections, main

    def renderNormalizedOrderBook(self, nm) -> None:
        """Submit a background section build; result delivered to _onSectionsReady."""
        self._render_gen += 1
        gen = self._render_gen
        alts_collapsed = self._alts_collapsed
        def _build():
            return self._buildSingleSections(nm, alts_collapsed)
        QThreadPool.globalInstance().start(
            _SectionBuildRunnable(gen, _build, self._render_signals))

    def _buildSingleSections(self, nm, alts_collapsed: bool) -> dict:
        import exchange_market_keys as _emk

        # Collect rows grouped by display section. For Over/Under markets,
        # produce two sections (OVER, UNDER); for team markets, produce
        # one section per (line, side) block.
        side_types_present: set = set()
        for ln in nm.lines:
            for s in ln.sides:
                side_types_present.add(s.side_type)
        is_over_under = (_emk.SIDE_OVER in side_types_present
                         or _emk.SIDE_UNDER in side_types_present)

        sections: list[tuple[str, list[Dict]]] = []
        distinct_strikes = {ln.strike for ln in nm.lines if ln.strike is not None}
        lead_header = False
        if len(distinct_strikes) >= 2:
            # Multi-strike spread/total (single source): feature the
            # most-liquid line with depth, collapse the rest to best price.
            per_strike, strike_liq = self._gridFromLines(
                [(None, nm)], is_over_under)
            sections, _main = self._composeMultiStrikeSections(
                per_strike, strike_liq, is_over_under, alts_collapsed)
            lead_header = True
        elif is_over_under:
            # Build the Over half: lines in strike-DESC order (already
            # sorted by the normalizer that way).
            over_rows: list[Dict] = []
            under_rows: list[Dict] = []
            for ln in nm.lines:
                strike_str = self._format_strike_short(ln.strike)
                for s in ln.sides:
                    side_word = ("over" if s.side_type == _emk.SIDE_OVER
                                 else "under" if s.side_type == _emk.SIDE_UNDER
                                 else s.side_type)
                    full_label = (f"{side_word} {strike_str}".strip()
                                  if strike_str else side_word)
                    short_label = full_label
                    bucket = (over_rows if s.side_type == _emk.SIDE_OVER
                              else under_rows if s.side_type == _emk.SIDE_UNDER
                              else None)
                    if bucket is None:
                        continue
                    for o in s.orders:
                        bucket.append(self._normalized_order_to_dict(
                            full_label, short_label, o,
                            source_line_id=ln.source_line_id,
                            source=nm.source))
            # Under half: existing widget shows Under with strikes ASCENDING
            # so the closest-to-center strike sits next to the separator.
            # Reverse the line order by reversing the rows-per-line groups.
            # Simplest: rebuild under_rows in strike-ascending order.
            under_rows = []
            for ln in list(reversed(nm.lines)):
                strike_str = self._format_strike_short(ln.strike)
                for s in ln.sides:
                    if s.side_type != _emk.SIDE_UNDER:
                        continue
                    side_word = "under"
                    full_label = (f"{side_word} {strike_str}".strip()
                                  if strike_str else side_word)
                    for o in s.orders:
                        under_rows.append(self._normalized_order_to_dict(
                            full_label, full_label, o,
                            source_line_id=ln.source_line_id,
                            source=nm.source))
            if over_rows:
                sections.append(("OVER", over_rows))
            if under_rows:
                sections.append(("UNDER", under_rows))
        else:
            # Team / moneyline / spread: render each (line, side) as a
            # block, lines in strike-desc order. Side label keeps the
            # strike suffix as provided by the normalizer.
            for ln in nm.lines:
                for s in ln.sides:
                    label = s.label or "?"
                    short = label[:10] + ".." if len(label) > 12 else label
                    rows = [self._normalized_order_to_dict(
                                label, short, o,
                                source_line_id=ln.source_line_id,
                                source=nm.source)
                            for o in s.orders]
                    # Sort by |american| (see dual path): the favorite's
                    # touch drops to the bottom of its block near the
                    # spread instead of sitting atop the book.
                    rows.sort(key=lambda od: abs(od["odds"]), reverse=True)
                    if rows:
                        sections.append((label, rows))

        sections = self._finalizeSections(sections, is_over_under, lead_header)
        return {"sections": sections, "lead_header": lead_header,
                "dual": False, "market_name": nm.market_name}

    # ------------------------------------------------------------------
    # Dual-source render path
    # ------------------------------------------------------------------
    # Per-source row tints. Two layers:
    #   - Side column text gets the full brand color (primary signal).
    #   - Row background gets a subtle tint (secondary signal).
    # Both are applied in _renderDualOrderBook after renderOrderRow runs.
    _PX_TEXT_COLOR = QColor(94, 234, 212)      # ProphetX mint
    _NV_TEXT_COLOR = QColor(59, 130, 246)      # Novig blue
    _PX_ROW_TINT = QColor(94, 234, 212, 55)    # mint, ~22% alpha
    _NV_ROW_TINT = QColor(59, 130, 246, 55)    # blue, ~22% alpha

    def setMarketDual(self, px_norm, nv_norm) -> None:
        """Render an order book from both ProphetX and Novig
        NormalizedMarkets for the same logical market. Orders are
        interleaved by strike + American odds; each row is tinted in
        the source's brand color (mint = ProphetX, blue = Novig) to
        visually distinguish provenance without consuming columns.

        Falls back to setNormalizedMarket(px_norm) when nv_norm is
        None / empty, so the widget keeps working when no Novig match
        exists for the current market.
        """
        if nv_norm is None or not nv_norm.lines:
            self.setNormalizedMarket(px_norm)
            return

        self._last_render = ("dual", px_norm, nv_norm)
        self.current_market = px_norm  # primary source for header
        # Header: small inline legend (PX/NV chips colored to match the
        # row colors in the table) followed by the market name. No
        # gradient backgrounds, no two-line wrapping.
        if hasattr(self, "market_title"):
            px_color = self.SOURCE_ACCENT_COLOR.get("prophetx", "#5eead4")
            nv_color = self.SOURCE_ACCENT_COLOR.get("novig:CASH", "#3b82f6")
            # Bullet glyphs in brand color give a tiny color-swatch
            # legend that matches the row text colors below.
            legend_html = (
                f'<span style="color:{px_color}; font-weight:700;">●&nbsp;PX</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:{nv_color}; font-weight:700;">●&nbsp;NV</span>'
            )
            title_html = (
                f'{legend_html}'
                f'&nbsp;&nbsp;<span style="color:#e8e9ed;">{px_norm.market_name}</span>'
            )
            self.market_title.setText(title_html)
            self.market_title.setTextFormat(Qt.TextFormat.RichText)
            # Reset any prior stylesheet so the label inherits the
            # plain header background from the QFrame stylesheet.
            self.market_title.setStyleSheet("")

        total_liq = (px_norm.total_liquidity_usd
                     + nv_norm.total_liquidity_usd)
        if hasattr(self, "stake_label"):
            self.stake_label.setText(
                f"Total Liquidity: ${total_liq:,.2f}")

        self._renderDualOrderBook(px_norm, nv_norm)

    def _renderDualOrderBook(self, px_norm, nv_norm) -> None:
        """Submit a background section build; result delivered to _onSectionsReady."""
        self._render_gen += 1
        gen = self._render_gen
        alts_collapsed = self._alts_collapsed
        def _build():
            return self._buildDualSections(px_norm, nv_norm, alts_collapsed)
        QThreadPool.globalInstance().start(
            _SectionBuildRunnable(gen, _build, self._render_signals))

    def _buildDualSections(self, px_norm, nv_norm, alts_collapsed: bool) -> dict:
        import exchange_market_keys as _emk

        # Combine lines by strike. Either source may have lines the
        # other lacks; we keep all of them. Strike None (moneyline)
        # treated as its own bucket.
        lines_by_strike: dict = {}
        line_order: list = []  # preserve descending-strike order
        def _add(line, source):
            key = line.strike
            if key not in lines_by_strike:
                lines_by_strike[key] = {"px": None, "nv": None}
                line_order.append(key)
            lines_by_strike[key][source] = line
        # PX first establishes order (already strike-desc from normalizer)
        for ln in px_norm.lines:
            _add(ln, "px")
        for ln in nv_norm.lines:
            _add(ln, "nv")
        # Re-sort line_order by strike desc, None last
        line_order.sort(key=lambda s: (s is None, -(s if s is not None else 0)))

        # Detect Over/Under layout
        side_types_seen: set = set()
        for ln_pair in lines_by_strike.values():
            for ln in (ln_pair["px"], ln_pair["nv"]):
                if ln is None:
                    continue
                for s in ln.sides:
                    side_types_seen.add(s.side_type)
        is_over_under = (_emk.SIDE_OVER in side_types_seen
                         or _emk.SIDE_UNDER in side_types_seen)

        def _orders_for_side(strike, side_type):
            """Return merged source-tagged orders for one (strike,
            side_type), sorted American-desc. Each tuple carries the
            source key, the side label, the NormalizedOrder, AND the
            parent Line's source_line_id (needed by the bet slip)."""
            out = []
            for src_key in ("px", "nv"):
                ln = lines_by_strike[strike][src_key]
                if ln is None:
                    continue
                for s in ln.sides:
                    if s.side_type != side_type:
                        continue
                    for o in s.orders:
                        out.append((src_key, s.label, o,
                                    ln.source_line_id))
            out.sort(key=lambda t: t[2].american, reverse=True)
            return out

        # Build sections
        sections: list[tuple[str, list]] = []  # (label, [(src, order_dict)])
        distinct_strikes = [s for s in line_order if s is not None]
        lead_header = False
        if len(distinct_strikes) >= 2:
            # Multi-strike spread/total: feature the most-liquid line with
            # real depth, collapse every other strike to its best price per
            # side. lead_header makes the render loop label the first
            # (main-line) section too.
            per_strike, strike_liq = self._gridFromLines(
                [("px", px_norm), ("nv", nv_norm)], is_over_under)
            sections, _main = self._composeMultiStrikeSections(
                per_strike, strike_liq, is_over_under, alts_collapsed)
            lead_header = True
        elif is_over_under:
            # Over half: lines in descending strike order
            over_rows = []
            for strike in line_order:
                strike_str = self._format_strike_short(strike)
                tagged = _orders_for_side(strike, _emk.SIDE_OVER)
                for src_key, _side_label, o, line_id in tagged:
                    label = (f"over {strike_str}".strip()
                             if strike_str else "over")
                    nm_source = (px_norm.source if src_key == "px"
                                 else nv_norm.source)
                    od = self._normalized_order_to_dict(
                        label, label, o,
                        source_line_id=line_id, source=nm_source)
                    od["_source"] = src_key
                    over_rows.append(od)
            # Under half: ascending strike order
            under_rows = []
            for strike in list(reversed(line_order)):
                strike_str = self._format_strike_short(strike)
                tagged = _orders_for_side(strike, _emk.SIDE_UNDER)
                for src_key, _side_label, o, line_id in tagged:
                    label = (f"under {strike_str}".strip()
                             if strike_str else "under")
                    nm_source = (px_norm.source if src_key == "px"
                                 else nv_norm.source)
                    od = self._normalized_order_to_dict(
                        label, label, o,
                        source_line_id=line_id, source=nm_source)
                    od["_source"] = src_key
                    under_rows.append(od)
            if over_rows:
                sections.append(("OVER", over_rows))
            if under_rows:
                sections.append(("UNDER", under_rows))
        else:
            # Team markets (moneyline, spread, etc.). PX labels the side
            # with the full team name ("San Francisco Giants") while NV
            # uses the symbol ("SF"). Canonicalize both through
            # team_to_symbol so PX + NV orders for the SAME team land in
            # the same section. Within a team, group by strike (desc),
            # sort American-desc within each strike — mirrors the
            # existing single-source ladder behavior.
            team_strike_groups: dict = {}   # {team_key: {strike: [(src, order)]}}
            team_display_names: dict = {}   # team_key → section header label
            team_order: list = []
            # Strip trailing signed decimal from a label so "OKC -1.5"
            # becomes "OKC" for team_to_symbol lookup. This is distinct
            # from strip_price_from_label (which targets American-odds
            # suffixes only) — here we want spread strikes off too.
            _strip_trailing_num = re.compile(r"\s*[+-]?\d+(?:\.\d+)?\s*$")
            for strike in line_order:
                for src_key in ("px", "nv"):
                    ln = lines_by_strike[strike][src_key]
                    if ln is None:
                        continue
                    for s in ln.sides:
                        # Per-side strike: parse from the side label
                        # ("OKC -1.5" -> -1.5). On a spread the two sides
                        # of one line have OPPOSITE-signed strikes, so
                        # we must NOT reuse ln.strike for both — that's
                        # the line's POV, not the side's. Fall back to
                        # ln.strike only when the label carries no
                        # number (moneyline, "Over"/"Under").
                        side_strike = _emk._parse_strike(s.label or "")
                        if side_strike is None:
                            side_strike = ln.strike
                        bare = _strip_trailing_num.sub("", s.label or "").strip()
                        sym = _emk.team_to_symbol(bare)
                        if sym is not None:
                            team_key = sym
                        else:
                            # Individual sport (tennis/UFC/golf): normalize
                            # to surname so "K. Pliskova", "Karolina
                            # Pliskova", "Pliskova" all bucket together
                            # across PX and NV labels.
                            surname = _emk._person_surname(bare)
                            team_key = surname or bare or s.side_type or "?"
                        if team_key not in team_strike_groups:
                            team_strike_groups[team_key] = {}
                            team_order.append(team_key)
                            # First label seen wins as display (PX is
                            # processed before NV, so full PX name takes
                            # precedence over NV abbreviation).
                            team_display_names[team_key] = sym or bare or team_key
                        team_strike_groups[team_key].setdefault(side_strike, [])
                        for o in s.orders:
                            team_strike_groups[team_key][side_strike].append(
                                (src_key, o, ln.source_line_id))

            for team_key in team_order:
                display = team_display_names[team_key]
                strikes_in_team = sorted(
                    team_strike_groups[team_key].keys(),
                    key=lambda s: (s is None, -(s if s is not None else 0)),
                )
                rows = []
                for strike in strikes_in_team:
                    tagged = team_strike_groups[team_key][strike]
                    # Sort by ABSOLUTE american so each side's touch (best
                    # price) sits nearest the separator. A favorite
                    # (negative) ascends — its least-juiced top-of-book
                    # (-144) lands at the BOTTOM of the block, right above
                    # the spread, instead of hiding at the very top of the
                    # book; an underdog (positive) keeps its longest price
                    # up top. Both sides' true market prices then meet at
                    # the spread in the middle.
                    tagged.sort(key=lambda t: abs(t[1].american), reverse=True)
                    strike_str = (self._format_strike_short(strike)
                                  if strike is not None else "")
                    # Prepend explicit "+" for positive spreads so the
                    # display matches sportsbook convention (+1.5, +12.5).
                    # Negative strikes already carry their sign; zero and
                    # blank pass through untouched.
                    if (strike_str and strike is not None and strike > 0
                            and not strike_str.startswith("+")):
                        strike_str = f"+{strike_str}"
                    row_label = (f"{display} {strike_str}".strip()
                                 if strike_str else display)
                    for src_key, o, line_id in tagged:
                        nm_source = (px_norm.source if src_key == "px"
                                     else nv_norm.source)
                        od = self._normalized_order_to_dict(
                            row_label, row_label, o,
                            source_line_id=line_id, source=nm_source)
                        od["_source"] = src_key
                        rows.append(od)
                if rows:
                    sections.append((display, rows))

        sections = self._finalizeSections(sections, is_over_under, lead_header)
        return {"sections": sections, "lead_header": lead_header,
                "dual": True, "market_name": None}

    def _finalizeSections(self, sections, is_over_under: bool,
                          lead_header: bool):
        """Shared render-path tail for both the single- and dual-source impls.

        Single-strike team markets (moneyline / one-line spread): order each
        block by SECTION POSITION so both sides' best prices meet at the centre
        separator — the top team's best at the BOTTOM of its block, the lower
        team's best at the TOP. Sorting by |american| only landed the best at
        the separator when the favourite happened to be the upper block; with
        the favourite on the bottom it shoved its best price (e.g. CAR -102) to
        the very bottom and its worst (-175) to the top. Skipped for Over/Under
        and the multi-strike path, which arrange their own ordering.

        Then drop orders with no liquidity behind them — Novig's indicative
        top-of-book quote carries $0 size (a price you can't actually take),
        and on a thin/early market those are the "dead" rows that render as
        bar-less slivers. The collapsed ALT LINES header is kept even though it
        is intentionally row-less."""
        if not is_over_under and not lead_header:
            for i, (_lbl, rows) in enumerate(sections):
                rows.sort(key=lambda od: od["odds"], reverse=(i != 0))
        sections = [(label, [o for o in rows if o.get("value", 0) > 0])
                    for label, rows in sections]
        return [(label, rows) for label, rows in sections
                if rows or "ALT LINES" in label]

    # ==================================================================
    # Shared row emission + in-place refresh animation (A + D)
    # ==================================================================
    # Both render paths (single + dual) funnel their finished `sections`
    # through _emitOrderbookRows. It computes a structural signature for
    # the layout; when a refresh produces the SAME shape as the previous
    # one (same separators, same per-section row counts, same source mix),
    # it updates the existing cells in place — no setRowCount churn, so no
    # full-viewport repaint "flash" — and animates the bars/odds that
    # actually moved. Any structural change falls back to a full rebuild.

    def _emitOrderbookRows(self, sections, lead_header: bool, dual: bool,
                           market_name: Optional[str] = None) -> None:
        tbl = self.orderbook_table

        total_orders = sum(len(rows) for _, rows in sections)
        if total_orders == 0:
            self._row_anim.clear()
            self._last_plan_sig = None
            tbl.clearSpans()
            tbl.setRowCount(1)
            item = QTableWidgetItem("No orders available")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            tbl.setItem(0, 0, item)
            colspan = 3 if self.compact_mode else 5
            tbl.setSpan(0, 0, 1, colspan)
            if hasattr(self, "footer_label"):
                suffix = f" — {market_name}" if market_name else ""
                self.footer_label.setText(f"No orders available{suffix}")
            return

        # Flatten sections into an ordered render plan. Each entry is a
        # separator or an order row carrying a stable identity = (section
        # label, ordinal-within-section, source). lead_header labels every
        # section including the first.
        plan: list[dict] = []
        cumulative = 0.0
        first_section = True
        alt_header_row = None
        for section_label, rows in sections:
            if (not first_section) or lead_header:
                plan.append({"kind": "sep", "label": section_label})
                if "ALT LINES" in section_label:
                    alt_header_row = len(plan) - 1
            first_section = False
            for ordinal, od in enumerate(rows):
                cumulative += od["value"]
                plan.append({"kind": "order", "section": section_label,
                             "ordinal": ordinal, "od": od,
                             "source": od.get("_source"),
                             "cumulative": cumulative})

        total_liquidity = sum(o["value"] for _, rows in sections for o in rows)
        max_stake = self._barScaleReference(
            o["value"] for _, rows in sections for o in rows)

        # Structural signature — if unchanged we can refresh in place.
        sig = (self.compact_mode, tbl.columnCount(), dual, tuple(
            (p["kind"], p.get("label"), p.get("section"),
             p.get("ordinal"), p.get("source"))
            for p in plan))
        in_place = (sig == self._last_plan_sig
                    and tbl.rowCount() == len(plan))

        if not in_place:
            # Full rebuild: clear the old separator spans (they're re-applied
            # by renderSideSeparatorRow below). On the in-place path the spans
            # are still valid and MUST be left alone — clearing them without
            # re-applying lets the unspanned separator text blow out the
            # ResizeToContents SIDE column and force a horizontal scrollbar.
            self._row_anim.clear()
            tbl.clearSpans()
            tbl.setRowCount(len(plan))

        self._alt_header_row = alt_header_row

        # On a full rebuild every setItem/setData fires a dataChanged signal
        # through Qt's model internals.  The outer setUpdatesEnabled(False)
        # already prevents repaints, so those signals are pure overhead — the
        # view will get one full repaint when setUpdatesEnabled(True) is
        # restored at the call site.  Block them for the duration of the loop
        # and leave the in-place path untouched (it updates individual cells
        # whose signals the view's selective-repaint logic can legitimately use).
        _model = tbl.model()
        if not in_place:
            _model.blockSignals(True)
        try:
            for row, p in enumerate(plan):
                if p["kind"] == "sep":
                    # Separators never change between same-shape refreshes;
                    # only (re)build them on a full rebuild.
                    if not in_place:
                        self.renderSideSeparatorRow(row, f"─ {p['label']} ─")
                    continue
                if in_place:
                    self._updateOrderRowInPlace(
                        row, p["od"], max_stake, p["cumulative"],
                        total_liquidity, dual)
                else:
                    self.renderOrderRow(row, p["od"], max_stake,
                                        p["cumulative"], total_liquidity)
                    if dual:
                        self._applyDualRowTint(row, p["source"])
        finally:
            if not in_place:
                _model.blockSignals(False)

        self._last_plan_sig = sig

        # Size the SIDE column from the rendered texts (see initUI for why
        # ResizeToContents can't be trusted here). ~40 string measurements.
        self._fitSideColumn(plan)

        if self._row_anim and not self._anim_timer.isActive():
            self._anim_timer.start()

        if hasattr(self, "footer_label"):
            all_rows = [o for _, rows in sections for o in rows]
            best_o = max(all_rows, key=lambda o: o["odds"])
            worst_o = min(all_rows, key=lambda o: o["odds"])
            if dual:
                self.footer_label.setText(
                    f"Showing {total_orders} price levels • "
                    f"Best: {best_o.get('displayOdds', 'N/A')} • "
                    f"Worst: {worst_o.get('displayOdds', 'N/A')} • "
                    f"PX+NV • Total Liquidity: ${total_liquidity:,.2f}")
            else:
                self.footer_label.setText(
                    f"Showing {total_orders} price levels • "
                    f"Best Bid: {best_o.get('displayOdds', 'N/A')} • "
                    f"Best Ask: {worst_o.get('displayOdds', 'N/A')} • "
                    f"Spread: {len(sections)} sides • "
                    f"Total Liquidity: ${total_liquidity:,.2f}")

    def _onSectionsReady(self, gen: int, result: object) -> None:
        """Main-thread slot: receive finished section data from the background
        builder and update the table. Drops stale results when the user has
        moved to a different market since the build was submitted."""
        if gen != self._render_gen:
            return
        if hasattr(self, "_orderbook_row_payloads"):
            self._orderbook_row_payloads.clear()
        self._alt_header_row = None
        tbl = self.orderbook_table
        tbl.setUpdatesEnabled(False)
        try:
            self._emitOrderbookRows(
                result["sections"], result["lead_header"],
                dual=result["dual"], market_name=result.get("market_name"))
        finally:
            tbl.setUpdatesEnabled(True)

    def _applyDualRowTint(self, row: int, source: Optional[str]) -> None:
        """Source differentiation for the dual path: brand-color the SIDE
        text and softly tint the whole row's background."""
        if source == "px":
            text_color = self._PX_TEXT_COLOR
            bg_tint = self._PX_ROW_TINT
        else:
            text_color = self._NV_TEXT_COLOR
            bg_tint = self._NV_ROW_TINT
        for c in range(self.orderbook_table.columnCount()):
            cell = self.orderbook_table.item(row, c)
            if cell is not None:
                cell.setBackground(bg_tint)
        side_cell = self.orderbook_table.item(row, 0)
        if side_cell is not None:
            side_cell.setForeground(text_color)

    def _updateOrderRowInPlace(self, row: int, order: Dict, max_stake: float,
                               cumulative: float, total_liquidity: float,
                               dual: bool) -> None:
        """Refresh one existing row's cells without recreating them. Only
        cells whose text actually changed are rewritten; the odds bar
        lerps toward its new width and a price change triggers a directional
        flash, both driven by _stepRowAnimations."""
        tbl = self.orderbook_table
        odds_item = tbl.item(row, 1)
        if odds_item is None:
            # Pool slot missing (shouldn't happen on a same-shape refresh);
            # fall back to a fresh build for just this row.
            self.renderOrderRow(row, order, max_stake, cumulative,
                                total_liquidity)
            if dual:
                self._applyDualRowTint(row, order.get("_source"))
            return

        display_name = order.get('displayName', order.get('abbreviatedName', '---'))
        display_odds = order.get('displayOdds', '---')
        value = order.get('value', 0)
        odds_value = order.get('odds', 0)
        side_type = 'bid' if odds_value < 0 else 'ask'

        # Keep the bet-slip payload current (price/size move every tick).
        place = order.get("_place")
        if place is not None and hasattr(self, "_orderbook_row_payloads"):
            self._orderbook_row_payloads[row] = {
                **place,
                "display_name": display_name,
                "display_odds": display_odds,
            }

        if value > 0 and max_stake > 0:
            bar_width = max(self.MIN_BAR_WIDTH,
                            min(100, int((value / max_stake) * 100)))
        else:
            bar_width = 0
        percentage = (value / total_liquidity * 100) if total_liquidity > 0 else 0

        # --- Odds cell: animate bar, flash on price change -------------
        prev_odds = odds_item.data(OddsBarDelegate.ODDS_ROLE)
        anim = self._row_anim.get(row)
        if anim is None:
            cur_bar = odds_item.data(OddsBarDelegate.BAR_WIDTH_ROLE) or bar_width
            # Previous numeric odds: carried in the anim across frames; on
            # a cold row parse it back from the displayed string.
            try:
                prev_num = int(str(prev_odds))
            except (TypeError, ValueError):
                prev_num = odds_value
            anim = {"bar_cur": float(cur_bar), "bar_tgt": bar_width,
                    "flash": 0.0, "odds_val": prev_num}
        else:
            anim["bar_tgt"] = bar_width
        if str(prev_odds) != str(display_odds):
            anim["flash"] = 1.0
            anim["flash_sign"] = 1 if odds_value > anim["odds_val"] else -1
            odds_item.setData(OddsBarDelegate.ODDS_ROLE, display_odds)
        anim["odds_val"] = odds_value
        if odds_item.data(OddsBarDelegate.SIDE_TYPE_ROLE) != side_type:
            odds_item.setData(OddsBarDelegate.SIDE_TYPE_ROLE, side_type)
        # Bar moves via the animator; only seed the role when it isn't already
        # at the current displayed width, so static rows don't repaint.
        cur_int = int(round(anim["bar_cur"]))
        if odds_item.data(OddsBarDelegate.BAR_WIDTH_ROLE) != cur_int:
            odds_item.setData(OddsBarDelegate.BAR_WIDTH_ROLE, cur_int)
        if abs(anim["bar_tgt"] - anim["bar_cur"]) > 0.5 or anim["flash"] > 0:
            self._row_anim[row] = anim
        else:
            self._row_anim.pop(row, None)

        # --- Text columns: rewrite only when the string changed --------
        if self.compact_mode:
            side_name = order.get('abbreviatedName', display_name)
            if len(side_name) > 10:
                parts = side_name.split()
                if parts:
                    last = parts[-1]
                    # Trailing spread/number (e.g. "-4.5", "+1.5"): show
                    # "Surname ±N.N" so both player and line are visible.
                    if len(parts) >= 2 and last.lstrip('+-').replace('.', '').isdigit():
                        side_name = f"{parts[-2]} {last}"
                    else:
                        side_name = last
            self._setCellText(row, 0, side_name)
            liquidity_text = (f"${value/1000:.1f}k" if value >= 1000
                              else f"${value:.0f}")
            self._setCellText(row, 2, liquidity_text)
        else:
            self._setCellText(row, 0, display_name)
            self._setCellText(row, 2, f"${value:,.2f}")
            self._setCellText(row, 3, f"${cumulative:,.2f}")
            self._setCellText(row, 4, f"{percentage:.1f}%")
            pct_cell = tbl.item(row, 4)
            if pct_cell is not None:
                pct_cell.setForeground(self._percentageColor(percentage))

        # Re-assert ask/bid side color (dual overrides col 0 separately).
        side_cell = tbl.item(row, 0)
        if side_cell is not None and not dual:
            side_cell.setForeground(QColor(252, 165, 165) if side_type == 'ask'
                                    else QColor(110, 231, 183))

    def _setCellText(self, row: int, col: int, text: str) -> None:
        """Set an existing cell's text only when it differs — avoids
        needless repaints of unchanged cells."""
        cell = self.orderbook_table.item(row, col)
        if cell is None:
            cell = QTableWidgetItem()
            self.orderbook_table.setItem(row, col, cell)
        if cell.text() != text:
            cell.setText(text)

    @staticmethod
    def _percentageColor(percentage: float) -> QColor:
        if percentage >= 10:
            return QColor(251, 191, 36)
        if percentage >= 5:
            return QColor(252, 211, 77)
        return QColor(156, 163, 175)

    def _stepRowAnimations(self) -> None:
        """Per-frame tick: lerp each animating row's bar toward its target
        and decay its flash. Repaints only the odds column of rows still in
        flight, then stops the timer once everything has settled."""
        if not self._row_anim:
            self._anim_timer.stop()
            return
        tbl = self.orderbook_table
        done: list[int] = []
        for row, anim in self._row_anim.items():
            if row >= tbl.rowCount():
                done.append(row)
                continue
            odds_item = tbl.item(row, 1)
            if odds_item is None:
                done.append(row)
                continue
            cur = anim["bar_cur"]
            tgt = anim["bar_tgt"]
            cur += (tgt - cur) * 0.30
            if abs(tgt - cur) < 0.5:
                cur = float(tgt)
            anim["bar_cur"] = cur
            odds_item.setData(OddsBarDelegate.BAR_WIDTH_ROLE, int(round(cur)))

            flash = anim.get("flash", 0.0)
            if flash > 0:
                flash *= 0.88
                if flash < 0.03:
                    flash = 0.0
                anim["flash"] = flash
                odds_item.setData(OddsBarDelegate.FLASH_ROLE,
                                  flash * anim.get("flash_sign", 1))
            else:
                odds_item.setData(OddsBarDelegate.FLASH_ROLE, 0.0)

            # Repaint just this row's odds cell.
            idx = tbl.model().index(row, 1)
            rect = tbl.visualRect(idx)
            if rect.isValid():
                tbl.viewport().update(rect)

            if cur == float(tgt) and anim.get("flash", 0.0) <= 0:
                done.append(row)
        for row in done:
            self._row_anim.pop(row, None)
        if not self._row_anim:
            self._anim_timer.stop()

    def _fitSideColumn(self, plan: list) -> None:
        """Set the SIDE column width from the actual rendered texts.

        Column 0 is on Fixed resize mode because the full-rebuild path blocks
        the model's signals while it writes items, which starves a
        ResizeToContents header of the dataChanged notifications it needs to
        re-measure — leaving the column at whatever width the previous
        content produced (the truncated-until-alt-toggle bug). Separator rows
        are ignored: they span the full table and must not widen the column."""
        tbl = self.orderbook_table
        fm = None
        widest = 0
        for row, p in enumerate(plan):
            if p["kind"] != "order":
                continue
            item = tbl.item(row, 0)
            if item is None:
                continue
            if fm is None:
                fm = QFontMetrics(item.font())
            widest = max(widest, fm.horizontalAdvance(item.text()))
        if widest:
            # Overhead beyond the raw text advance: the widget stylesheet's
            # QTableWidget::item horizontal padding (4px×2 compact / 12px×2
            # full) plus the style's ~3px×2 text margins — none of which
            # QFontMetrics sees. Measured 15px in compact mode; anything at
            # or under that leaves strings a pixel from the elide boundary
            # (the "first market truncated, next market fine" symptom).
            pad = 22 if self.compact_mode else 38
            tbl.setColumnWidth(0, widest + pad)

    def renderSideSeparatorRow(self, row: int, label: str):
        """Render a separator row between the two market sides"""
        if self.compact_mode:
            separator = QTableWidgetItem(label)
            font_size = 9
            row_height = 24
            colspan = 3
        else:
            separator = QTableWidgetItem(f"───── {label} ─────")
            font_size = 12
            row_height = 40
            colspan = 5

        separator.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        sep_font = QFont("SF Mono", font_size, QFont.Weight.Bold)
        sep_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        separator.setFont(sep_font)
        separator.setForeground(QColor(96, 165, 250))  # Professional blue
        separator.setBackground(QColor(31, 41, 55))  # Darker gray-blue
        # The separator cell lives in column 0 and spans the whole row. With
        # column 0 on ResizeToContents, Qt's sizeHintForColumn(0) otherwise
        # counts this long separator text and blows the SIDE column out to the
        # width of "─ ▸ ALT LINES (N) • tap to expand ─" (pushing ODDS to the
        # right edge, dropping LIQ off-screen). An explicit narrow size hint
        # keeps the text out of the column-width calc; the span still paints
        # the full string across all columns.
        separator.setSizeHint(QSize(1, row_height))

        self.orderbook_table.setItem(row, 0, separator)
        self.orderbook_table.setSpan(row, 0, 1, colspan)
        self.orderbook_table.setRowHeight(row, row_height)

    @staticmethod
    def _barScaleReference(values, pct: float = 0.90) -> float:
        """Reference size the liquidity bars scale against.

        Scaling to the raw max lets a single whale / stale order flatten
        every other bar to a sliver, and makes the whole ladder jump each
        time that order enters or leaves the view on a live tick (the
        "bars get small" flicker). Scaling to a high percentile instead
        keeps ordinary orders readable and simply lets outliers cap at
        full width, so the scale barely moves between ticks."""
        vals = sorted(v for v in values if v and v > 0)
        if not vals:
            return 1.0
        idx = min(len(vals) - 1, int(pct * (len(vals) - 1)))
        ref = vals[idx]
        return ref if ref > 0 else vals[-1]

    def renderOrderRow(self, row: int, order: Dict, max_stake: float, cumulative: float, total_liquidity: float):
        """Render a single order book row with team/selection, odds, liquidity, cumulative, and percentage"""
        display_name = order.get('displayName', order.get('abbreviatedName', '---'))
        display_odds = order.get('displayOdds', '---')
        value = order.get('value', 0)
        odds_value = order.get('odds', 0)

        # Register the row for bet-slip lookup. _place is stamped by the
        # normalized adapter; legacy PX-only path leaves it absent, which
        # the click handler treats as "not placeable from here".
        place = order.get("_place")
        if place is not None and hasattr(self, "_orderbook_row_payloads"):
            self._orderbook_row_payloads[row] = {
                **place,
                "display_name": display_name,
                "display_odds": display_odds,
            }

        # Determine if this is a favorite (negative odds) or underdog (positive odds)
        side_type = 'bid' if odds_value < 0 else 'ask'

        # Calculate liquidity bar width (0-100%). Clamp because the scale
        # is a percentile, so orders above it exceed 100% and should just
        # fill the bar. Floor any order with real size at MIN_BAR_WIDTH so
        # small orders stay readable; only true $0 dead quotes (already
        # filtered out upstream) get no bar.
        if value > 0 and max_stake > 0:
            bar_width = max(self.MIN_BAR_WIDTH,
                            min(100, int((value / max_stake) * 100)))
        else:
            bar_width = 0

        # Calculate percentage of total book
        percentage = (value / total_liquidity * 100) if total_liquidity > 0 else 0

        # Build the odds-bar cell as a QTableWidgetItem carrying three
        # data roles consumed by OddsBarDelegate.paint(). No QWidget is
        # created; no stylesheet is parsed.
        odds_item = QTableWidgetItem()
        odds_item.setData(OddsBarDelegate.ODDS_ROLE, display_odds)
        odds_item.setData(OddsBarDelegate.BAR_WIDTH_ROLE, int(bar_width))
        odds_item.setData(OddsBarDelegate.SIDE_TYPE_ROLE, side_type)

        if self.compact_mode:
            # Compact mode: only 3 columns - SIDE, ODDS, LIQ
            # Use abbreviated name for side column
            side_name = order.get('abbreviatedName', display_name)
            if len(side_name) > 10:
                parts = side_name.split()
                if parts:
                    last = parts[-1]
                    # Trailing spread/number (e.g. "-4.5", "+1.5"): show
                    # "Surname ±N.N" so both player and line are visible.
                    if len(parts) >= 2 and last.lstrip('+-').replace('.', '').isdigit():
                        side_name = f"{parts[-2]} {last}"
                    else:
                        side_name = last

            selection_item = self.createSelectionItem(side_name, side_type)

            # Compact liquidity display
            if value >= 1000:
                liquidity_text = f"${value/1000:.1f}k"
            else:
                liquidity_text = f"${value:.0f}"
            liquidity_item = self.createPlainItem(liquidity_text)

            self.orderbook_table.setItem(row, 0, selection_item)
            self.orderbook_table.setItem(row, 1, odds_item)
            self.orderbook_table.setItem(row, 2, liquidity_item)
        else:
            # Full mode: all 5 columns
            selection_item = self.createSelectionItem(display_name, side_type)
            liquidity_item = self.createPlainItem(f"${value:,.2f}")
            cumulative_item = self.createPlainItem(f"${cumulative:,.2f}")
            percentage_item = self.createPercentageItem(f"{percentage:.1f}%", percentage)

            self.orderbook_table.setItem(row, 0, selection_item)
            self.orderbook_table.setItem(row, 1, odds_item)
            self.orderbook_table.setItem(row, 2, liquidity_item)
            self.orderbook_table.setItem(row, 3, cumulative_item)
            self.orderbook_table.setItem(row, 4, percentage_item)

    def _mono_item(self, text: str, align: Qt.AlignmentFlag,
                   weight=QFont.Weight.DemiBold) -> QTableWidgetItem:
        """SF-Mono table item sized for the current mode, vertically centred.
        Shared font/alignment setup for the three item creators below."""
        item = QTableWidgetItem(text)
        item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
        font = QFont("SF Mono", 8 if self.compact_mode else 13, weight)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(font)
        return item

    def createSelectionItem(self, text: str, side_type: str) -> QTableWidgetItem:
        """Selection/team name item, colored by side (ask=red, bid=green)."""
        item = self._mono_item(text, Qt.AlignmentFlag.AlignLeft)
        item.setForeground(QColor(252, 165, 165) if side_type == 'ask'
                           else QColor(110, 231, 183))
        return item

    def createPlainItem(self, text: str) -> QTableWidgetItem:
        """Plain right-aligned gray table item."""
        item = self._mono_item(text, Qt.AlignmentFlag.AlignRight,
                               QFont.Weight.Normal)
        item.setForeground(QColor(209, 213, 219))
        return item

    def createPercentageItem(self, text: str, percentage: float) -> QTableWidgetItem:
        """Percentage item, amber/gold intensity scaled by share of book."""
        item = self._mono_item(text, Qt.AlignmentFlag.AlignRight)
        item.setForeground(self._percentageColor(percentage))
        return item

    # ==================================================================
    # Bet slip orchestration (cross-source: SGP scanner + orderbook)
    # ==================================================================

    @staticmethod
    def _orderbookRowKey(place: dict) -> str:
        """Stable key for an orderbook row click. Encodes source + the
        specific resting offer being targeted, so two distinct levels at
        the same price (e.g. two NV bids at $0.45 from different
        traders) don't collide."""
        return (f"OB|{place.get('source')}|{place.get('line_id')}|"
                f"{place.get('nv_outcome_id') or ''}|"
                f"{place.get('american')}|{place.get('size_usd'):.2f}")

    def _onLegPicked(self, leg: dict) -> None:
        """Leg arrived from the SGP scanner (or any other source)."""
        key = leg.get("key")
        if not key:
            return
        if key in self._slip_legs:
            self.bet_slip.remove_leg(key)
            return
        leg.setdefault("source_widget", "sgp")
        self._slip_legs[key] = leg
        self.bet_slip.add_leg(leg)
        self.bet_slip.expand()
        self._setLegHighlight(leg, slipped=True)

    def _onSlipLegRemoved(self, key: str) -> None:
        leg = self._slip_legs.pop(key, None)
        if leg is not None:
            self._setLegHighlight(leg, slipped=False)

    def _setLegHighlight(self, leg: dict, *, slipped: bool) -> None:
        """Tell the leg's source to repaint its row's selection state."""
        src_origin = leg.get("source_widget")
        if src_origin == "sgp":
            if hasattr(self, "sgp_panel"):
                self.sgp_panel.set_row_slipped(leg["key"], slipped)
        elif src_origin == "ob":
            # Orderbook rows live on `self.orderbook_table`. Iterate to
            # find the row with this place key — cheap, the table has at
            # most ~50 rows.
            target_row = leg.get("table_row")
            if target_row is not None:
                item = self.orderbook_table.item(target_row, 0)
                if item is not None:
                    if slipped:
                        item.setBackground(QColor(94, 234, 212, 60))
                    else:
                        item.setBackground(QColor(0, 0, 0, 0))

    def _onScanStarted(self) -> None:
        """Drop any SGP-sourced legs when a new scan begins; the cached
        savedParlayId quote IDs are about to be invalid server-side."""
        sgp_keys = [k for k, v in self._slip_legs.items()
                    if v.get("source_widget") == "sgp"]
        for k in sgp_keys:
            self.bet_slip.remove_leg(k)

    def _toggleAltsCollapsed(self) -> None:
        """Expand/collapse the multi-strike ALT LINES section and re-render
        the current book from the stashed inputs."""
        self._alts_collapsed = not self._alts_collapsed
        r = self._last_render
        if not r:
            return
        if r[0] == "dual":
            self.setMarketDual(r[1], r[2])
        else:
            self.setNormalizedMarket(r[1])

    def _onOrderbookRowClicked(self, table_row: int, _col: int) -> None:
        # Clicking the ALT LINES header toggles the collapsed section
        # rather than staging a bet.
        if table_row == self._alt_header_row:
            self._toggleAltsCollapsed()
            return
        place = self._orderbook_row_payloads.get(table_row)
        if not place:
            return
        key = self._orderbookRowKey(place)
        if key in self._slip_legs:
            self.bet_slip.remove_leg(key)
            return
        odds = place.get("american")
        leg = {
            "src": ("PX" if (place.get("source") or "").startswith("prophetx")
                    else "NV"),
            "key": key,
            "label": (f"{place.get('display_name', '?')} "
                      f"@ {place.get('display_odds', '?')} "
                      f"(${place.get('size_usd', 0):,.2f} avail)"),
            "odds": odds,
            "edge_pct": 0.0,
            "raw": place,
            "bet_kind": "wager",
            "status": "",
            "source_widget": "ob",
            "table_row": table_row,
        }
        self._slip_legs[key] = leg
        self.bet_slip.add_leg(leg)
        self.bet_slip.expand()
        self._setLegHighlight(leg, slipped=True)

    # ------------------------------------------------------------------
    # Place flow — dispatch by (src, bet_kind).
    # ------------------------------------------------------------------
    def _onSlipPlaceRequested(self, legs: list, wager: float) -> None:
        try:
            asyncio.ensure_future(self._runPlacements(legs, wager))
        except RuntimeError as e:
            print(f"[bet-slip] Cannot start placement task: {e}")
            self.bet_slip.place_btn.setEnabled(True)

    async def _runPlacements(self, legs: list, wager: float) -> None:
        try:
            for leg in legs:
                src = leg.get("src")
                kind = leg.get("bet_kind", "sgp")
                key = leg.get("key")
                self.bet_slip.mark_leg_status(key, "placing…", success=True)
                try:
                    if kind == "sgp" and src == "PX":
                        ok, msg, dollars = await self._placePXSGP(
                            leg.get("raw") or {}, wager)
                    elif kind == "sgp" and src == "NV":
                        ok, msg, dollars = await self._placeNVSGP(
                            leg.get("raw") or {}, wager)
                    elif kind == "wager" and src == "PX":
                        ok, msg, dollars = await self._placePXWager(
                            leg.get("raw") or {}, wager)
                    elif kind == "wager" and src == "NV":
                        ok, msg, dollars = await self._placeNVWager(
                            leg.get("raw") or {}, wager)
                    else:
                        ok, msg, dollars = False, f"unknown {src}/{kind}", 0.0
                except Exception as e:
                    ok, msg, dollars = False, f"error: {e}", 0.0
                self.bet_slip.mark_leg_status(key, msg, success=ok)
                if ok and dollars > 0:
                    self.bet_slip.record_placement(dollars, src=src or "")
        finally:
            self.bet_slip.place_btn.setEnabled(True)
            # Authoritative refresh of open positions — pulls the newly-
            # placed wager(s) into the slip's positions list. Balance is
            # already optimistically decremented by record_placement and
            # rarely changes outside of placements, so we skip re-fetching
            # it here. The ↻ button forces a full refresh if needed.
            QTimer.singleShot(500, self.bet_slip.refresh_positions)

    # --- SGP placements (unchanged from previous SGP-only slip) -------

    async def _placePXSGP(self, row: dict, wager: float) -> tuple:
        from ProphetXQuery import (GetSGPQuoteAsync, PlaceSGPAsync,
                                   load_prophetx_cookies)
        legs = row.get("legs")
        if not legs:
            return False, "no legs cached", 0.0
        import aiohttp
        try:
            cookies = load_prophetx_cookies()
        except FileNotFoundError:
            return False, "no PX session", 0.0
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(cookies=cookies,
                                         timeout=timeout) as session:
            quote = await GetSGPQuoteAsync(session, legs, stake=wager)
            if not quote or not quote.get("success"):
                return False, "re-quote failed", 0.0
            data = quote.get("data") or {}
            parlay_id = data.get("parlayId")
            offers = data.get("offers") or []
            if not parlay_id or not offers:
                return False, "no parlayId/offers", 0.0
            tier = None
            for o in offers:
                try:
                    s = float(o.get("stake") or 0)
                except (TypeError, ValueError):
                    continue
                if s <= wager + 1e-6 and (tier is None
                                          or s > float(tier["stake"])):
                    tier = o
            tier = tier or offers[0]
            live_odds = tier.get("odds")
            hr_odds = row.get("hr_odds")
            if (isinstance(live_odds, (int, float))
                    and isinstance(hr_odds, (int, float))):
                live_dec = (1 + live_odds / 100 if live_odds > 0
                            else 1 + 100 / abs(live_odds))
                hr_dec = (1 + hr_odds / 100 if hr_odds > 0
                          else 1 + 100 / abs(hr_odds))
                if live_dec <= hr_dec:
                    return False, "edge gone (drift)", 0.0
            actual_stake = float(tier.get("stake") or wager)
            resp = await PlaceSGPAsync(session, parlay_id,
                                       int(live_odds), actual_stake)
        if not resp or not resp.get("success"):
            return False, "place rejected", 0.0
        return True, f"placed ${actual_stake:.2f}", actual_stake

    async def _placeNVSGP(self, row: dict, wager: float) -> tuple:
        import aiohttp
        from novig_async import (price_parlay_async, place_parlay_async,
                                 REQUEST_TIMEOUT)
        dom_id = row.get("dominant_outcome_id")
        imp_id = row.get("implied_outcome_id")
        if not dom_id or not imp_id:
            return False, "missing outcome ids", 0.0
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                quote = await price_parlay_async(session, [dom_id, imp_id])
                saved_id = quote.get("id")
                if not saved_id:
                    return False, "no savedParlayId", 0.0
                try:
                    from NovigClient import summarize_parlay_quote
                    s = summarize_parlay_quote(quote)
                    live_combined = s.get("combined_price")
                except Exception:
                    live_combined = None
                cached_dom = row.get("dominant_price")
                if (live_combined is not None and cached_dom is not None
                        and float(live_combined) + 1e-6 >= float(cached_dom)):
                    return False, "edge gone (drift)", 0.0
                resp = await place_parlay_async(session, saved_id, wager)
        except Exception as e:
            return False, f"error: {e}", 0.0
        status = resp.get("status") if isinstance(resp, dict) else None
        if status in ("Filled", "Pending", "Open"):
            return True, f"{status} ${wager:.2f}", wager
        return False, f"status={status!r}", 0.0

    # --- Single-bet placements (new) ---------------------------------

    async def _placePXWager(self, place: dict, wager: float) -> tuple:
        """Take a resting offer on ProphetX. `place` is the orderbook
        row payload stamped by _normalized_order_to_dict."""
        from ProphetXQuery import (PlaceMarketOrderAsync,
                                   load_prophetx_cookies)
        line_id = place.get("px_line_id") or place.get("line_id")
        odds = place.get("american")
        avail = float(place.get("size_usd") or 0.0)
        if not line_id or odds is None:
            return False, "missing lineId/odds", 0.0
        actual_stake = min(wager, avail) if avail > 0 else wager
        if actual_stake <= 0:
            return False, "no liquidity", 0.0
        import aiohttp
        try:
            cookies = load_prophetx_cookies()
        except FileNotFoundError:
            return False, "no PX session", 0.0
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(cookies=cookies,
                                         timeout=timeout) as session:
            resp = await PlaceMarketOrderAsync(session, line_id,
                                               int(odds), actual_stake)
        if not resp:
            return False, "place rejected", 0.0
        # Server returns {orderId, status, ...} — accept anything 2xx-shaped
        # since the read-back GET handles fill detail.
        return True, f"placed ${actual_stake:.2f}", actual_stake

    async def _placeNVWager(self, place: dict, wager: float) -> tuple:
        """Take a resting offer on Novig. `place` is the orderbook row
        payload stamped by _normalized_order_to_dict.

        Geo-token expiry is handled transparently downstream: a 400
        "Geolocation validation has expired" inside place_order_async
        calls NovigClient.geo_harvester.force_refresh_geo_async() and
        retries once. One-time bootstrap if no cached token exists:
        `python NovigClient.py --refresh-geo`."""
        from novig_async import (place_order_async, stake_to_qty_centi,
                                 REQUEST_TIMEOUT, NovigError)
        market_id = place.get("nv_market_id")
        outcome_id = place.get("nv_outcome_id")
        is_bid = place.get("nv_is_bid")
        prob = float(place.get("prob") or 0.0)
        avail = float(place.get("size_usd") or 0.0)
        if not market_id or not outcome_id or prob <= 0:
            return False, "missing market/outcome", 0.0
        if is_bid is None:
            is_bid = True
        actual_stake = min(wager, avail) if avail > 0 else wager
        if actual_stake <= 0:
            return False, "no liquidity", 0.0
        qty_centi = stake_to_qty_centi(actual_stake, prob)
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                resp = await place_order_async(
                    session, market_id, outcome_id, prob, qty_centi,
                    is_bid=bool(is_bid))
        except NovigError as e:
            return False, f"NV: {e}", 0.0
        except Exception as e:
            return False, f"error: {e}", 0.0
        status = resp.get("status") if isinstance(resp, dict) else None
        if status in ("FILLED", "PENDING", "OPEN"):
            return True, f"{status.title()} ${actual_stake:.2f}", actual_stake
        return False, f"status={status!r}", 0.0


_EVENT_SOURCE_ROLE = Qt.ItemDataRole.UserRole + 1
# Mint green = ProphetX accent (matches OrderBookWidget.SOURCE_ACCENT_COLOR
# convention). Blue = Novig CASH "blue chip". Same hex values used
# elsewhere in this file so the event list reads consistently with the
# order-book badge once an event is selected.
_PX_COLOR = QColor("#5eead4")
_NV_COLOR = QColor("#3b82f6")


class _EventSourceDelegate(QStyledItemDelegate):
    """Paints a colored accent stripe on the left edge AND the row's
    text itself in a source-matched color. Painting the text directly
    bypasses two stubborn Qt quirks at once:
      - QComboBox QAbstractItemView { color: ... } QSS rules override
        per-item Qt.ItemDataRole.ForegroundRole, so setItemData(...,
        ForegroundRole) silently does nothing on a styled combo.
      - Even with the QSS rule removed, some Qt platforms/styles
        ignore ForegroundRole on combo views entirely.
    Source classes:
      'PX'   → mint-green stripe + mint-green text
      'NV'   → blue stripe + blue text
      'BOTH' → split stripe (top green / bottom blue) + light-grey text
              so the bicolor stripe is the visual anchor
    """

    STRIPE_WIDTH = 4
    TEXT_PAD_LEFT = 6   # gap between stripe and text
    TEXT_PAD_RIGHT = 6

    _NEUTRAL = QColor(230, 230, 230)

    def paint(self, painter: QPainter, option, index):
        # Render default background + selection + hover, but suppress
        # the default text draw — we'll paint the text ourselves
        # below in the correct color. Mutating a copy of option keeps
        # us from clobbering the caller's struct.
        from PyQt6.QtWidgets import QStyleOptionViewItem, QStyle
        from PyQt6.QtWidgets import QApplication
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = (opt.widget.style() if opt.widget
                 else QApplication.style())
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem,
                          opt, painter, opt.widget)

        source = index.data(_EVENT_SOURCE_ROLE) or ""
        rect = option.rect

        # Stripe
        if source:
            stripe = rect.adjusted(0, 0,
                                   -(rect.width() - self.STRIPE_WIDTH),
                                   0)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            if source == "PX":
                painter.setBrush(_PX_COLOR)
                painter.drawRect(stripe)
            elif source == "NV":
                painter.setBrush(_NV_COLOR)
                painter.drawRect(stripe)
            elif source == "BOTH":
                half = stripe.height() // 2
                painter.setBrush(_PX_COLOR)
                painter.drawRect(stripe.adjusted(0, 0, 0, -half))
                painter.setBrush(_NV_COLOR)
                painter.drawRect(stripe.adjusted(0, half, 0, 0))
            painter.restore()

        if not text:
            return

        # Text in source color
        text_color = (_PX_COLOR if source == "PX"
                      else _NV_COLOR if source == "NV"
                      else self._NEUTRAL if source == "BOTH"
                      else QColor(255, 255, 255))
        text_rect = rect.adjusted(self.STRIPE_WIDTH + self.TEXT_PAD_LEFT,
                                  0, -self.TEXT_PAD_RIGHT, 0)
        painter.save()
        painter.setPen(text_color)
        painter.setFont(opt.font)
        flags = (Qt.AlignmentFlag.AlignLeft
                 | Qt.AlignmentFlag.AlignVCenter)
        # Multi-line items (the QListWidget uses "\n" in display)
        # need word-wrap-ish handling; AlignVCenter on a multi-line
        # rect with TextWordWrap works for both single + multi line.
        if "\n" in text:
            flags |= Qt.TextFlag.TextWordWrap
        painter.drawText(text_rect, int(flags), text)
        painter.restore()


# --- Event dropdown volume gating --------------------------------------------
# The event list is decluttered by traded volume (metadata["stake"], in USD):
#   * untraded events ($0 / dust) are hidden outright;
#   * events more than _FAR_HORIZON_DAYS away are hidden UNLESS they already
#     carry real volume — so a futures market or an early-listed game people
#     are actually trading (e.g. a World Cup game 4 days out with $6k) stays,
#     but the long tail of empty pre-listings doesn't crowd the menu.
# Near-term events only need to clear the dust floor.
_MIN_EVENT_VOLUME = 1.0       # $ below this counts as untraded -> hide
_FAR_HORIZON_DAYS = 2.0       # events beyond this need real volume to show
_FAR_MIN_VOLUME = 1000.0      # "some trading going on" floor for far events


def _parse_event_start(ts: Optional[str]) -> Optional[datetime]:
    """Parse a Novig ('...+00:00') or ProphetX ('...Z') ISO start time into
    an aware UTC datetime. Returns None when absent/unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_passes_volume_filter(metadata: dict,
                                now: Optional[datetime] = None) -> bool:
    """Decide whether an event is worth listing in the dropdown. See the
    _MIN_EVENT_VOLUME / _FAR_* constants for the policy. Events with an
    unknown start time are kept as long as they clear the dust floor."""
    try:
        stake = float(metadata.get("stake") or 0.0)
    except (TypeError, ValueError):
        stake = 0.0
    if stake < _MIN_EVENT_VOLUME:
        return False
    start = _parse_event_start(metadata.get("startTime"))
    if start is None:
        return True
    now = now or datetime.now(timezone.utc)
    days_out = (start - now).total_seconds() / 86400.0
    if days_out > _FAR_HORIZON_DAYS and stake < _FAR_MIN_VOLUME:
        return False
    return True


class ProphetXBrowser(QWidget):
    """
    Complete ProphetX exchange browser widget.
    Includes event search, market selection, and order book display.
    """

    # Signal emitted when user selects an event (emits event_id)
    event_selected = pyqtSignal(int)

    # Signal emitted on startup to request fresh data fetch (emits nothing)
    # Connect this to trigger a full ProphetX scrape
    refresh_all_requested = pyqtSignal()

    # Signal emitted when loading state changes (emits is_loading bool)
    loading_state_changed = pyqtSignal(bool)

    def __init__(self, parent=None, compact_mode=False):
        super().__init__(parent)
        self.all_events = {}
        self.filtered_events = []
        self.current_event_data = None
        self.current_event_id = None
        self.compact_mode = compact_mode
        self._initial_load_complete = False
        self._pending_fresh_fetch = False
        # Orderbook reveal gating: for an event that has a Novig match the
        # book arrives in stages (PX render -> match-map dual re-render ->
        # async NV /book/batch depth), each restructuring/resizing the
        # table. We keep the loading overlay up across that build so the
        # user sees only the finished book. _reveal_gen tokenises the
        # in-flight gate so a stale fallback timer can't hide a newer load.
        self._reveal_gen = 0
        # Content signature of the last NV book actually painted, so the
        # 20s /book/batch refresh only re-renders (and re-scales the bars)
        # when the Novig side genuinely moved. Reset on market change.
        self._last_nv_render_sig = None
        # State for the refresh-time diff in _refreshMarketSelector:
        #   _last_market_name_order: names in the order they currently
        #     occupy the combo, so we can detect structure changes and
        #     fall back to a full rebuild when markets open/close or
        #     liquidity-based ordering shuffles them.
        #   _last_selected_market_sig: content hash of the most recently
        #     rendered market, so a refresh that doesn't move the
        #     selected market's orders skips the orderbook re-render
        #     entirely.
        self._last_market_name_order: Optional[List[str]] = None
        self._last_selected_market_sig: Optional[str] = None

        # Cross-source matching state. Built from the latest Novig
        # dump in _loadNovigMatchMap(); empty when no dump exists,
        # so the widget gracefully falls back to PX-only rendering.
        # Keyed by string ProphetX event_id.
        self._novig_event_map: Dict[str, object] = {}
        # NormalizedMarket for the currently displayed PX market.
        self._current_px_norm = None
        # Matched MarketPair for the current market (None if no NV side).
        self._current_market_pair = None
        # Active async book fetcher, kept alive until books_ready fires.
        self._novig_book_worker: Optional[NovigMarketBookWorker] = None
        # Async dump scraper — runs on init if no fresh dump on disk.
        self._novig_dump_worker: Optional[NovigDumpWorker] = None
        # Async match-map builder (load+normalize+match off the main thread).
        # _match_map_pending coalesces overlapping requests: the 2-3 startup
        # callers collapse into at most one in-flight build plus one queued
        # rebuild, so the freshest data always wins without stacking threads.
        self._match_map_worker: Optional[MatchMapWorker] = None
        self._match_map_pending: bool = False
        # Lazy market hydration: the match map is built from a metadata-only
        # index, so a paired event's markets are loaded from the full dump
        # only when the user opens it. The full dump is parsed at most once per
        # map build (cached here, ~122ms one-time off the startup path) and the
        # set tracks which events have already been hydrated so we don't redo
        # the per-event pairing on every re-render. Both reset in
        # _loadNovigMatchMap when a fresh map (and possibly fresh dump) lands.
        self._novig_full_dump_cache: Optional[dict] = None
        self._hydrated_event_ids: set = set()
        # Novig events that ProphetX doesn't list. Fetched live (list_events),
        # merged into all_events so they show in the dropdown like any other
        # event; their markets are fetched live on selection. Keyed by str id.
        self._nv_events: dict = {}
        self._nv_events_worker: Optional[NovigEventsWorker] = None
        self._nv_markets_worker: Optional[NovigEventMarketsWorker] = None
        self._current_nv_norm = None  # selected NV-only NormalizedMarket

        # --- Single initial populate gate ----------------------------------
        # The dropdown used to paint three times on startup: stale PX, then
        # fresh PX, then the NV merge — a visible PX-only -> combined flip.
        # We now hold the FIRST event-list build until all three inputs are
        # in: PX events, the live NV event list, and the PX<->NV match map
        # (so paired events are deduped/badged on the very first paint). A
        # watchdog forces the build if NV/map never arrive, so a slow or
        # failed Novig fetch can't leave the menu empty. Once the initial
        # build runs, later refreshes repopulate normally (already combined).
        self._initial_events_populated = False
        self._px_data_ready = False
        self._nv_data_ready = False
        self._match_map_ready = False
        # Bounded retry counter for the initial-populate watchdog so it can
        # wait out an in-progress Novig dump scrape without giving up forever.
        self._force_populate_attempts = 0
        # When a fresh Novig dump is being scraped on startup, the match map
        # builds twice — once from the stale on-disk index, then again after
        # the scrape writes + the new dump is parsed. The gate must wait for
        # that SECOND build (the dump is current), so the dropdown paints once
        # with final pairing. _dump_ready flips when the dump is confirmed
        # current: either the staleness check skipped the scrape, or the
        # scrape finished. Set eagerly by the watchdog so a failed scrape
        # can't wedge the gate.
        self._dump_ready = False

        self.initUI()
        # Defer Novig bootstrap: both calls must run AFTER the parent
        # widget (e.g. EffortOdds) has had a chance to connect signals
        # like refresh_all_requested / event_selected / data_ready. The
        # existing widget already uses a 100ms QTimer for its
        # refresh_all_requested emit (see _loadInitialData) — we delay
        # a bit longer so the Novig work never lands while the PX
        # fetch chain is still being wired up. Both deferred calls are
        # fast (and the dump worker is itself a QThread) so the user-
        # facing impact is zero.
        QTimer.singleShot(250, self._loadNovigMatchMap)
        # Live fetch of Novig's own event list so NV-only events appear in the
        # dropdown alongside ProphetX. Deferred past the PX wiring like above.
        QTimer.singleShot(300, self._loadNovigEvents)
        # Dump-refresh is now CHAINED off the match-map worker's completion
        # (see _onMatchMapReady), not a blind +350ms timer. The old timer
        # raced the 38MB parse: it fired before _novig_event_map was built,
        # so the "skip re-scrape if a fresh dump already covers us" guard saw
        # an empty map and scraped a fresh 38MB dump *unnecessarily* — while
        # the parse was still holding the GIL. Chaining serialises the two
        # heavy ops and lets the skip actually work. ([PERF-DIAG] herd fix)

    # ------------------------------------------------------------------
    # Novig dump bootstrap (async)
    # ------------------------------------------------------------------

    # If the most recent Novig dump is older than this, fire a fresh
    # scrape on startup. Otherwise reuse it — keeps API load minimal
    # across rapid widget restarts. Bumped from 10min because the dump's
    # role is the event roster (changes on hours-scale), not pricing —
    # live books come from /book/batch per selection. Coverage check
    # below forces a refresh sooner if PX events go unmatched.
    _NOVIG_DUMP_FRESH_SECONDS = 3600  # 1 hour

    # Leagues/tournaments Novig actually covers. Used by the coverage
    # check so we don't count PX-only events as "missing a Novig match".
    # PX labels these by tournament name (e.g. "French Open (M)") rather
    # than league code, so the set is matched against the canonical sport
    # bucket (see _novig_coverage_ok), not the raw tournament string.
    _NOVIG_LEAGUES = frozenset({
        "MLB", "NBA", "NHL", "NFL", "NCAAF", "NCAAB", "NCAABSB", "WNBA", "NCAAWB",
        "NCAA_FB",
        "EPL", "MLS", "Bundesliga", "Champions League", "Europa League",
        "La Liga", "Ligue 1", "Serie A", "FIFA World Cup",
        "ATP", "WTA",
        "PGA", "TGL",
        "UFC", "Boxing", "WBC",
        "Counter Strike 2", "Dota 2", "League of Legends",
        "Olympics Hockey Men",
        "ENTERTAINMENT",
    })

    # Minimum match-rate (PX events in Novig leagues paired to a Novig
    # event) below which we force a re-scrape regardless of dump age.
    _NOVIG_COVERAGE_MIN = 0.5

    def _novig_coverage_ok(self) -> bool:
        """Return True if the existing match map covers enough of the
        currently-loaded PX events. Used to bypass the time TTL when the
        dump is fresh-by-clock but stale-by-content (e.g. new games
        scheduled since the last scrape)."""
        if not self.all_events:
            # Nothing to compare against yet — defer to the time TTL.
            return True
        # PX labels non-team sports by tournament name ("French Open (M)"),
        # not league code, so compare the canonical sport bucket against
        # the set of buckets Novig covers rather than the raw string.
        try:
            from exchange_market_keys import canonical_sport
            covered = {canonical_sport(lg, None) for lg in self._NOVIG_LEAGUES}
        except Exception:
            covered, canonical_sport = self._NOVIG_LEAGUES, None
        eligible = []
        for eid, ev in self.all_events.items():
            meta = ev.get("event_metadata") or {}
            if canonical_sport is not None:
                bucket = canonical_sport(meta.get("tournament"),
                                         meta.get("sport"))
            else:
                bucket = meta.get("tournament")
            if bucket in covered:
                eligible.append(eid)
        if not eligible:
            return True
        if not self._novig_event_map:
            return False
        matched = sum(1 for eid in eligible
                      if str(eid) in self._novig_event_map)
        return (matched / len(eligible)) >= self._NOVIG_COVERAGE_MIN

    def _kickoffNovigDumpRefreshIfStale(self) -> None:
        """Start a NovigDumpWorker unless a fresh-enough dump already
        exists. Non-blocking: the widget continues to initialize and
        show PX data; the match map updates once the worker emits."""
        import time as _t
        try:
            from NovigClient import NOVIG_DUMP_DIR
        except Exception:
            # Can't evaluate staleness — release the gate so we don't hang
            # waiting on a scrape that will never run.
            self._dump_ready = True
            self._maybePopulateInitialEvents()
            return

        # Skip if we already have a fresh dump and the match map is
        # non-empty (no need to re-scrape just to reload the same data).
        if NOVIG_DUMP_DIR.exists():
            files = sorted(NOVIG_DUMP_DIR.glob("all_events_combined_*.json"),
                           key=lambda p: p.stat().st_mtime)
            if files:
                age = _t.time() - files[-1].stat().st_mtime
                if (age < self._NOVIG_DUMP_FRESH_SECONDS
                        and self._novig_event_map
                        and self._novig_coverage_ok()):
                    # On-disk dump is current; the first (index) match map
                    # already reflects it. Open the gate now — no scrape.
                    self._dump_ready = True
                    self._maybePopulateInitialEvents()
                    return

        if self._novig_dump_worker is not None and self._novig_dump_worker.isRunning():
            return  # already scraping (its completion will release the gate)

        # A scrape is needed. Before the first paint, hold the gate closed so
        # the loading overlay persists through it (the dump is the slow step);
        # _onNovigDumpReady / _onNovigDumpFailed reopen it when the scrape ends.
        if not self._initial_events_populated:
            self._dump_ready = False

        print("[LiquidityWidget] Starting Novig dump worker (async)...")
        worker = NovigDumpWorker(parent=self)
        worker.dump_ready.connect(self._onNovigDumpReady)
        worker.dump_failed.connect(self._onNovigDumpFailed)
        self._novig_dump_worker = worker
        worker.start()

    def onProphetxDataRefreshed(self) -> None:
        """Called by the parent after fresh PX data has been written to
        self.all_events. Rebuilds the match map against the latest PX
        event set, then re-evaluates Novig dump staleness — so a clock-
        fresh-but-content-stale dump gets refreshed when new PX events
        appear unmatched."""
        self._loadNovigMatchMap()
        self._kickoffNovigDumpRefreshIfStale()

    def load_fresh_prophetx_dump(self, all_markets: dict) -> None:
        """Single entry point for a freshly-scraped full PX dump (called by
        EffortOdds on startup + manual refresh).

        On the FIRST load this feeds the dump through the gated coordinator:
        the event list is NOT painted and the loading overlay is NOT dropped
        until the Novig event list + match map + (re)scraped NV dump are all
        in. The coordinator then paints once — already combined — and
        auto-opens the top event itself (see _autoSelectTopEvent). This is
        what makes startup a single 'one fell swoop' load instead of a
        PX-only paint followed by a matched repaint. Later loads (after the
        initial build) repopulate normally."""
        self.all_events = all_markets
        self._px_data_ready = True
        # Fresh dump in hand — release the pending-fetch hold on the gate.
        self._pending_fresh_fetch = False
        # Rebuild the match map against the fresh PX set + re-evaluate NV dump
        # staleness (a newly-listed PX event may be unmatched -> re-scrape).
        self.onProphetxDataRefreshed()
        if self._initial_events_populated:
            self._populateEventListOnly()
            self.hideLoading()
        else:
            # Gated: paints + reveals only once every source is ready.
            self._maybePopulateInitialEvents()

    def _onNovigDumpReady(self, n_events: int) -> None:
        """Slot fired when the async Novig scrape completes. Rebuilds
        the match map from the freshly-written dump; _loadNovigMatchMap
        itself re-renders the displayed market if one is selected."""
        print(f"[LiquidityWidget] Novig dump ready: {n_events} events. "
              f"Rebuilding match map...")
        # The dump is now current. Force the initial-populate gate to wait
        # for the map rebuilt from THIS dump (not the earlier stale-index
        # one) so the dropdown's first paint reflects final pairing.
        self._dump_ready = True
        self._match_map_ready = False
        self._loadNovigMatchMap()
        print(f"[LiquidityWidget] Match map built: "
              f"{len(self._novig_event_map)} paired events")

    def _rerenderCurrentMarketForMatchMap(self) -> None:
        """Re-render the currently selected market after the Novig match
        map has been (re)built. A market that first rendered PX-only —
        because the match map was still empty when it was selected —
        picks up its Novig side here without the user re-selecting the
        event. No-op when no market is selected yet."""
        combo = getattr(self, "market_combo", None)
        if combo is None:
            return
        idx = combo.currentIndex()
        if idx >= 0:
            self.onMarketSelected(idx)

    def _onScannerToggled(self, opened: bool) -> None:
        """When the order book's EV-scan panel opens, hand it the current
        MLB events plus the Novig match map so it can run per-event SGP
        scans on both exchanges. Closing the panel needs no action."""
        if not opened:
            return
        mlb_events = [
            (eid, ev) for eid, ev in self.all_events.items()
            if (ev.get("event_metadata") or {}).get("tournament") == "MLB"
        ]
        self.orderbook.sgp_panel.set_events(
            mlb_events, self._novig_event_map)

    def _onNovigDumpFailed(self, err: str) -> None:
        # Quiet failure — widget stays PX-only, user can retry by
        # restarting EffortOdds or running the scraper manually.
        print(f"[LiquidityWidget] Novig dump refresh failed: {err}")
        # Token expiry used to fail SILENTLY here: the scrape errored, the
        # widget kept serving the last good (stale) dump, and daily slates
        # like MLB vanished while multi-day events still matched — looking
        # like a matching bug rather than dead auth. Detect that case and say
        # so loudly, with the one-time fix.
        try:
            from NovigClient import novig_token_status
            st = novig_token_status()
            if st["expired"] or not st["has_token"]:
                print("[LiquidityWidget] >>> Novig access token is EXPIRED "
                      f"(since {st['exp_iso']}). Novig data is STALE. Fix: "
                      "paste a fresh access token into NOVIG_AUTH_TOKEN in "
                      "Creds.py (novig.com -> DevTools -> Network filter "
                      "'token' -> the auth.novig.us/oauth/token response -> "
                      "copy access_token). There is no browserless auto-"
                      "refresh — Auth0 enforces MFA. See NovigClient.py.")
        except Exception:
            pass
        # Don't wedge the initial-populate gate on a failed scrape: release
        # it with whatever data we already have (the stale-index match map
        # from the first build is still in place).
        self._dump_ready = True
        self._maybePopulateInitialEvents()

    def _loadNovigEvents(self) -> None:
        """Kick off the live Novig event-list fetch (off-thread)."""
        if (self._nv_events_worker is not None
                and self._nv_events_worker.isRunning()):
            return
        worker = NovigEventsWorker(parent=self)
        worker.events_ready.connect(self._onNvEventsReady)
        self._nv_events_worker = worker
        worker.start()

    def _onNvEventsReady(self, events: dict) -> None:
        """Store the live Novig event list and rebuild the dropdown so the
        NV-only events appear. Paired events are dropped from the NV set in
        _mergeNvEvents — they already show under their ProphetX entry."""
        self._nv_events = events or {}
        self._nv_events_worker = None
        # Mark NV ready even on an empty result so the gate can still open
        # (PX-only is a valid combined state — Novig simply listed nothing).
        self._nv_data_ready = True
        if not self._initial_events_populated:
            self._maybePopulateInitialEvents()
            return
        if not self._nv_events:
            return
        # Rebuild the list (merge happens inside the populate path). Preserve
        # the user's current selection across the rebuild.
        sel = self.current_event_id
        if self.compact_mode:
            self._populateEventListOnly()
        else:
            self.populateEventList()
        self._reselectEventById(sel)

    def _mergeNvEvents(self) -> None:
        """Fold the live Novig events into all_events (the dropdown's source).
        Skips events that paired with ProphetX — those already show as [PX/NV]
        under their PX entry. Called at the top of the list-build path so it
        survives ProphetX overwriting all_events on each refresh."""
        if not self._nv_events:
            return
        paired_nv = {ep.event_b.source_event_id
                     for ep in self._novig_event_map.values()}
        for eid, entry in self._nv_events.items():
            if eid in paired_nv:
                self.all_events.pop(eid, None)
            elif eid not in self.all_events:
                self.all_events[eid] = entry

    def _reselectEventById(self, event_id) -> None:
        """Re-select an event by id after a list rebuild without firing the
        selection handler (the caller re-renders if needed)."""
        if event_id is None:
            return
        target = str(event_id)
        if self.compact_mode:
            combo = getattr(self, "event_combo", None)
            if combo is None:
                return
            combo.blockSignals(True)
            try:
                for i in range(combo.count()):
                    ev = combo.itemData(i)
                    if ev and str((ev.get("metadata") or {}).get("id")) == target:
                        combo.setCurrentIndex(i)
                        break
            finally:
                combo.blockSignals(False)

    def _loadNovigMatchMap(self) -> None:
        """Kick off an off-thread build of the {prophetx_event_id_str:
        EventPair} lookup used by the event-selection handler.

        The actual load+normalize+match (a ~270-570ms GIL-bound burst on
        startup) runs in MatchMapWorker; the result is applied on the main
        thread in _onMatchMapReady. Silent no-op semantics are preserved —
        an absent dump or failed build emits an empty list and the widget
        keeps working as a PX-only viewer.

        Calls that arrive while a build is in flight set a pending flag
        instead of spawning a second thread; one final rebuild then runs
        with the freshest data once the current one finishes (covers the
        startup case where the refresh slot and the dump-ready slot both
        request a build moments apart)."""
        if self._match_map_worker is not None and self._match_map_worker.isRunning():
            self._match_map_pending = True
            return

        # A rebuild means a (possibly) fresh dump on disk — drop the cached
        # full dump and per-event hydration markers so the next event opened
        # re-reads current market data rather than a stale snapshot.
        self._novig_full_dump_cache = None
        self._hydrated_event_ids.clear()

        # Snapshot the PX side now (main thread) so the worker reads a
        # stable dict. self.all_events is preferred; the worker falls back
        # to the latest on-disk dump if it's empty (very early init).
        worker = MatchMapWorker(self.all_events, parent=self)
        worker.map_ready.connect(self._onMatchMapReady)
        self._match_map_worker = worker
        worker.start()

    def _onMatchMapReady(self, pairs: list) -> None:
        """Main-thread slot: apply the freshly-built match map and re-render
        the current market. Runs the only widget-touching part of the old
        _loadNovigMatchMap; everything before this happened off-thread."""
        if pairs:
            self._novig_event_map = {
                ep.event_a.source_event_id: ep for ep in pairs}
            # Before the initial gated build there's no list to restamp —
            # the coordinator below will build it once with correct badges.
            # After it, restamp in place (a periodic dump refresh rebuilt the
            # map) without a full repopulate.
            # Both follow-ups are deferred one loop pass each (singleShot(0)
            # fires FIFO, so badges still precede the re-render): running
            # restamp + full market re-render synchronously here made this
            # one slot routinely blow the ~120ms frame budget. Each callback
            # re-reads live state at fire time, so the deferral can't render
            # stale data.
            if self._initial_events_populated:
                QTimer.singleShot(0, self._refreshEventSourceBadges)
                # A rebuilt map may now match the market the user is already
                # looking at — re-render so the dual-source view engages
                # without a manual re-selection. On the FIRST build this is
                # unnecessary (and, deferred, would double-render): the gated
                # initial populate below auto-opens the top event with the
                # map already in place, so its render is already dual-source.
                QTimer.singleShot(0, self._rerenderCurrentMarketForMatchMap)
        # else: empty result — keep any existing map, stay PX-only.

        # Match map is in (even when empty): release the initial-populate
        # gate. The first build needs it so paired events are deduped and
        # badged [PX/NV] on the very first paint.
        self._match_map_ready = True
        self._maybePopulateInitialEvents()

        self._match_map_worker = None
        # A request arrived mid-build; run one more pass with current data.
        if self._match_map_pending:
            self._match_map_pending = False
            self._loadNovigMatchMap()
            return

        # [PERF-DIAG] One-shot startup dump-refresh, chained here so it runs
        # AFTER the 38MB parse (map now populated) instead of overlapping it on
        # a +350ms timer. _kickoffNovigDumpRefreshIfStale's fresh-dump skip now
        # works, so a normal restart with a recent dump won't re-scrape 38MB.
        if not getattr(self, "_dump_refresh_kicked", False):
            self._dump_refresh_kicked = True
            self._kickoffNovigDumpRefreshIfStale()

    def initUI(self):
        main_layout = QVBoxLayout(self)
        margins = 2 if self.compact_mode else 0
        main_layout.setContentsMargins(margins, margins, margins, margins)
        main_layout.setSpacing(0)

        if self.compact_mode:
            # Compact mode: only show event/market selector and orderbook
            top_panel = self.createCompactOrderBookPanel()
            main_layout.addWidget(top_panel)
        else:
            # Full mode: show everything
            # Top section: Market selector and order book
            top_panel = self.createOrderBookPanel()
            main_layout.addWidget(top_panel, 3)  # Give more space to order book

            # Bottom section: Event browser
            bottom_panel = self.createEventBrowserPanel()
            main_layout.addWidget(bottom_panel, 1)  # Give less space to event list

        # Don't auto-load stale data - instead request fresh fetch
        # The parent widget should connect to refresh_all_requested and trigger a fresh scrape
        # For now, load stale data as fallback but mark as pending refresh
        self._loadInitialData()

    def createOrderBookPanel(self):
        """Create top panel with event header, market selector and order book"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Event info header
        self.event_header = QLabel("Select an event to view markets")
        self.event_header.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        self.event_header.setStyleSheet("""
            QLabel {
                background-color: #1a1d24;
                color: #ffffff;
                padding: 15px;
                border-bottom: 2px solid #4a9eff;
            }
        """)
        layout.addWidget(self.event_header)

        # Market selector
        market_row = QHBoxLayout()
        market_label = QLabel("Market:")
        market_label.setStyleSheet("color: #8a92a3; padding: 5px;")

        self.market_combo = QComboBox()
        self.market_combo.currentIndexChanged.connect(self.onMarketSelected)
        self.market_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 4px;
                padding: 8px;
                color: #ffffff;
                min-width: 200px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                color: #ffffff;
            }
        """)

        market_row.addWidget(market_label)
        market_row.addWidget(self.market_combo, 1)
        market_row.addStretch()

        market_container = QWidget()
        market_container.setLayout(market_row)
        market_container.setStyleSheet("background-color: #0d0f14; padding: 10px;")
        layout.addWidget(market_container)

        # Order book widget (shows all lines automatically)
        self.orderbook = OrderBookWidget()
        self.orderbook.scannerToggled.connect(self._onScannerToggled)
        layout.addWidget(self.orderbook, 1)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def createCompactOrderBookPanel(self):
        """Create compact panel for terminal integration - event/market selector + orderbook only"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(1)

        # Create horizontal layout for side-by-side dropdowns
        dropdown_row = QHBoxLayout()
        dropdown_row.setSpacing(4)

        # Compact event selector
        self.event_combo = QComboBox()
        self.event_combo.currentIndexChanged.connect(self.onCompactEventSelected)
        # NOTE: no `color:` rule on QComboBox QAbstractItemView — that
        # selector overrides per-item Qt.ItemDataRole.ForegroundRole,
        # which is what refreshCompactEventCombo() uses to tint rows
        # by source (PX = mint, NV = blue, BOTH = neutral). The
        # closed-state `QComboBox { color: #ffffff }` only affects the
        # currently-selected line in the collapsed combo.
        self.event_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 2px;
                padding: 2px 4px;
                color: #ffffff;
                font-size: 9px;
                min-height: 18px;
                max-height: 22px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                width: 15px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                font-size: 9px;
            }
        """)
        dropdown_row.addWidget(self.event_combo, 1)

        # Compact market selector
        self.market_combo = QComboBox()
        self.market_combo.currentIndexChanged.connect(self.onMarketSelected)
        self.market_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                border-radius: 2px;
                padding: 2px 4px;
                color: #ffffff;
                font-size: 9px;
                min-height: 18px;
                max-height: 22px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                width: 15px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1d24;
                border: 1px solid #2a2d34;
                selection-background-color: #2a4a7a;
                color: #ffffff;
                font-size: 8px;
            }
        """)
        dropdown_row.addWidget(self.market_combo, 1)

        layout.addLayout(dropdown_row)

        # Order book widget in compact mode
        self.orderbook = OrderBookWidget(compact_mode=True)
        self.orderbook.scannerToggled.connect(self._onScannerToggled)
        layout.addWidget(self.orderbook, 1)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def createEventBrowserPanel(self):
        """Create bottom panel with event search and list"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # Title bar with search
        title_bar = QWidget()
        title_bar.setStyleSheet("background-color: #1a1d24; border-top: 2px solid #2a2d34;")
        title_layout = QHBoxLayout(title_bar)

        title = QLabel("ProphetX Events")
        title.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; padding: 8px;")
        title_layout.addWidget(title)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter events...")
        self.search_input.textChanged.connect(self.filterEvents)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #0d0f14;
                border: 1px solid #2a2d34;
                border-radius: 4px;
                padding: 6px;
                color: #ffffff;
                max-width: 300px;
            }
            QLineEdit:focus {
                border: 1px solid #4a9eff;
            }
        """)
        title_layout.addWidget(self.search_input)

        # Stats label
        self.stats_label = QLabel("No events loaded")
        self.stats_label.setStyleSheet("color: #8a92a3; padding: 8px; font-size: 11px;")
        title_layout.addWidget(self.stats_label)
        title_layout.addStretch()

        layout.addWidget(title_bar)

        # Event list (horizontal scrolling)
        self.event_list = QListWidget()
        self.event_list.itemClicked.connect(self.onEventSelected)
        self.event_list.setStyleSheet("""
            QListWidget {
                background-color: #0d0f14;
                border: none;
                color: #ffffff;
                outline: none;
            }
            QListWidget::item {
                padding: 10px;
                border-bottom: 1px solid #1a1d24;
            }
            QListWidget::item:hover {
                background-color: #1a1d24;
            }
            QListWidget::item:selected {
                background-color: #2a4a7a;
            }
        """)
        layout.addWidget(self.event_list)

        panel.setStyleSheet("background-color: #0d0f14;")
        return panel

    def _loadInitialData(self):
        """
        Initial data load on startup.
        Shows loading animation and requests fresh data fetch.
        Falls back to stale JSON if available while waiting.
        """
        # Show loading state immediately
        self.showLoading("Initializing ProphetX...")
        self._pending_fresh_fetch = True

        # Load stale data as fallback (marks PX ready; the dropdown build is
        # gated on NV + match map via the coordinator below).
        self._loadStaleDataAsFallback()

        # Watchdog: never leave the dropdown empty if the NV list or match
        # map are slow/fail. 4s comfortably clears the 250/300ms Novig
        # bootstrap + its network round-trip in the common case.
        QTimer.singleShot(4000, self._forceInitialPopulate)

        # Request fresh data fetch - parent widget should connect to this signal.
        # Delayed to 1.5s as part of startup staggering: the full FetchAllEventsAsync
        # scrape + orjson parse of ~28 events is a ~0.5s loop burst, so we let the
        # window paint and the ticker start scrolling first. Stale data (loaded above)
        # keeps the event list populated in the meantime.
        QTimer.singleShot(1500, self._requestFreshData)

    def _requestFreshData(self):
        """Emit signal to request fresh data fetch"""
        self.refresh_all_requested.emit()

    # ------------------------------------------------------------------
    # Single initial-populate coordinator
    # ------------------------------------------------------------------
    def _maybePopulateInitialEvents(self) -> None:
        """Run the FIRST event-list build only once PX events, the NV event
        list, and the match map are all ready — so the dropdown paints once,
        already combined and deduped, instead of flipping PX-only ->
        combined. After the first build this is inert; callers fall back to
        their normal repopulate path (see the `_initial_events_populated`
        guards at each hook)."""
        if self._initial_events_populated:
            return
        # _pending_fresh_fetch: the stale-JSON fallback sets _px_data_ready
        # immediately, which used to open the gate with stale data — the book
        # painted, then the deferred fresh scrape re-covered it with the
        # loading overlay and repainted everything (the visible populate ->
        # loading -> repopulate cycle). Hold the gate until the fresh dump
        # lands (or the watchdog gives up on it).
        if self._pending_fresh_fetch:
            return
        if not (self._px_data_ready and self._nv_data_ready
                and self._match_map_ready and self._dump_ready):
            return
        self._initial_events_populated = True
        self._populateEventListOnly()
        # All sources are in and the menu is painted once. Auto-open the top
        # event from the dump in hand, then let _gateOrderbookReveal drop the
        # overlay — immediately for a PX-only top event, or once the matched
        # event's Novig book depth lands, so the first frame is the finished
        # combined view rather than a list+book that keep restructuring.
        self._autoSelectTopEvent()

    def _autoSelectTopEvent(self) -> None:
        """Open the highest-volume event after the initial gated populate so
        the orderbook renders on first load — straight from the dump already
        in hand, no per-event refetch. Reveal is delegated to
        _gateOrderbookReveal. No-op (just reveals) if the user already has a
        selection or there are no events."""
        if self.current_event_id is not None or not self.filtered_events:
            self.hideLoading()
            return
        top = self.filtered_events[0]
        self.current_event_data = top['data']
        self.current_event_id = top['metadata'].get('id')
        if self.compact_mode:
            self.event_combo.blockSignals(True)
            self.event_combo.setCurrentIndex(0)
            self.event_combo.blockSignals(False)
        else:
            if self.event_list.count() > 0:
                self.event_list.blockSignals(True)
                self.event_list.setCurrentRow(0)
                self.event_list.blockSignals(False)
            name = top['metadata'].get('name', 'Unknown Event')
            dt = _parse_event_start(top['metadata'].get('startTime'))
            if dt is not None:
                name += f" • {dt.strftime('%b %d, %Y %I:%M %p')}"
            self.event_header.setText(name)
        # Renders the orderbook from the dump (and fires the NV /book/batch
        # refresh for a matched event) without a per-event refetch.
        self.populateMarketSelector()
        # Tell the worker which event to track for the periodic refresh.
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)
        self._gateOrderbookReveal()

    def _forceInitialPopulate(self) -> None:
        """Watchdog: ensure the dropdown is never stuck empty if a Novig input
        is slow or fails — WITHOUT cutting the intended single-load short. If
        PX isn't in yet or a Novig dump scrape is genuinely still running, the
        gate is being held on purpose, so reschedule rather than force an
        early PX-only paint. A bounded retry count (~24s total) still forces a
        build if a scrape hangs or the NV list/map never arrive."""
        if self._initial_events_populated:
            return
        self._force_populate_attempts += 1
        scraping = (self._novig_dump_worker is not None
                    and self._novig_dump_worker.isRunning())
        # An in-flight fresh PX fetch holds the gate on purpose (see
        # _maybePopulateInitialEvents) — wait it out like a running scrape.
        if ((not self._px_data_ready or scraping
                or self._pending_fresh_fetch)
                and self._force_populate_attempts <= 6):
            QTimer.singleShot(4000, self._forceInitialPopulate)
            return
        if not self._px_data_ready:
            return  # no PX data at all — nothing to paint yet
        # Treat the missing inputs as "done" so the coordinator proceeds.
        self._nv_data_ready = True
        self._match_map_ready = True
        self._dump_ready = True
        self._pending_fresh_fetch = False
        self._maybePopulateInitialEvents()

    def _loadStaleDataAsFallback(self):
        """Load stale JSON data to populate event list (but don't populate orderbook yet)"""
        dump_dir = Path.cwd() / "prophetx_dumps"
        if not dump_dir.exists():
            return

        # Find latest combined data file
        combined_files = list(dump_dir.glob("all_markets_combined_*.json"))
        if not combined_files:
            return

        latest_file = max(combined_files, key=lambda p: p.stat().st_mtime)
        self._loadDataFromFileWithoutOrderbook(latest_file)

    def _loadDataFromFileWithoutOrderbook(self, filepath: Path):
        """Load ProphetX data from JSON file but don't populate orderbook (it's loading)"""
        try:
            with open(filepath, 'r') as f:
                self.all_events = json.load(f)

            # PX side is in. Defer the actual build to the coordinator so we
            # paint once both sources are ready (or the watchdog fires). Once
            # the initial build has happened, this is a normal refresh.
            self._px_data_ready = True
            if self._initial_events_populated:
                self._populateEventListOnly()
            else:
                self._maybePopulateInitialEvents()

        except Exception as e:
            print(f"Error loading fallback data: {e}")

    def _populateEventListOnly(self):
        """Populate only the event selector/list without touching the orderbook"""
        self._mergeNvEvents()
        self.filtered_events = []

        for event_id, event_data in self.all_events.items():
            metadata = event_data.get('event_metadata', {})

            if not _event_passes_volume_filter(metadata):
                continue

            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', 'Unknown')
            stake = metadata.get('stake', 0)
            tournament = metadata.get('tournament', '')

            source = self._event_source_for(event_id)
            badge = _SOURCE_BADGES.get(source, "")
            display_text = f"{badge}{event_name}\n{sport}"
            if tournament:
                display_text += f" • {tournament}"
            display_text += f" • ${stake:,.0f} volume"

            self.filtered_events.append({
                'id': event_id,
                'metadata': metadata,
                'data': event_data,
                'display': display_text,
                'source': source,
            })

        # Sort games first (by volume desc), then futures (Championship
        # Winner / MVP / etc.) below them — the dropdown drops in a "FUTURES"
        # separator at the boundary. event_type=="Future" is the Novig flag;
        # ProphetX events lack it and read as games.
        self.filtered_events.sort(key=lambda x: (
            (x['metadata'].get('event_type') == 'Future'),
            -float(x['metadata'].get('stake') or 0.0),
        ))

        # Update UI based on mode
        if self.compact_mode:
            self.refreshCompactEventCombo()
        else:
            self.refreshEventList()
            total_events = len(self.filtered_events)
            total_stake = sum(e['metadata'].get('stake', 0) for e in self.filtered_events)
            self.stats_label.setText(f"{total_events} events • ${total_stake:,.0f} total volume")

    def showLoading(self, status_text: str = "Fetching live orderbook..."):
        """Show loading animation on the orderbook"""
        if hasattr(self, 'orderbook'):
            self.orderbook.showLoading(status_text)
        self.loading_state_changed.emit(True)

    def hideLoading(self):
        """Hide loading animation on the orderbook"""
        if hasattr(self, 'orderbook'):
            self.orderbook.hideLoading()
        self._pending_fresh_fetch = False
        self._initial_load_complete = True
        self.loading_state_changed.emit(False)

    def _gateOrderbookReveal(self):
        """Reveal the freshly-rendered orderbook only once it has settled
        into its FINAL form, so the post-data rebuild/resize (match-map
        dual render + collapsed alt-line config + async NV depth) happens
        behind the loading overlay instead of on screen.

        For a Novig-matched event the reveal is driven by the NV book
        WORKER FINISHING (_reapNovigBookWorker) — that's when the dual book
        is in its final form. The timer here is only a safety net for a hung
        worker; it must sit comfortably ABOVE the typical 2-3s /book/batch
        latency, otherwise it reveals the pre-book PX-only render early (the
        original 2s value did exactly that). PX-only events have no async
        second act, so they reveal immediately."""
        self._reveal_gen += 1
        eid = str(self.current_event_id or "")
        has_match = bool(self._novig_event_map and eid in self._novig_event_map)
        if not has_match:
            self.hideLoading()
            return
        gen = self._reveal_gen
        QTimer.singleShot(9000, lambda g=gen: self._revealIfCurrent(g))

    def _revealIfCurrent(self, gen: int):
        """Hide the overlay iff this is still the active load (no newer
        selection has superseded it) and we're actually still loading."""
        if gen == self._reveal_gen and getattr(self.orderbook, "is_loading",
                                               False):
            self.hideLoading()

    # populateEventList and _populateEventListOnly built the dropdown
    # identically; they're now one implementation. The name is retained for
    # the call sites that read as "(re)build the event list".
    populateEventList = _populateEventListOnly

    def _event_source_for(self, event_id) -> str:
        """Classify an event as 'PX', 'NV', or 'BOTH'.
        - NV: a Novig-native event merged in by _mergeNvEvents (ProphetX
          doesn't list it).
        - BOTH: a ProphetX event that paired with a Novig event.
        - PX: ProphetX only."""
        sid = str(event_id)
        if sid in self._nv_events:
            return "NV"
        has_nv = bool(self._novig_event_map
                      and sid in self._novig_event_map)
        return "BOTH" if has_nv else "PX"

    def _refreshEventSourceBadges(self) -> None:
        """Re-classify every listed event against the current Novig match
        map and update its [PX]/[PX/NV] badge IN PLACE.

        The event list/combo is populated before the async match map is
        built (_loadNovigMatchMap runs on a worker thread), so all rows
        are first badged [PX]. When _onMatchMapReady lands the map we
        must restamp the badges — but WITHOUT clear()+repopulate, which
        would reset the user's current selection and re-fire the
        orderbook render. So we walk the existing items and swap only the
        leading badge token, leaving selection and ordering untouched."""
        known = tuple(_SOURCE_BADGES.values())

        def _swap(text: str, source: str) -> str:
            for b in known:
                if text.startswith(b):
                    text = text[len(b):]
                    break
            return f"{_SOURCE_BADGES.get(source, '')}{text}"

        # Keep the cached filtered_events source/display in sync so a
        # later full repopulate (refreshEventList) starts from truth.
        for ev in getattr(self, "filtered_events", None) or []:
            source = self._event_source_for(ev['id'])
            ev['source'] = source
            if isinstance(ev.get('display'), str):
                ev['display'] = _swap(ev['display'], source)

        # Compact combo path.
        combo = getattr(self, "event_combo", None)
        if combo is not None and combo.count():
            combo.blockSignals(True)
            combo.setUpdatesEnabled(False)
            try:
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    eid = data.get('id') if isinstance(data, dict) else None
                    if eid is None:
                        continue
                    source = self._event_source_for(eid)
                    # Badge + role depend only on the source; skip the Qt
                    # model writes (each fires dataChanged) for the vast
                    # majority of rows whose source didn't change. This
                    # restamp runs on every map rebuild and was a ~130ms
                    # slice when it rewrote every row unconditionally.
                    if combo.itemData(i, _EVENT_SOURCE_ROLE) == source:
                        continue
                    combo.setItemText(i, _swap(combo.itemText(i), source))
                    combo.setItemData(i, source, _EVENT_SOURCE_ROLE)
            finally:
                combo.setUpdatesEnabled(True)
                combo.blockSignals(False)

        # Full list path.
        lst = getattr(self, "event_list", None)
        if lst is not None and lst.count():
            for i in range(lst.count()):
                item = lst.item(i)
                data = item.data(Qt.ItemDataRole.UserRole)
                eid = data.get('id') if isinstance(data, dict) else None
                if eid is None:
                    continue
                source = self._event_source_for(eid)
                # Same skip as the combo path: text/role/tint all derive
                # from the source, so an unchanged source means nothing
                # to rewrite.
                if item.data(_EVENT_SOURCE_ROLE) == source:
                    continue
                item.setText(_swap(item.text(), source))
                item.setData(_EVENT_SOURCE_ROLE, source)
                if source == "PX":
                    item.setForeground(_PX_COLOR)
                elif source == "NV":
                    item.setForeground(_NV_COLOR)
                else:  # BOTH
                    item.setForeground(QColor(230, 230, 230))

    def refreshEventList(self):
        """Refresh the event list widget display. Source-coloring is
        done by _EventSourceDelegate (left-edge stripe) plus a small
        text badge prefix in event['display'] so the indication
        survives even when the delegate isn't installed (e.g. in the
        compact combo)."""
        self.event_list.clear()
        # Install delegate idempotently — setItemDelegate replaces
        # whatever was there. _EventSourceDelegate is cheap; only
        # paints the stripe and defers everything else.
        if not isinstance(self.event_list.itemDelegate(),
                          _EventSourceDelegate):
            self.event_list.setItemDelegate(
                _EventSourceDelegate(self.event_list))

        futures_separator_added = False
        for event in self.filtered_events:
            is_future = (event['metadata'].get('event_type') == 'Future')
            # filtered_events is sorted games-first, futures last, so the
            # first future marks the boundary. Drop a non-selectable header
            # once (only when games precede it) signalling everything below
            # is a futures/outright market.
            if (is_future and not futures_separator_added
                    and self.event_list.count() > 0):
                sep = QListWidgetItem("──────  FUTURES  ──────")
                sep.setFlags(Qt.ItemFlag.NoItemFlags)
                sep.setForeground(QColor(140, 140, 150))
                sep.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.event_list.addItem(sep)
                futures_separator_added = True

            item = QListWidgetItem(event['display'])
            item.setData(Qt.ItemDataRole.UserRole, event)
            source = event.get('source', 'PX')
            item.setData(_EVENT_SOURCE_ROLE, source)
            # Foreground tint matches the stripe so single-source rows
            # read at a glance. BOTH rows stay neutral so the bicolor
            # stripe is the differentiator.
            if source == "PX":
                item.setForeground(_PX_COLOR)
            elif source == "NV":
                item.setForeground(_NV_COLOR)
            else:  # BOTH
                item.setForeground(QColor(230, 230, 230))

            self.event_list.addItem(item)

    def refreshCompactEventCombo(self):
        """Refresh the compact event combo box. Uses the same
        _EventSourceDelegate as the QListWidget — installed on the
        combo's popup view so the delegate's custom text painting
        wins over the combo's stylesheet (which would otherwise
        force all rows white)."""
        combo = self.event_combo
        # Build the desired rows first. A refresh tick usually changes only
        # the stake figures, so when the row STRUCTURE (event ids + separator
        # position) is unchanged we update texts/data in place instead of
        # clear()+addItem() — no model reset / item churn (the full rebuild
        # of hundreds of rows was a ~140-180ms main-loop stall per refresh-
        # all), and the user's current selection survives untouched. Same
        # pattern as _refreshMarketSelector.
        rows = []  # (key, display, source, event_dict_or_None_for_separator)
        futures_separator_added = False
        for event in self.filtered_events:
            metadata = event['metadata']
            is_future = (metadata.get('event_type') == 'Future')
            # Boundary header between games and futures (see refreshEventList).
            # A disabled item so it can't be selected; onCompactEventSelected
            # already no-ops on items whose data is None.
            if is_future and not futures_separator_added and rows:
                rows.append(("__SEP__", "──────  FUTURES  ──────", None, None))
                futures_separator_added = True

            event_name = metadata.get('name', 'Unknown Event')
            stake = metadata.get('stake', 0)
            source = event.get('source', 'PX')
            badge = _SOURCE_BADGES.get(source, "")
            display = f"{badge}{event_name} (${stake:,.0f})"
            rows.append((str(event['id']), display, source, event))

        # Block signals to prevent auto-selection when populating with stale data
        combo.blockSignals(True)
        combo.setUpdatesEnabled(False)
        try:
            # Install delegate idempotently. setItemDelegate on the combo
            # forwards to its view; we also set it on the view explicitly
            # because some Qt builds need both for the closed-state
            # display + popup-state list to agree.
            view = combo.view()
            if not isinstance(view.itemDelegate(),
                              _EventSourceDelegate):
                delegate = _EventSourceDelegate(view)
                view.setItemDelegate(delegate)
                combo.setItemDelegate(delegate)

            same_structure = combo.count() == len(rows)
            if same_structure:
                for i, (key, _d, _s, _e) in enumerate(rows):
                    data = combo.itemData(i)
                    cur_key = (str(data['id']) if isinstance(data, dict)
                               else "__SEP__")
                    if cur_key != key:
                        same_structure = False
                        break

            if same_structure:
                for i, (key, display, source, event) in enumerate(rows):
                    if event is None:
                        continue  # separator text/style never changes
                    if combo.itemText(i) != display:
                        combo.setItemText(i, display)
                    # Always refresh the stored dict — it carries the fresh
                    # 'data' payload onCompactEventSelected renders from.
                    combo.setItemData(i, event)
                    combo.setItemData(i, source, _EVENT_SOURCE_ROLE)
            else:
                combo.clear()
                for key, display, source, event in rows:
                    combo.addItem(display, event)
                    i = combo.count() - 1
                    if event is None:
                        sep_model_item = combo.model().item(i)
                        if sep_model_item is not None:
                            sep_model_item.setEnabled(False)
                            sep_model_item.setForeground(QColor(140, 140, 150))
                    else:
                        # Stash the source on the same role the delegate reads.
                        combo.setItemData(i, source, _EVENT_SOURCE_ROLE)
                # clear() wiped the selection; point the closed-state display
                # back at the event the user is viewing when it's still
                # listed (signals are blocked — no re-render fires). Falls
                # back to item 0, as before, when it's gone.
                if self.current_event_id is not None:
                    for i in range(combo.count()):
                        data = combo.itemData(i)
                        if (isinstance(data, dict)
                                and str(data.get('id'))
                                    == str(self.current_event_id)):
                            combo.setCurrentIndex(i)
                            break
        finally:
            combo.setUpdatesEnabled(True)
            # Re-enable signals - don't auto-select, wait for fresh data
            combo.blockSignals(False)

    def onCompactEventSelected(self, index: int):
        """Handle event selection in compact mode"""
        if index < 0:
            return

        event = self.event_combo.itemData(index)
        if not event:
            return

        metadata = event['metadata']
        new_event_id = metadata.get('id')

        # Show loading when selecting a different event
        if new_event_id != self.current_event_id:
            event_name = metadata.get('name', 'event')
            self.showLoading(f"Fetching {event_name}...")

        self.current_event_data = event['data']
        self.current_event_id = new_event_id

        # Novig-only event: ProphetX has no worker for it, so fetch its
        # markets live from Novig and render them ourselves.
        if event.get('source') == 'NV':
            self._fetchNvEventMarkets(self.current_event_id)
            return

        # Populate immediately from dump data so market_combo is always in
        # sync with current_event_id. Without this, any match-map callback
        # that fires _rerenderCurrentMarketForMatchMap() during the async PX
        # fetch would re-render the PREVIOUS event's market (still in the
        # combo) and, for PX-only markets, also drop the loading overlay early.
        self.populateMarketSelector()

        # Emit signal for external listeners (e.g., worker to refresh data)
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)

    def filterEvents(self):
        """Filter events based on search text"""
        search_text = self.search_input.text().lower()

        if not search_text:
            self.populateEventList()
            return

        # Filter events
        filtered = []
        for event in self.filtered_events:
            event_name = event['metadata'].get('name', '').lower()
            sport = event['metadata'].get('sport', '').lower()
            tournament = event['metadata'].get('tournament', '').lower()

            if (search_text in event_name or
                search_text in sport or
                search_text in tournament):
                filtered.append(event)

        self.filtered_events = filtered
        self.refreshEventList()

    def onEventSelected(self, item: QListWidgetItem):
        """Handle event selection in full mode"""
        event = item.data(Qt.ItemDataRole.UserRole)
        metadata = event['metadata']
        new_event_id = metadata.get('id')

        # Show loading when selecting a different event
        if new_event_id != self.current_event_id:
            event_name = metadata.get('name', 'event')
            self.showLoading(f"Fetching {event_name}...")

        self.current_event_data = event['data']
        self.current_event_id = new_event_id

        # Update header
        event_name = metadata.get('name', 'Unknown Event')
        dt = _parse_event_start(metadata.get('startTime'))
        if dt is not None:
            event_name += f" • {dt.strftime('%b %d, %Y %I:%M %p')}"

        self.event_header.setText(event_name)

        # Novig-only event: ProphetX has no worker for it, so fetch its
        # markets live from Novig and render them ourselves.
        if event.get('source') == 'NV':
            self._fetchNvEventMarkets(self.current_event_id)
            return

        # Populate immediately from dump data — keeps market_combo in sync
        # with current_event_id so any async callback that fires during the
        # live fetch renders the correct event.
        self.populateMarketSelector()

        # Emit signal for external listeners (e.g., worker to refresh data)
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)

    def populateMarketSelector(self):
        """Initial-load entry point. Resets the diff state so the next
        call rebuilds the combo from scratch, then delegates to the
        shared refresh path."""
        self._last_market_name_order = None
        self._last_selected_market_sig = None
        self._refreshMarketSelector(prev_selected_name=None,
                                    force_orderbook_render=True)

    def _marketDisplayLabel(self, market: Dict, active_liquidity: float) -> str:
        name = market.get('name', 'Unknown')
        if self.compact_mode:
            return f"{name} (${active_liquidity:,.0f})"
        total_stake = market.get('totalStake', 0)
        return (f"{name} (${active_liquidity:,.0f} active • "
                f"${total_stake:,.0f} total)")

    @staticmethod
    def _marketContentSignature(market: Dict) -> str:
        """Compact hash of a market's visible state. Includes line names,
        order display labels, odds, and dollar values — exactly the
        fields the orderbook render reads. If two refreshes produce
        identical signatures for the selected market, the rendered
        view would be identical, so we can skip the render entirely."""
        # Fast path: cache stamped off the main thread (see
        # prophetx_async.precompute_market_caches). Only the displayed
        # market's signature is consulted per refresh, so the cache hit
        # rate here is whatever fetch_prophetx_event produced.
        cached = market.get('_content_signature')
        if cached is not None:
            return cached
        import hashlib
        h = hashlib.blake2b(digest_size=12)
        h.update((market.get('name', '') or '').encode())

        def _hash_orders(side_orders):
            if not side_orders:
                return
            for o in side_orders:
                h.update(b"|")
                h.update(str(o.get('odds', '')).encode())
                h.update(b",")
                h.update(str(o.get('value', '')).encode())
                h.update(b",")
                h.update((o.get('displayName') or '').encode())
                # Include lineID so the v1->v2 transition (v1 lacks the
                # per-order lineID; v2 carries it) bumps the signature
                # and forces a re-render. Without this, fresh v2 data is
                # ingested but the table keeps showing stale v1 order
                # dicts, breaking single-bet placement.
                h.update(b",")
                h.update(str(o.get('lineID') or '').encode())

        market_lines = market.get('marketLines') or []
        if market_lines:
            for ml in market_lines:
                h.update(b"\n")
                h.update((ml.get('name', '') or '').encode())
                for side_orders in ml.get('selections', []) or []:
                    _hash_orders(side_orders)
        else:
            for side_orders in market.get('selections', []) or []:
                _hash_orders(side_orders)
        return h.hexdigest()

    def _refreshMarketSelector(self, prev_selected_name: Optional[str],
                               force_orderbook_render: bool = False):
        """Refresh the market combo + orderbook with minimal work.

        Common case (refresh tick where the same markets are still
        listed in the same order): zero clear/addItem churn — we just
        update the dollar figures via setItemText/setItemData. No
        spurious currentIndexChanged fires from the combo, so the
        orderbook only re-renders if the selected market's actual
        content changed (detected via _marketContentSignature).

        Structural change case (markets added/removed/reordered): full
        rebuild under blockSignals, then a single explicit render call.
        """
        if not self.current_event_data:
            return

        markets = self.current_event_data.get('data', {}).get('markets', [])

        markets_with_liquidity = [
            (m, self.calculateActiveMarketLiquidity(m)) for m in markets
        ]
        markets_with_liquidity.sort(key=lambda x: x[1], reverse=True)

        new_names = [m.get('name', 'Unknown') for m, _ in markets_with_liquidity]
        same_structure = (new_names == self._last_market_name_order)

        self.market_combo.blockSignals(True)
        self.market_combo.setUpdatesEnabled(False)
        try:
            if same_structure:
                # In-place: update display strings + stored market dicts.
                # Combo selection (currentIndex) is preserved automatically.
                for i, (market, liq) in enumerate(markets_with_liquidity):
                    self.market_combo.setItemText(i, self._marketDisplayLabel(market, liq))
                    self.market_combo.setItemData(i, market)
            else:
                self.market_combo.clear()
                for market, liq in markets_with_liquidity:
                    self.market_combo.addItem(self._marketDisplayLabel(market, liq), market)
                # Restore previous selection if the same market is still listed.
                if prev_selected_name is not None:
                    for i in range(self.market_combo.count()):
                        m = self.market_combo.itemData(i)
                        if m and m.get('name') == prev_selected_name:
                            self.market_combo.setCurrentIndex(i)
                            break
                # Structural change invalidates the cached render signature
                # because the previously-selected market may have moved or
                # been replaced.
                self._last_selected_market_sig = None
            self._last_market_name_order = new_names
        finally:
            self.market_combo.setUpdatesEnabled(True)
            self.market_combo.blockSignals(False)

        # Decide whether to re-render the orderbook.
        idx = self.market_combo.currentIndex()
        if idx < 0:
            return
        selected_market = self.market_combo.itemData(idx)
        if selected_market is None:
            return

        new_sig = self._marketContentSignature(selected_market)
        if not force_orderbook_render and new_sig == self._last_selected_market_sig:
            # ProphetX side is byte-identical to the last render, so the
            # PX repaint is skipped. BUT the Novig book is fetched live
            # per /book/batch and has no refresh trigger of its own — it
            # only rode along on a PX-sig change. On a quiet PX market
            # that froze the NV ladder indefinitely. So refresh the NV
            # depth here too, decoupled from PX movement, reusing the
            # 20s ProphetX refresh cadence. Skip if a fetch is already in
            # flight so periodic ticks don't stack workers.
            mp = self._current_market_pair
            if mp is not None:
                w = self._novig_book_worker
                if w is None or not w.isRunning():
                    self._scheduleNovigBookRefresh(mp)
            return
        self._last_selected_market_sig = new_sig

        # Render exactly once via the existing selection handler so the
        # Novig-pair lookup and dual-source render path are exercised
        # the same way as a user-initiated change. onMarketSelected also
        # fires its own _launchNovigBookRefresh, so the NV side refreshes
        # whenever the PX side moves.
        self.onMarketSelected(idx)

    def calculateActiveMarketLiquidity(self, market: Dict) -> float:
        """
        Calculate the total active liquidity currently on the orderbook for a market.
        This sums up all the 'value' fields from unmatched orders.

        Args:
            market: Market dictionary from ProphetX API

        Returns:
            Total active liquidity (sum of all order values)
        """
        # Fast path: cache stamped by prophetx_async.precompute_market_caches
        # off the main thread. Falls back to live compute only for paths
        # that don't go through fetch_prophetx_event (initial JSON-dump
        # loads, manual refreshes).
        cached = market.get('_active_liquidity')
        if cached is not None:
            return cached

        total_active = 0.0

        # For markets with multiple lines (spread/total)
        if 'marketLines' in market and market['marketLines']:
            for market_line in market['marketLines']:
                selections = market_line.get('selections', [])
                # Each selection array is [side1_orders, side2_orders]
                for side_orders in selections:
                    if not side_orders:
                        continue
                    for order in side_orders:
                        total_active += order.get('value', 0)

        # For simple markets (moneyline)
        elif 'selections' in market:
            selections = market.get('selections', [])
            for side_orders in selections:
                if not side_orders:
                    continue
                for order in side_orders:
                    total_active += order.get('value', 0)

        return total_active

    def _fetchNvEventMarkets(self, event_id) -> None:
        """Kick off a live Novig markets fetch for an NV-only event."""
        prev = self._nv_markets_worker
        if prev is not None and prev.isRunning():
            try:
                prev.markets_ready.disconnect()
            except Exception:
                pass
        worker = NovigEventMarketsWorker(str(event_id), parent=self)
        worker.markets_ready.connect(self._onNvMarketsReady)
        self._nv_markets_worker = worker
        worker.start()

    def _onNvMarketsReady(self, event_id: str, nev) -> None:
        """Populate the market selector + orderbook from a live Novig event
        fetch. Drops stale results if the user has navigated away."""
        self._nv_markets_worker = None
        if event_id != str(self.current_event_id or ""):
            return
        markets = list(nev.markets) if nev is not None else []
        markets.sort(key=lambda nm: nm.total_liquidity_usd, reverse=True)
        # Force the PX diff-path to fully rebuild next time a PX event loads
        # (its in-place update assumes PX dict combo items).
        self._last_market_name_order = None
        self._last_selected_market_sig = None
        self.market_combo.blockSignals(True)
        self.market_combo.clear()
        for nm in markets:
            self.market_combo.addItem(
                f"{nm.market_name} (${nm.total_liquidity_usd:,.0f})", nm)
        self.market_combo.blockSignals(False)
        if self.market_combo.count() > 0:
            self.market_combo.setCurrentIndex(0)
            self.onMarketSelected(0)
        else:
            self._current_nv_norm = None
            self.hideLoading()

    def onMarketSelected(self, index: int):
        """Handle market selection - displays all available lines.

        If a matched Novig market exists for the current event, render
        with both sources interleaved + tinted; then fire an async
        /book/batch fetch to refresh Novig depth without blocking the
        UI. Otherwise fall back to the original PX-only rendering.
        """
        if index < 0:
            return

        market = self.market_combo.itemData(index)
        if not market:
            return

        # Novig-only event: the combo holds a NormalizedMarket directly (no PX
        # dict to normalize, no pair to look up). Render through the same
        # pipeline — setMarketDual tints by source, so it shows as [NV].
        try:
            import exchange_market_keys as emk
            if isinstance(market, emk.NormalizedMarket):
                self._current_px_norm = None
                self._current_market_pair = None
                self._current_nv_norm = market
                self.orderbook.setMarketDual(market, None)
                if getattr(self.orderbook, "is_loading", False):
                    self.hideLoading()
                return
        except Exception:
            pass

        market_pair = self._lookupCurrentMarketPair(market)
        self._current_market_pair = market_pair

        # Normalize the selected PX market so the renderer always sees
        # a NormalizedMarket regardless of whether a Novig match exists.
        # setMarketDual falls through to setNormalizedMarket when
        # nv_norm is None, so the PX-only and dual paths share one
        # render pipeline.
        try:
            import exchange_market_keys as emk
            px_norm = emk.from_prophetx_market(
                market,
                event_meta=(self.current_event_data or {}).get("event_metadata") or {},
            )
        except Exception as e:
            print(f"[LiquidityWidget] PX market normalize failed: {e}")
            return

        self._current_px_norm = px_norm
        nv_norm = market_pair.market_b if market_pair is not None else None
        # New market on screen — drop the NV render signature so the first
        # /book/batch result for it always paints (rather than being
        # diffed against the previous market's book).
        self._last_nv_render_sig = None
        # Render immediately with whatever Novig depth is currently
        # cached on the matched NV market (dump-level top-of-book).
        self.orderbook.setMarketDual(px_norm, nv_norm)

        if market_pair is None:
            # PX-only market — no async book to wait on, so this render IS
            # the final form. Reveal now instead of leaving the overlay up
            # for the grace timer.
            if getattr(self.orderbook, "is_loading", False):
                self.hideLoading()
            return

        # Refresh in the background: pull /book/batch for each Novig
        # line under this market, and re-render when results arrive.
        # Deferred one loop pass so this slot ends at the render — the
        # worker launch (and its first-time NovigClient import) gets its
        # own slice instead of extending an already-long selection slot.
        self._scheduleNovigBookRefresh(market_pair)

    def _lookupCurrentMarketPair(self, prophetx_market: Dict):
        """Return the MarketPair for the given raw PX market dict, or
        None if no Novig match exists."""
        if not self._novig_event_map or not self.current_event_id:
            return None
        ep = self._novig_event_map.get(str(self.current_event_id))
        if ep is None:
            return None
        # Lazy market hydration: the match map was built from a metadata-only
        # index, so this pair's market_pairs are empty until we fill in the NV
        # markets from the full dump. Done once per event, on first open.
        self._hydrateEventPairMarkets(ep)
        # Match by ProphetX market id — that's stable across calls.
        px_market_id = str(prophetx_market.get("id") or "")
        if not px_market_id:
            return None
        for mp in ep.market_pairs:
            if mp.market_a.source_market_id == px_market_id:
                return mp
        return None

    def _hydrateEventPairMarkets(self, ep) -> None:
        """Fill in the Novig markets + market pairs for one paired event.

        The startup match map is built from a metadata-only index for speed, so
        every EventPair starts with empty market_pairs. The first time the user
        opens a paired event we read its markets out of the full dump and run
        the deferred market/line pairing. The full dump is parsed at most once
        per map build (cached); each event is hydrated at most once. On any
        failure we still mark it hydrated so we don't thrash on every render —
        the event simply stays PX-only, same as a genuine no-match."""
        eid = getattr(ep.event_b, "source_event_id", None)
        if not eid or eid in self._hydrated_event_ids:
            return
        self._hydrated_event_ids.add(eid)
        # Sidecar first: one ~100KB file per event (<1ms) instead of the
        # ~38MB full-dump parse that stalled the main loop ~175ms on the
        # first paired-event open. ([PERF-DIAG])
        entry = None
        try:
            from NovigClient import NovigQueries
            entry = NovigQueries.load_event_entry(eid)
        except Exception:
            entry = None
        if entry is None:
            # Fallback: dump predates sidecars — parse the full dump once
            # (cached for the lifetime of this match map).
            if self._novig_full_dump_cache is None:
                try:
                    from NovigClient import NovigQueries
                    self._novig_full_dump_cache = (
                        NovigQueries.load_latest_dump() or {})
                except Exception as e:
                    print(f"[LiquidityWidget] full dump load failed: {e!r}")
                    self._novig_full_dump_cache = {}
            entry = self._novig_full_dump_cache.get(eid)
        if not entry:
            return
        try:
            import exchange_market_keys as emk
            emk.hydrate_event_pair_markets(ep, entry, currency="CASH")
        except Exception as e:
            print(f"[LiquidityWidget] market hydration failed for {eid}: {e!r}")

    def _scheduleNovigBookRefresh(self, market_pair) -> None:
        """Queue _launchNovigBookRefresh on the next event-loop pass.

        The launch used to run synchronously at the tail of
        onMarketSelected / the 20s refresh tick — the watchdog kept
        catching ~120-250ms stalls with the stack parked on it because it
        extended a slot that had already spent its frame budget on the
        render. One pass later the loop has painted; the guard drops the
        launch if the user selected a different market in between."""
        def _fire(mp=market_pair):
            if self._current_market_pair is not mp:
                return
            self._launchNovigBookRefresh(mp)
        QTimer.singleShot(0, _fire)

    def _launchNovigBookRefresh(self, market_pair) -> None:
        """Fire a NovigMarketBookWorker for the lines on the matched NV
        market. Books are merged back into the NormalizedMarket on
        completion and the orderbook re-rendered."""
        # Gather Novig line/source ids — each NormalizedLine carries a
        # source_line_id that's the underlying Novig market UUID.
        market_ids: List[str] = [
            ln.source_line_id for ln in market_pair.market_b.lines
            if ln.source_line_id
        ]
        if not market_ids:
            return

        # Stop / drop any previous worker so a stale result can't land
        # after the user has moved on.
        prev = self._novig_book_worker
        if prev is not None and prev.isRunning():
            try:
                prev.books_ready.disconnect()
            except Exception:
                pass

        worker = NovigMarketBookWorker(
            prophetx_event_id=str(self.current_event_id or ""),
            novig_market_ids=market_ids,
            parent=self,
        )
        worker.books_ready.connect(self._onNovigBooksReady)
        # Reap the QThread once it finishes — this path now fires on every
        # 20s refresh tick, so without cleanup the finished workers would
        # accumulate as children of self for the session's lifetime. Clear
        # the handle BEFORE deleteLater so the next tick's isRunning()
        # probe never touches a deleted C++ object.
        worker.finished.connect(lambda w=worker: self._reapNovigBookWorker(w))
        self._novig_book_worker = worker
        worker.start()

    def _reapNovigBookWorker(self, worker) -> None:
        """finished-signal handler: drop our handle (if it's still this
        worker) and schedule the QThread for deletion.

        This also drives the orderbook reveal: the worker's `finished` fires
        AFTER its books_ready slot (_onNovigBooksReady) has re-rendered the
        dual book, so by here the table is in its FINAL combined form. Lift
        the loading overlay now — the old fixed 2s grace timer fired BEFORE
        the 2-3s /book/batch landed, exposing the pre-book PX-only dump
        render and producing the visible PX-only -> combined flip. Guard on
        event id so a superseded selection's late worker can't reveal a load
        the user has navigated away from."""
        if self._novig_book_worker is worker:
            self._novig_book_worker = None
        try:
            if (getattr(self.orderbook, "is_loading", False)
                    and str(getattr(worker, "prophetx_event_id", "")) ==
                        str(self.current_event_id or "")):
                self.hideLoading()
        except Exception:
            pass
        worker.deleteLater()

    def _onNovigBooksReady(self, prophetx_event_id: str, books: dict) -> None:
        """Slot fired when async /book/batch results arrive. Rebuilds
        the NV side of the current NormalizedMarket with full ladder
        depth, then re-renders. Drops stale results (those for an
        event the user has navigated away from)."""
        if not books:
            return
        if prophetx_event_id != str(self.current_event_id or ""):
            return  # user moved on
        mp = self._current_market_pair
        if mp is None or self._current_px_norm is None:
            return

        # Reconstruct the NV side: rebuild each NormalizedLine using the
        # fresh book for that line's source_line_id. The from_novig_event
        # adapter takes a {market_id: book_entry} map.
        try:
            import exchange_market_keys as emk
        except Exception:
            return

        # `mp.market_b.raw` is {"group": [<source novig market dicts>]}
        # set by from_novig_event when it aggregated the group. Rebuild
        # the lines from those raw markets + the fresh books.
        raw_bundle = mp.market_b.raw or {}
        raw_markets = raw_bundle.get("group") if isinstance(raw_bundle, dict) else None
        if not raw_markets:
            return

        new_lines = []
        try:
            for raw_m in raw_markets:
                # `_novig_line_from_market` is a private helper but is
                # public-enough for this purpose. Pass the matched book
                # if available; otherwise top-of-book fallback applies.
                line = emk._novig_line_from_market(
                    raw_m,
                    books.get(raw_m.get("id")),
                    mp.market_b.market_type,
                )
                new_lines.append(line)
        except Exception:
            return

        new_lines.sort(key=lambda ln: (ln.strike is None,
                                       -(ln.strike if ln.strike is not None else 0)))
        refreshed = emk.NormalizedMarket(
            source=mp.market_b.source,
            source_market_id=mp.market_b.source_market_id,
            event_id=mp.market_b.event_id,
            event_name=mp.market_b.event_name,
            market_name=mp.market_b.market_name,
            market_type=mp.market_b.market_type,
            market_subtype=mp.market_b.market_subtype,
            player_name=mp.market_b.player_name,
            lines=new_lines,
            total_liquidity_usd=sum(ln.total_liquidity_usd for ln in new_lines),
            raw=raw_bundle,
        )
        # Replace in the live MarketPair so subsequent renders see the
        # fresh depth (data model stays current even when we skip the paint).
        mp.market_b = refreshed

        # Only repaint when the NV side actually moved — the /book/batch
        # refresh fires every ~20s, and re-rendering an unchanged book just
        # rebuilds the table and re-scales every bar for nothing (the flicker
        # / "bars jump" on a quiet market). Signature = per-line, per-side
        # (american, size) tuples.
        sig = tuple(
            (ln.strike,
             tuple((s.label,
                    tuple((o.american, round(o.size_usd, 1)) for o in s.orders))
                   for s in ln.sides))
            for ln in new_lines)
        if sig != self._last_nv_render_sig:
            self._last_nv_render_sig = sig
            self.orderbook.setMarketDual(self._current_px_norm, refreshed)

        # If a load was gated waiting on this NV depth, reveal now that the
        # book is in its final form (deferred a tick so the paint lands
        # first). Harmless no-op on the periodic 20s refresh ticks.
        if getattr(self.orderbook, "is_loading", False):
            QTimer.singleShot(0, self.hideLoading)

    def updateEventMarkets(self, markets_data: Dict):
        """
        Update the current event with fresh markets data from async worker.
        This is called when fresh data arrives - hides loading and displays fresh orderbook.

        Args:
            markets_data: Fresh markets data from ProphetXQueryAsync
        """
        if not markets_data:
            # Data fetch failed - hide loading but show error state
            self.hideLoading()
            return

        # If no event is selected yet, this might be the initial load
        # In that case, also update the event list with fresh data
        if not self.current_event_id:
            # This is fresh data for potentially multiple events
            # Update all_events if we received a full dump
            if isinstance(markets_data, dict) and 'data' not in markets_data:
                # This looks like a full events dump
                self.all_events = markets_data
                # Gate the first build on NV + match map + dump; afterwards
                # this is a normal (already-combined) refresh.
                self._px_data_ready = True
                self._pending_fresh_fetch = False
                if self._initial_events_populated:
                    self._populateEventListOnly()
                    self.hideLoading()
                else:
                    # Keep the loading overlay up — the coordinator hides it
                    # once every source is in and the menu paints once.
                    self._maybePopulateInitialEvents()
            else:
                self.hideLoading()
            return

        # Store currently selected market name to restore after refresh
        current_market_name = None
        current_index = self.market_combo.currentIndex()
        if current_index >= 0:
            current_market = self.market_combo.itemData(current_index)
            if isinstance(current_market, dict):
                # ProphetX market — plain dump dict.
                current_market_name = current_market.get('name')
            elif current_market is not None:
                # NV-native market — a NormalizedMarket object (added by
                # _onNvMarketsReady), which has no .get(); read its name attr.
                current_market_name = getattr(
                    current_market, 'market_name', None)

        # Update the stored data for this event.
        #
        # The per-event refresh response (GetEventMarketsAsync) carries
        # only `data` — no `event_metadata`. Overwriting the full-dump
        # entry with it blind strips the teams / league / start time
        # that the Novig match map relies on: from_prophetx_event would
        # then produce an empty source_event_id and no team symbols, so
        # match_events can't pair the event and it silently drops out of
        # the dual-source view.
        #
        # Two further hazards: ProphetX event ids are ints in-memory but
        # become strings once a dump round-trips through JSON, and the
        # old code always keyed by str(...) — which either collided with
        # and clobbered the rich entry (string keys) or left a stale
        # duplicate (int keys). So: find the existing entry under either
        # key type, carry its event_metadata across, and write back
        # under that same key.
        existing_key = None
        for k in (self.current_event_id, str(self.current_event_id)):
            if k in self.all_events:
                existing_key = k
                break
        if existing_key is not None and "event_metadata" not in markets_data:
            prior_meta = (self.all_events[existing_key] or {}).get(
                "event_metadata")
            if prior_meta is not None:
                markets_data = {**markets_data, "event_metadata": prior_meta}
        store_key = (existing_key if existing_key is not None
                     else str(self.current_event_id))
        self.all_events[store_key] = markets_data

        # Update current event data
        self.current_event_data = markets_data

        # Diff-aware refresh: avoids the clear+addItem churn, the
        # spurious currentIndexChanged-driven orderbook render, and the
        # orderbook re-render when the selected market's content hasn't
        # actually moved between refreshes.
        self._refreshMarketSelector(prev_selected_name=current_market_name,
                                    force_orderbook_render=False)
        # Keep the overlay up until the dual book + NV depth finish
        # building (see _gateOrderbookReveal) so the user doesn't watch
        # the table rebuild/resize after data lands.
        self._gateOrderbookReveal()

    def refreshData(self):
        """Refresh data from ProphetX API"""
        # Run full scrape in background
        QTimer.singleShot(0, self._refreshDataAsync)

    def _refreshDataAsync(self):
        """Async data refresh"""
        try:
            all_markets = ProphetXQuery.ScrapeAllMarkets(
                save_individual=False,
                save_combined=True
            )

            if all_markets:
                self.all_events = all_markets
                self._px_data_ready = True
                self._pending_fresh_fetch = False
                if self._initial_events_populated:
                    self.populateEventList()
                else:
                    self._maybePopulateInitialEvents()

        except Exception as e:
            print(f"Error refreshing data: {e}")


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    # Set dark theme
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(13, 15, 20))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(26, 29, 36))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(13, 15, 20))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    app.setPalette(palette)

    # Create and show widget
    browser = ProphetXBrowser()
    browser.setWindowTitle("ProphetX Order Book Browser")
    browser.resize(1200, 800)
    browser.show()

    sys.exit(app.exec())
