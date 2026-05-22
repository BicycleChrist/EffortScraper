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
    QStyledItemDelegate
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve, QRectF, QPointF, QThread
from PyQt6.QtGui import QFont, QColor, QPalette, QPainter, QPen, QBrush, QLinearGradient, QRadialGradient, QPainterPath
import asyncio
import json
import math
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import ProphetXQuery


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
        self.run_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #1f6f5e; color: #e8fff8;
                border: 1px solid #5eead4; border-radius: 4px;
                padding: {3 if self.compact_mode else 6}px
                         {10 if self.compact_mode else 16}px;
                font-size: {fs}px; font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #2a8c76; }}
            QPushButton:disabled {{
                background-color: #1a1d24; color: #555b66;
                border: 1px solid #2a2d34;
            }}
        """)
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
        """Run the PX and NV scanners for one event concurrently. Returns
        a list of (source, row) tuples. A failure on one exchange is
        logged and does not abort the other."""
        px_rows, nv_rows = await asyncio.gather(
            self._scanPX(ev),
            self._scanNV(ev, idx, total),
            return_exceptions=True,
        )
        out = []
        if isinstance(px_rows, Exception):
            print(f"[sgp-panel] PX scan failed for {ev['name']}: "
                  f"{px_rows!r}")
        else:
            out += [("PX", r) for r in px_rows]
        if isinstance(nv_rows, Exception):
            print(f"[sgp-panel] NV scan failed for {ev['name']}: "
                  f"{nv_rows!r}")
        else:
            out += [("NV", r) for r in nv_rows]
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
    def _addRow(self, src: str, row: dict) -> None:
        r = self.results.rowCount()
        self.results.insertRow(r)
        if src == "PX":
            player = row.get("player", "")
            legs = row.get("chain", "")
            single = self._fmtOdds(row.get("hr_odds"))
            sgp = self._fmtOdds(row.get("sgp_odds"))
            edge = float(row.get("edge_pct") or 0.0)
            src_color = QColor(94, 234, 212)     # ProphetX mint
        else:
            player = row.get("player", "")
            legs = self._nvLegs(row)
            single = row.get("american_dominant", "") or "--"
            sgp = row.get("american_combined", "") or "--"
            edge = self._nvEdgePct(row)
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

    def __init__(self, parent=None, compact_mode: bool = False):
        super().__init__(parent)
        self.compact_mode = compact_mode
        # Build the font once; reused for every paint.
        font_size = 10 if compact_mode else 16
        self._font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        # Color palette pre-built so paint() does no QColor allocation
        # in the hot path.
        self._ask_text = QColor("#f87171")
        self._bid_text = QColor("#34d399")
        self._ask_bar = QColor(248, 113, 113)
        self._bid_bar = QColor(52, 211, 153)
        self._transparent = QColor(13, 15, 20, 0)

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

        # Odds text, left-aligned with the original horizontal padding.
        text_pad = 2 if self.compact_mode else 8
        text_rect = rect.adjusted(text_pad, 0, -text_pad, 0)
        painter.setPen(text_color)
        painter.setFont(self._font)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            str(odds),
        )
        painter.restore()


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
        self.initUI()
        self._setupLoadingOverlay()

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

        # Set column widths
        header = self.orderbook_table.horizontalHeader()
        if self.compact_mode:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        else:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
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

        # Footer with spread info (compact or hidden in compact mode)
        if not self.compact_mode:
            self.footer_label = QLabel()
            self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.footer_label.setStyleSheet("""
                QLabel {
                    background-color: #1a1d24;
                    color: #8a92a3;
                    padding: 12px;
                    font-size: 14px;
                    border-top: 1px solid #2a2d34;
                }
            """)
            layout.addWidget(self.footer_label)
        else:
            self.footer_label = QLabel()
            self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.footer_label.setStyleSheet("""
                QLabel {
                    background-color: #1a1d24;
                    color: #8a92a3;
                    padding: 2px;
                    font-size: 8px;
                    border-top: 1px solid #2a2d34;
                }
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

    def setMarket(self, market_data: Dict):
        """
        Update order book display with new market data - shows ALL lines

        Args:
            market_data: Full market dict from ProphetX API
        """
        self.current_market = market_data

        # Update header
        market_name = market_data.get('name', 'Unknown Market')
        total_stake = market_data.get('totalStake', 0)
        self.market_title.setText(market_name)
        self.stake_label.setText(f"Total Liquidity: ${total_stake:,.2f}")

        # Render all lines from this market
        self.renderOrderBook(market_data)

    # ------------------------------------------------------------------
    # Source-agnostic render path (additive — existing setMarket above
    # stays untouched so legacy ProphetX-dict callers keep working).
    # Consumes a NormalizedMarket from exchange_market_keys.
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

        Multi-strike layout matches setMarket(): for Over/Under markets,
        Overs are rendered with strikes descending, Unders with strikes
        ascending, separated by a labeled divider. For team markets each
        line is rendered as its own block.

        Source identity is communicated via:
          - A 2px colored top border on the market title (accent bar)
          - A brand-colored badge prefixing the market name in rich text
        Neither touches table spacing or row heights.
        """
        self.current_market = nm
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
                                  order) -> Dict:
        """Adapt one NormalizedOrder into the dict shape that
        renderOrderRow / _extract_market_side already consume. This lets
        the new path reuse every styling helper unchanged."""
        american = order.american
        display_odds = f"+{american}" if american > 0 else str(american)
        return {
            "displayName": side_label,
            "abbreviatedName": abbreviated,
            "displayOdds": display_odds,
            "value": float(order.size_usd or 0.0),
            "odds": int(american),
        }

    @staticmethod
    def _format_strike_short(strike: Optional[float]) -> str:
        if strike is None:
            return ""
        if strike == int(strike):
            return str(int(strike))
        return f"{strike}"

    def renderNormalizedOrderBook(self, nm) -> None:
        """Walk Lines -> Sides -> Orders and emit one row per Order using
        the existing renderOrderRow / separator helpers."""
        self.orderbook_table.setUpdatesEnabled(False)
        try:
            self._renderNormalizedOrderBookImpl(nm)
        finally:
            self.orderbook_table.setUpdatesEnabled(True)

    def _renderNormalizedOrderBookImpl(self, nm) -> None:
        # Bring SIDE_* enums into local scope without forcing import-time
        # coupling at the top of the file.
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
        if is_over_under:
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
                            full_label, short_label, o))
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
                            full_label, full_label, o))
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
                    rows = [self._normalized_order_to_dict(label, short, o)
                            for o in s.orders]
                    if rows:
                        sections.append((label, rows))

        # See _renderOrderBookImpl comment on why we don't clearContents().
        self.orderbook_table.clearSpans()

        total_orders = sum(len(rows) for _, rows in sections)
        if total_orders == 0:
            self.orderbook_table.setRowCount(1)
            item = QTableWidgetItem("No orders available")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.orderbook_table.setItem(0, 0, item)
            colspan = 3 if self.compact_mode else 5
            self.orderbook_table.setSpan(0, 0, 1, colspan)
            if hasattr(self, "footer_label"):
                self.footer_label.setText(
                    f"No orders available — {nm.market_name}")
            return

        has_separator = len(sections) >= 2
        total_rows = total_orders + (len(sections) - 1 if has_separator else 0)
        self.orderbook_table.setRowCount(total_rows)

        total_liquidity = sum(o["value"] for _, rows in sections for o in rows)
        max_stake = max((o["value"] for _, rows in sections for o in rows
                        if o["value"] > 0), default=1.0)

        current_row = 0
        cumulative = 0.0
        first_section = True
        first_order: Optional[Dict] = None
        last_order: Optional[Dict] = None
        for section_label, rows in sections:
            if not first_section:
                self.renderSideSeparatorRow(current_row, f"─ {section_label} ─")
                current_row += 1
            first_section = False
            for od in rows:
                cumulative += od["value"]
                self.renderOrderRow(current_row, od, max_stake,
                                    cumulative, total_liquidity)
                if first_order is None:
                    first_order = od
                last_order = od
                current_row += 1

        if hasattr(self, "footer_label") and first_order is not None:
            best_bid = first_order.get("displayOdds", "N/A")
            best_ask = last_order.get("displayOdds", "N/A") if last_order else "N/A"
            self.footer_label.setText(
                f"Showing {total_orders} price levels • "
                f"Best Bid: {best_bid} • Best Ask: {best_ask} • "
                f"Spread: {len(sections)} sides • "
                f"Total Liquidity: ${total_liquidity:,.2f}")

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
        """Build rows by merging PX + NV per strike and side, sorted
        American-desc within strike, then dispatch to the existing
        renderOrderRow / separator helpers. Each rendered row gets a
        background tint based on its source."""
        self.orderbook_table.setUpdatesEnabled(False)
        try:
            self._renderDualOrderBookImpl(px_norm, nv_norm)
        finally:
            self.orderbook_table.setUpdatesEnabled(True)

    def _renderDualOrderBookImpl(self, px_norm, nv_norm) -> None:
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
            side_type), sorted American-desc."""
            out = []
            for src_key in ("px", "nv"):
                ln = lines_by_strike[strike][src_key]
                if ln is None:
                    continue
                for s in ln.sides:
                    if s.side_type != side_type:
                        continue
                    for o in s.orders:
                        out.append((src_key, s.label, o))
            out.sort(key=lambda t: t[2].american, reverse=True)
            return out

        # Build sections
        sections: list[tuple[str, list]] = []  # (label, [(src, order_dict)])
        if is_over_under:
            # Over half: lines in descending strike order
            over_rows = []
            for strike in line_order:
                strike_str = self._format_strike_short(strike)
                tagged = _orders_for_side(strike, _emk.SIDE_OVER)
                for src_key, _side_label, o in tagged:
                    label = (f"over {strike_str}".strip()
                             if strike_str else "over")
                    od = self._normalized_order_to_dict(label, label, o)
                    od["_source"] = src_key
                    over_rows.append(od)
            # Under half: ascending strike order
            under_rows = []
            for strike in list(reversed(line_order)):
                strike_str = self._format_strike_short(strike)
                tagged = _orders_for_side(strike, _emk.SIDE_UNDER)
                for src_key, _side_label, o in tagged:
                    label = (f"under {strike_str}".strip()
                             if strike_str else "under")
                    od = self._normalized_order_to_dict(label, label, o)
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
            team_strike_groups: dict = {}   # {team_sym: {strike: [(src, order)]}}
            team_order: list = []
            for strike in line_order:
                for src_key in ("px", "nv"):
                    ln = lines_by_strike[strike][src_key]
                    if ln is None:
                        continue
                    for s in ln.sides:
                        # Strip a trailing signed number ("-1.5", "+160")
                        # so "Athletics -1.5" -> "Athletics" before
                        # the symbol lookup.
                        bare = _emk.strip_price_from_label(s.label or "")
                        team_sym = (_emk.team_to_symbol(bare)
                                    or bare or s.side_type or "?")
                        if team_sym not in team_strike_groups:
                            team_strike_groups[team_sym] = {}
                            team_order.append(team_sym)
                        team_strike_groups[team_sym].setdefault(strike, [])
                        for o in s.orders:
                            team_strike_groups[team_sym][strike].append((src_key, o))

            for team_sym in team_order:
                strikes_in_team = sorted(
                    team_strike_groups[team_sym].keys(),
                    key=lambda s: (s is None, -(s if s is not None else 0)),
                )
                rows = []
                for strike in strikes_in_team:
                    tagged = team_strike_groups[team_sym][strike]
                    tagged.sort(key=lambda t: t[1].american, reverse=True)
                    strike_str = (self._format_strike_short(strike)
                                  if strike is not None else "")
                    row_label = (f"{team_sym} {strike_str}".strip()
                                 if strike_str else team_sym)
                    for src_key, o in tagged:
                        od = self._normalized_order_to_dict(
                            row_label, row_label, o)
                        od["_source"] = src_key
                        rows.append(od)
                if rows:
                    sections.append((team_sym, rows))

        # See _renderOrderBookImpl comment on why we don't clearContents().
        self.orderbook_table.clearSpans()

        total_orders = sum(len(rows) for _, rows in sections)
        if total_orders == 0:
            self.orderbook_table.setRowCount(1)
            item = QTableWidgetItem("No orders available")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.orderbook_table.setItem(0, 0, item)
            colspan = 3 if self.compact_mode else 5
            self.orderbook_table.setSpan(0, 0, 1, colspan)
            return

        has_separator = len(sections) >= 2
        total_rows = total_orders + (len(sections) - 1 if has_separator else 0)
        self.orderbook_table.setRowCount(total_rows)

        total_liquidity = sum(o["value"] for _, rows in sections for o in rows)
        max_stake = max((o["value"] for _, rows in sections for o in rows
                        if o["value"] > 0), default=1.0)

        current_row = 0
        cumulative = 0.0
        first_section = True
        first_order = None
        last_order = None
        for section_label, rows in sections:
            if not first_section:
                self.renderSideSeparatorRow(current_row, f"─ {section_label} ─")
                current_row += 1
            first_section = False
            for od in rows:
                cumulative += od["value"]
                self.renderOrderRow(current_row, od, max_stake,
                                    cumulative, total_liquidity)
                # Source differentiation: (a) recolor the SIDE column
                # text in the source's brand color so each row's origin
                # is immediately visible; (b) tint the entire row's
                # background as a softer secondary cue.
                src = od.get("_source")
                if src == "px":
                    text_color = self._PX_TEXT_COLOR
                    bg_tint = self._PX_ROW_TINT
                else:
                    text_color = self._NV_TEXT_COLOR
                    bg_tint = self._NV_ROW_TINT
                col_count = self.orderbook_table.columnCount()
                for c in range(col_count):
                    cell = self.orderbook_table.item(current_row, c)
                    if cell is not None:
                        cell.setBackground(bg_tint)
                # Override the existing ask/bid color on the side column
                # (column 0). Ask/bid info is still visible via the
                # odds column's red/green gradient.
                side_cell = self.orderbook_table.item(current_row, 0)
                if side_cell is not None:
                    side_cell.setForeground(text_color)
                if first_order is None:
                    first_order = od
                last_order = od
                current_row += 1

        if hasattr(self, "footer_label") and first_order is not None:
            best_bid = first_order.get("displayOdds", "N/A")
            best_ask = (last_order.get("displayOdds", "N/A")
                        if last_order else "N/A")
            self.footer_label.setText(
                f"Showing {total_orders} price levels • "
                f"Best: {best_bid} • Worst: {best_ask} • "
                f"PX+NV • Total Liquidity: ${total_liquidity:,.2f}")

    def _extract_market_side(self, display_name: str) -> str:
        """
        Extract the market side (team or over/under) from display name.
        Returns just the side identifier, ignoring the line value.

        Examples:
            "over 15.5" -> "over"
            "under 12.5" -> "under"
            "BUF +1" -> "BUF"
            "Buffalo Bills +1.5" -> "Buffalo Bills"
            "DEN -3.5" -> "DEN"
        """
        name_lower = display_name.lower().strip()

        # Handle over/under totals and props
        if name_lower.startswith('over'):
            return 'over'
        elif name_lower.startswith('under'):
            return 'under'

        # Handle spread/moneyline markets - extract team name (everything before the number)
        # Remove the spread number to get just the team identifier
        match = re.match(r'^(.+?)\s*[+-]?\d', display_name)
        if match:
            return match.group(1).strip()

        # Fallback: return the full name
        return display_name

    def renderOrderBook(self, market_data: Dict):
        """
        Render the order book showing ALL available lines with enhanced depth visualization.
        Groups orders by the two market sides (over/under, or team1/team2) with one separator.

        Args:
            market_data: Full market dict from ProphetX API
        """
        # Suspend paints across the whole rebuild so the dozens of
        # per-cell writes coalesce into a single repaint at the end.
        # try/finally guards against a render-time exception leaving
        # the widget in a permanently un-updating state.
        self.orderbook_table.setUpdatesEnabled(False)
        try:
            self._renderOrderBookImpl(market_data)
        finally:
            self.orderbook_table.setUpdatesEnabled(True)

    def _renderOrderBookImpl(self, market_data: Dict):
        all_orders = []

        # Check if this market has multiple lines (spread/total) or simple selections (moneyline)
        if 'marketLines' in market_data and market_data['marketLines']:
            # Spread/Total markets - collect all orders from all lines
            for market_line in market_data['marketLines']:
                line_name = market_line.get('name', '')
                selections = market_line.get('selections', [])

                # Each selection array is [side1_orders, side2_orders]
                for side_orders in selections:
                    if not side_orders:
                        continue
                    for order in side_orders:
                        all_orders.append(order)

        elif 'selections' in market_data:
            # Moneyline/simple markets
            selections = market_data.get('selections', [])
            for side_orders in selections:
                if not side_orders:
                    continue
                for order in side_orders:
                    all_orders.append(order)

        # Clear spans (separator positions can move) but DO NOT call
        # clearContents() or setRowCount(0) — both destroy the pooled
        # bar widgets in col 1, forcing the ~15-30ms QWidget/layout/
        # stylesheet reallocation that previously caused the visible
        # tickertape stutter. Surviving rows keep their cell widgets;
        # the final setRowCount() at the bottom only destroys widgets
        # in rows that are actually dropped.
        self.orderbook_table.clearSpans()

        if not all_orders:
            self.orderbook_table.setRowCount(1)
            item = QTableWidgetItem("No orders available")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.orderbook_table.setItem(0, 0, item)
            self.orderbook_table.setSpan(0, 0, 1, 5)
            return

        # Group orders by market side (over/under, or team1/team2)
        side_groups = {}
        for order in all_orders:
            display_name = order.get('displayName', order.get('abbreviatedName', ''))
            side = self._extract_market_side(display_name)

            if side not in side_groups:
                side_groups[side] = []
            side_groups[side].append(order)

        # Sort each group by odds (best price first - highest value, closest to even money)
        for side, orders in side_groups.items():
            orders.sort(key=lambda x: x.get('odds', 0), reverse=True)

        # Determine display order for the two sides
        if 'over' in side_groups and 'under' in side_groups:
            # Over/under market - show overs first, then unders
            side_order = ['over', 'under']
        else:
            # Other market types - sort by total liquidity (highest first)
            side_liquidity = [(side, sum(o.get('value', 0) for o in orders))
                             for side, orders in side_groups.items()]
            side_liquidity.sort(key=lambda x: x[1], reverse=True)
            side_order = [s[0] for s in side_liquidity]

        # Build ordered list: side1 orders, separator, side2 orders
        max_per_side = 50
        ordered_sections = [(side, side_groups[side][:max_per_side])
                           for side in side_order if side in side_groups]

        # Calculate total rows (orders + 1 separator if we have 2 sides)
        total_orders = sum(len(orders) for _, orders in ordered_sections)
        has_separator = len(ordered_sections) == 2
        total_rows = total_orders + (1 if has_separator else 0)
        self.orderbook_table.setRowCount(total_rows)

        # Calculate totals for display
        total_liquidity = sum(order.get('value', 0) for _, orders in ordered_sections for order in orders)
        max_stake = max((order.get('value', 1) for _, orders in ordered_sections for order in orders), default=1)

        # Render: first side, separator, second side
        current_row = 0
        cumulative = 0
        all_rendered_orders = []

        for section_idx, (side, orders) in enumerate(ordered_sections):
            # Add separator between the two sides (after first section)
            if section_idx == 1 and has_separator:
                separator_label = side.upper() if side in ('over', 'under') else side
                self.renderSideSeparatorRow(current_row, f"─ {separator_label} ─")
                current_row += 1

            # Render orders in this section
            for order in orders:
                cumulative += order.get('value', 0)
                self.renderOrderRow(current_row, order, max_stake, cumulative, total_liquidity)
                all_rendered_orders.append(order)
                current_row += 1

        # Update footer with summary info
        if all_rendered_orders:
            best_order = all_rendered_orders[0]
            self.footer_label.setText(
                f"Showing {len(all_rendered_orders)} price levels • "
                f"Best Bid: {best_order.get('displayOdds', 'N/A')} • "
                f"Best Ask: {all_rendered_orders[-1].get('displayOdds', 'N/A') if len(all_rendered_orders) > 1 else 'N/A'} • "
                f"Spread: {len(side_groups)} sides • "
                f"Total Liquidity: ${total_liquidity:,.2f}"
            )
        else:
            self.footer_label.setText("No orders available")

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

        self.orderbook_table.setItem(row, 0, separator)
        self.orderbook_table.setSpan(row, 0, 1, colspan)
        self.orderbook_table.setRowHeight(row, row_height)

    def renderSeparatorRow(self, row: int):
        """Render a separator row between asks and bids showing the spread"""
        if self.compact_mode:
            separator = QTableWidgetItem("─ SPREAD ─")
            font_size = 9
            row_height = 24
            colspan = 3
        else:
            separator = QTableWidgetItem("───── SPREAD ─────")
            font_size = 12
            row_height = 40
            colspan = 5

        separator.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        sep_font = QFont("SF Mono", font_size, QFont.Weight.Bold)
        sep_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        separator.setFont(sep_font)
        separator.setForeground(QColor(96, 165, 250))  # Professional blue
        separator.setBackground(QColor(31, 41, 55))  # Darker gray-blue

        self.orderbook_table.setItem(row, 0, separator)
        self.orderbook_table.setSpan(row, 0, 1, colspan)

        # Set row height slightly larger for visual separation
        self.orderbook_table.setRowHeight(row, row_height)

    def renderOrderRow(self, row: int, order: Dict, max_stake: float, cumulative: float, total_liquidity: float):
        """Render a single order book row with team/selection, odds, liquidity, cumulative, and percentage"""
        display_name = order.get('displayName', order.get('abbreviatedName', '---'))
        display_odds = order.get('displayOdds', '---')
        value = order.get('value', 0)
        odds_value = order.get('odds', 0)

        # Determine if this is a favorite (negative odds) or underdog (positive odds)
        side_type = 'bid' if odds_value < 0 else 'ask'

        # Calculate liquidity bar width (0-100%)
        bar_width = int((value / max_stake) * 100) if max_stake > 0 else 0

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
            # Shorten even more if too long
            if len(side_name) > 10:
                side_name = side_name[:8] + ".."

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

    def createSelectionItem(self, text: str, side_type: str) -> QTableWidgetItem:
        """Create selection/team name item with color coding"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        selection_font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        selection_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(selection_font)

        if side_type == 'ask':
            item.setForeground(QColor(252, 165, 165))  # Softer red for underdogs
        else:
            item.setForeground(QColor(110, 231, 183))  # Softer green for favorites

        return item

    def createPlainItem(self, text: str) -> QTableWidgetItem:
        """Create plain text table item"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(209, 213, 219))  # Professional gray

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        plain_font = QFont("SF Mono", font_size, QFont.Weight.Normal)
        plain_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(plain_font)
        return item

    def createPercentageItem(self, text: str, percentage: float) -> QTableWidgetItem:
        """Create percentage item with color intensity based on percentage"""
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Use smaller font in compact mode with better font
        font_size = 8 if self.compact_mode else 13
        pct_font = QFont("SF Mono", font_size, QFont.Weight.DemiBold)
        pct_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        item.setFont(pct_font)

        # Refined color coding - more professional amber/gold tones
        if percentage >= 10:
            item.setForeground(QColor(251, 191, 36))  # Professional amber for large orders
        elif percentage >= 5:
            item.setForeground(QColor(252, 211, 77))  # Lighter amber for medium orders
        else:
            item.setForeground(QColor(156, 163, 175))  # Muted gray for small orders

        return item

    def formatOdds(self, odds: int) -> str:
        """Format odds for display"""
        if odds > 0:
            return f"+{odds}"
        return str(odds)


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
        QTimer.singleShot(350, self._kickoffNovigDumpRefreshIfStale)

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

    # Leagues Novig actually covers. Used by coverage check so we don't
    # count PX-only sports (tennis, soccer outside EPL, etc.) as missing.
    _NOVIG_LEAGUES = frozenset({
        "MLB", "NBA", "NHL", "NFL", "NCAAF", "NCAAB", "WNBA", "EPL",
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
        eligible = [
            eid for eid, ev in self.all_events.items()
            if (ev.get("event_metadata") or {}).get("tournament")
            in self._NOVIG_LEAGUES
        ]
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
                    return

        if self._novig_dump_worker is not None and self._novig_dump_worker.isRunning():
            return  # already scraping

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

    def _onNovigDumpReady(self, n_events: int) -> None:
        """Slot fired when the async Novig scrape completes. Rebuilds
        the match map from the freshly-written dump; _loadNovigMatchMap
        itself re-renders the displayed market if one is selected."""
        print(f"[LiquidityWidget] Novig dump ready: {n_events} events. "
              f"Rebuilding match map...")
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

    def _loadNovigMatchMap(self) -> None:
        """Load the latest Novig dump from disk, normalize both sources,
        and build the {prophetx_event_id_str: EventPair} lookup used by
        the event-selection handler. Silent no-op when either dump is
        absent — the widget continues to work as a PX-only viewer."""
        try:
            from NovigClient import NovigQueries
            import exchange_market_keys as emk
        except Exception:
            return

        nv_data = NovigQueries.load_latest_dump()
        if not nv_data:
            return

        # ProphetX side: prefer the in-memory `self.all_events` once the
        # browser has loaded it. If empty (very early init), pull from
        # the latest combined dump on disk.
        px_data = self.all_events
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
            return

        try:
            px_events = emk.load_prophetx_normalized_events(
                px_data, league_filter=None)
            nv_events = emk.load_novig_normalized_events(
                nv_data, league_filter=None, currency="CASH")
            pairs = emk.match_events(px_events, nv_events)
        except Exception as e:
            # Do not swallow silently — a broken match build degrades the
            # widget to PX-only, which is exactly the kind of failure
            # that should be visible rather than mysterious.
            import traceback
            print(f"[LiquidityWidget] Novig match-map build failed: {e!r}")
            traceback.print_exc()
            return

        self._novig_event_map = {ep.event_a.source_event_id: ep for ep in pairs}

        # The map may now contain a match for the market the user is
        # already looking at (it commonly renders PX-only on the very
        # first event because this map is built async, after the first
        # market is auto-selected). Re-render so the dual-source view
        # engages without a manual event re-selection.
        self._rerenderCurrentMarketForMatchMap()

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
                color: #ffffff;
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

        # Load stale data as fallback (populates event list but orderbook stays in loading)
        self._loadStaleDataAsFallback()

        # Request fresh data fetch - parent widget should connect to this signal
        # Use a short delay to let the UI settle before emitting
        QTimer.singleShot(100, self._requestFreshData)

    def _requestFreshData(self):
        """Emit signal to request fresh data fetch"""
        self.refresh_all_requested.emit()

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

            # Only populate the event list, not the orderbook
            self._populateEventListOnly()

        except Exception as e:
            print(f"Error loading fallback data: {e}")

    def _populateEventListOnly(self):
        """Populate only the event selector/list without touching the orderbook"""
        self.filtered_events = []

        for event_id, event_data in self.all_events.items():
            metadata = event_data.get('event_metadata', {})

            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', 'Unknown')
            stake = metadata.get('stake', 0)
            tournament = metadata.get('tournament', '')

            display_text = f"{event_name}\n{sport}"
            if tournament:
                display_text += f" • {tournament}"
            display_text += f" • ${stake:,.0f} volume"

            self.filtered_events.append({
                'id': event_id,
                'metadata': metadata,
                'data': event_data,
                'display': display_text
            })

        # Sort by stake/volume
        self.filtered_events.sort(key=lambda x: x['metadata'].get('stake', 0), reverse=True)

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

    def loadLatestData(self):
        """Load the most recent ProphetX data dump (legacy method for compatibility)"""
        dump_dir = Path.cwd() / "prophetx_dumps"
        if not dump_dir.exists():
            return

        # Find latest combined data file
        combined_files = list(dump_dir.glob("all_markets_combined_*.json"))
        if not combined_files:
            return

        latest_file = max(combined_files, key=lambda p: p.stat().st_mtime)
        self.loadDataFromFile(latest_file)

    def loadDataFromFile(self, filepath: Path):
        """Load ProphetX data from JSON file"""
        try:
            with open(filepath, 'r') as f:
                self.all_events = json.load(f)

            self.populateEventList()

        except Exception as e:
            print(f"Error loading data: {e}")

    def populateEventList(self):
        """Populate event list from loaded data"""
        self.filtered_events = []

        for event_id, event_data in self.all_events.items():
            metadata = event_data.get('event_metadata', {})

            # Create display item
            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', 'Unknown')
            stake = metadata.get('stake', 0)
            tournament = metadata.get('tournament', '')

            display_text = f"{event_name}\n{sport}"
            if tournament:
                display_text += f" • {tournament}"
            display_text += f" • ${stake:,.0f} volume"

            self.filtered_events.append({
                'id': event_id,
                'metadata': metadata,
                'data': event_data,
                'display': display_text
            })

        # Sort by stake/volume
        self.filtered_events.sort(key=lambda x: x['metadata'].get('stake', 0), reverse=True)

        # Update UI based on mode
        if self.compact_mode:
            self.refreshCompactEventCombo()
        else:
            self.refreshEventList()
            # Update stats
            total_events = len(self.filtered_events)
            total_stake = sum(e['metadata'].get('stake', 0) for e in self.filtered_events)
            self.stats_label.setText(f"{total_events} events • ${total_stake:,.0f} total volume")

    def refreshEventList(self):
        """Refresh the event list widget display"""
        self.event_list.clear()

        for event in self.filtered_events:
            item = QListWidgetItem(event['display'])
            item.setData(Qt.ItemDataRole.UserRole, event)

            # Color code by sport
            sport = event['metadata'].get('sport', '').lower()
            if 'football' in sport or 'nfl' in sport:
                item.setForeground(QColor(255, 165, 0))
            elif 'basketball' in sport or 'nba' in sport:
                item.setForeground(QColor(255, 100, 100))
            elif 'baseball' in sport or 'mlb' in sport:
                item.setForeground(QColor(100, 150, 255))
            elif 'hockey' in sport or 'nhl' in sport:
                item.setForeground(QColor(100, 200, 255))
            else:
                item.setForeground(QColor(200, 200, 200))

            self.event_list.addItem(item)

    def refreshCompactEventCombo(self):
        """Refresh the compact event combo box"""
        # Block signals to prevent auto-selection when populating with stale data
        self.event_combo.blockSignals(True)
        self.event_combo.clear()

        for event in self.filtered_events:
            metadata = event['metadata']
            event_name = metadata.get('name', 'Unknown Event')
            sport = metadata.get('sport', '')
            stake = metadata.get('stake', 0)

            # Compact display
            display = f"{event_name} (${stake:,.0f})"
            self.event_combo.addItem(display, event)

        # Re-enable signals - don't auto-select, wait for fresh data
        self.event_combo.blockSignals(False)

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

        # Emit signal for external listeners (e.g., worker to refresh data)
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)

        # Don't populate market selector yet - wait for fresh data
        # The updateEventMarkets method will be called when fresh data arrives

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
        start_time = metadata.get('startTime', '')
        if start_time:
            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            time_str = dt.strftime('%b %d, %Y %I:%M %p')
            event_name += f" • {time_str}"

        self.event_header.setText(event_name)

        # Emit signal for external listeners (e.g., worker to refresh data)
        if self.current_event_id:
            self.event_selected.emit(self.current_event_id)

        # Don't populate market selector yet - wait for fresh data
        # The updateEventMarkets method will be called when fresh data arrives

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
            # Selected market's orders, sizes, and labels are identical to
            # the last render. Nothing to repaint.
            return
        self._last_selected_market_sig = new_sig

        # Render exactly once via the existing selection handler so the
        # Novig-pair lookup and dual-source render path are exercised
        # the same way as a user-initiated change.
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

        market_pair = self._lookupCurrentMarketPair(market)
        self._current_market_pair = market_pair

        if market_pair is None:
            # No Novig match — preserve the existing PX-only render.
            self.orderbook.setMarket(market)
            return

        # Normalize the selected PX market on the fly so the dual
        # renderer has a uniform input.
        try:
            import exchange_market_keys as emk
            px_norm = emk.from_prophetx_market(
                market,
                event_meta=(self.current_event_data or {}).get("event_metadata") or {},
            )
        except Exception:
            self.orderbook.setMarket(market)
            return

        self._current_px_norm = px_norm

        # Render immediately with whatever Novig depth is currently
        # cached on the matched NV market (dump-level top-of-book).
        self.orderbook.setMarketDual(px_norm, market_pair.market_b)

        # Then refresh in the background: pull /book/batch for each
        # Novig line under this market, and re-render when results
        # arrive. Cancel any previous worker first so stale results
        # don't overwrite the fresh display.
        self._launchNovigBookRefresh(market_pair)

    def _lookupCurrentMarketPair(self, prophetx_market: Dict):
        """Return the MarketPair for the given raw PX market dict, or
        None if no Novig match exists."""
        if not self._novig_event_map or not self.current_event_id:
            return None
        ep = self._novig_event_map.get(str(self.current_event_id))
        if ep is None:
            return None
        # Match by ProphetX market id — that's stable across calls.
        px_market_id = str(prophetx_market.get("id") or "")
        if not px_market_id:
            return None
        for mp in ep.market_pairs:
            if mp.market_a.source_market_id == px_market_id:
                return mp
        return None

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
        self._novig_book_worker = worker
        worker.start()

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
        # fresh depth.
        mp.market_b = refreshed
        self.orderbook.setMarketDual(self._current_px_norm, refreshed)

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
                self._populateEventListOnly()
            self.hideLoading()
            return

        # Store currently selected market name to restore after refresh
        current_market_name = None
        current_index = self.market_combo.currentIndex()
        if current_index >= 0:
            current_market = self.market_combo.itemData(current_index)
            if current_market:
                current_market_name = current_market.get('name')

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
        self.hideLoading()

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
                self.populateEventList()

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
