"""
Flight Tracker Panel - Revamped for Maximum Utility
Displays team travel intelligence with live flight tracking integration
"""
from typing import List, Dict, Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
    QPushButton, QComboBox, QSpinBox, QProgressBar, QSizePolicy,
    QGraphicsDropShadowEffect
)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QColor
from datetime import datetime, timedelta

from database_manager import TeamInfo, GameData


class FlightControlPanel(QWidget):
    """Revamped flight control panel with maximum data utilization"""
    
    # Signals
    modeChanged = pyqtSignal(str)
    teamChanged = pyqtSignal(str)
    refreshRequested = pyqtSignal()
    amadeusAnalysisRequested = pyqtSignal(str, int)
    trackOnGlobeRequested = pyqtSignal(str)  # icao24
    
    # Fonts
    FONT_HEADER = QFont("Segoe UI", 10, QFont.Weight.Bold)
    FONT_TITLE = QFont("Consolas", 9, QFont.Weight.Bold)
    FONT_BODY = QFont("Segoe UI", 9)
    FONT_SMALL = QFont("Consolas", 8)
    FONT_MONO = QFont("Consolas", 8)
    
    # Colors
    COLOR_BG = "#0d1117"
    COLOR_CARD = "#161b22"
    COLOR_BORDER = "#30363d"
    COLOR_ACCENT = "#58a6ff"
    COLOR_SUCCESS = "#3fb950"
    COLOR_WARNING = "#d29922"
    COLOR_DANGER = "#f85149"
    COLOR_MUTED = "#8b949e"
    COLOR_TEXT = "#e6edf3"
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_league = "NHL"
        self.current_intelligence = None
        self.live_flight = None
        self.city_timezones = self._load_city_timezones()
        
        self._init_ui()
        self._apply_styles()
        self._connect_signals()
    
    def _load_city_timezones(self) -> Dict[str, int]:
        """UTC offsets for major cities"""
        return {
            "New York": -5, "Boston": -5, "Philadelphia": -5, "Washington": -5,
            "Miami": -5, "Atlanta": -5, "Detroit": -5, "Cleveland": -5,
            "Chicago": -6, "Milwaukee": -6, "Minneapolis": -6, "Dallas": -6,
            "Houston": -6, "San Antonio": -6, "New Orleans": -6, "Memphis": -6,
            "Denver": -7, "Phoenix": -7, "Salt Lake City": -7,
            "Los Angeles": -8, "San Francisco": -8, "Seattle": -8, "Portland": -8,
            "Las Vegas": -8, "Sacramento": -8, "San Diego": -8,
            "Toronto": -5, "Montreal": -5, "Vancouver": -8, "Calgary": -7,
            "Edmonton": -7, "Winnipeg": -6, "Ottawa": -5,
        }
    
    def _init_ui(self):
        """Initialize the UI layout"""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)
        
        # Section 1: Header Bar
        self.header_widget = self._create_header_section()
        layout.addWidget(self.header_widget)
        
        # Section 2: Live Flight Banner (hidden by default)
        self.live_flight_banner = self._create_live_flight_banner()
        self.live_flight_banner.setVisible(False)
        layout.addWidget(self.live_flight_banner)
        
        # Section 3: Route Timeline (scrollable)
        self.route_scroll = self._create_route_timeline()
        layout.addWidget(self.route_scroll, 1)
        
        # Section 4: Alerts & Recommendations
        self.alerts_widget = self._create_alerts_section()
        layout.addWidget(self.alerts_widget)
        
        # Section 5: Trip Summary
        self.summary_widget = self._create_summary_section()
        layout.addWidget(self.summary_widget)
    
    def _create_header_section(self) -> QFrame:
        """Create the header bar with team info and key metrics"""
        frame = QFrame()
        frame.setObjectName("headerFrame")
        frame.setFixedHeight(70)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        
        # Top row: Team label + selectors (compact)
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        
        # Team display - use abbreviation
        self.team_label = QLabel("---")
        self.team_label.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
        self.team_label.setStyleSheet(f"color: {self.COLOR_SUCCESS}; background: transparent;")
        top_row.addWidget(self.team_label)
        
        # Hidden live indicator (kept for API compatibility)
        self.live_indicator = QLabel("")
        self.live_indicator.setVisible(False)
        
        # League selector - right after team label
        self.league_combo = QComboBox()
        self.league_combo.addItems(["MLB", "NBA", "NHL"])
        self.league_combo.setCurrentText("NHL")
        self.league_combo.setFixedWidth(58)
        top_row.addWidget(self.league_combo)
        
        # Team selector (tight spacing)
        self.team_combo = QComboBox()
        self.team_combo.setFixedWidth(70)
        self.team_combo.setPlaceholderText("Team")
        top_row.addWidget(self.team_combo)
        
        # Days selector
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 30)
        self.days_spin.setValue(14)
        self.days_spin.setSuffix("d")
        self.days_spin.setFixedWidth(48)
        top_row.addWidget(self.days_spin)
        
        # Analyze button
        self.analyze_btn = QPushButton("RUN")
        self.analyze_btn.setFixedWidth(70)
        self.analyze_btn.setEnabled(False)
        top_row.addWidget(self.analyze_btn)
        
        top_row.addStretch()
        
        layout.addLayout(top_row)
        
        # Bottom row: Key metrics
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(20)
        
        self.metric_miles = self._create_metric_label("--", "miles")
        self.metric_games = self._create_metric_label("--", "games")
        self.metric_risk = self._create_metric_label("--", "risk")
        self.metric_tz = self._create_metric_label("--", "tz hops")
        
        metrics_row.addWidget(self.metric_miles)
        metrics_row.addWidget(self.metric_games)
        metrics_row.addWidget(self.metric_risk)
        metrics_row.addWidget(self.metric_tz)
        metrics_row.addStretch()
        
        # Progress bar (hidden until analysis)
        self.analysis_progress = QProgressBar()
        self.analysis_progress.setFixedHeight(3)
        self.analysis_progress.setTextVisible(False)
        self.analysis_progress.setVisible(False)
        metrics_row.addWidget(self.analysis_progress)
        
        layout.addLayout(metrics_row)
        
        return frame
    
    def _create_metric_label(self, value: str, label: str) -> QWidget:
        """Create a compact metric display"""
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        value_lbl = QLabel(value)
        value_lbl.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        value_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        value_lbl.setObjectName(f"metric_{label}_value")
        
        label_lbl = QLabel(label)
        label_lbl.setFont(self.FONT_SMALL)
        label_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        
        layout.addWidget(value_lbl)
        layout.addWidget(label_lbl)
        
        return widget
    
    def _create_live_flight_banner(self) -> QFrame:
        """Create the live flight tracking banner"""
        frame = QFrame()
        frame.setObjectName("liveBanner")
        frame.setFixedHeight(60)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        
        # Top row: Flight info
        top_row = QHBoxLayout()
        
        self.live_icon = QLabel("✈️ IN TRANSIT")
        self.live_icon.setFont(self.FONT_TITLE)
        self.live_icon.setStyleSheet(f"color: {self.COLOR_ACCENT}; background: transparent;")
        top_row.addWidget(self.live_icon)
        
        self.live_callsign = QLabel("---")
        self.live_callsign.setFont(self.FONT_MONO)
        self.live_callsign.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        top_row.addWidget(self.live_callsign)
        
        self.live_aircraft = QLabel("---")
        self.live_aircraft.setFont(self.FONT_MONO)
        self.live_aircraft.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        top_row.addWidget(self.live_aircraft)
        
        self.live_altitude = QLabel("---")
        self.live_altitude.setFont(self.FONT_MONO)
        self.live_altitude.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        top_row.addWidget(self.live_altitude)
        
        self.live_speed = QLabel("---")
        self.live_speed.setFont(self.FONT_MONO)
        self.live_speed.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        top_row.addWidget(self.live_speed)
        
        top_row.addStretch()
        
        self.track_globe_btn = QPushButton("Track on Globe")
        self.track_globe_btn.setFixedWidth(100)
        top_row.addWidget(self.track_globe_btn)
        
        layout.addLayout(top_row)
        
        # Bottom row: Progress and ETA
        bottom_row = QHBoxLayout()
        
        self.live_route = QLabel("--- → ---")
        self.live_route.setFont(self.FONT_MONO)
        self.live_route.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        bottom_row.addWidget(self.live_route)
        
        self.live_progress_bar = QProgressBar()
        self.live_progress_bar.setFixedHeight(8)
        self.live_progress_bar.setFixedWidth(150)
        self.live_progress_bar.setTextVisible(False)
        bottom_row.addWidget(self.live_progress_bar)
        
        self.live_eta = QLabel("ETA --")
        self.live_eta.setFont(self.FONT_MONO)
        self.live_eta.setStyleSheet(f"color: {self.COLOR_SUCCESS}; background: transparent;")
        bottom_row.addWidget(self.live_eta)
        
        bottom_row.addStretch()
        
        layout.addLayout(bottom_row)
        
        return frame
    
    def _create_route_timeline(self) -> QScrollArea:
        """Create the scrollable route timeline"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setObjectName("routeScroll")
        
        self.routes_container = QWidget()
        self.routes_layout = QVBoxLayout(self.routes_container)
        self.routes_layout.setSpacing(8)
        self.routes_layout.setContentsMargins(0, 0, 0, 0)
        self.routes_layout.addStretch()
        
        # Placeholder
        placeholder = QLabel("Select a team and click ANALYZE to view travel intelligence")
        placeholder.setFont(self.FONT_BODY)
        placeholder.setStyleSheet(f"color: {self.COLOR_MUTED}; padding: 40px;")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setObjectName("routePlaceholder")
        self.routes_layout.insertWidget(0, placeholder)
        
        scroll.setWidget(self.routes_container)
        return scroll
    
    def _create_alerts_section(self) -> QFrame:
        """Create the alerts and recommendations section"""
        frame = QFrame()
        frame.setObjectName("alertsFrame")
        frame.setMaximumHeight(100)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        
        # Header
        header = QLabel("⚠️ ALERTS & INSIGHTS")
        header.setFont(self.FONT_TITLE)
        header.setStyleSheet(f"color: {self.COLOR_WARNING}; background: transparent;")
        layout.addWidget(header)
        
        # Alerts container
        self.alerts_container = QVBoxLayout()
        self.alerts_container.setSpacing(2)
        
        placeholder = QLabel("No alerts")
        placeholder.setFont(self.FONT_SMALL)
        placeholder.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        placeholder.setObjectName("alertsPlaceholder")
        self.alerts_container.addWidget(placeholder)
        
        layout.addLayout(self.alerts_container)
        
        return frame
    
    def _create_summary_section(self) -> QFrame:
        """Create the trip summary statistics section"""
        frame = QFrame()
        frame.setObjectName("summaryFrame")
        frame.setFixedHeight(65)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        
        # Header
        header = QLabel("📊 TRIP STATISTICS")
        header.setFont(self.FONT_TITLE)
        header.setStyleSheet(f"color: {self.COLOR_ACCENT}; background: transparent;")
        layout.addWidget(header)
        
        # Stats row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(24)
        
        self.stat_total = self._create_stat_item("Total", "--")
        self.stat_avg = self._create_stat_item("Avg/Game", "--")
        self.stat_longest = self._create_stat_item("Longest", "--")
        self.stat_rest = self._create_stat_item("Rest Days", "--")
        self.stat_complexity = self._create_stat_item("Complexity", "--")
        
        stats_row.addWidget(self.stat_total)
        stats_row.addWidget(self.stat_avg)
        stats_row.addWidget(self.stat_longest)
        stats_row.addWidget(self.stat_rest)
        stats_row.addWidget(self.stat_complexity)
        stats_row.addStretch()
        
        layout.addLayout(stats_row)
        
        return frame
    
    def _create_stat_item(self, label: str, value: str) -> QWidget:
        """Create a stat display item"""
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        value_lbl = QLabel(value)
        value_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        value_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        value_lbl.setObjectName(f"stat_{label.replace('/', '_')}_value")
        
        label_lbl = QLabel(label)
        label_lbl.setFont(QFont("Segoe UI", 7))
        label_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        
        layout.addWidget(value_lbl)
        layout.addWidget(label_lbl)
        
        return widget
    
    def _create_route_card(self, route, index: int, total_routes: int, 
                          intelligence: 'TeamTravelIntelligence') -> QFrame:
        """Create a route card with full details"""
        frame = QFrame()
        frame.setObjectName("routeCard")
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        
        # Get data
        game = route.game_data
        travel_data = route.travel_data if hasattr(route, 'travel_data') and route.travel_data else None
        
        # Determine game date
        game_date = game.date if game else (travel_data.game_date if travel_data else datetime.now())
        opponent = game.home_team.display_name if game else (travel_data.opponent if travel_data else "Unknown")
        venue_name = game.venue.name if game and game.venue else "Unknown Venue"
        venue_city = game.venue.city if game and game.venue else (travel_data.arrival_city if travel_data else "Unknown")
        
        # Departure/arrival
        dep_city = travel_data.departure_city if travel_data else "Home"
        arr_city = travel_data.arrival_city if travel_data else venue_city
        dep_airport = travel_data.departure_airport if travel_data else "---"
        arr_airport = travel_data.arrival_airport if travel_data else "---"
        
        # Road trip context
        road_game_num = travel_data.homestand_game_number if travel_data else None
        series_game = travel_data.series_game_number if travel_data else None
        
        # === Header Row ===
        header_row = QHBoxLayout()
        
        # Date badge
        date_str = game_date.strftime("%a %m/%d")
        date_lbl = QLabel(date_str)
        date_lbl.setFont(self.FONT_TITLE)
        date_lbl.setStyleSheet(f"color: {self.COLOR_ACCENT}; background: transparent;")
        header_row.addWidget(date_lbl)
        
        # Confidence badge
        conf = route.travel_confidence if hasattr(route, 'travel_confidence') else "MEDIUM"
        conf_color = self.COLOR_SUCCESS if conf == "HIGH" else self.COLOR_WARNING if conf == "MEDIUM" else self.COLOR_DANGER
        conf_lbl = QLabel(f"[{conf}]")
        conf_lbl.setFont(self.FONT_SMALL)
        conf_lbl.setStyleSheet(f"color: {conf_color}; background: transparent;")
        header_row.addWidget(conf_lbl)
        
        # Back-to-back detection
        if index > 0 and intelligence.upcoming_routes:
            prev_route = intelligence.upcoming_routes[index - 1]
            prev_date = prev_route.game_data.date if prev_route.game_data else None
            if prev_date and (game_date - prev_date).total_seconds() < 86400:
                b2b_lbl = QLabel("⚡ B2B")
                b2b_lbl.setFont(self.FONT_SMALL)
                b2b_lbl.setStyleSheet(f"color: {self.COLOR_DANGER}; background: transparent;")
                header_row.addWidget(b2b_lbl)
        
        header_row.addStretch()
        
        # Distance
        distance = route.travel_distance if hasattr(route, 'travel_distance') else 0
        dist_lbl = QLabel(f"{distance:,.0f} mi")
        dist_lbl.setFont(self.FONT_MONO)
        dist_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        header_row.addWidget(dist_lbl)
        
        layout.addLayout(header_row)
        
        # === Opponent & Venue Row ===
        venue_row = QHBoxLayout()
        venue_row.setSpacing(4)
        
        # Truncate opponent name if needed
        opp_display = opponent[:20] + "…" if len(opponent) > 20 else opponent
        opp_lbl = QLabel(f"@ {opp_display}")
        opp_lbl.setFont(self.FONT_BODY)
        opp_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        venue_row.addWidget(opp_lbl)
        
        # Truncate venue name
        venue_display = venue_name[:18] + "…" if len(venue_name) > 18 else venue_name
        venue_lbl = QLabel(f"· {venue_display}")
        venue_lbl.setFont(self.FONT_SMALL)
        venue_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
        venue_row.addWidget(venue_lbl)
        
        venue_row.addStretch()
        
        layout.addLayout(venue_row)
        
        # === Route Row ===
        route_row = QHBoxLayout()
        
        route_str = f"{dep_airport} → {arr_airport}"
        route_lbl = QLabel(route_str)
        route_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        route_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
        route_row.addWidget(route_lbl)
        
        # Road trip context
        if road_game_num:
            ctx_lbl = QLabel(f"· Road Game {road_game_num}")
            ctx_lbl.setFont(self.FONT_SMALL)
            ctx_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
            route_row.addWidget(ctx_lbl)
        
        # Timezone change
        tz_diff = self._calculate_timezone_diff(dep_city, arr_city)
        if tz_diff != 0:
            tz_str = f"+{tz_diff}h" if tz_diff > 0 else f"{tz_diff}h"
            tz_lbl = QLabel(f"· {tz_str} TZ")
            tz_lbl.setFont(self.FONT_SMALL)
            tz_lbl.setStyleSheet(f"color: {self.COLOR_WARNING}; background: transparent;")
            route_row.addWidget(tz_lbl)
        
        route_row.addStretch()
        
        layout.addLayout(route_row)
        
        # === Airport Info ===
        if route.primary_airport:
            airport_row = QHBoxLayout()
            airport_row.setSpacing(6)
            
            primary = route.primary_airport
            otp = primary.on_time_probability if hasattr(primary, 'on_time_probability') else 0.85
            otp_pct = int(otp * 100) if otp <= 1 else int(otp)
            otp_color = self.COLOR_SUCCESS if otp_pct >= 80 else self.COLOR_WARNING if otp_pct >= 65 else self.COLOR_DANGER
            
            dist_km = primary.distance_from_venue if hasattr(primary, 'distance_from_venue') else 0
            
            airport_lbl = QLabel(f"🛫 {primary.iata_code} {otp_pct}% · {dist_km:.0f}km")
            airport_lbl.setFont(self.FONT_SMALL)
            airport_lbl.setStyleSheet(f"color: {otp_color}; background: transparent;")
            airport_row.addWidget(airport_lbl)
            
            # Alternate airports - more compact
            if hasattr(route, 'alternate_airports') and route.alternate_airports:
                alts = route.alternate_airports[:2]
                alt_strs = [f"{a.iata_code}" for a in alts]
                if alt_strs:
                    alt_lbl = QLabel(f"ALT: {'/'.join(alt_strs)}")
                    alt_lbl.setFont(self.FONT_SMALL)
                    alt_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
                    airport_row.addWidget(alt_lbl)
            
            airport_row.addStretch()
            layout.addLayout(airport_row)
        
        # === Hotel Info ===
        if hasattr(route, 'destination_hotels') and route.destination_hotels:
            hotel = route.destination_hotels[0]
            stars = self._get_star_rating(hotel)
            dist = hotel.distance_from_venue if hasattr(hotel, 'distance_from_venue') else 0
            
            hotel_row = QHBoxLayout()
            hotel_row.setSpacing(4)
            
            # Truncate hotel name more aggressively
            hotel_name = hotel.name[:22] + "…" if len(hotel.name) > 22 else hotel.name
            hotel_lbl = QLabel(f"🏨 {hotel_name}")
            hotel_lbl.setFont(self.FONT_SMALL)
            hotel_lbl.setStyleSheet(f"color: {self.COLOR_TEXT}; background: transparent;")
            hotel_row.addWidget(hotel_lbl)
            
            # Stars and distance
            detail_lbl = QLabel(f"{stars} {dist:.1f}km")
            detail_lbl.setFont(self.FONT_SMALL)
            detail_lbl.setStyleSheet(f"color: {self.COLOR_WARNING}; background: transparent;")
            hotel_row.addWidget(detail_lbl)
            
            # Additional hotels count
            if len(route.destination_hotels) > 1:
                more_lbl = QLabel(f"+{len(route.destination_hotels) - 1}")
                more_lbl.setFont(self.FONT_SMALL)
                more_lbl.setStyleSheet(f"color: {self.COLOR_MUTED}; background: transparent;")
                hotel_row.addWidget(more_lbl)
            
            hotel_row.addStretch()
            layout.addLayout(hotel_row)
        
        # === Risk Factors ===
        if hasattr(route, 'risk_factors') and route.risk_factors:
            for risk in route.risk_factors[:2]:
                risk_lbl = QLabel(f"⚠️ {risk}")
                risk_lbl.setFont(self.FONT_SMALL)
                risk_lbl.setStyleSheet(f"color: {self.COLOR_WARNING}; background: transparent;")
                layout.addWidget(risk_lbl)
        
        return frame
    
    def _calculate_timezone_diff(self, from_city: str, to_city: str) -> int:
        """Calculate timezone difference between cities"""
        from_tz = self.city_timezones.get(from_city, -5)
        to_tz = self.city_timezones.get(to_city, -5)
        return to_tz - from_tz
    
    def _get_star_rating(self, hotel) -> str:
        """Convert hotel rating to star display"""
        if hasattr(hotel, 'forbes_rating') and hotel.forbes_rating:
            rating = hotel.forbes_rating.get_numeric_rating()
            return "★" * max(2, min(5, rating))
        elif hasattr(hotel, 'overall_rating'):
            if hotel.overall_rating >= 90:
                return "★★★★★"
            elif hotel.overall_rating >= 80:
                return "★★★★"
            elif hotel.overall_rating >= 70:
                return "★★★"
            else:
                return "★★"
        return "★★★"
    
    def _apply_styles(self):
        """Apply stylesheet to the panel"""
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {self.COLOR_BG};
                color: {self.COLOR_TEXT};
                font-family: 'Segoe UI', Arial, sans-serif;
            }}
            
            QFrame#headerFrame {{
                background-color: {self.COLOR_CARD};
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 8px;
            }}
            
            QFrame#liveBanner {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1a3a5c, stop:1 #0d2137);
                border: 1px solid {self.COLOR_ACCENT};
                border-radius: 8px;
            }}
            
            QFrame#routeCard {{
                background-color: {self.COLOR_CARD};
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 6px;
            }}
            
            QFrame#routeCard:hover {{
                border-color: {self.COLOR_ACCENT};
            }}
            
            QFrame#alertsFrame {{
                background-color: {self.COLOR_CARD};
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 8px;
            }}
            
            QFrame#summaryFrame {{
                background-color: {self.COLOR_CARD};
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 8px;
            }}
            
            QScrollArea#routeScroll {{
                background: transparent;
                border: none;
            }}
            
            QScrollArea#routeScroll > QWidget > QWidget {{
                background: transparent;
            }}
            
            QComboBox {{
                background-color: #21262d;
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                color: {self.COLOR_TEXT};
                font-size: 11px;
            }}
            
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            
            QComboBox QAbstractItemView {{
                background-color: #21262d;
                border: 1px solid {self.COLOR_BORDER};
                selection-background-color: {self.COLOR_ACCENT};
            }}
            
            QSpinBox {{
                background-color: #21262d;
                border: 1px solid {self.COLOR_BORDER};
                border-radius: 4px;
                padding: 4px;
                color: {self.COLOR_TEXT};
                font-size: 11px;
            }}
            
            QPushButton {{
                background-color: {self.COLOR_ACCENT};
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                color: white;
                font-weight: 600;
                font-size: 11px;
            }}
            
            QPushButton:hover {{
                background-color: #79b8ff;
            }}
            
            QPushButton:disabled {{
                background-color: #21262d;
                color: {self.COLOR_MUTED};
            }}
            
            QProgressBar {{
                background-color: #21262d;
                border: none;
                border-radius: 2px;
            }}
            
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {self.COLOR_ACCENT}, stop:1 {self.COLOR_SUCCESS});
                border-radius: 2px;
            }}
            
            QScrollBar:vertical {{
                background: {self.COLOR_BG};
                width: 8px;
                border-radius: 4px;
            }}
            
            QScrollBar::handle:vertical {{
                background: {self.COLOR_BORDER};
                border-radius: 4px;
                min-height: 30px;
            }}
            
            QScrollBar::handle:vertical:hover {{
                background: {self.COLOR_MUTED};
            }}
            
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
    
    def _connect_signals(self):
        """Connect UI signals"""
        self.league_combo.currentTextChanged.connect(self._on_league_changed)
        self.team_combo.currentTextChanged.connect(self._on_team_changed)
        self.analyze_btn.clicked.connect(self._on_analyze_clicked)
        self.track_globe_btn.clicked.connect(self._on_track_globe_clicked)
    
    def _on_league_changed(self, league: str):
        """Handle league selection change"""
        self.current_league = league
        self.modeChanged.emit(league)
    
    def _on_team_changed(self, text: str):
        """Handle team selection change"""
        if self.team_combo.currentData():
            self.analyze_btn.setEnabled(True)
            self.teamChanged.emit(self.team_combo.currentData())
        else:
            self.analyze_btn.setEnabled(False)
    
    def _on_analyze_clicked(self):
        """Handle analyze button click"""
        team_id = self.team_combo.currentData()
        if team_id:
            self.analysis_progress.setVisible(True)
            self.analysis_progress.setValue(0)
            self.analyze_btn.setEnabled(False)
            self.analyze_btn.setText("...")
            self.amadeusAnalysisRequested.emit(team_id, self.days_spin.value())
    
    def _on_track_globe_clicked(self):
        """Handle track on globe button click"""
        if self.live_flight and hasattr(self.live_flight, 'icao24'):
            self.trackOnGlobeRequested.emit(self.live_flight.icao24)
    
    # === Public API ===
    
    def set_league(self, league: str):
        """Set the current league"""
        self.current_league = league
        self.league_combo.setCurrentText(league)
    
    def load_teams_for_league(self, teams: List['TeamInfo']):
        """Load teams into the combo box"""
        self.team_combo.blockSignals(True)
        self.team_combo.clear()
        self.team_combo.addItem("Team...", "")
        
        for team in sorted(teams, key=lambda t: t.display_name):
            # Use abbreviation for compact display
            self.team_combo.addItem(f"{team.abbreviation}", team.team_id)
        
        self.team_combo.blockSignals(False)
    
    def update_team_selection_programmatically(self, team_id: str):
        """Update team selection without triggering signals"""
        self.team_combo.blockSignals(True)
        for i in range(self.team_combo.count()):
            if self.team_combo.itemData(i) == team_id:
                self.team_combo.setCurrentIndex(i)
                self.analyze_btn.setEnabled(True)
                break
        self.team_combo.blockSignals(False)
    
    def on_analysis_progress(self, percentage: int, message: str):
        """Handle analysis progress updates"""
        self.analysis_progress.setValue(percentage)
    
    def on_analysis_complete(self, intelligence: 'TeamTravelIntelligence'):
        """Handle completed analysis"""
        self.current_intelligence = intelligence
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("...")
        
        if intelligence:
            self._update_display(intelligence)
    
    def on_analysis_error(self, error: str):
        """Handle analysis error"""
        self.analysis_progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("ANALYZE")
    
    def update_live_flight(self, flight_data: dict):
        """Update live flight display"""
        self.live_flight = flight_data
        self.live_flight_banner.setVisible(True)
        
        self.live_callsign.setText(flight_data.get('callsign', '---'))
        self.live_aircraft.setText(flight_data.get('aircraft_type', '---'))
        
        alt = flight_data.get('altitude_ft', 0)
        self.live_altitude.setText(f"FL{int(alt/100)}" if alt else "---")
        
        speed = flight_data.get('speed_kts', 0)
        self.live_speed.setText(f"{int(speed)}kts" if speed else "---")
        
        # Route and progress
        origin = flight_data.get('origin_airport', '---')
        dest = flight_data.get('destination_airport', '---')
        self.live_route.setText(f"{origin} → {dest}")
        
        progress = flight_data.get('progress', 0)
        self.live_progress_bar.setValue(int(progress * 100))
        
        eta = flight_data.get('eta_minutes', 0)
        if eta:
            hours = int(eta // 60)
            mins = int(eta % 60)
            self.live_eta.setText(f"ETA {hours}h {mins}m" if hours else f"ETA {mins}m")
    
    def clear_live_flight(self):
        """Clear live flight display"""
        self.live_flight = None
        self.live_flight_banner.setVisible(False)
    
    def _update_display(self, intelligence: 'TeamTravelIntelligence'):
        """Update all display elements with new intelligence"""
        # Update team label - use abbreviation to prevent clipping
        if intelligence.team_info:
            abbr = intelligence.team_info.abbreviation.upper()
            self.team_label.setText(abbr)
        
        # Update header metrics
        self._update_metric(self.metric_miles, f"{intelligence.total_travel_distance:,.0f}")
        self._update_metric(self.metric_games, str(len(intelligence.upcoming_routes)))
        
        complexity = intelligence.travel_complexity_score if hasattr(intelligence, 'travel_complexity_score') else 0
        self._update_metric(self.metric_risk, f"{complexity:.0f}/100")
        
        # Calculate timezone hops
        tz_hops = self._calculate_total_tz_hops(intelligence)
        self._update_metric(self.metric_tz, str(tz_hops))
        
        # Clear and rebuild routes
        self._clear_routes()
        
        if intelligence.upcoming_routes:
            for i, route in enumerate(intelligence.upcoming_routes):
                card = self._create_route_card(route, i, len(intelligence.upcoming_routes), intelligence)
                self.routes_layout.insertWidget(i, card)
        
        # Update alerts
        self._update_alerts(intelligence)
        
        # Update summary stats
        self._update_summary(intelligence)
    
    def _update_metric(self, widget: QWidget, value: str):
        """Update a metric widget's value"""
        value_lbl = widget.findChild(QLabel)
        if value_lbl:
            value_lbl.setText(value)
    
    def _calculate_total_tz_hops(self, intelligence) -> int:
        """Calculate total timezone changes across all routes"""
        if not intelligence.upcoming_routes:
            return 0
        
        total_hops = 0
        for route in intelligence.upcoming_routes:
            if hasattr(route, 'travel_data') and route.travel_data:
                td = route.travel_data
                diff = abs(self._calculate_timezone_diff(td.departure_city, td.arrival_city))
                if diff > 0:
                    total_hops += 1
        return total_hops
    
    def _clear_routes(self):
        """Clear all route cards"""
        while self.routes_layout.count() > 1:
            item = self.routes_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
    
    def _update_alerts(self, intelligence):
        """Update alerts section"""
        # Clear existing alerts
        while self.alerts_container.count():
            item = self.alerts_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        alerts = []
        
        # Check for high-risk routes
        high_risk = [r for r in intelligence.upcoming_routes 
                    if hasattr(r, 'travel_confidence') and r.travel_confidence == "LOW"]
        if high_risk:
            alerts.append((f"⚠️ {len(high_risk)} high-risk route(s) flagged", self.COLOR_DANGER))
        
        # Check for back-to-backs
        b2b_count = 0
        for i, route in enumerate(intelligence.upcoming_routes[1:], 1):
            prev = intelligence.upcoming_routes[i-1]
            if route.game_data and prev.game_data:
                diff = (route.game_data.date - prev.game_data.date).total_seconds()
                if diff < 86400:
                    b2b_count += 1
        if b2b_count:
            alerts.append((f"⚡ {b2b_count} back-to-back game(s) detected", self.COLOR_WARNING))
        
        # Aggregate risk factors
        all_risks = set()
        for route in intelligence.upcoming_routes:
            if hasattr(route, 'risk_factors') and route.risk_factors:
                all_risks.update(route.risk_factors)
        for risk in list(all_risks)[:2]:
            alerts.append((f"⚠️ {risk}", self.COLOR_WARNING))
        
        # Add recommendations
        if hasattr(intelligence, 'recommendations') and intelligence.recommendations:
            for rec in intelligence.recommendations[:2]:
                alerts.append((f"💡 {rec}", self.COLOR_ACCENT))
        
        # Display alerts
        if alerts:
            for text, color in alerts:
                lbl = QLabel(text)
                lbl.setFont(self.FONT_SMALL)
                lbl.setStyleSheet(f"color: {color}; background: transparent;")
                self.alerts_container.addWidget(lbl)
        else:
            lbl = QLabel("✓ No alerts - all routes look good")
            lbl.setFont(self.FONT_SMALL)
            lbl.setStyleSheet(f"color: {self.COLOR_SUCCESS}; background: transparent;")
            self.alerts_container.addWidget(lbl)
    
    def _update_summary(self, intelligence):
        """Update summary statistics"""
        routes = intelligence.upcoming_routes
        
        if not routes:
            return
        
        # Total miles
        total = intelligence.total_travel_distance
        self._update_stat(self.stat_total, f"{total:,.0f} mi")
        
        # Average per game
        avg = total / len(routes) if routes else 0
        self._update_stat(self.stat_avg, f"{avg:,.0f} mi")
        
        # Longest leg
        longest = max((r.travel_distance for r in routes if hasattr(r, 'travel_distance')), default=0)
        self._update_stat(self.stat_longest, f"{longest:,.0f} mi")
        
        # Rest days (days with no games)
        if len(routes) >= 2:
            total_days = 0
            rest_days = 0
            for i, route in enumerate(routes[1:], 1):
                prev = routes[i-1]
                if route.game_data and prev.game_data:
                    days_between = (route.game_data.date - prev.game_data.date).days
                    total_days += days_between
                    if days_between > 1:
                        rest_days += days_between - 1
            self._update_stat(self.stat_rest, f"{rest_days}/{total_days}")
        
        # Complexity score
        complexity = intelligence.travel_complexity_score if hasattr(intelligence, 'travel_complexity_score') else 0
        self._update_stat(self.stat_complexity, f"{complexity:.0f}/100")
    
    def _update_stat(self, widget: QWidget, value: str):
        """Update a stat widget's value"""
        for child in widget.findChildren(QLabel):
            if child.objectName().endswith("_value"):
                child.setText(value)
                break
    
    # === Compatibility methods for existing travelViz integration ===
    
    def update_ui_for_league(self, league: str):
        """Legacy compatibility method"""
        self.set_league(league)
    
    def update_travel_data(self, travel_data):
        """Legacy compatibility method"""
        pass
    
    def update_intelligence_display(self, intelligence):
        """Legacy compatibility method"""
        self._update_display(intelligence)
