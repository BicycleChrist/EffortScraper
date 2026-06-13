"""Embeddable travel-visualization panel.

TravelVizPanel is a plain QWidget that owns the globe, control panel,
overlays, data aggregator, and live flight trackers. It has no menubar,
no status bar, and no window-level assumptions, so it can be dropped into
any container (standalone wrapper window, EffortOdds tab, dock, etc.).

Host integration surface:
  - signals: statusMessage, titleChanged, connectionChanged, progressChanged,
    travelCountChanged, gameSelected, teamSelected, fatigueReportsReady
  - self.actions: dict of QActions (also installed as the panel context menu)
  - shutdown(): stop threads/timers; call from the host's closeEvent
"""

import math
import json
import logging
import threading
from pathlib import Path
from typing import Dict, List
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QWidget, QSplitter,
                             QMessageBox, QProgressBar, QLabel, QFileDialog,
                             QComboBox, QGroupBox, QPushButton, QInputDialog,
                             QSlider)
from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QVector3D

from data_client import ESPNSportsDataAggregator, TeamTravelData
from globe_widget import FlightGlobeWidget
from globe_overlays import UpcomingGamesOverlay, WeatherOverlay
from live_flight_tracker import (RealTimeFlightTracker, DirectFlightTracker,
                                 FlightControlPanel)
from venue_tooltip import VenueTooltip
from database_manager import FatigueEngine
from flashscore_client import FlashscoreLiveFeed

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


class ConfigLoader:
    """Load API configuration from files (anchored to this module's directory)."""

    def __init__(self, config_file: str = "api_keys.json"):
        path = Path(config_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        self.config_file = path
        self.config = {}
        self.load_config()

    def load_config(self) -> Dict[str, str]:
        if not self.config_file.exists():
            self.create_default_config()
            return {}
        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
            return self.config
        except Exception as e:
            logger.error("Error loading config: %s", e)
            return {}

    def create_default_config(self):
        default_config = {
            "amadeus": "",
            "amadeus_secret": "",
            "backup_apis_note": "Additional APIs for future expansion"
        }
        try:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info("Created default config file: %s", self.config_file)
        except Exception as e:
            logger.error("Error creating config file: %s", e)

    def get_api_keys(self) -> Dict[str, str]:
        return self.config

    def is_configured(self) -> bool:
        return True


class TravelVizPanel(QWidget):
    """Self-contained travel visualization widget with multi-league support."""

    # Host-facing signals (replace the old QMainWindow status bar / title)
    statusMessage = pyqtSignal(str, int)          # message, timeout ms (0 = sticky)
    titleChanged = pyqtSignal(str)                # suggested window/tab title
    connectionChanged = pyqtSignal(str, bool)     # text, healthy
    progressChanged = pyqtSignal(int)             # 0-100 (<=0 or >=100 means hide)
    travelCountChanged = pyqtSignal(int)
    # Betting-side integration signals
    gameSelected = pyqtSignal(str, dict)          # league, game payload
    teamSelected = pyqtSignal(str, str)           # league, team_id
    fatigueReportsReady = pyqtSignal(str, str, list)  # league, season, [GameFatigueReport]
    liveScoresUpdated = pyqtSignal(dict)          # {league: [mapped live event, ...]}

    # internal: marshals live-poller callbacks (worker thread) onto the GUI thread
    _liveScoresArrived = pyqtSignal(object)
    # internal: lineup fetch results (worker thread) -> GUI thread
    _lineupsArrived = pyqtSignal(object, object)  # game key, lineup dict|None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TravelVizPanel")

        self.config_loader = ConfigLoader()

        self.sports_aggregator = None
        self.current_travel_data = []
        self.all_teams = []
        self.current_league = "NHL"  # If default league is not in season, UI will hang
        self.current_season = str(datetime.now().year)

        self.flight_tracker = None
        self.direct_tracker = None
        self.live_tracking_active = False

        self.fatigue_engine = None
        self._fatigue_reports = []          # latest GameFatigueReport list
        self._team_fatigue = {}             # team_id -> TeamFatigue (next game)

        self.live_feed = None               # FlashscoreLiveFeed (started on show)
        self._live_payload = {}             # last {league: [live event dict]}
        self._lineups_cache = {}            # game key -> lineup dict
        self._lineups_pending = set()       # game keys with an in-flight fetch

        self.setup_ui()
        self._create_actions()
        self.setup_sports_system()
        self.setup_live_flight_tracker()
        self.connect_signals()
        self.synchronize_league_state()

        self.settings = QSettings("SportsTracker", "TeamTravel")
        self.start_sports_monitoring()

    # ------------------------------------------------------------------ UI

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Control panel (left side)
        control_widget = QWidget()
        control_layout = QVBoxLayout(control_widget)

        season_group = self.create_season_controls()
        control_layout.addWidget(season_group)

        self.control_panel = FlightControlPanel()
        control_layout.addWidget(self.control_panel)

        control_widget.setMinimumWidth(350)
        control_widget.setMaximumWidth(500)
        splitter.addWidget(control_widget)

        # Globe widget (right side)
        self.globe_widget = FlightGlobeWidget()
        splitter.addWidget(self.globe_widget)

        passive_spin_button = QPushButton(
            f"passive spin: {('enabled' if self.globe_widget.passive_spin else 'disabled')}")

        def PassiveSpinToggleCallback(_):
            self.globe_widget.passive_spin = not self.globe_widget.passive_spin
            passive_spin_button.setText(
                f"passive spin: {('enabled' if self.globe_widget.passive_spin else 'disabled')}")
        passive_spin_button.clicked.connect(PassiveSpinToggleCallback)
        passive_spin_button.setFixedWidth(150)
        passive_spin_button.setStyleSheet(""" QPushButton {
            background-color: rgba(31, 41, 55, 10);
            color: white;
        }""")

        slider_label = QLabel("rotation speed: 0.25")
        slider_label.setFixedWidth(150)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-200, 200)
        slider.setValue(25)
        slider.setFixedWidth(150)
        slider.setStyleSheet(""" QSlider {
            background-color: rgba(31, 41, 55, 10);
            color: #10B98110;
        }""")

        def RotationSliderCallback(slider_val):
            slider_label.setText(f"rotation speed: {slider_val/100:.3f}")
            self.globe_widget.passive_rotation_speed = QVector3D(0.05, slider_val / 100, 0)

        slider.valueChanged.connect(RotationSliderCallback)

        slider_layout = QVBoxLayout(self.globe_widget)
        slider_layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        slider_layout.setContentsMargins(10, 10, 0, 0)
        slider_layout.addWidget(passive_spin_button)
        slider_layout.addWidget(slider_label)
        slider_layout.addWidget(slider)

        # Upcoming games overlay (floating over globe)
        self.games_overlay = UpcomingGamesOverlay(self.globe_widget)
        self.games_overlay.setFixedHeight(300)
        self.games_overlay.move(0, 10)
        self.games_overlay.raise_()

        # Weather overlay (right side of globe)
        self.weather_overlay = WeatherOverlay(self.globe_widget)
        self.weather_overlay.setFixedHeight(350)
        self.weather_overlay.move(self.globe_widget.width() - 30, 10)
        self.weather_overlay.raise_()
        self.weather_overlay.venueSelected.connect(self.on_weather_venue_selected)
        self.weather_overlay.visibilityChanged.connect(self.on_weather_overlay_toggled)
        self.weather_overlay.weatherUpdated.connect(self._on_venue_weather)

        # Venue tooltip for marker clicks
        self.venue_tooltip = VenueTooltip(self)

        splitter.setSizes([400, 1500])

        self.apply_sports_theme()

    def create_season_controls(self) -> QGroupBox:
        group = QGroupBox("SEASON DATA")
        layout = QVBoxLayout(group)

        league_layout = QHBoxLayout()
        league_layout.addWidget(QLabel("League:"))

        self.league_status_label = QLabel("MLB")
        self.league_status_label.setStyleSheet("font-weight: bold; color: #00AA00;")
        league_layout.addWidget(self.league_status_label)

        league_layout.addStretch()
        layout.addLayout(league_layout)

        season_layout = QHBoxLayout()
        season_layout.addWidget(QLabel("Season:"))
        self.season_combo = QComboBox()
        self.update_season_combo_for_league("MLB")
        season_layout.addWidget(self.season_combo)
        layout.addLayout(season_layout)

        load_layout = QHBoxLayout()

        self.load_full_season_btn = QPushButton("Load Full Season")
        self.load_full_season_btn.setToolTip("Load complete season schedule (recommended)")
        load_layout.addWidget(self.load_full_season_btn)

        self.load_current_week_btn = QPushButton("Current Week")
        self.load_current_week_btn.setToolTip("Load just current week for quick preview")
        load_layout.addWidget(self.load_current_week_btn)

        layout.addLayout(load_layout)

        team_layout = QHBoxLayout()
        team_layout.addWidget(QLabel("Focus Team:"))
        self.focus_team_combo = QComboBox()
        self.focus_team_combo.addItem("All Teams", "")
        team_layout.addWidget(self.focus_team_combo)
        layout.addLayout(team_layout)

        self.season_status_label = QLabel("No season data loaded")
        self.season_status_label.setStyleSheet("font-style: italic; color: #888;")
        layout.addWidget(self.season_status_label)

        self.season_progress = QProgressBar()
        self.season_progress.setVisible(False)
        layout.addWidget(self.season_progress)

        return group

    def _create_actions(self):
        """Build QActions usable by both an embedding host and this panel's
        own context menu. Shortcuts are widget-scoped so they don't collide
        with the host window's shortcuts."""
        self.actions: Dict[str, QAction] = {}

        def make(key, text, slot, shortcut=None, checkable=False, checked=False):
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(shortcut)
                act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            act.setCheckable(checkable)
            if checkable:
                act.setChecked(checked)
            act.triggered.connect(slot)
            self.actions[key] = act
            return act

        make("load_season", "Load Full Season", self.load_full_season, "Ctrl+L")
        make("refresh", "Refresh Season Data", self.force_refresh_season_data, "F5")
        make("export", "Export Travel Data...", self.export_travel_data, "Ctrl+E")

        make("start_animation", "Start Team Animation", self.start_team_animation_dialog, "Ctrl+A")
        make("stop_animation", "Stop Animation",
             lambda: self.globe_widget.stop_team_animation(), "Esc")

        self.start_tracking_action = make("start_tracking", "Start Live Tracking",
                                          self.start_live_tracking, "Ctrl+T")
        self.stop_tracking_action = make("stop_tracking", "Stop Live Tracking",
                                         self.stop_live_tracking, "Ctrl+Shift+T")
        self.stop_tracking_action.setEnabled(False)
        make("track_aircraft", "Track Aircraft...", self.show_track_aircraft_dialog, "Ctrl+F")
        make("watchlist", "Show Watchlist", self.show_watchlist_dialog)
        make("clear_flights", "Clear All Flights",
             lambda: self.globe_widget.clear_live_flights())

        make("league_mlb", "Switch to MLB", lambda: self.on_league_changed("MLB"))
        make("league_nba", "Switch to NBA", lambda: self.on_league_changed("NBA"))
        make("league_nhl", "Switch to NHL", lambda: self.on_league_changed("NHL"))
        make("league_stats", "League Statistics", self.show_league_statistics)

        make("toggle_paths", "Team Travel Paths", self.toggle_travel_paths,
             checkable=True, checked=True)
        make("toggle_cities", "Team Cities", self.toggle_team_cities,
             checkable=True, checked=True)
        make("toggle_day_night", "Day/Night Cycle", self.toggle_day_night,
             checkable=True, checked=False)
        make("reset_view", "Reset View", lambda: self.globe_widget.reset_view(), "R")

        # Context menu for embedded use (right-click anywhere on the panel)
        for key in ("load_season", "refresh", "start_animation", "stop_animation",
                    "start_tracking", "stop_tracking", "track_aircraft",
                    "toggle_paths", "toggle_cities", "toggle_day_night", "reset_view"):
            self.addAction(self.actions[key])
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

    def update_season_combo_for_league(self, league: str):
        self.season_combo.clear()

        current_year = datetime.now().year
        current_month = datetime.now().month

        if league in ['NBA', 'NHL']:
            # NBA/NHL seasons span two years (October to June)
            if current_month >= 10:
                current_season_start = current_year
            else:
                current_season_start = current_year - 1

            for i in range(6):
                start_year = current_season_start - i
                season_str = f"{start_year}-{str(start_year + 1)[2:]}"
                self.season_combo.addItem(season_str, season_str)
        else:  # MLB
            for year in range(current_year, current_year - 5, -1):
                season_str = str(year)
                self.season_combo.addItem(season_str, season_str)

        if self.sports_aggregator:
            current_season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
            index = self.season_combo.findText(current_season)
            if index >= 0:
                self.season_combo.setCurrentIndex(index)
                self.current_season = current_season
            else:
                logger.warning("Could not find season '%s' in combo box", current_season)
                if self.season_combo.count() > 0:
                    fallback_season = self.season_combo.itemText(0)
                    self.season_combo.setCurrentIndex(0)
                    self.current_season = fallback_season

    # -------------------------------------------------------- data systems

    def setup_sports_system(self):
        api_keys = self.config_loader.get_api_keys()

        self.sports_aggregator = ESPNSportsDataAggregator(
            api_keys, db_path=str(BASE_DIR / "sports_data.db"))

        amadeus_key = api_keys.get("amadeus")
        amadeus_secret = api_keys.get("amadeus_secret")

        if amadeus_key and amadeus_secret:
            success = self.sports_aggregator.set_amadeus_credentials(amadeus_key, amadeus_secret)
            if success:
                logger.info("Amadeus integration configured")
            else:
                logger.warning("Amadeus integration failed to initialize")
        else:
            logger.info("Amadeus API keys not found - analysis features disabled")

        self.sports_aggregator.set_league(self.current_league)

        self.sports_aggregator.dataUpdated.connect(self.on_travel_data_updated)
        self.sports_aggregator.progressUpdated.connect(self.on_progress_updated)
        self.sports_aggregator.errorOccurred.connect(self.on_data_error)
        self.sports_aggregator.seasonDataLoaded.connect(self.on_season_data_loaded)
        self.sports_aggregator.scheduleRefreshed.connect(self.on_schedule_refreshed)

        self.control_panel.aggregator = self.sports_aggregator

        self.fatigue_engine = FatigueEngine(self.sports_aggregator.db)

        # Flashscore rolling refresh: shortly after startup, then every 15 min.
        # Cheap (~1s/league, off-thread) and keeps the cache stamp fresh so the
        # staleness check never falls back to a full ESPN re-scrape.
        self._liveScoresArrived.connect(self._on_live_scores,
                                        Qt.ConnectionType.QueuedConnection)
        self._lineupsArrived.connect(self._on_lineups_arrived,
                                     Qt.ConnectionType.QueuedConnection)
        self._fs_refresh_timer = QTimer(self)
        self._fs_refresh_timer.setInterval(15 * 60 * 1000)
        self._fs_refresh_timer.timeout.connect(self._kick_flashscore_refresh)
        self._fs_refresh_timer.start()
        QTimer.singleShot(8000, self._kick_flashscore_refresh)

    def _kick_flashscore_refresh(self):
        if self.sports_aggregator:
            self.sports_aggregator.refresh_upcoming_schedule(self.current_league)

    def on_schedule_refreshed(self, league: str, season: str, summary: dict):
        changed = summary.get("inserted", 0) + summary.get("updated", 0)
        logger.info("Schedule refreshed (%s %s): %s", league, season, summary)
        if changed:
            self.statusMessage.emit(
                f"📡 {league} schedule refreshed: {summary.get('inserted', 0)} new, "
                f"{summary.get('updated', 0)} updated", 5000)
            if league == self.current_league:
                self.refresh_fatigue_reports(season, league)
                self.update_overlay_games()
            # Today's markers may have gained/lost games (e.g. playoffs)
            if not self.focus_team_combo.currentData():
                self.display_todays_games_startup()
                self._stamp_live_markers()

    # ----------------------------------------------------------- live scores

    def _ensure_live_feed(self):
        if self.live_feed is None and self.sports_aggregator:
            self.live_feed = FlashscoreLiveFeed(
                self.sports_aggregator.flashscore,
                leagues=("MLB", "NBA", "NHL"),
                interval=30.0,
                on_update=self._liveScoresArrived.emit,  # poller thread -> queued
            )
        if self.live_feed and not self.live_feed.is_running():
            self.live_feed.start()

    def _on_live_scores(self, payload: dict):
        """GUI-thread handler for live score ticks."""
        self._live_payload = payload or {}
        # Feed game states to the flight tracker: "team is live" vetoes its
        # travel window; a live→finished transition is the exact game end
        if self.flight_tracker:
            self.flight_tracker.update_live_states(self._live_payload)
        self._stamp_live_markers()
        self.liveScoresUpdated.emit(self._live_payload)
        total = sum(len(v) for v in self._live_payload.values())
        if total:
            self.connectionChanged.emit(
                f"Connected ({self.current_league}) · {total} live", True)
        self.globe_widget.update()

    def _stamp_live_markers(self):
        """Attach live score dicts to today's game markers (home/away id match)."""
        index = {}
        for league, events in self._live_payload.items():
            for ev in events:
                index[(league, ev["home_id"], ev["away_id"])] = ev

        for marker in self.globe_widget.team_city_markers:
            key = (marker.get('league'),
                   (marker.get('home_team_id') or '').lower(),
                   (marker.get('away_team_id') or '').lower())
            live = index.get(key)
            if live:
                marker['live'] = live
            else:
                marker.pop('live', None)
            # Re-stamp cached game details (markers are rebuilt on schedule
            # refresh; the unfold panel reads marker['lineups'/'final'])
            if marker.get('game_info'):
                cached = self._lineups_cache.get(
                    self.globe_widget._marker_game_key(marker))
                if cached is not None:
                    if cached.get('lineups') is not None:
                        marker['lineups'] = cached['lineups']
                    if cached.get('final') is not None:
                        marker['final'] = cached['final']

        self.globe_widget.invalidate_marker_clusters()

    def _stamp_fatigue_markers(self):
        """Attach fatigue data to game markers: max score drives the near-zoom
        rings, per-team detail feeds the game card."""
        if not self._team_fatigue:
            return
        for marker in self.globe_widget.team_city_markers:
            scores = []
            detail = {}
            m_league = marker.get('league') or self.current_league
            for side, key in (('home', 'home_team_id'), ('away', 'away_team_id')):
                tf = self._team_fatigue.get(
                    (m_league, (marker.get(key) or '').lower()))
                if tf:
                    scores.append(tf.score)
                    detail[side] = tf
            if scores:
                marker['fatigue_max'] = max(scores)
                marker['fatigue_detail'] = detail

    def _on_venue_weather(self, venue_id: str, weather_data: dict):
        """Stamp fetched venue weather onto matching game markers (game card)."""
        for marker in self.globe_widget.team_city_markers:
            game = (marker.get('game_info') or {}).get('game')
            if game is not None and game.venue.venue_id == venue_id:
                marker['weather'] = weather_data

    def setup_live_flight_tracker(self):
        if not self.sports_aggregator:
            logger.warning("Cannot setup flight tracker - sports aggregator not initialized")
            return

        api_keys = self.config_loader.get_api_keys()

        try:
            self.flight_tracker = RealTimeFlightTracker(
                db=self.sports_aggregator.db,
                api_keys=api_keys,
                league=self.current_league
            )
            self.flight_tracker.flightDetected.connect(self.on_flight_detected)
            self.flight_tracker.flightUpdated.connect(self.on_flight_updated)
            self.flight_tracker.flightLanded.connect(self.on_flight_landed)
            self.flight_tracker.statusUpdate.connect(self.on_tracker_status)
            logger.info("Live flight tracker initialized")
        except Exception as e:
            logger.warning("Failed to initialize flight tracker: %s", e)
            self.flight_tracker = None

        try:
            self.direct_tracker = DirectFlightTracker()
            self.direct_tracker.flightFound.connect(
                self.on_direct_flight_found, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.flightUpdated.connect(
                self.on_direct_flight_updated, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.flightLost.connect(
                self.on_direct_flight_lost, Qt.ConnectionType.QueuedConnection)
            self.direct_tracker.statusUpdate.connect(
                self.on_tracker_status, Qt.ConnectionType.QueuedConnection)
            logger.info("Direct flight tracker initialized")
        except Exception as e:
            logger.exception("Failed to initialize direct flight tracker: %s", e)
            self.direct_tracker = None

    # ------------------------------------------------------- live tracking

    def _leagues_with_games_today(self) -> list:
        """Leagues with games within ±36h — the ones whose charters could
        plausibly be in the air right now."""
        if not self.sports_aggregator:
            return []
        now = datetime.now()
        lo, hi = now - timedelta(hours=36), now + timedelta(hours=36)
        leagues = []
        for league in ("MLB", "NBA", "NHL"):
            try:
                season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
                games = self.sports_aggregator.db.load_games(season, league)
                if any(lo <= g.date <= hi for g in games):
                    leagues.append(league)
            except Exception as e:
                logger.debug("league activity check failed for %s: %s", league, e)
        return leagues

    def _auto_start_live_tracking(self):
        """Default view: track charters for every league that has games on
        the slate (runs once shortly after startup)."""
        if self.flight_tracker and not self.live_tracking_active:
            self.start_live_tracking()

    def start_live_tracking(self, leagues: list = None):
        if not self.flight_tracker:
            self.statusMessage.emit("Flight tracker not available", 3000)
            return

        if self.live_tracking_active:
            self.statusMessage.emit("Live tracking already active", 3000)
            return

        self.globe_widget.clear_live_flights()

        leagues = leagues or self._leagues_with_games_today() or [self.current_league]
        self.flight_tracker.set_leagues(leagues)

        self.flight_tracker.start()
        self.live_tracking_active = True
        self.statusMessage.emit(
            f"🛫 Starting flight tracking ({', '.join(leagues)})...", 3000)

        self.start_tracking_action.setEnabled(False)
        self.stop_tracking_action.setEnabled(True)

    def stop_live_tracking(self):
        if not self.flight_tracker or not self.live_tracking_active:
            return

        self.flight_tracker.stop()
        self.flight_tracker.wait()
        self.live_tracking_active = False

        self.globe_widget.clear_live_flights()
        self.statusMessage.emit("🛑 Live flight tracking stopped", 3000)

        self.start_tracking_action.setEnabled(True)
        self.stop_tracking_action.setEnabled(False)

    def on_flight_detected(self, flight_data: dict):
        # Tracker stamps its own league (multi-league); fall back for the
        # direct/watchlist path which has none
        if not flight_data.get('league'):
            flight_data['league'] = self.current_league
        self.globe_widget.add_live_flight(flight_data)

        team_id = flight_data.get('team_id', 'Unknown')
        callsign = flight_data.get('callsign', flight_data['icao24'])
        confidence = flight_data.get('confidence', 0)
        self.statusMessage.emit(
            f"✈️ New flight: {team_id} - {callsign} ({confidence}% confidence)", 5000)

    def on_flight_updated(self, flight_data: dict):
        if not flight_data.get('league'):
            flight_data['league'] = self.current_league
        self.globe_widget.update_live_flight(flight_data)

    def on_flight_landed(self, icao24: str):
        self.globe_widget.remove_live_flight(icao24)

    def on_tracker_status(self, message: str):
        # Paused-polling notice stays in the banner until the next status
        # (it recurs only every 2 min; a 3s timeout would leave it blank)
        timeout = 0 if message.startswith("💤") else 3000
        self.statusMessage.emit(message, timeout)

    def on_direct_flight_found(self, flight_data: dict):
        self.globe_widget.add_live_flight(flight_data)
        label = flight_data.get('label', flight_data.get('registration', 'Unknown'))
        self.statusMessage.emit(f"🎯 Found: {label}", 5000)

    def on_direct_flight_updated(self, flight_data: dict):
        self.globe_widget.update_live_flight(flight_data)

    def on_direct_flight_lost(self, identifier: str):
        self.globe_widget.remove_live_flight(identifier)

    def show_track_aircraft_dialog(self):
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLineEdit, QDialogButtonBox

        dialog = QDialog(self)
        dialog.setWindowTitle("Track Aircraft")
        dialog.setMinimumWidth(350)

        layout = QFormLayout(dialog)

        identifier_input = QLineEdit()
        identifier_input.setPlaceholderText("e.g., N801DM, DAL123, a801dm")
        layout.addRow("Identifier:", identifier_input)

        type_combo = QComboBox()
        type_combo.addItem("Callsign (e.g., SWA2294, DAL123)", "callsign")
        type_combo.addItem("Tail Number (e.g., N801DM, N8882Q)", "registration")
        type_combo.addItem("ICAO24 Hex (e.g., ac3e3a)", "hex")
        layout.addRow("Type:", type_combo)

        def auto_detect_type():
            text = identifier_input.text().strip().upper()
            if not text:
                return
            if text.startswith('N') and len(text) >= 5 and text[1:2].isdigit():
                type_combo.setCurrentIndex(1)  # Registration
            elif len(text) == 6 and all(c in '0123456789ABCDEF' for c in text):
                type_combo.setCurrentIndex(2)  # Hex
            elif any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
                type_combo.setCurrentIndex(0)  # Callsign

        identifier_input.textChanged.connect(auto_detect_type)

        label_input = QLineEdit()
        label_input.setPlaceholderText("e.g., Mavericks Charter (optional)")
        layout.addRow("Label:", label_input)

        team_combo = QComboBox()
        team_combo.addItem("None", "")
        for team in sorted(self.all_teams, key=lambda t: t.display_name):
            team_combo.addItem(f"{team.display_name} ({team.abbreviation})", team.team_id)
        layout.addRow("Team:", team_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            identifier = identifier_input.text().strip()
            if not identifier:
                QMessageBox.warning(self, "Error", "Please enter an identifier")
                return

            identifier_type = type_combo.currentData()
            label = label_input.text().strip() or identifier
            team_id = team_combo.currentData() or None

            if self.direct_tracker:
                self.direct_tracker.add_to_watchlist(
                    identifier=identifier,
                    identifier_type=identifier_type,
                    label=label,
                    team_id=team_id
                )
                if not self.direct_tracker.isRunning():
                    self.direct_tracker.start()
                    self.statusMessage.emit(f"🎯 Started tracking: {label}", 3000)
                else:
                    self.statusMessage.emit(f"🎯 Added to watchlist: {label}", 3000)
            else:
                QMessageBox.warning(self, "Error", "Direct flight tracker not available")

    def show_watchlist_dialog(self):
        from PyQt6.QtWidgets import QDialog, QListWidget, QDialogButtonBox, QListWidgetItem

        if not self.direct_tracker:
            QMessageBox.warning(self, "Error", "Direct flight tracker not available")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Flight Watchlist")
        dialog.setMinimumWidth(400)
        dialog.setMinimumHeight(300)

        layout = QVBoxLayout(dialog)
        list_widget = QListWidget()

        watchlist = self.direct_tracker.get_watchlist()
        if not watchlist:
            item = QListWidgetItem("No aircraft in watchlist")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            list_widget.addItem(item)
        else:
            for entry in watchlist:
                status = "✈️ ACTIVE" if entry['is_active'] else "📡 Searching"
                team_str = f" [{entry['team_id']}]" if entry['team_id'] else ""
                item_text = f"{status} {entry['label']}{team_str}\n   {entry['type']}: {entry['identifier']}"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, entry['identifier'])
                list_widget.addItem(item)

        layout.addWidget(list_widget)

        buttons = QDialogButtonBox()
        remove_btn = buttons.addButton("Remove Selected", QDialogButtonBox.ButtonRole.ActionRole)
        clear_btn = buttons.addButton("Clear All", QDialogButtonBox.ButtonRole.DestructiveRole)
        close_btn = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)

        def remove_selected():
            current = list_widget.currentItem()
            if current:
                identifier = current.data(Qt.ItemDataRole.UserRole)
                if identifier:
                    self.direct_tracker.remove_from_watchlist(identifier)
                    list_widget.takeItem(list_widget.row(current))

        def clear_all():
            self.direct_tracker.clear_watchlist()
            self.globe_widget.clear_live_flights()
            list_widget.clear()
            item = QListWidgetItem("No aircraft in watchlist")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            list_widget.addItem(item)

        remove_btn.clicked.connect(remove_selected)
        clear_btn.clicked.connect(clear_all)
        close_btn.clicked.connect(dialog.reject)

        layout.addWidget(buttons)
        dialog.exec()

    # ------------------------------------------------------------- signals

    def connect_signals(self):
        self.season_combo.currentTextChanged.connect(self.on_season_changed)
        self.load_full_season_btn.clicked.connect(self.load_full_season)
        self.load_current_week_btn.clicked.connect(self.load_current_week)
        self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

        self.control_panel.refreshRequested.connect(self.force_refresh_season_data)
        self.control_panel.modeChanged.connect(self.on_league_changed)
        self.control_panel.teamChanged.connect(self.on_control_panel_team_changed)
        self.control_panel.amadeusAnalysisRequested.connect(self.start_amadeus_analysis)

        self.globe_widget.locationSelected.connect(self.on_location_selected)
        self.globe_widget.markerClicked.connect(self.on_marker_clicked)

        self.globe_widget.animationStatusChanged.connect(self.on_animation_status_changed)
        self.globe_widget.animationProgressChanged.connect(self.on_animation_progress_changed)

        self.games_overlay.daysFilterChanged.connect(self.on_overlay_days_changed)

    def on_control_panel_team_changed(self, team_abbr: str):
        logger.debug("Control panel team changed to: %s", team_abbr)

        try:
            self.focus_team_combo.currentTextChanged.disconnect()
        except Exception:
            pass

        main_window_index = -1
        for i in range(self.focus_team_combo.count()):
            if self.focus_team_combo.itemData(i) == team_abbr:
                main_window_index = i
                break

        if main_window_index >= 0:
            self.focus_team_combo.setCurrentIndex(main_window_index)
        else:
            logger.debug("Could not find team %s in focus combo", team_abbr)

        self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

        if team_abbr:
            season = self.season_combo.currentText()
            if self.sports_aggregator:
                self.sports_aggregator.load_team_season_schedule(team_abbr, season, self.current_league)
            self.teamSelected.emit(self.current_league, team_abbr)

        self.update_overlay_games()

    def start_amadeus_analysis(self, team_abbr: str, days_ahead: int):
        logger.debug("Analysis request for %s, %s days", team_abbr, days_ahead)

        if not self.sports_aggregator:
            return

        config = self.config_loader.get_api_keys()
        amadeus_key = config.get("amadeus")
        amadeus_secret = config.get("amadeus_secret")

        if amadeus_key and amadeus_secret:
            success = self.sports_aggregator.set_amadeus_credentials(amadeus_key, amadeus_secret)
            if success:
                if not hasattr(self, '_amadeus_signals_connected'):
                    self.sports_aggregator.amadeusIntelligenceReady.connect(
                        self.control_panel.on_analysis_complete)
                    self.sports_aggregator.errorOccurred.connect(
                        self.control_panel.on_analysis_error)
                    self.sports_aggregator.amadeusProgressUpdated.connect(
                        self.control_panel.on_analysis_progress)
                    self._amadeus_signals_connected = True

                self.control_panel.analysis_progress.setVisible(True)
                self.control_panel.analysis_progress.setValue(0)
                self.control_panel.analyze_btn.setEnabled(False)

                self.sports_aggregator.get_team_travel_intelligence_async(team_abbr, days_ahead)
            else:
                logger.error("Failed to set Amadeus credentials")
        else:
            logger.warning("Amadeus credentials not found in config")
            self.control_panel.status_message.setText("Amadeus API keys not configured")

    def on_league_changed(self, league: str):
        if league not in ["MLB", "NBA", "NHL"]:
            logger.warning("Unsupported league: %s", league)
            return

        if league == self.current_league:
            return

        logger.info("Switching from %s to %s", self.current_league, league)
        self.current_league = league

        if self.sports_aggregator:
            self.sports_aggregator.set_league(league)

        if self.flight_tracker:
            # Keep tracking every active league, with the new one included
            leagues = list(dict.fromkeys([league] + self._leagues_with_games_today()))
            self.flight_tracker.set_leagues(leagues)
            self.globe_widget.clear_live_flights()

        self.league_status_label.setText(league)
        self.update_season_combo_for_league(league)

        current_season = self.season_combo.currentText() or self.current_season
        self.titleChanged.emit(
            f"Sports Team Travel Tracker v4.0 - {league} {current_season} Season")

        self.current_travel_data = []
        self.globe_widget.load_flight_data([])

        self.populate_team_combo()

        self.season_status_label.setText(f"{league} - No data loaded")
        self.connectionChanged.emit(f"Connected to ESPN ({league})", True)

        self.load_current_week()

    def start_sports_monitoring(self):
        if not self.sports_aggregator:
            self.setup_sports_system()

        if self.sports_aggregator:
            self.sports_aggregator.set_league(self.current_league)
            self.populate_team_combo()

        self.display_todays_games_startup()

        # Score fatigue for ALL leagues shortly after startup so today's
        # markers (which span leagues) get rings/cards/tooltips with data
        QTimer.singleShot(2000, self._refresh_all_league_fatigue)

        # Default view: auto-start charter tracking for leagues on today's
        # slate once startup work has settled
        QTimer.singleShot(12000, self._auto_start_live_tracking)

    def _refresh_all_league_fatigue(self):
        if not self.sports_aggregator:
            return
        for league in ("MLB", "NBA", "NHL"):
            season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
            self.refresh_fatigue_reports(season, league)

    def get_todays_games(self) -> List[Dict]:
        if not self.sports_aggregator:
            return []

        today = datetime.now().date()
        todays_games = []
        seen_games = set()
        seen_teams = set()  # prevent multiple games per team

        for league in ["MLB", "NBA", "NHL"]:
            try:
                season = self.sports_aggregator.espn_scraper.get_current_season_for_league(league)
                games = self.sports_aggregator.db.load_games(season, league)

                for game in games:
                    game_date = game.date.date() if hasattr(game.date, 'date') else game.date
                    if game_date == today:
                        game_key = f"{game.home_team.team_id}_{game.away_team.team_id}_{game.date.strftime('%Y%m%d_%H%M')}"
                        home_key = f"{league}_{game.home_team.team_id}"
                        away_key = f"{league}_{game.away_team.team_id}"

                        if game_key not in seen_games and home_key not in seen_teams and away_key not in seen_teams:
                            seen_games.add(game_key)
                            seen_teams.add(home_key)
                            seen_teams.add(away_key)

                            todays_games.append({
                                'game': game,
                                'league': league,
                                'home_team': game.home_team,
                                'away_team': game.away_team,
                                'venue_city': game.venue.city
                            })
            except Exception as e:
                logger.warning("Error loading %s games for today: %s", league, e)
                continue

        logger.debug("Total unique games today: %d", len(todays_games))
        return todays_games

    def display_todays_games_startup(self):
        todays_games = self.get_todays_games()

        if not todays_games:
            logger.info("No games today - displaying empty globe")
            self.globe_widget.team_city_markers = []
            return

        stacked_markers = []

        games_by_city = {}
        for game_info in todays_games:
            venue_city = game_info['venue_city']
            games_by_city.setdefault(venue_city, []).append(game_info)

        for venue_city, city_games in games_by_city.items():
            coords = self.globe_widget.get_city_coordinates(venue_city)
            if not coords:
                continue

            lat, lon = coords[0], coords[1]

            for i, game_info in enumerate(city_games):
                home_team = game_info['home_team']
                away_team = game_info['away_team']
                league = game_info['league']

                if len(city_games) > 1:
                    angle = (2 * 3.14159 * i) / len(city_games)
                    radius = 0.8 + (len(city_games) * 0.2)
                    lat_offset = radius * math.cos(angle)
                    lon_offset = radius * math.sin(angle)
                else:
                    lat_offset = 0
                    lon_offset = 0

                adjusted_lat = lat + lat_offset
                adjusted_lon = lon + lon_offset

                cube_3d = self.globe_widget.lat_lon_to_3d(adjusted_lat, adjusted_lon, 1.05)

                split_cube_marker = {
                    'position': cube_3d,
                    'team_id': home_team.team_id,
                    'league': league,
                    'size': 1.5,
                    'type': 'split_cube_today',
                    'city_name': venue_city,
                    'game_info': game_info,
                    'home_team_id': home_team.team_id,
                    'away_team_id': away_team.team_id,
                    'is_split_cube': True
                }
                stacked_markers.append(split_cube_marker)

        self.globe_widget.team_city_markers = stacked_markers

        game_count = len(todays_games)
        leagues_today = set(g['league'] for g in todays_games)
        leagues_str = ", ".join(sorted(leagues_today))

        self.season_status_label.setText(f"Today: {game_count} games ({leagues_str})")
        logger.info("Displaying %d games today across %s", game_count, leagues_str)

        self.update_weather_venues()  # No season = use today's games

    def synchronize_league_state(self):
        self.league_status_label.setText(self.current_league)
        self.update_season_combo_for_league(self.current_league)
        self.titleChanged.emit(
            f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")

    def populate_team_combo(self):
        try:
            if self.sports_aggregator and self.sports_aggregator.current_league != self.current_league:
                self.sports_aggregator.set_league(self.current_league)

            teams = self.sports_aggregator.get_all_teams(self.current_league)
            self.all_teams = teams

            logger.debug("Populating combo with %d %s teams", len(teams), self.current_league)

            try:
                self.focus_team_combo.currentTextChanged.disconnect()
            except Exception:
                pass

            current_selection = self.focus_team_combo.currentData()
            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")

            for team in sorted(teams, key=lambda t: t.display_name):
                display_text = f"{team.display_name} ({team.abbreviation})"
                self.focus_team_combo.addItem(display_text, team.team_id)

            if current_selection:
                index = self.focus_team_combo.findData(current_selection)
                if index >= 0:
                    self.focus_team_combo.setCurrentIndex(index)

            self.focus_team_combo.currentTextChanged.connect(self.on_focus_team_changed)

            self.control_panel.load_teams_for_league(teams)

        except Exception as e:
            logger.exception("Error populating team combo: %s", e)
            self.focus_team_combo.clear()
            self.focus_team_combo.addItem(f"All {self.current_league} Teams", "")
            if hasattr(self.control_panel, 'team_combo'):
                self.control_panel.team_combo.clear()
                self.control_panel.team_combo.addItem("Error loading teams", "")

    def on_season_changed(self, season_text: str):
        if season_text and season_text != self.current_season:
            self.current_season = season_text
            self.titleChanged.emit(
                f"Sports Team Travel Tracker v4.0 - {self.current_league} {self.current_season} Season")
            self.season_status_label.setText(f"{self.current_league} {season_text} - No data loaded")
            self.current_travel_data = []
            self.globe_widget.load_flight_data([])

    def load_full_season(self):
        if not self.sports_aggregator:
            return

        season = self.season_combo.currentText()
        if not season:
            return

        self.load_full_season_btn.setEnabled(False)
        self.load_full_season_btn.setText("Loading Season...")
        self.season_status_label.setText(f"Loading {self.current_league} {season} season schedule...")

        try:
            started = self.sports_aggregator.load_full_season_schedule(season, self.current_league, False)
            if not started:
                self._reset_load_buttons()
                self.statusMessage.emit("A schedule load is already running", 4000)
        except Exception as e:
            self.on_data_error(f"Failed to load {self.current_league} {season} season: {str(e)}")
            self._reset_load_buttons()

    def load_current_week(self):
        if not self.sports_aggregator:
            return

        self.load_current_week_btn.setEnabled(False)
        self.load_current_week_btn.setText("Loading...")

        try:
            started = self.sports_aggregator.get_current_week_schedule(self.current_league)
            if not started:
                self._reset_load_buttons()
                self.statusMessage.emit("A schedule load is already running", 4000)
        except Exception as e:
            self.on_data_error(f"Failed to load current week: {str(e)}")
            self._reset_load_buttons()

    def on_focus_team_changed(self):
        team_id = self.focus_team_combo.currentData()
        season = self.season_combo.currentText()

        logger.debug("Focus team changed to: %s", team_id)

        if hasattr(self.control_panel, 'update_team_selection_programmatically'):
            self.control_panel.update_team_selection_programmatically(team_id or "")
        else:
            if hasattr(self.control_panel, 'team_combo') and self.control_panel.team_combo:
                try:
                    self.control_panel.team_combo.currentTextChanged.disconnect()
                except Exception:
                    pass

                control_panel_index = -1
                for i in range(self.control_panel.team_combo.count()):
                    if self.control_panel.team_combo.itemData(i) == team_id:
                        control_panel_index = i
                        break

                if control_panel_index >= 0:
                    self.control_panel.team_combo.setCurrentIndex(control_panel_index)
                    if team_id and team_id != "":
                        self.control_panel.analyze_btn.setEnabled(True)
                        self.control_panel.analyze_btn.setToolTip(f"Analyze travel for {team_id}")

                self.control_panel.team_combo.currentTextChanged.connect(
                    self.control_panel.on_team_selection_changed)

        if team_id and self.sports_aggregator:
            self.sports_aggregator.load_team_season_schedule(team_id, season, self.current_league)
            self.teamSelected.emit(self.current_league, team_id)
        elif not team_id and self.current_travel_data:
            self.globe_widget.load_flight_data(self.current_travel_data)

        self.update_overlay_games()

    def on_animation_status_changed(self, active: bool, team_id: str):
        if active:
            self.statusMessage.emit(f"Animating {team_id} travel sequence...", 0)
        else:
            self.statusMessage.emit("Animation stopped", 0)

    def on_animation_progress_changed(self, progress: float, segment_info: dict):
        if segment_info:
            current_segment = segment_info.get('departure_city', '') + " → " + segment_info.get('arrival_city', '')
            self.statusMessage.emit(f"Animation: {progress*100:.1f}% - {current_segment}", 0)

    def on_overlay_days_changed(self, days: int):
        self.update_overlay_games()

    def update_overlay_games(self):
        if not hasattr(self, 'games_overlay') or not self.sports_aggregator:
            return

        team_id = self.focus_team_combo.currentData()
        season = self.season_combo.currentText()
        days_ahead = self.games_overlay.get_days_ahead()

        if not team_id or not season:
            self.games_overlay.update_games([])
            return

        db = self.sports_aggregator.db
        games = db.load_games(season, self.current_league)

        cutoff_date = datetime.now() + timedelta(days=days_ahead)

        team_id_lower = team_id.lower() if team_id else ""
        upcoming = [
            g for g in games
            if datetime.now() <= g.date <= cutoff_date
               and (g.home_team.team_id.lower() == team_id_lower or
                    g.away_team.team_id.lower() == team_id_lower)
        ]

        game_strings = []
        for g in upcoming:
            vs = f"{g.away_team.abbreviation} @ {g.home_team.abbreviation}"
            time_str = g.date.strftime("%b %d, %I:%M%p")
            game_strings.append(f"{time_str} - {vs}")

        self.games_overlay.update_games(game_strings)

    def on_season_data_loaded(self, season: str, league: str, game_count: int):
        if league == self.current_league:
            self.season_status_label.setText(
                f"{league} {season}: {game_count:,} games, {len(self.current_travel_data):,} travel records"
            )
            self.update_overlay_games()
            self.update_weather_venues(season)
            self.refresh_fatigue_reports(season, league)

        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")

    # ------------------------------------------------------------- fatigue

    def refresh_fatigue_reports(self, season: str, league: str, days_ahead: int = 14):
        """Recompute schedule-fatigue scores for upcoming games and publish them."""
        if not self.fatigue_engine:
            return
        try:
            reports = self.fatigue_engine.score_upcoming_games(
                league, season, days_ahead=days_ahead)
        except Exception as e:
            logger.exception("Fatigue scoring failed: %s", e)
            return

        self._fatigue_reports = reports
        # Keyed by (league, team_id): team ids collide across leagues ('det'
        # is both Tigers and Red Wings), and we hold all leagues at once
        self._team_fatigue = {k: v for k, v in self._team_fatigue.items()
                              if k[0] != league}
        for report in reports:
            # Keep the soonest report per team (next game = most relevant)
            for tf in (report.home, report.away):
                key = (league, tf.team_id)
                if key not in self._team_fatigue:
                    self._team_fatigue[key] = tf

        logger.info("Fatigue engine: %d upcoming %s games scored", len(reports), league)
        self._stamp_fatigue_markers()
        self.fatigueReportsReady.emit(league, season, reports)

        # Surface the biggest schedule edges in the status line
        flagged = [r for r in reports if abs(r.differential) >= 25.0]
        if flagged:
            top = max(flagged, key=lambda r: abs(r.differential))
            tired = top.away if top.differential > 0 else top.home
            rested = top.home if top.differential > 0 else top.away
            self.statusMessage.emit(
                f"⚠️ Schedule edge: {tired.team_id.upper()} fatigued "
                f"({tired.score:.0f} vs {rested.score:.0f}) "
                f"on {top.game_date.strftime('%b %d')} — {len(flagged)} flagged games", 8000)

    def get_fatigue_reports(self) -> list:
        """Latest list of GameFatigueReport for the current league/season."""
        return self._fatigue_reports

    def get_team_fatigue(self, team_id: str, league: str = None):
        """TeamFatigue for a team's next upcoming game, or None."""
        return self._team_fatigue.get(
            ((league or self.current_league), team_id.lower()))

    # ------------------------------------------------------------- weather

    def update_weather_venues(self, season: str = None):
        if not hasattr(self, 'weather_overlay') or not self.sports_aggregator:
            return

        venues_seen = set()
        venues = []
        cutoff_date = datetime.now() + timedelta(days=7)

        if season:
            db = self.sports_aggregator.db
            games = db.load_games(season, self.current_league)
            upcoming = [g for g in games if datetime.now() <= g.date <= cutoff_date]
        else:
            todays_games = self.get_todays_games()
            upcoming = [g['game'] for g in todays_games]

        for game in upcoming:
            venue = game.venue
            venue_id = venue.venue_id

            if venue_id in venues_seen:
                continue
            venues_seen.add(venue_id)

            if venue.latitude and venue.longitude:
                venues.append({
                    'id': venue_id,
                    'name': f"{venue.name} ({venue.city})",
                    'lat': venue.latitude,
                    'lon': venue.longitude
                })
            else:
                coords = self.globe_widget.get_city_coordinates(venue.city)
                if coords:
                    venues.append({
                        'id': venue_id,
                        'name': f"{venue.name} ({venue.city})",
                        'lat': coords[0],
                        'lon': coords[1]
                    })

        self.weather_overlay.load_venues(venues[:10])

    def force_refresh_season_data(self):
        season = self.season_combo.currentText()
        if season and self.sports_aggregator:
            self.sports_aggregator.load_full_season_schedule(season, self.current_league, force_refresh=True)

    # ------------------------------------------------------------- styling

    def apply_sports_theme(self):
        style = """
        QWidget#TravelVizPanel {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #0A1428, stop:0.5 #051225, stop:1 #020815);
            color: #E0E0E0;
        }

        QGroupBox {
            font-weight: bold;
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 8px;
            margin-top: 1ex;
            padding-top: 15px;
            background-color: rgba(10, 25, 50, 180);
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            left: 15px;
            padding: 0 10px 0 10px;
            color: rgba(200, 220, 255, 255);
            font-size: 12px;
        }

        QPushButton {
            background-color: rgba(0, 100, 180, 160);
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 6px;
            padding: 10px 15px;
            font-weight: bold;
            font-size: 11px;
            min-height: 25px;
            color: white;
        }

        QPushButton:hover {
            background-color: rgba(0, 120, 200, 180);
            border: 1px solid rgba(120, 170, 220, 180);
        }

        QPushButton:pressed {
            background-color: rgba(0, 80, 160, 200);
        }

        QPushButton:disabled {
            background-color: rgba(60, 60, 60, 100);
            color: rgba(150, 150, 150, 150);
            border: 1px solid rgba(80, 80, 80, 100);
        }

        QComboBox, QSpinBox {
            background-color: rgba(20, 40, 70, 200);
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 4px;
            padding: 6px 10px;
            color: white;
            font-size: 11px;
            min-height: 20px;
        }

        QComboBox::drop-down {
            border: none;
            width: 25px;
        }

        QComboBox::down-arrow {
            color: white;
            width: 12px;
            height: 12px;
        }

        QComboBox QAbstractItemView {
            background-color: rgba(15, 30, 55, 250);
            color: white;
            border: 1px solid rgba(100, 150, 200, 150);
            selection-background-color: rgba(0, 120, 200, 180);
        }

        QLabel {
            color: rgba(220, 220, 220, 255);
            font-size: 11px;
        }

        QProgressBar {
            border: 1px solid rgba(100, 150, 200, 150);
            border-radius: 4px;
            text-align: center;
            font-size: 10px;
            background-color: rgba(20, 40, 70, 150);
        }

        QProgressBar::chunk {
            background-color: rgba(0, 150, 250, 200);
            border-radius: 3px;
        }

        QSplitter::handle {
            background-color: rgba(100, 150, 200, 120);
            width: 3px;
        }

        QSplitter::handle:hover {
            background-color: rgba(100, 150, 200, 200);
        }
        """
        self.setStyleSheet(style)

    # ----------------------------------------------------------- misc/info

    def show_league_statistics(self):
        try:
            stats_text = "Multi-League Statistics:\n\n"

            for league in ["MLB", "NBA", "NHL"]:
                teams = self.sports_aggregator.get_all_teams(league)
                cached_seasons = self.sports_aggregator.get_cached_seasons(league)

                stats_text += f"{league}:\n"
                stats_text += f"  Teams: {len(teams)}\n"
                stats_text += f"  Cached Seasons: {len(cached_seasons)}\n"

                if cached_seasons:
                    latest_season = cached_seasons[0]
                    stats_text += f"  Latest Season: {latest_season['season']}\n"
                    stats_text += f"  Games: {latest_season['games_count']}\n"
                    stats_text += f"  Travel Records: {latest_season['travel_count']}\n"

                stats_text += "\n"

            db_stats = self.sports_aggregator.get_database_stats()
            stats_text += f"Database:\n"
            stats_text += f"  Total Teams: {db_stats.get('teams_count', 0)}\n"
            stats_text += f"  Total Games: {db_stats.get('games_count', 0)}\n"
            stats_text += f"  Total Travel Records: {db_stats.get('travel_count', 0)}\n"
            stats_text += f"  Database Size: {db_stats.get('db_size_mb', 0):.1f} MB\n"

            QMessageBox.information(self, "League Statistics", stats_text)

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to get league statistics: {str(e)}")

    def on_travel_data_updated(self, travel_data: List[TeamTravelData]):
        self.current_travel_data = travel_data

        self.globe_widget.load_flight_data(travel_data)
        self.control_panel.update_travel_data(travel_data)

        self.travelCountChanged.emit(len(travel_data))
        self.connectionChanged.emit(f"Connected ({self.current_league})", True)

        # Cache-hit loads (current week, single team) complete via this signal
        # without ever emitting seasonDataLoaded — restore the buttons here too,
        # or they sit on "Loading..." forever.
        self._reset_load_buttons()
        if travel_data:
            self.season_status_label.setText(
                f"{self.current_league}: {len(travel_data):,} travel records loaded")

    def _reset_load_buttons(self):
        self.load_full_season_btn.setEnabled(True)
        self.load_full_season_btn.setText("Load Full Season")
        self.load_current_week_btn.setEnabled(True)
        self.load_current_week_btn.setText("Current Week")

    def start_team_animation_dialog(self):
        available_teams = self.globe_widget.get_available_teams()
        if not available_teams:
            QMessageBox.information(
                self, "No Teams",
                f"No {self.current_league} teams available for animation. Load season data first.")
            return

        team_id, ok = QInputDialog.getItem(
            self, "Select Team", f"Choose {self.current_league} team to animate:",
            available_teams, 0, False)
        if ok and team_id:
            success = self.globe_widget.start_team_animation(team_id)
            if success:
                self.statusMessage.emit(f"Started animation for {team_id}", 3000)
            else:
                QMessageBox.warning(self, "Animation Failed", f"Could not start animation for {team_id}")

    def on_progress_updated(self, progress: int):
        if 0 < progress < 100:
            self.season_progress.setVisible(True)
            self.season_progress.setValue(progress)
        else:
            self.season_progress.setVisible(False)
        self.progressChanged.emit(progress)

    def on_data_error(self, error_message: str):
        self.connectionChanged.emit("Error", False)
        self.statusMessage.emit(f"Error: {error_message}", 10000)
        self._reset_load_buttons()

    def on_location_selected(self, lat: float, lon: float, location_name: str):
        self.statusMessage.emit(f"Location: {location_name} ({lat:.2f}, {lon:.2f})", 5000)

    def on_marker_clicked(self, marker_id: str, marker_data: dict):
        # Enrich the tooltip with live score + fatigue intel when we have it
        marker_data = dict(marker_data)

        live = marker_data.get('live')
        if live:
            progress = live.get('progress') or ''
            marker_data['live_summary'] = (
                f"LIVE  {live.get('away_score', '?')}–{live.get('home_score', '?')}"
                + (f"  ·  {progress}" if progress and progress.upper() != 'LIVE' else ''))

        fatigue_lines = []
        marker_league = marker_data.get('league') or self.current_league
        for key in ('away_team_id', 'home_team_id', 'team_id'):
            team_id = marker_data.get(key)
            if not team_id:
                continue
            tf = self.get_team_fatigue(team_id, marker_league)
            if tf and not any(team_id in line for line in fatigue_lines):
                fatigue_lines.append(
                    f"{team_id.upper()}: fatigue {tf.score:.0f} "
                    f"({tf.miles_14d:,.0f} mi/14d, rest {tf.rest_days}d)")
        if fatigue_lines:
            marker_data['fatigue_summary'] = fatigue_lines

        # Game cubes get the on-globe game card instead of the floating
        # tooltip; the tooltip stays for non-game markers (e.g. flights)
        if not marker_data.get('game_info'):
            cursor_pos = self.globe_widget.mapToGlobal(self.globe_widget.mapFromGlobal(
                self.globe_widget.cursor().pos()
            ))
            self.venue_tooltip.show_marker(marker_data, cursor_pos)

        city = marker_data.get('city_name', marker_data.get('city', ''))
        self.statusMessage.emit(f"Selected: {marker_id} - {city}", 3000)

        # Publish game selection for host applications (EffortOdds)
        game_info = marker_data.get('game_info')
        if game_info:
            league = marker_data.get('league', self.current_league)
            game = game_info.get('game')
            payload = {
                'league': league,
                'home_team': marker_data.get('home_team_id'),
                'away_team': marker_data.get('away_team_id'),
                'venue_city': marker_data.get('city_name'),
                'game_date': getattr(game, 'date', None),
            }
            self.gameSelected.emit(league, payload)

            # Feed the cube-unfold panel: fetch flashscore lineups off-thread
            self._maybe_fetch_lineups(marker_data)

    # ------------------------------------------------------------- lineups

    def _maybe_fetch_lineups(self, marker: dict):
        """Fetch flashscore game details for a clicked cube: lineups, and the
        FINAL SCORE for finished games (the day feed keeps finished events
        with scores; the live feed drops them). Stamps marker['lineups'] /
        marker['final'] when they arrive.

        Cache policy: a result with a final score is immutable — reuse it.
        Anything else (pregame/live) is stamped immediately for instant
        display but still refreshed in the background (lineups post late,
        games finish)."""
        if not self.sports_aggregator:
            return
        game = (marker.get('game_info') or {}).get('game')
        league = (marker.get('league') or self.current_league).upper()
        if game is None or league not in ('MLB', 'NBA', 'NHL'):
            return

        key = self.globe_widget._marker_game_key(marker)
        cached = self._lineups_cache.get(key)
        if cached is not None:
            if cached.get('lineups') is not None:
                marker['lineups'] = cached['lineups']
            if cached.get('final') is not None:
                marker['final'] = cached['final']
            self.globe_widget.update()
            if cached.get('final') is not None:
                return  # game over: nothing can change
        if key in self._lineups_pending:
            return
        self._lineups_pending.add(key)
        if cached is None:
            marker['lineups_pending'] = True

        live = marker.get('live') or {}
        is_live = bool(live)
        event_id = live.get('event_id')
        game_id = getattr(game, 'game_id', '') or ''
        if not event_id and game_id.startswith('fs_'):
            event_id = game_id[3:]
        home_id = (marker.get('home_team_id') or '').lower()
        away_id = (marker.get('away_team_id') or '').lower()
        game_date = getattr(game, 'date', None)
        source = self.sports_aggregator.flashscore

        def work():
            lineups = None
            final = None
            try:
                # Not live: pull the day-feed event — final score for
                # finished games, event id for ESPN-id games
                ev = None
                if not is_live and game_date is not None:
                    ev = source.find_event(league, home_id, away_id, game_date)
                eid = event_id or (ev.event_id if ev else None)
                if ev is not None and ev.stage == 'finished':
                    final = {'away': ev.away_score, 'home': ev.home_score}
                if eid:
                    lineups = source.fetch_game_lineups(league, eid)
            except Exception as e:
                logger.warning("game detail fetch failed for %s: %s", key, e)
            self._lineupsArrived.emit(key, {'lineups': lineups, 'final': final})

        threading.Thread(target=work, daemon=True,
                         name="travelviz-lineups").start()

    def _on_lineups_arrived(self, key, data):
        """GUI-thread: cache game details and stamp matching markers."""
        self._lineups_pending.discard(key)
        lineups = data.get('lineups')
        final = data.get('final')
        if lineups is not None or final is not None:
            self._lineups_cache[key] = data
        for marker in self.globe_widget.team_city_markers:
            if not marker.get('game_info'):
                continue
            if self.globe_widget._marker_game_key(marker) == key:
                marker.pop('lineups_pending', None)
                if final is not None:
                    marker['final'] = final
                if lineups is not None:
                    marker['lineups'] = lineups
                elif marker.get('lineups') is None:
                    # Signal "tried, nothing available" to the panel
                    marker['lineups'] = {'posted': False,
                                         'home': {'players': [], 'starter': None},
                                         'away': {'players': [], 'starter': None}}
        self.globe_widget.update()

    def toggle_travel_paths(self, checked: bool):
        self.globe_widget.set_display_options(checked, True, True, True)

    def toggle_team_cities(self, checked: bool):
        self.globe_widget.set_display_options(True, checked, True, True)

    def toggle_day_night(self, checked: bool):
        self.globe_widget.show_day_night = checked
        self.globe_widget.update()

    def on_weather_venue_selected(self, lat: float, lon: float, venue_name: str):
        self.globe_widget.center_on_location(lat, lon)
        self.statusMessage.emit(f"Centered on {venue_name}", 3000)

    def on_weather_overlay_toggled(self, is_expanded: bool):
        if not hasattr(self, 'weather_overlay') or not hasattr(self, 'globe_widget'):
            return
        globe_width = self.globe_widget.width()
        overlay_width = self.weather_overlay.width()
        self.weather_overlay.move(globe_width - overlay_width, 10)

    def export_travel_data(self):
        if not self.current_travel_data:
            QMessageBox.information(self, "No Data", f"No {self.current_league} travel data to export.")
            return

        season = self.season_combo.currentText()
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Travel Data",
            f"{self.current_league.lower()}_travel_{season}_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            "JSON Files (*.json);;CSV Files (*.csv)"
        )

        if filename:
            try:
                export_data = []
                for travel in self.current_travel_data:
                    export_data.append({
                        'league': self.current_league,
                        'team_name': travel.team_name,
                        'team_id': travel.team_id,
                        'departure_city': travel.departure_city,
                        'arrival_city': travel.arrival_city,
                        'game_date': travel.game_date.isoformat() if travel.game_date else None,
                        'travel_date': travel.travel_date.isoformat() if travel.travel_date else None,
                        'confidence': travel.confidence,
                        'season': season
                    })

                with open(filename, 'w') as f:
                    json.dump(export_data, f, indent=2)

                self.statusMessage.emit(
                    f"Exported {len(export_data)} {self.current_league} travel records to {filename}", 5000)

            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export data: {str(e)}")

    # ----------------------------------------------------------- lifecycle

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'weather_overlay') and hasattr(self, 'globe_widget'):
            globe_width = self.globe_widget.width()
            overlay_width = self.weather_overlay.width()
            self.weather_overlay.move(globe_width - overlay_width, 10)

    def showEvent(self, event):
        super().showEvent(event)
        # Live polling only while the panel is actually visible (tab shown)
        self._ensure_live_feed()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.live_feed is not None:
            self.live_feed.stop(join_timeout=0.1)

    def shutdown(self):
        """Stop threads/timers. Call from the host's closeEvent."""
        if self.live_feed is not None:
            self.live_feed.stop()

        if hasattr(self, '_fs_refresh_timer'):
            self._fs_refresh_timer.stop()
        if self.flight_tracker and self.live_tracking_active:
            logger.info("Stopping flight tracker on exit...")
            self.flight_tracker.stop()
            self.flight_tracker.wait(5000)

        if self.direct_tracker and self.direct_tracker.isRunning():
            logger.info("Stopping direct flight tracker on exit...")
            self.direct_tracker.stop()
            self.direct_tracker.wait(5000)

        if self.sports_aggregator:
            self.sports_aggregator.shutdown()

        if hasattr(self, 'weather_overlay'):
            self.weather_overlay.cleanup()
