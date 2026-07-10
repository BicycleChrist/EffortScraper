import qasync
import asyncio
import aiohttp
import time as _time   # module-level alias for the per-frame Live hot path (no per-call import)
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QHBoxLayout, QScrollArea, QSizePolicy,
    QSpinBox, QMessageBox, QListWidget, QListWidgetItem, QFrame,
    QLineEdit, QStyle, QStyleOptionComboBox, QStylePainter, QAbstractItemView
)
from PyQt6.QtGui import QColor, QPixmap, QPainter, QFont, QBrush, QPen, QFontMetrics, QAction
from PyQt6.QtWidgets import QApplication as _QApplication

# The volume heat map's QWebEngineView (imported lazily on demand) requires
# AA_ShareOpenGLContexts to be set BEFORE the QApplication is created. This module
# is imported before the app exists (EffortOdds.py imports it at top-level, ahead
# of QApplication([])), so set it here. Guarded: no-op if an app already exists.
if _QApplication.instance() is None:
    _QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

from KalshiClient import KalshiClient, KalshiStreamClient, KalshiLiveBook
from polymarketquery import (PolymarketSportsClient, PolymarketStreamClient,
                             PolymarketLiveBook, place_pm_order, get_pm_open_orders,
                             cancel_pm_order)
import re
import math
import bisect
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

# Native (C++/simdjson) order-book parsers. Optional: if the compiled module
# isn't present we fall back to the pure-Python books transparently. Currently
# used for the Polymarket live book in this widget (a trial before wider use).
# Build with: cmake -S cppparser -B cppparser/build && cmake --build cppparser/build
try:
    import os as __os_for_parsers
    sys = __import__('sys')
    __cpp_dir = __os_for_parsers.path.join(
        __os_for_parsers.path.dirname(__os_for_parsers.path.abspath(__file__)), 'cppparser')
    if __cpp_dir not in sys.path:
        sys.path.insert(0, __cpp_dir)
    import QuickieParse as _native_parsers
    NATIVE_PARSERS = True
except Exception as _np_err:  # pragma: no cover - environment dependent
    _native_parsers = None
    NATIVE_PARSERS = False
    print(f"[parsers] native QuickieParse unavailable, using Python books: {_np_err}")

# [PERF-DIAG] Timing-probe prints ([task-probe]/[sportmatch-probe]/
# [allsports-probe]/[post-probe]) are dormant unless the app is launched with
# EFFORTODDS_PERF_DIAG=1 — same switch as the stall watchdog in EffortOdds
# main(). See PERF_DIAGNOSTICS.md.
import os as _os
PERF_DIAG = _os.environ.get("EFFORTODDS_PERF_DIAG") == "1"

# Smoother lines/markers for the historical odds plot.
pg.setConfigOptions(antialias=True)

# Central team-logo asset root (shared repo dir), resolved relative to THIS
# file so it works regardless of the host app's working directory.
LOGO_DIR = Path(__file__).resolve().parent.parent / "TeamLogos"

# Sportsbook/exchange brand logos (kalshi.png, polymarket.png, ...) used in the
# crosshair hover readout in place of plain colored dots.
SPORTSBOOK_LOGO_DIR = Path(__file__).resolve().parent / "sportsbooklogos"

# Series line colors keyed by bookmaker. Kalshi=green, Polymarket=blue (matches
# their brand colors). Paid-OddsAPI books fall back to the palette by index.
BOOKMAKER_LINE_COLORS = {
    # Venue identity hues match each exchange's BRAND colour (sampled from their
    # logos) so a glance at a line/percentage reads as the right venue: Kalshi the
    # mint green of its mark, Polymarket its royal blue. Both pop on the near-black
    # terminal screen. The candle bodies use a softer bull green / bear red, so the
    # saturated Kalshi mint stays distinct from a green candle.
    'kalshi': (0, 211, 151),        # Kalshi brand mint-green
    'polymarket': (46, 92, 255),    # Polymarket brand blue
}
FALLBACK_LINE_COLORS = [
    (255, 127, 14), (214, 39, 40), (148, 103, 189),
    (140, 86, 75), (23, 190, 207), (188, 189, 34),
]

# Event/game start marker line color (amber — distinct from green/blue series).
# The x-axis "Time" label shows a matching legend swatch when the line is drawn.
START_LINE_COLOR = (224, 168, 80)
MARKET_POST_LINE_COLOR = (110, 170, 200)  # cool blue, distinct from the amber start

# Filenames under SPORTSBOOK_LOGO_DIR by bookmaker key (for the hover readout).
BOOKMAKER_LOGO_FILE = {
    'kalshi': 'kalshi_alt.png',  # compact "K" mark; renders cleaner at small sizes
    'polymarket': 'polymarket.png',
}

# Canonical team name (as produced by EventMatcher.normalize_team_name) ->
# logo filename under LOGO_DIR/<LEAGUE>/. Verified against the on-disk assets;
# teams without a dedicated PNG fall back to their league logo. See generation
# note: built from EventMatcher canonicals.
LOGO_FILE_BY_TEAM = {
    'MLB': {
        'arizona diamondbacks': 'Diamondbacks.png', 'atlanta braves': 'Braves.png',
        'baltimore orioles': 'Orioles.png', 'boston red sox': 'Redsox.png',
        'chicago white sox': 'WhiteSox.png', 'chicago cubs': 'Cubs.png',
        'cincinnati reds': 'Reds.png', 'cleveland guardians': 'Guardians.png',
        'colorado rockies': 'Rockies.png', 'detroit tigers': 'Tigers.png',
        'houston astros': 'Astros.png', 'kansas city royals': 'Royals.png',
        'los angeles angels': 'Angels.png', 'los angeles dodgers': 'Dodgers.png',
        'miami marlins': 'Marlins.png', 'milwaukee brewers': 'Brewers.png',
        'minnesota twins': 'Twins.png', 'new york yankees': 'Yankees.png',
        'new york mets': 'Mets.png', 'oakland athletics': 'Athletics.png',
        'philadelphia phillies': 'Phillies.png', 'pittsburgh pirates': 'Pirates.png',
        'san diego padres': 'Padres.png', 'san francisco giants': 'Giants.png',
        'seattle mariners': 'Mariners.png', 'st. louis cardinals': 'Cardinals.png',
        'tampa bay rays': 'Rays.png', 'texas rangers': 'RangersTX.png',
        'toronto blue jays': 'BlueJays.png', 'washington nationals': 'Nationals.png',
    },
    'NBA': {
        'atlanta hawks': 'Hawks.png', 'boston celtics': 'Celtics.png',
        'brooklyn nets': 'Nets.png', 'charlotte hornets': 'Hornets.png',
        'chicago bulls': 'Bulls.png', 'cleveland cavaliers': 'Cavaliers.png',
        'dallas mavericks': 'Mavericks.png', 'denver nuggets': 'Nuggets.png',
        'detroit pistons': 'Pistons.png', 'golden state warriors': 'Warriors.png',
        'houston rockets': 'Rockets.png', 'indiana pacers': 'Pacers.png',
        'la clippers': 'Clippers.png', 'la lakers': 'Lakers.png',
        'memphis grizzlies': 'Grizzles.png', 'milwaukee bucks': 'Bucks.png',
        'minnesota timberwolves': 'Timberwolves.png', 'new orleans pelicans': 'Pelicans.png',
        'new york knicks': 'Knicks.png', 'oklahoma city thunder': 'Thunder.png',
        'orlando magic': 'Magic.png', 'philadelphia 76ers': '76ers.png',
        'phoenix suns': 'Suns.png', 'portland trail blazers': 'TrailBlazers.png',
        'sacramento kings': 'SACKings.png', 'san antonio spurs': 'Spurs.png',
        'toronto raptors': 'Raptors.png', 'utah jazz': 'Jazz.png',
        'washington wizards': 'Wizards.png',
    },
    'NFL': {
        'arizona cardinals': 'Cardinals.png', 'atlanta falcons': 'Falcons.png',
        'baltimore ravens': 'Ravens.png', 'buffalo bills': 'Bills.png',
        'carolina panthers': 'Panthers.png', 'chicago bears': 'Bears.png',
        'cincinnati bengals': 'Bengals.png', 'cleveland browns': 'Browns.png',
        'dallas cowboys': 'Cowboys.png', 'denver broncos': 'Broncos.png',
        'detroit lions': 'Lions.png', 'green bay packers': 'Packers.png',
        'houston texans': 'Texans.png', 'indianapolis colts': 'Colts.png',
        'jacksonville jaguars': 'Jaguars.png', 'kansas city chiefs': 'Chiefs.png',
        'las vegas raiders': 'Raiders.png', 'los angeles chargers': 'Chargers.png',
        'los angeles rams': 'Rams.png', 'miami dolphins': 'Dolphins.png',
        'minnesota vikings': 'Vikings.png', 'new england patriots': 'Patriots.png',
        'new orleans saints': 'Saints.png', 'new york giants': 'NYGiants.png',
        'new york jets': 'NYJets.png', 'philadelphia eagles': 'Eagles.png',
        'pittsburgh steelers': 'Steelers.png', 'san francisco 49ers': '49ers.png',
        'seattle seahawks': 'Seahawks.png', 'tampa bay buccaneers': 'Buccaneers.png',
        'tennessee titans': 'Titans.png', 'washington commanders': 'Commanders.png',
    },
    'NHL': {
        'carolina hurricanes': 'Hurricanes.png', 'columbus blue jackets': 'BlueJackets.png',
        'new jersey devils': 'Devils.png', 'new york islanders': 'Islanders.png',
        'new york rangers': 'Rangers.png', 'philadelphia flyers': 'Flyers.png',
        'pittsburgh penguins': 'Penguins.png', 'washington capitals': 'Capitals.png',
        'boston bruins': 'Bruins.png', 'buffalo sabres': 'Sabers.png',
        'detroit red wings': 'RedWings.png', 'florida panthers': 'Panthers.png',
        'montreal canadiens': 'Canadiens.png', 'ottawa senators': 'Senators.png',
        'tampa bay lightning': 'Lightning.png', 'toronto maple leafs': 'MapleLeafs.png',
        'arizona coyotes': 'Coyotes.png', 'chicago blackhawks': 'Blackhawks.png',
        'colorado avalanche': 'Avalanche.png', 'dallas stars': 'Stars.png',
        'minnesota wild': 'Wild.png', 'nashville predators': 'Predators.png',
        'st. louis blues': 'Blues.png', 'winnipeg jets': 'Jets.png',
        'anaheim ducks': 'Ducks.png', 'calgary flames': 'Flames.png',
        'edmonton oilers': 'Oilers.png', 'los angeles kings': 'Kings.png',
        'san jose sharks': 'Sharks.png', 'seattle kraken': 'NHLleague.png',
        'utah hockey club': 'NHLleague.png', 'vancouver canucks': 'Canucks.png',
        'vegas golden knights': 'GoldenKnights.png',
    },
}

# Per-process pixmap cache so repeated market switches never re-decode a PNG.
_LOGO_PIXMAP_CACHE = {}


def resolve_team_logo(team_name, sport, size=64, opacity=0.72):
    """Return a dimmed QPixmap for a team's logo, or None if unavailable.

    Lookup chain: canonical name -> per-team PNG -> league PNG. Results
    (including misses) are cached by (sport, canonical, size, opacity) so the
    per-tick plot path never pays decode/scale cost.
    """
    sport = (sport or '').upper()
    canonical = EventMatcher.normalize_team_name(team_name, sport)
    cache_key = (sport, canonical, size, opacity)
    if cache_key in _LOGO_PIXMAP_CACHE:
        return _LOGO_PIXMAP_CACHE[cache_key]

    filename = LOGO_FILE_BY_TEAM.get(sport, {}).get(canonical)
    if not filename:
        # Unknown team (e.g. an Over/Under or spread outcome): no logo.
        _LOGO_PIXMAP_CACHE[cache_key] = None
        return None

    path = LOGO_DIR / sport / filename
    src = QPixmap(str(path))
    if src.isNull():
        _LOGO_PIXMAP_CACHE[cache_key] = None
        return None

    scaled = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    # Pre-dim into a transparent canvas so the watermark reads without
    # fighting the odds lines (cheaper than per-paint opacity at render time).
    dimmed = QPixmap(scaled.size())
    dimmed.fill(Qt.GlobalColor.transparent)
    painter = QPainter(dimmed)
    painter.setOpacity(opacity)
    painter.drawPixmap(0, 0, scaled)
    painter.end()

    _LOGO_PIXMAP_CACHE[cache_key] = dimmed
    return dimmed

#TODO: Refactor TEAM_ALIASES dict so each league; everything is under the NFL right now LOL
@dataclass
class UnifiedEvent:
    """Unified event representing a game across multiple prediction markets"""
    sport: str
    home_team: str
    away_team: str
    start_time: str

    # Kalshi data
    kalshi_event_ticker: Optional[str] = None
    kalshi_series_ticker: Optional[str] = None
    kalshi_markets: Optional[list] = None

    # Polymarket data
    polymarket_game_id: Optional[str] = None
    polymarket_title: Optional[str] = None
    polymarket_markets: Optional[list] = None

    # Futures (season-long, N-candidate markets — World Series, MVP, Cy Young …).
    # When True this is NOT a head-to-head game: kalshi_markets holds one market per
    # candidate (team/player), each yes_sub_title = the candidate name. future_label
    # is the human title ("World Series", "AL MVP"). home_team/away_team are unused.
    is_future: bool = False
    future_label: Optional[str] = None

    def get_display_title(self):
        """Get formatted title for display"""
        if self.is_future:
            src = "[K]"  # futures are Kalshi-only for now
            return f"{src} 🏆 {self.future_label or self.home_team}"
        sources = []
        if self.kalshi_event_ticker:
            sources.append("K")
        if self.polymarket_game_id:
            sources.append("P")
        source_str = f"[{'+'.join(sources)}]" if sources else ""

        # Format the date if available
        date_str = ""
        if self.start_time:
            try:
                # Both Kalshi and Polymarket use ISO format with timestamp
                # Kalshi: "2023-11-07T05:31:56Z" (has 'Z' suffix)
                # Polymarket: "2025-11-25T00:00:00" (no 'Z', but is UTC)
                from datetime import datetime, timezone
                if 'T' in self.start_time:
                    # Parse ISO datetime - handle both with and without 'Z' suffix
                    if self.start_time.endswith('Z'):
                        dt_str = self.start_time.replace('Z', '+00:00')
                    elif '+' not in self.start_time and self.start_time.count(':') >= 2:
                        # No timezone specified, assume UTC (common for Polymarket)
                        dt_str = self.start_time + '+00:00'
                    else:
                        dt_str = self.start_time

                    dt = datetime.fromisoformat(dt_str)

                    # Only convert to local time if we have actual time info (not just date)
                    # Kalshi events parsed from ticker are midnight UTC (date only)
                    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                        # This is likely a date-only field (from Kalshi ticker)
                        # Don't convert timezone - just show the date
                        date_str = dt.strftime("%m/%d").lstrip('0')
                    else:
                        # This has actual time info (from Polymarket) - convert to local
                        dt_local = dt.astimezone()
                        date_str = dt_local.strftime("%m/%d %I:%M%p").lstrip('0').replace(' 0', ' ')
                # Fallback for date-only format (shouldn't happen but just in case)
                else:
                    dt = datetime.fromisoformat(self.start_time)
                    date_str = dt.strftime("%m/%d").lstrip('0')
            except Exception as e:
                # If parsing fails, just skip the date
                # Could happen with malformed timestamps
                pass

        # Build display string: [K+P] [NFL] 1/15 7:00PM Away @ Home
        sport_tag = f"[{self.sport}]" if self.sport else ""
        date_tag = f"{date_str} " if date_str else ""

        return f"{source_str} {sport_tag} {date_tag}{self.away_team} @ {self.home_team}"

    def has_kalshi(self):
        return self.kalshi_event_ticker is not None

    def has_polymarket(self):
        return self.polymarket_game_id is not None


@dataclass
class UnifiedMarket:
    """Unified market representing a specific bet across multiple prediction markets"""
    market_type: str  # 'moneyline', 'spread', 'total', 'prop'
    display_name: str

    # Kalshi data - can be a list for moneyline (2 markets, one per team)
    kalshi_markets: Optional[list] = None  # List of market dicts
    kalshi_tickers: Optional[list] = None  # List of tickers
    kalshi_titles: Optional[list] = None   # List of titles
    kalshi_event_ticker: Optional[str] = None

    # Polymarket data
    polymarket_market: Optional[object] = None
    polymarket_id: Optional[str] = None
    polymarket_question: Optional[str] = None
    polymarket_game_id: Optional[str] = None

    def has_kalshi(self):
        return self.kalshi_markets is not None and len(self.kalshi_markets) > 0

    def has_polymarket(self):
        return self.polymarket_market is not None

    def get_source_indicator(self):
        """Get [K], [P], or [K+P] indicator"""
        sources = []
        if self.has_kalshi():
            sources.append("K")
        if self.has_polymarket():
            sources.append("P")
        return f"[{'+'.join(sources)}]" if sources else ""


class EventMatcher:
    """Matches sports events across Kalshi and Polymarket APIs"""

    # Teams that share names across sports - require sport context for disambiguation
    CROSS_SPORT_CONFLICTS = {
        'cardinals': ['NFL', 'MLB'],  # Arizona Cardinals (NFL) vs St. Louis Cardinals (MLB)
        'rangers': ['NHL', 'MLB'],     # New York Rangers (NHL) vs Texas Rangers (MLB)
        'kings': ['NHL', 'NBA'],       # Los Angeles Kings (NHL) vs Sacramento Kings (NBA)
        'panthers': ['NFL', 'NHL'],    # Carolina Panthers (NFL) vs Florida Panthers (NHL)
        'jets': ['NFL', 'NHL'],        # New York Jets (NFL) vs Winnipeg Jets (NHL)
        'giants': ['NFL', 'MLB'],      # New York Giants (NFL) vs San Francisco Giants (MLB)
    }

    @staticmethod
    def parse_kalshi_event_date(event_ticker: str) -> str:
        """
        Parse game date from Kalshi event ticker.

        Format: KXNHLGAME-25NOV20OTTANA -> 2025-11-20T00:00:00Z
        Format: KXNFLGAME-24NOV21MIABUF -> 2024-11-21T00:00:00Z

        Args:
            event_ticker: Kalshi event ticker string

        Returns:
            ISO 8601 datetime string, or empty string if parsing fails
        """
        import re
        from datetime import datetime

        # Pattern: series-YYMONDDteams
        # Example: KXNHLGAME-25NOV20OTTANA
        match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', event_ticker)
        if not match:
            return ''

        year_short, month_abbr, day = match.groups()

        # Convert 2-digit year to 4-digit (20XX range)
        year = f"20{year_short}"

        # Month abbreviations
        months = {
            'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
            'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
            'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'
        }

        month = months.get(month_abbr, '01')

        # Return as ISO datetime (using midnight UTC as we don't have exact time)
        return f"{year}-{month}-{day}T00:00:00Z"

    @staticmethod
    def parse_kalshi_event_datetime(event_ticker: str):
        """
        Parse the full game start datetime (UTC) from a Kalshi event ticker.

        Kalshi tickers encode the date AND a 4-digit UTC time after the day,
        e.g. KXMLBGAME-26MAY301915ATLCIN -> 2026-05-30 19:15 UTC. The time is
        what disambiguates two same-matchup games on different days, which
        parse_kalshi_event_date() (date-only) cannot do.

        Returns a timezone-aware datetime, or None if the ticker can't be parsed.
        """
        import re
        from datetime import datetime, timezone

        match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})?', event_ticker)
        if not match:
            return None

        year_short, month_abbr, day, hhmm = match.groups()
        months = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
            'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }
        month = months.get(month_abbr)
        if not month:
            return None

        hour, minute = 0, 0
        if hhmm:
            hour, minute = int(hhmm[:2]), int(hhmm[2:])

        try:
            return datetime(int(f"20{year_short}"), month, int(day),
                            hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def dates_compatible(event_ticker: str, poly_start_time, tolerance_hours: float = 12.0) -> bool:
        """
        Check whether a Kalshi event and a Polymarket game refer to the same
        calendar game, by comparing start times.

        Two same-matchup games (e.g. a doubleheader or back-to-back days) share
        team names but differ in start time, so team-only matching can pair the
        live game's Polymarket market with a different day's (untraded) Kalshi
        event - which renders as a flat Kalshi line next to a moving PM line.

        Returns True when the start times are within tolerance_hours. If the
        Kalshi ticker has no time component, or either side's time is
        missing/unparseable, returns True so matching falls back to team-only
        behavior rather than dropping a valid match.
        """
        import re
        from datetime import datetime

        # Only enforce the time window when the ticker actually encodes a time
        # (HHMM after the day). Date-only tickers can't disambiguate by time, so
        # fall back to team-only matching.
        m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})', event_ticker)
        if not m:
            return True

        k_dt = EventMatcher.parse_kalshi_event_datetime(event_ticker)
        if k_dt is None or not poly_start_time:
            return True

        try:
            p_dt = datetime.fromisoformat(str(poly_start_time).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return True

        if p_dt.tzinfo is None:
            from datetime import timezone
            p_dt = p_dt.replace(tzinfo=timezone.utc)

        return abs((k_dt - p_dt).total_seconds()) <= tolerance_hours * 3600.0

    @staticmethod
    def poly_game_is_stale(poly_start_time, max_age_hours: float = 8.0) -> bool:
        """True when a Polymarket game started more than max_age_hours ago.

        Gamma keeps a game's events active well past the final out, so
        yesterday's games linger as Polymarket-only rows (the Kalshi side,
        which is filtered by status='open', is already gone). No game runs
        8 hours, so anything older is settled — but today's live games stay.
        Unknown/unparseable start times are NOT stale (kept, to be safe)."""
        from datetime import datetime, timezone
        if not poly_start_time:
            return False
        try:
            p_dt = datetime.fromisoformat(str(poly_start_time).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return False
        if p_dt.tzinfo is None:
            p_dt = p_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - p_dt).total_seconds() / 3600.0
        return age > max_age_hours

    @staticmethod
    def start_time_delta_hours(event_ticker: str, poly_start_time) -> float:
        """|Kalshi start - Polymarket start| in hours, or +inf when either
        side is missing/unparseable.

        Used to pick the BEST Polymarket candidate instead of the first
        team-matching one: PM lists each game of a back-to-back series /
        playoff series / doubleheader separately, all sharing team names.
        Date-only Kalshi tickers parse to midnight UTC, which still
        separates consecutive days (24h) cleanly."""
        from datetime import datetime, timezone
        k_dt = EventMatcher.parse_kalshi_event_datetime(event_ticker)
        if k_dt is None or not poly_start_time:
            return float('inf')
        try:
            p_dt = datetime.fromisoformat(str(poly_start_time).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return float('inf')
        if p_dt.tzinfo is None:
            p_dt = p_dt.replace(tzinfo=timezone.utc)
        return abs((k_dt - p_dt).total_seconds()) / 3600.0

    # Team name variations for matching
    TEAM_ALIASES_BY_SPORT = {
    'NFL': {
        'arizona cardinals': ['arizona', 'cardinals', 'ari', 'az'],
        'atlanta falcons': ['atlanta', 'falcons', 'atl'],
        'baltimore ravens': ['baltimore', 'ravens', 'bal'],
        'buffalo bills': ['buffalo', 'bills', 'buf'],
        'carolina panthers': ['carolina', 'panthers', 'car'],
        'chicago bears': ['chicago', 'bears', 'chi'],
        'cincinnati bengals': ['cincinnati', 'bengals', 'cin'],
        'cleveland browns': ['cleveland', 'browns', 'cle'],
        'dallas cowboys': ['dallas', 'cowboys', 'dal'],
        'denver broncos': ['denver', 'broncos', 'den'],
        'detroit lions': ['detroit', 'lions', 'det'],
        'green bay packers': ['green bay', 'packers', 'gb'],
        'houston texans': ['houston', 'texans', 'hou'],
        'indianapolis colts': ['indianapolis', 'colts', 'ind'],
        'jacksonville jaguars': ['jacksonville', 'jaguars', 'jax'],
        'kansas city chiefs': ['kansas city', 'chiefs', 'kc'],
        'las vegas raiders': ['las vegas', 'raiders', 'lv', 'oak', 'oakland'],
        'los angeles chargers': ['los angeles chargers', 'la chargers', 'chargers', 'lac'],
        'los angeles rams': ['los angeles rams', 'la rams', 'rams', 'lar', 'los angeles r'],
        'miami dolphins': ['miami', 'dolphins', 'mia'],
        'minnesota vikings': ['minnesota', 'vikings', 'min'],
        'new england patriots': ['new england', 'patriots', 'ne'],
        'new orleans saints': ['new orleans', 'saints', 'no'],
        'new york giants': ['new york giants', 'ny giants', 'giants', 'nyg', 'new york g'],
        'new york jets': ['new york jets', 'ny jets', 'jets', 'nyj', 'new york j'],
        'philadelphia eagles': ['philadelphia', 'eagles', 'phi'],
        'pittsburgh steelers': ['pittsburgh', 'steelers', 'pit'],
        'san francisco 49ers': ['san francisco', '49ers', 'sf'],
        'seattle seahawks': ['seattle', 'seahawks', 'sea'],
        'tampa bay buccaneers': ['tampa bay', 'buccaneers', 'bucs', 'tb'],
        'tennessee titans': ['tennessee', 'titans', 'ten'],
        'washington commanders': ['washington', 'commanders', 'wash', 'wsh', 'was']
    },

    'NBA': {
        'atlanta hawks': ['atlanta', 'hawks', 'atl'],
        'boston celtics': ['boston', 'celtics', 'bos'],
        'brooklyn nets': ['brooklyn', 'nets', 'bkn'],
        'charlotte hornets': ['charlotte', 'hornets', 'cha'],
        'chicago bulls': ['chicago', 'bulls', 'chi'],
        'cleveland cavaliers': ['cleveland', 'cavaliers', 'cavs', 'cle'],
        'dallas mavericks': ['dallas', 'mavericks', 'mavs', 'dal'],
        'denver nuggets': ['denver', 'nuggets', 'den'],
        'detroit pistons': ['detroit', 'pistons', 'det'],
        'golden state warriors': ['golden state', 'warriors', 'gsw'],
        'houston rockets': ['houston', 'rockets', 'hou'],
        'indiana pacers': ['indiana', 'pacers', 'ind'],
        'la clippers': ['los angeles clippers', 'la clippers', 'clippers', 'lac', 'los angeles c', 'la c'],
        'la lakers': ['los angeles lakers', 'la lakers', 'lakers', 'lal', 'los angeles l', 'la l'],
        'memphis grizzlies': ['memphis', 'grizzlies', 'mem'],
        'milwaukee bucks': ['milwaukee', 'bucks', 'mil'],
        'minnesota timberwolves': ['minnesota', 'timberwolves', 'wolves', 'min'],
        'new orleans pelicans': ['new orleans', 'pelicans', 'no'],
        'new york knicks': ['new york knicks', 'knicks', 'nyk', 'ny knicks', 'nyknicks'],
        'oklahoma city thunder': ['oklahoma city', 'thunder', 'okc'],
        'orlando magic': ['orlando', 'magic', 'orl'],
        'philadelphia 76ers': ['philadelphia', '76ers', 'sixers', 'phi'],
        'phoenix suns': ['phoenix', 'suns', 'phx'],
        'portland trail blazers': ['portland', 'trail blazers', 'blazers', 'por'],
        'sacramento kings': ['sacramento', 'kings', 'sac'],
        'san antonio spurs': ['san antonio', 'spurs', 'sas'],
        'toronto raptors': ['toronto', 'raptors', 'tor'],
        'utah jazz': ['utah', 'jazz', 'uta', 'utah j'],
        'washington wizards': ['washington', 'wizards', 'wash', 'wsh']
    },

    'NHL': {
        # Metropolitan Division
        'carolina hurricanes': ['carolina', 'hurricanes', 'canes', 'car'],
        'columbus blue jackets': ['columbus', 'blue jackets', 'jackets', 'cbj'],
        'new jersey devils': ['new jersey', 'devils', 'njd', 'nj'],
        'new york islanders': ['new york i', 'new york islanders', 'ny islanders', 'islanders', 'isles', 'nyi'],
        'new york rangers': ['new york r', 'new york rangers', 'ny rangers', 'rangers', 'nyr'],
        'philadelphia flyers': ['philadelphia', 'flyers', 'phi'],
        'pittsburgh penguins': ['pittsburgh', 'penguins', 'pens', 'pit'],
        'washington capitals': ['washington', 'capitals', 'caps', 'wsh', 'was'],

        # Atlantic Division
        'boston bruins': ['boston', 'bruins', 'bos'],
        'buffalo sabres': ['buffalo', 'sabres', 'buf'],
        'detroit red wings': ['detroit', 'red wings', 'wings', 'det'],
        'florida panthers': ['florida', 'panthers', 'fla'],
        'montreal canadiens': ['montreal', 'canadiens', 'habs', 'mtl'],
        'ottawa senators': ['ottawa', 'senators', 'sens', 'ott'],
        'tampa bay lightning': ['tampa bay', 'lightning', 'bolts', 'tb', 'tbl'],
        'toronto maple leafs': ['toronto', 'maple leafs', 'leafs', 'tor'],

        # Central Division
        'arizona coyotes': ['arizona', 'coyotes', 'yotes', 'ari'],
        'chicago blackhawks': ['chicago', 'blackhawks', 'hawks', 'chi'],
        'colorado avalanche': ['colorado', 'avalanche', 'avs', 'col'],
        'dallas stars': ['dallas', 'stars', 'dal'],
        'minnesota wild': ['minnesota', 'wild', 'min'],
        'nashville predators': ['nashville', 'predators', 'preds', 'nsh'],
        'st. louis blues': ['st. louis', 'st louis', 'blues', 'stl'],
        'winnipeg jets': ['winnipeg', 'jets', 'wpg'],

        # Pacific Division
        'anaheim ducks': ['anaheim', 'ducks', 'ana'],
        'calgary flames': ['calgary', 'flames', 'cgy'],
        'edmonton oilers': ['edmonton', 'oilers', 'edm'],
        'los angeles kings': ['los angeles', 'la', 'kings', 'lak'],
        'san jose sharks': ['san jose', 'sharks', 'sjs', 'sj'],
        'seattle kraken': ['seattle', 'kraken', 'sea'],
        'utah hockey club': ['utah hockey club', 'utah hc', 'utah', 'uta', 'uhc'],
        'vancouver canucks': ['vancouver', 'canucks', 'nucks', 'van'],
        'vegas golden knights': ['vegas', 'las vegas', 'golden knights', 'knights', 'vgk']
    },

    'MLB': {
        'arizona diamondbacks': ['arizona', 'diamondbacks', 'd-backs', 'ari'],
        'atlanta braves': ['atlanta', 'braves', 'atl'],
        'baltimore orioles': ['baltimore', 'orioles', 'bal'],
        'boston red sox': ['boston', 'red sox', 'bos'],
        # 'chicago ws' / 'los angeles d' / 'new york m' are Kalshi's
        # truncated event-title forms ("Los Angeles D vs Chicago WS").
        'chicago white sox': ['chicago white sox', 'white sox', 'cws', 'chicago ws'],
        'chicago cubs': ['chicago cubs', 'cubs', 'chc', 'chicago c'],
        'cincinnati reds': ['cincinnati', 'reds', 'cin'],
        'cleveland guardians': ['cleveland', 'guardians', 'cle'],
        'colorado rockies': ['colorado', 'rockies', 'col'],
        'detroit tigers': ['detroit', 'tigers', 'det'],
        'houston astros': ['houston', 'astros', 'hou'],
        'kansas city royals': ['kansas city', 'royals', 'kc'],
        'los angeles angels': ['los angeles angels', 'la angels', 'angels', 'laa'],
        'los angeles dodgers': ['los angeles dodgers', 'la dodgers', 'dodgers', 'lad'],
        'miami marlins': ['miami', 'marlins', 'mia'],
        'milwaukee brewers': ['milwaukee', 'brewers', 'mil'],
        'minnesota twins': ['minnesota', 'twins', 'min'],
        'new york yankees': ['new york yankees', 'ny yankees', 'yankees', 'yanks', 'nyy'],
        'new york mets': ['new york mets', 'ny mets', 'mets', 'nym'],
        'oakland athletics': ['oakland', 'athletics', 'a\'s', 'oak'],
        'philadelphia phillies': ['philadelphia', 'phillies', 'phi'],
        'pittsburgh pirates': ['pittsburgh', 'pirates', 'pit'],
        'san diego padres': ['san diego', 'padres', 'sd'],
        'san francisco giants': ['san francisco', 'giants', 'sf'],
        'seattle mariners': ['seattle', 'mariners', 'sea'],
        'st. louis cardinals': ['st. louis', 'st louis', 'cardinals', 'stl'],
        'tampa bay rays': ['tampa bay', 'rays', 'tb'],
        'texas rangers': ['texas', 'rangers', 'tex'],
        'toronto blue jays': ['toronto', 'blue jays', 'jays', 'tor'],
        'washington nationals': ['washington', 'nationals', 'wash', 'wsh']
    }
}

    @staticmethod
    def normalize_team_name(team_name: str, sport: str = None) -> str:
        """
        Normalize team name for matching with sport-specific aliases.
        
        Uses deterministic matching: longest alias first to avoid ambiguity.
        Avoids cross-sport conflicts when sport context is available.
        """
        if not team_name:
            return ""

        # Convert to lowercase and strip
        normalized = team_name.lower().strip()

        # Remove common suffixes
        normalized = re.sub(r'\s+(football|basketball|hockey|baseball)(\s+team)?$', '', normalized)

        # If we know the sport, use sport-specific aliases
        if sport and sport in EventMatcher.TEAM_ALIASES_BY_SPORT:
            sport_aliases = EventMatcher.TEAM_ALIASES_BY_SPORT[sport]

            # Phase 1: Exact match in aliases (highest priority)
            for canonical, aliases in sport_aliases.items():
                if normalized in aliases or normalized == canonical:
                    return canonical

            # Phase 2: Substring matching - SORT BY LENGTH (longest first) for deterministic results
            # This ensures "los angeles chargers" matches before "los angeles"
            all_alias_pairs = []
            for canonical, aliases in sport_aliases.items():
                for alias in aliases:
                    if len(alias) >= 3:  # Minimum alias length
                        all_alias_pairs.append((alias, canonical))
            
            # Sort by alias length descending for deterministic matching
            all_alias_pairs.sort(key=lambda x: len(x[0]), reverse=True)
            
            for alias, canonical in all_alias_pairs:
                if alias in normalized:
                    # Check for cross-sport conflicts - skip ambiguous names without more context
                    if alias in EventMatcher.CROSS_SPORT_CONFLICTS:
                        conflict_sports = EventMatcher.CROSS_SPORT_CONFLICTS[alias]
                        if sport in conflict_sports:
                            # Only match if it's unambiguous within this sport
                            # (the canonical name should be sport-specific)
                            return canonical
                    else:
                        return canonical

        # Fallback: check all sports (less precise but better than nothing)
        # Only use exact matches in fallback to avoid cross-sport confusion
        for sport_key, sport_aliases in EventMatcher.TEAM_ALIASES_BY_SPORT.items():
            for canonical, aliases in sport_aliases.items():
                if normalized in aliases or normalized == canonical:
                    return canonical

        return normalized
    
    @staticmethod
    def normalize_team_name_fuzzy(team_name: str, sport: str = None) -> tuple[str, float]:
        """
        Normalize team name with fuzzy matching fallback.
        
        Returns:
            Tuple of (canonical_name, confidence_score)
            confidence_score: 1.0 = exact match, 0.8+ = high confidence fuzzy match
        """
        from difflib import SequenceMatcher
        
        if not team_name:
            return "", 0.0

        # Try exact normalization first
        exact_result = EventMatcher.normalize_team_name(team_name, sport)
        
        # Check if we got a canonical match
        if sport and sport in EventMatcher.TEAM_ALIASES_BY_SPORT:
            if exact_result in EventMatcher.TEAM_ALIASES_BY_SPORT[sport]:
                return exact_result, 1.0
        
        # Fuzzy matching fallback
        normalized = team_name.lower().strip()
        best_match = None
        best_score = 0.0
        
        sports_to_check = [sport] if sport else EventMatcher.TEAM_ALIASES_BY_SPORT.keys()
        
        for sport_key in sports_to_check:
            if sport_key not in EventMatcher.TEAM_ALIASES_BY_SPORT:
                continue
            sport_aliases = EventMatcher.TEAM_ALIASES_BY_SPORT[sport_key]
            
            for canonical, aliases in sport_aliases.items():
                # Check canonical name
                score = SequenceMatcher(None, normalized, canonical).ratio()
                if score > best_score:
                    best_score = score
                    best_match = canonical
                
                # Check all aliases
                for alias in aliases:
                    score = SequenceMatcher(None, normalized, alias).ratio()
                    if score > best_score:
                        best_score = score
                        best_match = canonical
        
        # Return fuzzy match if confidence is high enough
        if best_score >= 0.7 and best_match:
            return best_match, best_score
        
        return exact_result, 0.5  # Low confidence, return normalized form

    @staticmethod
    def parse_kalshi_title(title: str) -> tuple[str, str]:
        """Parse Kalshi event title into (away_team, home_team)"""
        # Playoff/series titles carry a PREFIX before the matchup
        # ("Game 6: Carolina at Vegas") while regular-season market rows
        # carry a SUFFIX after it ("Dallas at Las Vegas: Spread"). A bare
        # split(':')[0] returns "Game 6" for the former, so every playoff
        # event parsed both teams as "Game N" and never matched. When the
        # text after the first colon contains the matchup separator and
        # the text before it doesn't, the prefix is a descriptor — drop it.
        if ':' in title:
            pre, post = title.split(':', 1)
            sep_in = lambda s: (' at ' in s or ' vs ' in s or ' vs. ' in s)
            if sep_in(post) and not sep_in(pre):
                title = post.strip()
        # Remove suffixes like ": Spread", ": Total Points"
        base_title = title.split(':')[0].strip()

        if ' at ' in base_title:
            away, home = base_title.split(' at ', 1)
            return away.strip(), home.strip()
        elif ' vs ' in base_title:
            home, away = base_title.split(' vs ', 1)
            return away.strip(), home.strip()

        return base_title, base_title

    @staticmethod
    def parse_polymarket_title(title: str) -> tuple[str, str]:
        """
        Parse Polymarket event title into (away_team, home_team)

        Polymarket format: "Team1 vs. Team2" - Team1 is typically listed first
        but doesn't necessarily indicate home/away. For matching purposes,
        we treat Team1 as "team A" and Team2 as "team B" since Polymarket
        doesn't always follow strict home/away conventions.
        """
        # Remove any trailing/leading whitespace
        title = title.strip()

        # Polymarket uses "vs." with period
        if ' vs. ' in title:
            parts = title.split(' vs. ', 1)
            team1 = parts[0].strip()
            team2 = parts[1].strip()
            return team1, team2
        elif ' vs ' in title:
            parts = title.split(' vs ', 1)
            team1 = parts[0].strip()
            team2 = parts[1].strip()
            return team1, team2
        elif ' @ ' in title:
            away, home = title.split(' @ ', 1)
            return away.strip(), home.strip()
        elif ' at ' in title:
            away, home = title.split(' at ', 1)
            return away.strip(), home.strip()

        # Default: can't parse, return same for both
        return title, title

    @staticmethod
    def _generic_normalize(name: str) -> str:
        """Diacritic-free, lowercase, punctuation-stripped form for matching
        participant names across platforms in sports WITHOUT curated aliases
        (soccer/international/esports/etc.)."""
        if not name:
            return ""
        import unicodedata
        s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
        s = s.lower()
        s = re.sub(r'[^a-z0-9 ]', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    # Genuine cross-platform dual-names (FIFA English/Portuguese/official variants)
    # that token-set/fuzzy can't bridge. Canonical-keyed; normalized form -> canon.
    _NATIONAL_TEAM_ALIASES = {
        'cabo verde': 'cape verde',
        'cote d ivoire': 'ivory coast', 'cote divoire': 'ivory coast',
        'korea republic': 'south korea', 'republic of korea': 'south korea',
        'korea dpr': 'north korea', 'dpr korea': 'north korea',
        'usa': 'united states', 'united states of america': 'united states',
        'china pr': 'china',
        'czechia': 'czech republic',
        'turkiye': 'turkey',
        'ir iran': 'iran',
    }

    @staticmethod
    def _canon_participant(name: str) -> str:
        n = EventMatcher._generic_normalize(name)
        return EventMatcher._NATIONAL_TEAM_ALIASES.get(n, n)

    @staticmethod
    def _teams_match_generic(team1: str, team2: str) -> bool:
        """Alias-free participant match for non-big-4 sports. Handles the real
        cross-platform naming differences observed (Kalshi 'IR Iran' vs PM 'Iran';
        'Congo DR' vs 'DR Congo') via a TOKEN-SET test, a national-team dual-name
        table, plus diacritics + fuzzy."""
        na = EventMatcher._canon_participant(team1)
        nb = EventMatcher._canon_participant(team2)
        if not na or not nb:
            return False
        if na == nb:
            return True
        ta, tb = set(na.split()), set(nb.split())
        # Significant tokens (>=3 chars) of one are a subset of the other's tokens:
        # catches qualifier prefixes ('iran' ⊆ {'ir','iran'}) and word-order swaps
        # ('congo' ⊆ {'dr','congo'} and vice-versa) without matching distinct names
        # like 'south korea' vs 'north korea'.
        sa = {t for t in ta if len(t) >= 3} or ta
        sb = {t for t in tb if len(t) >= 3} or tb
        if (sa and sa <= tb) or (sb and sb <= ta):
            return True
        from difflib import SequenceMatcher
        return SequenceMatcher(None, na, nb).ratio() >= 0.88

    @staticmethod
    def teams_match(team1: str, team2: str, sport: str = None) -> bool:
        """Check if two team names refer to the same team. Big-4 sports use the
        curated alias tables (UNCHANGED behavior); every other sport uses the
        generic alias-free matcher."""
        # --- Big-4: original alias-based logic, untouched ---
        if sport and sport in EventMatcher.TEAM_ALIASES_BY_SPORT:
            norm1 = EventMatcher.normalize_team_name(team1, sport)
            norm2 = EventMatcher.normalize_team_name(team2, sport)
            if norm1 == norm2:
                return True
            if norm1 in norm2 or norm2 in norm1:
                return True
            sport_aliases = EventMatcher.TEAM_ALIASES_BY_SPORT[sport]
            for canonical, aliases in sport_aliases.items():
                if norm1 in aliases and norm2 in aliases:
                    return True
            return False
        # --- All other sports (soccer/intl/esports/college/...): generic ---
        return EventMatcher._teams_match_generic(team1, team2)

    @staticmethod
    def events_match(kalshi_away: str, kalshi_home: str, poly_team1: str, poly_team2: str, sport: str = None) -> bool:
        """
        Check if Kalshi and Polymarket events represent the same game.

        Since Polymarket doesn't always follow strict home/away conventions,
        we match if both teams are present regardless of order.
        """
        # Check if kalshi_home matches either poly team
        home_matches_1 = EventMatcher.teams_match(kalshi_home, poly_team1, sport)
        home_matches_2 = EventMatcher.teams_match(kalshi_home, poly_team2, sport)

        # Check if kalshi_away matches either poly team
        away_matches_1 = EventMatcher.teams_match(kalshi_away, poly_team1, sport)
        away_matches_2 = EventMatcher.teams_match(kalshi_away, poly_team2, sport)

        # Match if home and away are both present (in either order)
        match_order_1 = home_matches_1 and away_matches_2  # home=team1, away=team2
        match_order_2 = home_matches_2 and away_matches_1  # home=team2, away=team1

        return match_order_1 or match_order_2

    # ---- Futures / outright matching -------------------------------------
    # Pure filler stripped from futures titles. NOTE: winner/champion are NOT here
    # — they're the distinguishing market term, mapped to a synonym token below so
    # 'Winner' == 'Champion' but != 'Golden Boot'.
    _FUTURES_STOPWORDS = {"the", "to", "of", "a", "will", "be", "win",
                          "outright", "2024", "2025", "2026", "2027"}
    # Synonym class: the outright-winner market under any of these words.
    _FUTURES_WINNER_SYNS = {"winner", "champion", "champions", "championship",
                            "champ", "wins"}

    @staticmethod
    def _futures_key_tokens(title: str) -> set:
        toks = EventMatcher._generic_normalize(title).split()
        out = set()
        for t in toks:
            if t in EventMatcher._FUTURES_WINNER_SYNS:
                out.add("__title__")          # collapse winner/champion synonyms
            elif t not in EventMatcher._FUTURES_STOPWORDS and len(t) >= 3:
                out.add(t)
        return out

    @staticmethod
    def futures_match(kalshi_title: str, poly_title: str) -> bool:
        """Match a Kalshi futures series ↔ a Polymarket futures event by title
        (e.g. 'World Cup Winner' ↔ 'World Cup Winner'; 'NBA Champion' ↔ 'NBA
        Championship Winner'). Compares the significant (non-stopword) tokens:
        match when one title's key tokens are a subset of the other's."""
        ka = EventMatcher._futures_key_tokens(kalshi_title)
        pa = EventMatcher._futures_key_tokens(poly_title)
        if not ka or not pa:
            return False
        if ka == pa or ka <= pa or pa <= ka:
            return True
        # Otherwise require strong overlap (Jaccard) to avoid e.g. 'World Cup
        # Winner' matching 'World Cup Golden Boot'.
        inter = len(ka & pa)
        union = len(ka | pa)
        return union > 0 and inter / union >= 0.75

    @staticmethod
    def match_futures_outcomes(kalshi_outcomes, poly_outcomes):
        """Pair outcome names (teams/players) of a matched futures market across
        platforms, using the generic participant matcher. Returns list of
        (kalshi_outcome, poly_outcome) pairs."""
        pairs = []
        used = set()
        for ko in kalshi_outcomes:
            for i, po in enumerate(poly_outcomes):
                if i in used:
                    continue
                if EventMatcher._teams_match_generic(ko, po):
                    pairs.append((ko, po))
                    used.add(i)
                    break
        return pairs


class MarketMatcher:
    """Matches individual markets across Kalshi and Polymarket"""

    @staticmethod
    def extract_spread_value(text):
        """
        Extract spread value from market text.

        Examples:
            Kalshi: "Dallas wins by over 10.5 points?" -> 10.5
            Polymarket: "Spread: Eagles (-3.5)" -> 3.5
        """
        import re

        # Polymarket spread format: "Spread: Team (±X.X)"
        poly_match = re.search(r'\(([+-]?)(\d+(?:\.\d+)?)\)', text)
        if poly_match:
            return float(poly_match.group(2))

        # Kalshi spread format: "Team wins by over X.X points?" — the unit
        # varies by sport: points (NFL/NBA), goals (NHL), runs (MLB).
        kalshi_match = re.search(
            r'(?:over|under)\s+(\d+(?:\.\d+)?)\s+(?:points?|goals?|runs?)',
            text.lower())
        if kalshi_match:
            return float(kalshi_match.group(1))

        return None

    @staticmethod
    def extract_total_value(text, ticker=None):
        """
        Extract total points value from market text or ticker.

        Examples:
            Polymarket: "Texas State vs. Arkansas State: O/U 63.5" -> 63.5
            Kalshi title: "Dallas at Las Vegas: Total Points" -> None (not in title)
            Kalshi ticker: "KXNFLTOTAL-25NOV17DALLV-61" -> 61.0

        Args:
            text: Market title/question text
            ticker: Optional Kalshi ticker to extract value from

        Returns:
            float or None
        """
        import re

        # Polymarket total format: "O/U X.X" or "Over/Under X.X"
        total_match = re.search(r'o/u\s+(\d+(?:\.\d+)?)', text.lower())
        if total_match:
            return float(total_match.group(1))

        # Alternative format
        total_match = re.search(r'(?:over|under)[/\s]+(\d+(?:\.\d+)?)', text.lower())
        if total_match:
            return float(total_match.group(1))

        # Kalshi might have it in title (rare)
        total_match = re.search(r'total\s+(?:points?\s+)?(?:over|under)?\s*(\d+(?:\.\d+)?)', text.lower())
        if total_match:
            return float(total_match.group(1))

        # Kalshi encodes total in ticker: KXNFLTOTAL-25NOV17DALLV-61
        # The number after the last dash is the total (may include half points like 60H for 60.5)
        if ticker:
            ticker_match = re.search(r'-(\d+(?:H)?)$', ticker)
            if ticker_match:
                value_str = ticker_match.group(1)
                # Handle half-point notation: 60H = 60.5
                if value_str.endswith('H'):
                    return float(value_str[:-1]) + 0.5
                else:
                    return float(value_str)

        return None

    @staticmethod
    def get_market_period(text, sport: str = None):
        """
        Determine the period/time frame for a market.

        Args:
            text: Market title or question text
            sport: Optional sport for sport-specific period detection

        Returns:
            'full_game', '1h', '2h', '1q', '2q', '3q', '4q', '1p', '2p', '3p', 'f5', etc.
        """
        if not text:
            return 'full_game'  # Default to full game

        text_lower = text.lower()

        # === HALVES (Basketball, Football) ===
        if any(p in text_lower for p in ['1h', 'h1', 'first half', '1st half', 'half 1']):
            return '1h'
        if any(p in text_lower for p in ['2h', 'h2', 'second half', '2nd half', 'half 2']):
            return '2h'

        # === QUARTERS (Basketball, Football) ===
        if any(p in text_lower for p in ['1q', 'q1', 'first quarter', '1st quarter']):
            return '1q'
        if any(p in text_lower for p in ['2q', 'q2', 'second quarter', '2nd quarter']):
            return '2q'
        if any(p in text_lower for p in ['3q', 'q3', 'third quarter', '3rd quarter']):
            return '3q'
        if any(p in text_lower for p in ['4q', 'q4', 'fourth quarter', '4th quarter']):
            return '4q'

        # === NHL PERIODS ===
        if any(p in text_lower for p in ['1p', 'p1', 'first period', '1st period']):
            return '1p'
        if any(p in text_lower for p in ['2p', 'p2', 'second period', '2nd period']):
            return '2p'
        if any(p in text_lower for p in ['3p', 'p3', 'third period', '3rd period']):
            return '3p'
        if 'overtime' in text_lower or ' ot' in text_lower:
            return 'ot'

        # === MLB INNINGS ===
        if any(p in text_lower for p in ['f5', 'first 5', 'first five', '1st 5 innings']):
            return 'f5'
        if any(p in text_lower for p in ['first inning', '1st inning']):
            return '1inn'
        # Check for specific inning numbers (e.g., "5th inning")
        import re
        inning_match = re.search(r'(\d+)(?:st|nd|rd|th)\s+inning', text_lower)
        if inning_match:
            return f'{inning_match.group(1)}inn'

        return 'full_game'

    @staticmethod
    def get_market_type(kalshi_title=None, poly_question=None):
        """
        Determine market type from title/question.

        Returns: 'moneyline', 'spread', 'total', 'prop', or None
        """
        import re

        text = (kalshi_title or poly_question or '').lower()

        # Player props phrased as O/U lines (Polymarket): the stat comes
        # straight after the player's name — "Jalen Brunson: Points O/U
        # 27.5", "Victor Wembanyama: Rebounds O/U 11.5". Must be detected
        # BEFORE the generic total check or every PM player prop
        # classifies as a game total (and can never pair with Kalshi's
        # prop markets, which DO classify as 'prop' via the keyword
        # check below). Game totals are unaffected: their colon prefix is
        # the matchup ("Knicks vs. Spurs: O/U 203.5") so the text after
        # the colon starts with "o/u", not a stat name.
        if re.match(
            r"^[^:]+:\s*(?:points|rebounds|assists|threes|three[- ]?pointers|"
            r"3[- ]?pointers|pra|steals|blocks|turnovers|goals|shots on goal|"
            r"saves|strikeouts|hits|total bases|home runs|rbis|runs)\s+"
            r"o/u\s+\d", text.strip()):
            return 'prop'

        # Spread indicators (check first, more specific)
        if 'spread' in text or 'wins by' in text or re.search(r'\([+-]\d+', text):
            return 'spread'

        # Total indicators (check second, also specific)
        if 'total' in text or 'o/u' in text or 'over/under' in text:
            return 'total'

        # Props (check before moneyline, more specific)
        # NFL props
        if any(keyword in text for keyword in ['touchdown', 'td', 'yards', 'passing', 'rushing', 'receiving', 
                                                'sack', 'interception', 'reception', 'completions']):
            return 'prop'
        # NBA props
        if any(keyword in text for keyword in ['points', 'rebounds', 'assists', '3-pointer', 'three pointer',
                                                'double-double', 'triple-double', 'steals', 'blocks']):
            return 'prop'
        # MLB props
        if any(keyword in text for keyword in ['home run', 'hits', 'strikeout', 'rbi', 'stolen base',
                                                'pitcher', 'batter', 'innings pitched']):
            return 'prop'
        # NHL props
        if any(keyword in text for keyword in ['goal', 'assist', 'save', 'shutout', 'shots on goal',
                                                'power play', 'penalty']):
            return 'prop'

        # Moneyline indicators
        # Kalshi: "Dallas at Las Vegas Winner?"
        # Polymarket: "Cowboys vs. Raiders" (just team names with vs/vs./@ separator)
        if 'winner' in text or 'to win' in text:
            return 'moneyline'

        # Polymarket moneyline: Simple format with just teams and vs/@ separator
        # Pattern: "Team1 vs. Team2" or "Team1 vs Team2" or "Team1 @ Team2"
        # Char class allows numbers (49ers, 76ers) AND punctuation that appears
        # inside real team names: '.' (St. Louis), apostrophe (Oakland A's),
        # '-' / '&'. Without the period, "Cincinnati Reds vs. St. Louis
        # Cardinals" failed to match and never paired with the Kalshi moneyline.
        # ':' / '?' are intentionally excluded so totals ("...: O/U 10.5") and
        # prop questions ("...inning?: ...") still fall through to None.
        if re.match(r"^[\w\s.'&-]+\s+(?:vs\.?|@)\s+[\w\s.'&-]+$", text.strip()):
            # Make sure it's not a spread or total (those have extra info)
            if 'spread' not in text and 'o/u' not in text and not re.search(r'\([+-]?\d+', text):
                return 'moneyline'

        return None

    # Stat-token canonicalization shared by the player-prop parsers. Keys are
    # the phrases each source uses, values a shared token.
    _PROP_STAT_CANON = {
        'points': 'points', 'pts': 'points',
        'rebounds': 'rebounds', 'reb': 'rebounds',
        'assists': 'assists', 'ast': 'assists',
        'threes': 'threes', 'three-pointers': 'threes',
        'three pointers': 'threes', '3-pointers': 'threes',
        '3 pointers': 'threes', 'threepointers': 'threes',
        'goals': 'goals', 'shots on goal': 'shots_on_goal',
        'saves': 'saves',
        'strikeouts': 'strikeouts', 'hits': 'hits',
        'total bases': 'total_bases', 'home runs': 'home_runs',
        'rbis': 'rbis', 'runs': 'runs',
        'receiving yards': 'rec_yds', 'rushing yards': 'rush_yds',
        'passing yards': 'pass_yds', 'receptions': 'receptions',
    }

    @staticmethod
    def parse_player_prop(text: str):
        """Parse a player-prop market title from either source into
        (player_lower, stat_token, strike) or None.

        Kalshi:     "Victor Wembanyama: 25+ points"   -> 24.5 (N+ = over N-0.5)
        Polymarket: "Victor Wembanyama: Points O/U 24.5" -> 24.5
        """
        import re
        if not text:
            return None
        t = text.strip().lower()
        # Polymarket "Name: Stat O/U X.X"
        m = re.match(r"^(?P<player>[^:]+):\s*(?P<stat>[\w 3-]+?)\s+o/u\s+"
                     r"(?P<line>\d+(?:\.\d+)?)", t)
        if m:
            stat = MarketMatcher._PROP_STAT_CANON.get(m.group('stat').strip())
            if stat:
                return (m.group('player').strip(), stat,
                        float(m.group('line')))
        # Kalshi "Name: N+ stat"
        m = re.match(r"^(?P<player>[^:]+):\s*(?P<n>\d+)\+\s*"
                     r"(?P<stat>[\w 3-]+?)\s*\??$", t)
        if m:
            stat = MarketMatcher._PROP_STAT_CANON.get(m.group('stat').strip())
            if stat:
                return (m.group('player').strip(), stat,
                        float(m.group('n')) - 0.5)
        return None

    @staticmethod
    def _prop_players_match(p1: str, p2: str) -> bool:
        """Diacritic/punctuation-insensitive player name comparison."""
        import unicodedata
        def norm(s):
            s = unicodedata.normalize('NFKD', s)
            s = ''.join(ch for ch in s if not unicodedata.combining(ch))
            return ' '.join(re.sub(r"[^\w\s]", '', s.lower()).split())
        n1, n2 = norm(p1), norm(p2)
        return bool(n1) and bool(n2) and (n1 == n2 or n1 in n2 or n2 in n1)

    @staticmethod
    def markets_match(kalshi_market, poly_market, home_team, away_team, sport=None):
        """
        Check if a Kalshi market and Polymarket market represent the same bet.

        Args:
            kalshi_market: Kalshi market dict with 'title' key
            poly_market: Polymarket market object with 'question' attribute
            home_team: Home team name (normalized)
            away_team: Away team name (normalized)

        Returns:
            bool: True if markets match
        """
        k_title = kalshi_market.get('title', '').lower()
        k_ticker = kalshi_market.get('ticker', '')
        p_question = poly_market.question.lower()

        # Get market types
        k_type = MarketMatcher.get_market_type(kalshi_title=k_title)
        p_type = MarketMatcher.get_market_type(poly_question=p_question)
        # Must be same type
        if k_type != p_type or k_type is None:
            return False

        # Moneyline: just check it's a winner market
        if k_type == 'moneyline':
            # Both are moneyline/winner markets - they match!
            return True

        # Spread: must have same spread value, same period, AND same team favored
        if k_type == 'spread':
            # Check periods match
            k_period = MarketMatcher.get_market_period(k_title)
            p_period = MarketMatcher.get_market_period(p_question)

            if k_period != p_period:
                return False

            k_spread = MarketMatcher.extract_spread_value(k_title)
            p_spread = MarketMatcher.extract_spread_value(p_question)

            if k_spread is None or p_spread is None:
                return False

            # Check spread values match
            if abs(k_spread - p_spread) >= 0.01:
                return False

            # CRITICAL: Check which team is favored
            # Kalshi format: "TeamName wins by over X.X points?"
            # Polymarket format: "Spread: TeamName (-X.X)" or "Spread: TeamName (+X.X)"

            # Extract team from Kalshi title (team mentioned in "X wins by over...")
            k_team_lower = None
            if 'wins by' in k_title:
                # Find team name before "wins by"
                parts = k_title.split('wins by')
                if parts:
                    k_team_lower = parts[0].strip().lower()

            # Extract team from Polymarket question (team with negative spread is favored)
            p_team_lower = None
            import re
            # Match "Spread: TeamName (-X)" format
            poly_match = re.search(r'spread:\s*([^(]+)\s*\([-+]', p_question.lower())
            if poly_match:
                p_team_lower = poly_match.group(1).strip()

            # Both must have identified teams
            if not k_team_lower or not p_team_lower:
                return False

            # Normalize team names for comparison with sport context
            teams_match = EventMatcher.teams_match(k_team_lower, p_team_lower, sport)

            return teams_match

        # Total: Kalshi has generic "Total Points" title, but value is in ticker
        # Polymarket has specific value in question: "O/U 63.5"
        # KEY: Kalshi uses whole numbers (50, 51), Polymarket uses half-points (50.5, 51.5)
        if k_type == 'total':
            # Check periods match
            k_period = MarketMatcher.get_market_period(k_title)
            p_period = MarketMatcher.get_market_period(p_question)

            if k_period != p_period:
                return False

            # Extract total from Kalshi ticker (e.g., KXNFLTOTAL-25NOV17DALLV-61 -> 61.0)
            k_total = MarketMatcher.extract_total_value(k_title, ticker=k_ticker)
            # Extract total from Polymarket question (e.g., "O/U 63.5" -> 63.5)
            p_total_raw = MarketMatcher.extract_total_value(p_question)

            # Both must have values to match
            if k_total is not None and p_total_raw is not None:
                # Polymarket uses half-points (50.5, 51.5), Kalshi uses whole numbers (50, 51)
                # Round down Polymarket's value to match Kalshi's format
                # 50.5 -> 50, 51.5 -> 51, etc.
                import math
                p_total = math.floor(p_total_raw)

                # Now compare
                return abs(k_total - p_total) < 0.01

            # If either is missing a value, can't match
            return False

        # Player props: same player + same stat + same implied strike.
        # Kalshi phrases the line as "N+" (over N-0.5); Polymarket as
        # "O/U X.5" — both reduce to the same half-point strike.
        if k_type == 'prop':
            k_prop = MarketMatcher.parse_player_prop(k_title)
            p_prop = MarketMatcher.parse_player_prop(p_question)
            if not k_prop or not p_prop:
                return False
            k_player, k_stat, k_strike = k_prop
            p_player, p_stat, p_strike = p_prop
            if k_stat != p_stat:
                return False
            if abs(k_strike - p_strike) >= 0.01:
                return False
            return MarketMatcher._prop_players_match(k_player, p_player)

        return False


def kalshi_cents_to_american_odds(cents):
    """
    Convert Kalshi price in cents (0-100) to American odds.

    Args:
        cents: Price in cents (0-100), representing probability as percentage

    Returns:
        American odds as integer (e.g., -110, +150)
    """
    if cents is None:
        return None

    # Clamp extreme values to prevent division by zero
    if cents <= 1:
        cents = 1
    elif cents >= 99:
        cents = 99

    # Convert cents to probability (0.01 to 0.99)
    prob = cents / 100.0

    # Convert probability to American odds
    if prob >= 0.5:
        # Favorite: negative odds
        american = -(prob / (1 - prob)) * 100
    else:
        # Underdog: positive odds
        american = ((1 - prob) / prob) * 100

    return int(round(american))


def american_odds_to_kalshi_cents(american_odds):
    """
    Convert American odds to Kalshi price in cents (0-100).

    Args:
        american_odds: American odds (e.g., -110, +150)

    Returns:
        Price in cents (0-100)
    """
    if american_odds is None or american_odds == 0:
        return None

    # Convert American odds to probability
    if american_odds < 0:
        # Favorite
        prob = abs(american_odds) / (abs(american_odds) + 100)
    else:
        # Underdog
        prob = 100 / (american_odds + 100)

    # Convert to cents
    return int(round(prob * 100))


def american_to_implied_pct(american_odds):
    """Convert American odds to implied probability percentage (0-100, float).

    The chart's primary y-axis is implied chance; American odds are shown only
    in parentheses on labels/readouts. Returns None for unusable input.
    """
    if american_odds is None:
        return None
    try:
        am = float(american_odds)
    except (TypeError, ValueError):
        return None
    if am == 0:
        return None
    if am < 0:
        return (-am) / (-am + 100.0) * 100.0
    return 100.0 / (am + 100.0) * 100.0


def format_pct_with_american(american_odds, pct=None):
    """Label text: 'NN% (+150)'. Derives pct from odds when not supplied."""
    if pct is None:
        pct = american_to_implied_pct(american_odds)
    if pct is None:
        return ""
    try:
        am = int(round(float(american_odds)))
        am_str = f"+{am}" if am > 0 else f"{am}"
        return f"{pct:.0f}% ({am_str})"
    except (TypeError, ValueError):
        return f"{pct:.0f}%"


class HistoricalOddsClient:
    """Client for fetching historical odds data from theOddsAPI"""

    def __init__(self, api_key, interval_minutes:int):
        self.api_key = api_key
        self.base_url = "https://api.the-odds-api.com/v4/historical"
        self.cache = {}
        self.min_interval = timedelta(minutes=interval_minutes)

    async def get_historical_snapshots(self, session, sport_key, event_id, market,
                                     start_time, end_time=None, regions="us"):
        """Fetches historical odds snapshots in parallel batches"""
        if end_time is None:
            end_time = datetime.now()
        else:
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))

        start_time = datetime.fromisoformat(start_time.replace('Z', ''))
        print(f"Fetching snapshots from {start_time} to {end_time}")

        # Generate time intervals (more efficient than sequential fetching)
        time_points = []
        current_time = start_time
        while current_time < end_time:
            time_points.append(current_time)
            current_time += self.min_interval

        # Set a reasonable concurrency limit to avoid overloading the API
        # and getting rate limited
        concurrency_limit = 5

        # Split time points into batches for controlled parallelism
        snapshot_batches = []
        for i in range(0, len(time_points), concurrency_limit):
            batch = time_points[i:i+concurrency_limit]

            # Create tasks for this batch
            batch_tasks = [
                self._fetch_single_snapshot(
                    session, sport_key, event_id, market,
                    t.isoformat() + 'Z', regions
                )
                for t in batch
            ]

            # Wait for all tasks in this batch to complete
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            # Filter out errors and None results
            valid_snapshots = [
                result for result in batch_results
                if not isinstance(result, Exception) and result is not None
            ]

            snapshot_batches.extend(valid_snapshots)

            # Brief pause between batches to be nice to the API
            await asyncio.sleep(0.2)

        # Sort snapshots by timestamp
        snapshots = sorted(
            snapshot_batches,
            key=lambda x: datetime.fromisoformat(x['timestamp'].replace('Z', '')).timestamp()
        )

        print(f"Retrieved {len(snapshots)} valid snapshots")
        return snapshots

    async def _fetch_single_snapshot(self, session, sport_key, event_id, market, date, regions):
        """Fetch a single historical snapshot"""
        url = f"{self.base_url}/sports/{sport_key}/events/{event_id}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": market,
            "date": date
        }

        try:
            async with session.get(url, params=params) as response:
                response_data = await response.json()

                if response.status == 200:
                    print(f"Successful snapshot fetch for {date}")
                    # Add point change detection
                    response_data['point_changes'] = self._detect_point_changes(response_data)
                    return response_data
                else:
                    error_msg = f"Error {response.status} fetching snapshot: {response_data.get('message', 'No error message')}"
                    print(error_msg)
                    return None

        except Exception as e:
            print(f"Exception fetching snapshot: {str(e)}")
            return None

    def _detect_point_changes(self, snapshot):
        """Detect point changes across bookmakers"""
        point_changes = {}
        for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
            for market in bookmaker.get('markets', []):
                for outcome in market.get('outcomes', []):
                    if 'point' in outcome:
                        key = (outcome.get('name'), outcome.get('description', ''))
                        point_changes.setdefault(key, set()).add(outcome['point'])
        return {k: sorted(v) for k, v in point_changes.items() if len(v) > 1}


class PolymarketHistoricalOddsClient:
    """Client for fetching historical odds data from Polymarket using PolymarketSportsClient"""

    def __init__(self):
        self.polymarket_client = PolymarketSportsClient()
        self.cache = {}

    @staticmethod
    def _merge_satellite_games(games):
        """Fold Polymarket 'satellite' events into their base game.

        The gamma series listing returns extra EVENTS per game whose title
        is the matchup plus a suffix — "Atlanta Braves vs. Chicago White
        Sox - Player Props" / "... - First 5 Innings Winner". Those are
        markets of the same game, not separate events; left alone they
        clutter the event selector as P-only rows. When a base game with
        the same title prefix exists (closest start time within 24h), the
        satellite's markets are appended to it and the satellite event is
        dropped. Satellites with no base game are kept unchanged.
        """
        from datetime import datetime, timezone

        def _ts(g):
            try:
                t = datetime.fromisoformat(str(g.start_time).replace('Z', '+00:00'))
                return t.replace(tzinfo=t.tzinfo or timezone.utc)
            except (ValueError, TypeError):
                return None

        bases: dict[str, list] = {}
        satellites = []
        for g in games:
            title = (g.title or '').strip()
            if ' - ' in title:
                satellites.append(g)
            else:
                bases.setdefault(title.lower(), []).append(g)

        sat_ids = {id(s) for s in satellites}
        out = [g for g in games if id(g) not in sat_ids]
        for s in satellites:
            base_title = (s.title or '').split(' - ')[0].strip().lower()
            best, best_dt = None, None
            s_ts = _ts(s)
            for b in bases.get(base_title, []):
                b_ts = _ts(b)
                if s_ts is None or b_ts is None:
                    delta = 0.0  # no time info — same-title base is fine
                else:
                    delta = abs((s_ts - b_ts).total_seconds())
                    if delta > 24 * 3600:
                        continue
                if best is None or delta < best_dt:
                    best, best_dt = b, delta
            if best is not None:
                best.markets.extend(s.markets or [])
            else:
                out.append(s)
        return out

    async def get_sport_games(self, sport: str = None, series_id: int = None):
        """
        Get all games for a sport from Polymarket (async, non-blocking). Accepts a
        sport name (big-4) OR an explicit gamma series_id (discovered non-big-4
        leagues like the World Cup).
        """
        try:
            # Use run_in_executor for CPU-bound work to avoid blocking Qt event loop
            loop = asyncio.get_event_loop()
            games = await loop.run_in_executor(
                None,
                lambda: self.polymarket_client.get_sport_markets(
                    sport,
                    series_id=series_id,
                    limit=100,
                    include_orderbook=False,  # Skip orderbook for faster loading
                    include_trades=False,     # Skip trades for faster loading
                    days_ahead=10,  # Near-term window; the unfiltered series
                                    # listing is creation-ordered and crowds
                                    # out games 2-3 days away (K-only rows)
                )
            )
            return self._merge_satellite_games(games)
        except Exception as e:
            print(f"Error fetching Polymarket games for {sport or series_id}: {e}")
            return []

    async def get_historical_candlesticks(self, session, token_id, outcome_name,
                                         start_time, end_time=None, fidelity=60, market_type=None):
        """
        Fetches historical candlestick data from Polymarket (async, non-blocking).

        Args:
            session: aiohttp session for making requests
            token_id: Polymarket CLOB token ID
            outcome_name: Name of the outcome (e.g., team name, "Yes", "No")
            start_time: Start time as datetime or ISO string
            end_time: End time as datetime or ISO string
            fidelity: Resolution in minutes (default 60)
            market_type: Type of market ('moneyline', 'spread', 'total') to set correct market key

        Returns:
            List of snapshot dictionaries formatted like TheOddsAPI for compatibility
        """
        if end_time is None:
            end_time = datetime.now()
        elif isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))

        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', ''))

        print(f"Fetching Polymarket price history from {start_time} to {end_time}")
        print(f"Token: {token_id}, Outcome: {outcome_name}, Fidelity: {fidelity} minutes")

        try:
            # Use aiohttp session for truly async HTTP request
            url = "https://clob.polymarket.com/prices-history"
            params = {
                'market': token_id,
                'startTs': int(start_time.timestamp()),
                'endTs': int(end_time.timestamp()),
                'fidelity': fidelity
            }

            async with session.get(url, params=params, timeout=15) as response:
                if response.status != 200:
                    error_text = await response.text()
                    print(f"Error fetching Polymarket history: {response.status} - {error_text}")
                    return []

                data = await response.json()
                history = data.get('history', [])

            print(f"Retrieved {len(history)} price points from Polymarket")

            # Convert Polymarket format to TheOddsAPI-like snapshot format
            snapshots = []

            for point in history:
                timestamp = point.get('t')  # Unix timestamp
                price = point.get('p')  # Price (0-1 decimal or 0-100 cents)

                if timestamp is None or price is None:
                    continue

                # Convert timestamp
                timestamp_dt = datetime.fromtimestamp(timestamp)

                # Convert Polymarket price to cents if needed (0-1 → 0-100)
                if price <= 1.0:
                    price_cents = price * 100
                else:
                    price_cents = price

                # Convert to American odds
                american_odds = kalshi_cents_to_american_odds(price_cents)
                if american_odds is None:
                    continue

                # Determine correct market key based on market type
                # Map market_type to TheOddsAPI market keys
                market_key = 'h2h'  # Default to moneyline
                if market_type == 'spread':
                    market_key = 'spreads'
                elif market_type == 'total':
                    market_key = 'totals'
                elif market_type == 'moneyline':
                    market_key = 'h2h'

                # Format as TheOddsAPI-like snapshot
                snapshot = {
                    'timestamp': timestamp_dt.isoformat() + 'Z',
                    'data': {
                        'bookmakers': [{
                            'key': 'polymarket',
                            'title': 'Polymarket',
                            'markets': [{
                                'key': market_key,
                                'outcomes': [{
                                    'name': outcome_name,
                                    'price': american_odds,
                                    'polymarket_price': price,  # Store original
                                }]
                            }]
                        }]
                    }
                }

                snapshots.append(snapshot)

            print(f"Converted {len(snapshots)} valid snapshots")
            return snapshots

        except Exception as e:
            print(f"Error fetching Polymarket price history: {e}")
            import traceback
            traceback.print_exc()
            return []


class KalshiHistoricalOddsClient:
    """Client for fetching historical odds data from Kalshi API"""

    def __init__(self, api_key=None):
        self.kalshi_client = KalshiClient(api_key=api_key)
        self.cache = {}

    async def get_event_markets(self, event_ticker):
        """
        Get all markets for a specific event.

        Args:
            event_ticker: Kalshi event ticker (e.g., 'KXNFLGAME-25OCT27WASKC')

        Returns:
            List of market dictionaries with market info
        """
        try:
            # Run synchronous Kalshi API call in thread pool
            loop = asyncio.get_event_loop()
            event_data = await loop.run_in_executor(
                None,
                lambda: self.kalshi_client.get_event(
                    event_ticker=event_ticker,
                    with_nested_markets=True
                )
            )
            return event_data.get('event', {}).get('markets', [])
        except Exception as e:
            print(f"Error fetching markets for event {event_ticker}: {e}")
            return []

    async def get_historical_candlesticks(self, session, market_ticker, series_ticker,
                                         start_time, end_time=None, period_interval=60, market_type=None):
        """
        Fetches historical candlestick data from Kalshi.

        Args:
            session: Not used for Kalshi (synchronous API), kept for interface compatibility
            market_ticker: Kalshi market ticker (e.g., 'KXNFLGAME-25OCT27WASKC-KC')
            series_ticker: Kalshi series ticker (e.g., 'KXNFLGAME')
            start_time: Start time as datetime or ISO string
            end_time: End time as datetime or ISO string
            period_interval: Candlestick interval in minutes (1, 60, or 1440)
            market_type: Type of market ('moneyline', 'spread', 'total') to set correct market key

        Returns:
            List of snapshot dictionaries formatted like TheOddsAPI for compatibility
        """
        if end_time is None:
            end_time = datetime.now()
        elif isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace('Z', ''))

        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace('Z', ''))

        print(f"Fetching Kalshi candlesticks from {start_time} to {end_time}")
        print(f"Market: {market_ticker}, Interval: {period_interval} minutes")

        try:
            # Fetch candlestick data from Kalshi asynchronously
            # Run the synchronous call in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            candlesticks_data = await loop.run_in_executor(
                None,  # Use default executor
                lambda: self.kalshi_client.get_market_candlesticks(
                    ticker=market_ticker,
                    series_ticker=series_ticker,
                    period_interval=period_interval,
                    start_ts=int(start_time.timestamp()),
                    end_ts=int(end_time.timestamp())
                )
            )

            candles = candlesticks_data.get('candlesticks', [])
            print(f"Retrieved {len(candles)} candlesticks from Kalshi")

            # Convert Kalshi candlestick format to TheOddsAPI-like snapshot format
            snapshots = []
            skipped_none = 0
            skipped_time = 0

            def _dollars_to_cents(v):
                # Kalshi returns dollar-denominated strings like "0.5100".
                # Accept int/float too in case the schema flips back.
                if v is None:
                    return None
                try:
                    return int(round(float(v) * 100))
                except (TypeError, ValueError):
                    return None

            for candle in candles:
                price_data = candle.get('price') or {}

                # Kalshi candlestick schema uses `*_dollars` suffix with
                # string dollar values. Prefer the period's executed-trade
                # close; fall back to previous trade; finally fall back to
                # the bid/ask mid (still meaningful for resting-order-only
                # periods at the start of a market).
                close_price = _dollars_to_cents(
                    price_data.get('close_dollars')
                    or price_data.get('close')
                )
                if close_price is None:
                    close_price = _dollars_to_cents(
                        price_data.get('previous_dollars')
                        or price_data.get('previous')
                    )
                if close_price is None:
                    yb = (candle.get('yes_bid') or {})
                    ya = (candle.get('yes_ask') or {})
                    yb_c = _dollars_to_cents(
                        yb.get('close_dollars') or yb.get('close'))
                    ya_c = _dollars_to_cents(
                        ya.get('close_dollars') or ya.get('close'))
                    if yb_c is not None and ya_c is not None:
                        close_price = (yb_c + ya_c) // 2
                    elif yb_c is not None:
                        close_price = yb_c
                    elif ya_c is not None:
                        close_price = ya_c

                # Skip candlesticks with no price data at all
                if close_price is None:
                    skipped_none += 1
                    continue

                # Convert timestamp
                ts = candle.get('end_period_ts', 0)
                timestamp_dt = datetime.fromtimestamp(ts)

                # For Kalshi, don't filter by time range - show all available data
                # The time range will just determine how far back we fetch
                # But we display everything we get

                # Determine correct market key based on market type
                # Map market_type to TheOddsAPI market keys
                market_key = 'h2h'  # Default to moneyline
                if market_type == 'spread':
                    market_key = 'spreads'
                elif market_type == 'total':
                    market_key = 'totals'
                elif market_type == 'moneyline':
                    market_key = 'h2h'

                # For totals and spreads, Kalshi has YES/NO within single market
                # YES = Over/Favorite, NO = Under/Underdog
                # We need to create TWO outcomes per snapshot
                if market_type in ['total', 'spread']:
                    # Get YES price (Over/Favorite)
                    yes_price_cents = close_price
                    yes_american_odds = kalshi_cents_to_american_odds(yes_price_cents)

                    # Calculate NO price (Under/Underdog) - NO = 100 - YES for binary markets
                    no_price_cents = 100 - yes_price_cents if yes_price_cents is not None else None
                    no_american_odds = kalshi_cents_to_american_odds(no_price_cents) if no_price_cents else None

                    if yes_american_odds is None or no_american_odds is None:
                        continue

                    # Extract the line value from ticker (e.g., "49" from "KXNFLTOTAL-25NOV17DALLV-49")
                    line_value = market_ticker.split('-')[-1]

                    # Create snapshot with BOTH outcomes
                    snapshot = {
                        'timestamp': timestamp_dt.isoformat() + 'Z',
                        'data': {
                            'bookmakers': [{
                                'key': 'kalshi',
                                'title': 'Kalshi',
                                'markets': [{
                                    'key': market_key,
                                    'outcomes': [
                                        {
                                            'name': f'Over {line_value}' if market_type == 'total' else f'{line_value}',
                                            'price': yes_american_odds,
                                            'kalshi_cents': yes_price_cents,
                                        },
                                        {
                                            'name': f'Under {line_value}' if market_type == 'total' else f'Not {line_value}',
                                            'price': no_american_odds,
                                            'kalshi_cents': no_price_cents,
                                        }
                                    ]
                                }]
                            }]
                        }
                    }
                    snapshots.append(snapshot)
                else:
                    # Moneyline - single outcome per market (original behavior)
                    american_odds = kalshi_cents_to_american_odds(close_price)
                    if american_odds is None:
                        continue

                    snapshot = {
                        'timestamp': timestamp_dt.isoformat() + 'Z',
                        'data': {
                            'bookmakers': [{
                                'key': 'kalshi',
                                'title': 'Kalshi',
                                'markets': [{
                                    'key': market_key,
                                    'outcomes': [{
                                        'name': market_ticker.split('-')[-1],  # Extract team code
                                        'price': american_odds,
                                        'kalshi_cents': close_price,
                                    }]
                                }]
                            }]
                        }
                    }
                    snapshots.append(snapshot)

            print(f"Converted {len(snapshots)} valid snapshots (skipped {skipped_none} with no price, {skipped_time} outside time range)")
            return snapshots

        except Exception as e:
            print(f"Error fetching Kalshi candlesticks: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def get_ohlc_candles(self, market_ticker, series_ticker,
                               start_ts, end_ts, period_interval=1):
        """Return real OHLC candles for a Kalshi market as
        [{'t': end_period_ts, 'o','h','l','c'}] with prices in YES cents.

        Unlike get_historical_candlesticks (which keeps only the close and emits
        a TheOddsAPI-style line snapshot), this preserves open/high/low so the
        chart can render true candlesticks. Missing legs fall back to the close.
        """
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: self.kalshi_client.get_market_candlesticks(
                ticker=market_ticker, series_ticker=series_ticker,
                period_interval=period_interval,
                start_ts=int(start_ts), end_ts=int(end_ts)))

        def c(v):
            if v is None:
                return None
            try:
                return int(round(float(v) * 100))
            except (TypeError, ValueError):
                return None

        out = []
        for cd in data.get('candlesticks', []):
            p = cd.get('price') or {}
            o = c(p.get('open_dollars') or p.get('open'))
            hi = c(p.get('high_dollars') or p.get('high'))
            lo = c(p.get('low_dollars') or p.get('low'))
            cl = c(p.get('close_dollars') or p.get('close')
                   or p.get('previous_dollars') or p.get('previous'))
            if cl is None:  # resting-order-only period: fall back to bid/ask mid
                yb, ya = cd.get('yes_bid') or {}, cd.get('yes_ask') or {}
                ybc = c(yb.get('close_dollars') or yb.get('close'))
                yac = c(ya.get('close_dollars') or ya.get('close'))
                if ybc is not None and yac is not None:
                    cl = (ybc + yac) // 2
                else:
                    cl = ybc if ybc is not None else yac
            if cl is None:
                continue
            o = o if o is not None else cl
            hi = hi if hi is not None else max(o, cl)
            lo = lo if lo is not None else min(o, cl)
            t = cd.get('end_period_ts', 0)
            if not t:
                continue
            out.append({'t': int(t), 'o': o, 'h': hi, 'l': lo, 'c': cl})
        print(f"Fetched {len(out)} OHLC candles for {market_ticker}")
        return out


# ---------------------------------------------------------------------------
# Combined Kalshi × Polymarket volume heat map — DATA LAYER ONLY
# ---------------------------------------------------------------------------
# Rolls per-market USD-notional volume up Sport -> League -> Event -> Market off
# the already-built UnifiedEvents. The existing EventMatcher/MarketMatcher mapping
# is the source of truth: this neither re-matches nor re-fetches events, it only
# aggregates volume over what load_all_sports already produced. Rendering is
# intentionally absent — to_records()/to_nested() feed a px.treemap / go.Treemap
# (or ECharts/D3 later) without any plotting import leaking into the data layer.
#
# Volume unit = trailing-window USD notional on BOTH platforms (apples-to-apples):
#   * Polymarket: Market.volume_24hr (native USDC notional, already fetched bulk)
#   * Kalshi:    SUM(candle.volume_fp * candle.price.mean_dollars) via the batch
#                candlesticks endpoint — price-weighted, reuses
#                KalshiClient.candle_dollar_volume. Falls back to bulk contract
#                `volume` * last_price only when candles are unavailable, and flags
#                that node approximate (HeatmapNode.approx).
@dataclass
class HeatmapNode:
    id: str
    parent: str          # "" for sport-level roots
    label: str
    level: str           # 'sport' | 'league' | 'event' | 'market'
    value: float = 0.0           # rolled-up USD notional (Kalshi + Polymarket)
    kalshi_value: float = 0.0
    poly_value: float = 0.0
    approx: bool = False         # any contributing Kalshi leg used the fallback
    meta: dict = field(default_factory=dict)


class VolumeHeatmap:
    """Aggregates UnifiedEvents into a Sport->League->Event->Market volume tree.

    Usage:
        hm = await VolumeHeatmap(kalshi_client).compute(unified_events,
                                                        sport_by_league)
        records = hm.to_records()   # flat rows for Plotly treemap
        tree    = hm.to_nested()    # nested dict for custom renderers
    """

    # League -> sport category for common leagues. Non-big-4 leagues fall back to
    # the league label as its own sport bucket unless sport_by_league overrides.
    SPORT_CATEGORY = {
        'NFL': 'Football', 'NCAAF': 'Football',
        'NBA': 'Basketball', 'NCAAB': 'Basketball',
        'MLB': 'Baseball',
        'NHL': 'Hockey',
        'EPL': 'Soccer', 'UCL': 'Soccer', 'MLS': 'Soccer', 'LA_LIGA': 'Soccer',
        'BUNDESLIGA': 'Soccer', 'SERIE_A': 'Soccer', 'LIGUE_1': 'Soccer',
    }

    # Event lifecycle: a game is 'live' for this many hours after its start, then
    # 'final'. Generous enough to cover the longest games (extra innings / OT).
    LIVE_WINDOW_HOURS = 4.0
    STATUS_ICON = {'live': '🔴 ', 'final': '✓ ', 'future': '🏆 '}

    @staticmethod
    def _event_status(start_time) -> str:
        """live / upcoming / final / unknown from an ISO start_time. Date-only
        (midnight-UTC, from a Kalshi ticker with no time) -> 'unknown' since the
        intraday clock can't be inferred."""
        if not start_time:
            return 'unknown'
        from datetime import datetime, timezone
        try:
            s = start_time.replace('Z', '+00:00')
            if '+' not in s and 'T' in s and s.count(':') >= 2:
                s += '+00:00'
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return 'unknown'
        # Midnight-exact == date-only Kalshi parse; can't tell live vs final.
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return 'unknown'
        hrs = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        if hrs < 0:
            return 'upcoming'
        return 'live' if hrs <= VolumeHeatmap.LIVE_WINDOW_HOURS else 'final'

    def __init__(self, kalshi_client=None, window_hours: int = 24,
                 period_interval: int = 60):
        self.kalshi_client = kalshi_client      # KalshiClient (batch candles)
        self.window_hours = window_hours
        self.period_interval = period_interval
        self.nodes: dict = {}                    # id -> HeatmapNode

    async def compute(self, unified_events, sport_by_league: dict = None):
        """Fetch Kalshi candle dollars (batched, off-thread) then build the tree.
        sport_by_league: optional {league_label: sport_category}, e.g. from the
        ranked-series tags. Returns self."""
        kalshi_vwaps = await self._kalshi_vwaps(unified_events)
        self._build(unified_events, kalshi_vwaps, sport_by_league or {})
        return self

    # -- Kalshi price (VWAP) from batched candlesticks ---------------------
    async def _kalshi_vwaps(self, unified_events) -> dict:
        """{market_ticker: vwap_in_dollars} from the 24h candles — the volume-
        weighted average traded price. We dollarize the AUTHORITATIVE 24h contract
        count (volume_24h_fp on the bulk market dict) by this VWAP rather than
        summing raw candle volume, because candle volume can materially under-report
        a busy market's true 24h total (observed ~50% on a 2.3M-contract market),
        whereas the VWAP is a ratio and stays accurate. Empty dict -> callers fall
        back to the book mid. No client / no tickers -> empty."""
        if not self.kalshi_client:
            return {}
        tickers = []
        for ev in unified_events:
            for m in (ev.kalshi_markets or []):
                if isinstance(m, dict) and m.get('ticker'):
                    tickers.append(m['ticker'])
        tickers = list(dict.fromkeys(tickers))   # de-dupe, preserve order
        if not tickers:
            return {}
        end_ts = int(_time.time())
        start_ts = end_ts - self.window_hours * 3600
        loop = asyncio.get_event_loop()
        try:
            candle_map = await loop.run_in_executor(
                None,
                lambda: self.kalshi_client.get_markets_candlesticks_batch(
                    tickers, start_ts, end_ts, self.period_interval))
        except Exception as e:
            print(f"[heatmap] kalshi candle batch failed: {e}")
            return {}
        out = {}
        for tk, c in candle_map.items():
            contracts = 0.0
            for cd in (c or []):
                try:
                    contracts += float(cd.get('volume_fp', cd.get('volume', 0)) or 0)
                except (TypeError, ValueError):
                    pass
            if contracts > 0:                       # VWAP = $ / contracts
                out[tk] = KalshiClient.candle_dollar_volume(c) / contracts
        return out

    # -- per-market helpers ------------------------------------------------
    @staticmethod
    def _kalshi_24h_contracts(m: dict) -> float:
        """Authoritative trailing-24h contract count from the bulk market dict."""
        try:
            return float(m.get('volume_24h_fp', m.get('volume_fp', 0)) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _market_mid_dollars(m: dict) -> float:
        """Fallback price (dollars) when no candle VWAP: last/previous trade, else
        bid/ask mid. Kalshi market dicts carry *_dollars price fields."""
        for k in ('last_price_dollars', 'previous_price_dollars'):
            v = m.get(k)
            if v not in (None, ''):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        yb, ya = m.get('yes_bid_dollars'), m.get('yes_ask_dollars')
        try:
            if yb not in (None, '') and ya not in (None, ''):
                return (float(yb) + float(ya)) / 2.0
        except (TypeError, ValueError):
            pass
        lp = m.get('last_price')                    # legacy cents field
        try:
            return float(lp) / 100.0 if lp not in (None, '') else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _poly_market_usd(m) -> float:
        """Polymarket 24h USDC notional (native). Matches the 24h Kalshi window."""
        v = getattr(m, 'volume_24hr', None)
        if v is None and isinstance(m, dict):
            v = m.get('volume_24hr', m.get('volume24hr'))
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # -- tree assembly ------------------------------------------------------
    def _build(self, unified_events, kalshi_vwaps: dict, sport_by_league: dict):
        self.nodes = {}
        for ev in unified_events:
            league = ev.sport or 'Other'
            sport = (sport_by_league.get(league)
                     or self.SPORT_CATEGORY.get(league, league))
            sport_id = f"S::{sport}"
            league_id = f"L::{sport}::{league}"
            ev_key = (ev.kalshi_event_ticker or ev.polymarket_game_id
                      or ev.future_label or ev.get_display_title())
            event_id = f"E::{league_id}::{ev_key}"
            self._ensure(sport_id, '', sport, 'sport')
            self._ensure(league_id, sport_id, league, 'league')
            ev_name = (ev.future_label if ev.is_future
                       else f"{ev.away_team} @ {ev.home_team}".strip(' @')) or ev_key
            # Lifecycle status (sports markets, unlike stocks, have a game clock):
            # tag live/upcoming/final so finished-game tiles are honest, and the
            # view can offer a 'live & upcoming only' toggle.
            status = 'future' if ev.is_future else self._event_status(ev.start_time)
            ev_label = f"{self.STATUS_ICON.get(status, '')}{ev_name}".strip()
            self._ensure(event_id, league_id, ev_label, 'event', meta={
                'kalshi_event_ticker': ev.kalshi_event_ticker,
                'polymarket_game_id': ev.polymarket_game_id,
                'start_time': ev.start_time, 'is_future': ev.is_future,
                'status': status})

            for m in (ev.kalshi_markets or []):
                if not isinstance(m, dict):
                    continue
                tk = m.get('ticker')
                # USD = authoritative 24h contracts * VWAP (candle-derived). When
                # no candle VWAP, fall back to the book mid and flag approximate.
                contracts = self._kalshi_24h_contracts(m)
                vwap = kalshi_vwaps.get(tk)
                approx = vwap is None
                if approx:
                    vwap = self._market_mid_dollars(m)
                usd = contracts * vwap
                label = (m.get('yes_sub_title') or m.get('subtitle')
                         or m.get('title') or tk or 'market')
                self._add_market(f"M::{event_id}::K::{tk or label}", event_id,
                                 label, usd, kalshi=usd, poly=0.0, approx=approx,
                                 meta={'platform': 'kalshi', 'ticker': tk})

            for m in (ev.polymarket_markets or []):
                usd = self._poly_market_usd(m)
                q = getattr(m, 'question', None)
                if q is None and isinstance(m, dict):
                    q = m.get('question')
                pid = getattr(m, 'id', None) or (
                    m.get('id') if isinstance(m, dict) else None)
                self._add_market(f"M::{event_id}::P::{pid or q}", event_id,
                                 q or 'market', usd, kalshi=0.0, poly=usd,
                                 approx=False,
                                 meta={'platform': 'polymarket', 'id': pid})
        return self

    def _ensure(self, node_id, parent, label, level, meta=None):
        n = self.nodes.get(node_id)
        if n is None:
            n = HeatmapNode(id=node_id, parent=parent, label=label,
                            level=level, meta=meta or {})
            self.nodes[node_id] = n
        return n

    def _add_market(self, node_id, parent, label, value, kalshi, poly,
                    approx, meta):
        n = self.nodes.get(node_id)
        if n is None:
            n = HeatmapNode(id=node_id, parent=parent, label=label,
                            level='market', meta=meta or {})
            self.nodes[node_id] = n
        n.value += value
        n.kalshi_value += kalshi
        n.poly_value += poly
        n.approx = n.approx or approx
        self._rollup(parent, value, kalshi, poly, approx)

    def _rollup(self, node_id, value, kalshi, poly, approx):
        while node_id:
            n = self.nodes.get(node_id)
            if n is None:
                break
            n.value += value
            n.kalshi_value += kalshi
            n.poly_value += poly
            n.approx = n.approx or approx
            node_id = n.parent

    # -- outputs (rendering-agnostic) --------------------------------------
    def to_records(self) -> list:
        """Flat rows for px.treemap / go.Treemap (branchvalues='total'): parents
        equal the sum of their children because every leaf is rolled up exactly
        once. Carries the K/P split + approx flag for color modes."""
        return [{
            'id': n.id, 'parent': n.parent, 'label': n.label, 'level': n.level,
            'value': round(n.value, 2),
            'kalshi_value': round(n.kalshi_value, 2),
            'poly_value': round(n.poly_value, 2),
            'approx': n.approx,
            **{f'meta_{k}': v for k, v in n.meta.items()},
        } for n in self.nodes.values()]

    def to_nested(self) -> list:
        """Nested [{...,'children':[...]}] from the sport roots down, volume-sorted."""
        kids = {}
        for n in self.nodes.values():
            kids.setdefault(n.parent, []).append(n)

        def build(node):
            return {
                'id': node.id, 'label': node.label, 'level': node.level,
                'value': round(node.value, 2),
                'kalshi_value': round(node.kalshi_value, 2),
                'poly_value': round(node.poly_value, 2),
                'approx': node.approx, 'meta': node.meta,
                'children': [build(c) for c in sorted(
                    kids.get(node.id, []), key=lambda x: -x.value)],
            }
        return [build(r) for r in sorted(kids.get('', []),
                                         key=lambda x: -x.value)]


class OrderbookLadderWidget(QWidget):
    """Compact live depth ladder for the YES outcome

    Custom-painted so the per-tick update is cheap (one repaint, no widget churn):
    asks (red) stacked on top with the best ask just above the mid row, a mid row
    showing spread + last trade, then bids (green) below. A translucent horizontal
    bar behind each level encodes size relative to the largest visible level.
    Fed via set_data(asks, bids, last, stale); cents-denominated.
    """

    def __init__(self, levels: int = 8, parent=None):
        super().__init__(parent)
        self._levels = levels
        self._asks = []   # [(price_cents, qty)] best (lowest) first
        self._bids = []   # [(price_cents, qty)] best (highest) first
        self._last = None
        self._spread = None
        self._stale = False
        self.row_h = 16
        self._font = QFont("monospace", 9)
        self._font_b = QFont("monospace", 9); self._font_b.setBold(True)
        self.setMinimumWidth(150)
        self.setFixedHeight((levels * 2 + 1) * self.row_h + 4)

    def set_data(self, asks, bids, last, stale=False):
        self._asks = list(asks)[:self._levels]
        self._bids = list(bids)[:self._levels]
        self._last = last
        self._spread = (self._asks[0][0] - self._bids[0][0]) \
            if (self._asks and self._bids) else None
        self._stale = stale
        self.update()

    def clear(self):
        self._asks, self._bids = [], []
        self._last = self._spread = None
        self._stale = False
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        w, rh = self.width(), self.row_h
        p.fillRect(self.rect(), QColor(13, 17, 23))

        maxq = 1
        for _, q in (self._asks + self._bids):
            if q > maxq:
                maxq = q

        red, green = QColor(220, 90, 90), QColor(80, 200, 120)
        y = 2
        # Asks: reversed so the best (lowest) ask sits directly above the mid row.
        for price, qty in reversed(self._asks):
            self._row(p, y, w, rh, price, qty, maxq, red)
            y += rh
        # Mid row: spread (left) + last (right)
        p.fillRect(0, y, w, rh, QColor(22, 27, 34))
        p.setFont(self._font_b)
        spr = f"spr {self._spread}¢" if self._spread is not None else "spr —"
        p.setPen(QColor(150, 160, 170))
        p.drawText(QRectF(6, y, w - 12, rh),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), spr)
        last = f"last {self._last}¢" if self._last is not None else "last —"
        if self._stale:
            last += " ·resync"
        p.setPen(QColor(224, 176, 80))
        p.drawText(QRectF(6, y, w - 12, rh),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), last)
        y += rh
        # Bids: best (highest) first, directly below the mid row.
        for price, qty in self._bids:
            self._row(p, y, w, rh, price, qty, maxq, green)
            y += rh
        p.end()

    def _row(self, p, y, w, rh, price, qty, maxq, color):
        frac = min(1.0, qty / maxq) if maxq else 0.0
        bar_w = int(frac * (w - 8))
        bar = QColor(color); bar.setAlpha(50)
        p.fillRect(w - 4 - bar_w, y + 1, bar_w, rh - 2, bar)
        p.setFont(self._font)
        p.setPen(color.lighter(125))
        p.drawText(QRectF(6, y, 54, rh),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), f"{price}¢")
        p.setPen(QColor(200, 206, 214))
        p.drawText(QRectF(w - 76, y, 70, rh),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), f"{qty:,}")


class FilterableEventCombo(QComboBox):
    """Event selector that's a drop-in QComboBox (addItem/itemData/currentIndex…
    all preserved, so on_event_changed fires as usual) but whose popup is a custom
    CRT frame: a search box + Sort (Volume / Soonest / League / A–Z) + Platform
    filter (All / K+P / Kalshi / Poly) over a scrollable list. The button text is
    elided to the combo's fixed width, so long event titles never widen the header.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup = None
        self._sort_mode = 'Volume'
        self._platform = 'All'

    # --- elide the closed-combo text so long titles don't grow the header ---
    def paintEvent(self, _e):
        sp = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        text = opt.currentText
        opt.currentText = ''                       # draw frame/arrow without text
        sp.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        r = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self).adjusted(4, 0, -4, 0)
        elided = self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, r.width())
        sp.setPen(self.palette().color(self.foregroundRole()))
        sp.drawText(r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)

    def showPopup(self):
        self._ensure_popup()
        self._search.clear()
        self._rebuild_list()
        self._popup.setFixedWidth(max(self.width(), 380))
        self._popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self._popup.show()
        self._search.setFocus()

    def hidePopup(self):
        if self._popup:
            self._popup.hide()

    def _ensure_popup(self):
        if self._popup is not None:
            return
        fr = QFrame(self, Qt.WindowType.Popup)
        fr.setStyleSheet(
            "QFrame{background:#0d1117;border:1px solid #2b333d;}"
            "QLineEdit{background:#11161d;color:#cfd6df;border:1px solid #2b333d;"
            "font-family:monospace;font-size:11px;padding:2px 4px;}"
            "QComboBox{background:#161b22;color:#cfd6df;border:1px solid #2b333d;"
            "font-family:monospace;font-size:10px;padding:1px 2px;}"
            "QLabel{color:#7e8794;font-family:monospace;font-size:10px;}"
            "QListWidget{background:#0d1117;color:#cfd6df;border:1px solid #222a35;"
            "font-family:monospace;font-size:11px;}"
            "QListWidget::item:hover{background:#1c2530;}")
        v = QVBoxLayout(fr); v.setContentsMargins(4, 4, 4, 4); v.setSpacing(3)
        self._search = QLineEdit(); self._search.setPlaceholderText("search team / league…")
        self._search.textChanged.connect(self._rebuild_list)
        v.addWidget(self._search)
        row = QHBoxLayout(); row.setSpacing(4)
        self._sort = QComboBox(); self._sort.addItems(['Volume', 'Soonest', 'League', 'A–Z'])
        self._sort.currentTextChanged.connect(self._on_sort)
        self._plat = QComboBox(); self._plat.addItems(['All', 'K+P', 'Kalshi', 'Poly'])
        self._plat.currentTextChanged.connect(self._on_plat)
        row.addWidget(QLabel("Sort")); row.addWidget(self._sort, 1)
        row.addWidget(QLabel("Show")); row.addWidget(self._plat, 1)
        v.addLayout(row)
        self._count_lbl = QLabel("")
        v.addWidget(self._count_lbl)
        self._list = QListWidget(); self._list.setFixedHeight(420)
        self._list.itemClicked.connect(self._on_pick)
        v.addWidget(self._list)
        self._popup = fr

    def _on_sort(self, t):
        self._sort_mode = t; self._rebuild_list()

    def _on_plat(self, t):
        self._platform = t; self._rebuild_list()

    @staticmethod
    def _epoch(start_time):
        if not start_time:
            return None
        try:
            s = str(start_time)
            if s.endswith('Z'):
                s = s.replace('Z', '+00:00')
            elif '+' not in s and s.count(':') >= 2:
                s = s + '+00:00'
            return datetime.fromisoformat(s).timestamp()
        except (ValueError, TypeError):
            return None

    def _meta(self, i):
        text = self.itemText(i)
        data = self.itemData(i)
        if text.startswith('[K+P]'):
            plat = 'K+P'
        elif text.startswith('[K]'):
            plat = 'Kalshi'
        elif text.startswith('[P]'):
            plat = 'Poly'
        else:
            plat = '?'
        epoch = self._epoch(getattr(data, 'start_time', None))
        league = (getattr(data, 'sport', '') or '').lower()
        return text, plat, epoch, league

    def _rebuild_list(self):
        if self._popup is None:
            return
        q = self._search.text().lower().strip()
        rows = []
        for i in range(self.count()):
            text, plat, epoch, league = self._meta(i)
            if self._platform != 'All' and plat != self._platform:
                continue
            if q and q not in text.lower():
                continue
            rows.append((i, text, epoch, league))
        if self._sort_mode == 'Soonest':
            rows.sort(key=lambda r: (r[2] is None, r[2] or 0))
        elif self._sort_mode == 'League':
            rows.sort(key=lambda r: (r[3], r[2] or 0))
        elif self._sort_mode == 'A–Z':
            rows.sort(key=lambda r: r[1].lower())
        # 'Volume' = original (series-volume-ranked) order — leave as-is.
        self._list.clear()
        for orig_i, text, _e, _l in rows:
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, orig_i)
            self._list.addItem(it)
        self._count_lbl.setText(f"{len(rows)} of {self.count()} events")

    def _on_pick(self, item):
        orig_i = item.data(Qt.ItemDataRole.UserRole)
        self.hidePopup()
        if orig_i is not None:
            self.setCurrentIndex(int(orig_i))   # fires currentIndexChanged


class CandleBodyItem(pg.BarGraphItem):
    """BarGraphItem whose dataBounds honors orthoRange.

    Stock BarGraphItem.dataBounds ignores orthoRange and reports the full extent
    of EVERY bar. With the ViewBox in autoVisible-y mode that meant the %-axis was
    fitted to all rendered candles — including the off-screen overdraw margin,
    whose truncated edge candle changes shape on every live flush as the follow
    window slides — so the whole chart bounced vertically at ~30 fps in candle
    mode (lines were fine: PlotDataItem clips its bounds to the visible window).
    This subclass clips the reported y-extent to the bars whose x-slot intersects
    the visible x-range (and vice versa), restoring parity with the line items.
    """

    def dataBounds(self, ax, frac=1.0, orthoRange=None):
        opts = self.opts
        x = opts.get('x')
        if x is None or len(x) == 0:
            return None, None
        x = np.asarray(x, dtype=float)
        n = x.size
        y0 = np.broadcast_to(np.asarray(opts.get('y0') if opts.get('y0') is not None
                                        else 0.0, dtype=float), (n,))
        h = np.broadcast_to(np.asarray(opts.get('height') if opts.get('height') is not None
                                       else 0.0, dtype=float), (n,))
        w = opts.get('width')
        w = float(np.max(np.asarray(w, dtype=float))) if w is not None and np.ndim(w) else float(w or 0.0)
        xlo, xhi = x - w / 2.0, x + w / 2.0
        ylo, yhi = np.minimum(y0, y0 + h), np.maximum(y0, y0 + h)
        lo, hi = (xlo, xhi) if ax == 0 else (ylo, yhi)
        olo, ohi = (ylo, yhi) if ax == 0 else (xlo, xhi)
        if orthoRange is not None:
            mask = (ohi >= orthoRange[0]) & (olo <= orthoRange[1])
            if not mask.any():
                return None, None
            lo, hi = lo[mask], hi[mask]
        pw = self._penWidth[0] * 0.5 if getattr(self, '_penWidth', None) else 0.0
        return float(lo.min()) - pw, float(hi.max()) + pw


class HistoricalOddsWidget(QWidget):
    """Widget for displaying historical odds movement with point change handling"""

    # Loading progress (0-100). Routed to the host's bottom status banner instead
    # of an in-widget progress bar so the plot can extend to the widget's edge.
    loading_progress = pyqtSignal(int)

    # Combined Time+Interval presets: label -> (time_range_text, interval_text).
    # Drives the hidden backing time_range / kalshi_interval combos. Index 0 is the
    # DEFAULT: Live (sub-second feed). These are prediction markets — the live book is
    # the point; the historical 1m/1h/1d windows are mostly for sportsbook-style odds.
    TIMEFRAME_PRESETS = [
        ("Live",     ("24h", "Live")),  # seed last day of 1-min OHLC candles, then stream
        ("1m · 1h",  ("1h",  "1m")),
        ("1m · 6h",  ("6h",  "1m")),
        ("1h · 24h", ("24h", "60m")),
        ("1d · 7d",  ("7d",  "1440m")),
    ]

    def __init__(self, api_key, interval_minutes:int, parent=None, kalshi_api_key=None):
        super().__init__(parent)
        self.sport_key = None
        self.event_id = None
        self.market_key = None
        self.home_team = None
        self.away_team = None
        self.api_key = api_key

        # Initialize all three clients
        self.theoddsapi_client = HistoricalOddsClient(self.api_key, interval_minutes)
        self.kalshi_client = KalshiHistoricalOddsClient(api_key=kalshi_api_key)
        self.polymarket_client = PolymarketHistoricalOddsClient()

        # Default to Kalshi as primary
        self.client = self.kalshi_client
        self.data_source = 'kalshi'  # 'kalshi', 'polymarket', or 'theoddsapi'

        # Kalshi-specific attributes
        self.kalshi_event_ticker = None
        self.kalshi_series_ticker = None
        self.kalshi_market_ticker = None
        self.kalshi_available_markets = []

        # Polymarket-specific attributes
        self.polymarket_game = None
        self.polymarket_market = None
        self.polymarket_token_id = None
        self.polymarket_outcome_name = None

        # Unified event attributes
        self.current_unified_event = None  # Currently selected UnifiedEvent
        self.selected_market_data = None  # Currently selected market data dict

        self.bookmaker_visible = {}
        self.current_snapshots = []
        self._load_task = None  # Track current loading task
        self.interval_minutes = interval_minutes

        # Live update functionality
        self.auto_refresh_enabled = True
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.on_auto_refresh)
        self.refresh_interval_ms = 60000  # 60 seconds

        # Kalshi WebSocket for live odds updates
        self.kalshi_stream_client = None
        self.websocket_enabled = False  # Toggle for websocket vs polling

        # --- Sub-second "Live" feed state (only active when Interval == "Live") ---
        self.live_mode = False
        # Maintains best bid/ask + last trade per market from trade/orderbook stream.
        # on_gap -> resubscribe for a fresh snapshot when a seq number is skipped.
        # Native (C++/simdjson) Kalshi book when available — fed the raw frame
        # string via KalshiStreamClient.raw_frame so the JSON parse happens in C++.
        # EFFORTODDS_K_NATIVE=0 forces the Python book (A/B). on_gap is honored by
        # both (the native ingest re-acquires the GIL to call it on a seq gap).
        _force_k = __import__('os').environ.get('EFFORTODDS_K_NATIVE')
        self._k_native = NATIVE_PARSERS and _force_k != '0'
        self.live_book = (_native_parsers.KalshiLiveBook()
                          if self._k_native
                          else KalshiLiveBook(on_gap=self._on_live_book_gap))
        # Polymarket live book (CLOB market channel; full-snapshot + absolute-size
        # deltas, no sequence numbers so no gap handling needed). Trial: use the
        # native (C++/simdjson) book when available — fed by the raw frame string
        # via PolymarketStreamClient.raw_frame so the JSON parse happens in C++.
        # Falls back to the pure-Python book transparently.
        # EFFORTODDS_PM_NATIVE=0 forces the pure-Python book even when the native
        # module is present — a one-env-var A/B to confirm whether the native path
        # is responsible for any behaviour difference.
        _force_native = __import__('os').environ.get('EFFORTODDS_PM_NATIVE')
        self._pm_native = NATIVE_PARSERS and _force_native != '0'
        self.pm_live_book = (_native_parsers.PolymarketLiveBook()
                             if self._pm_native else PolymarketLiveBook())
        # Opt-in PM feed diagnostic (EFFORTODDS_PM_DEBUG=1): a one-shot confirmation
        # that raw frames are actually reaching the handler (the heavy parity rig —
        # shadow book, /tmp frame dump, live depth-diff — was retired once the native
        # book was verified and wired; this just answers "is the feed delivering?").
        self._pm_debug = __import__('os').environ.get('EFFORTODDS_PM_DEBUG') == '1'
        # Cached Polymarket top-of-book from the `best_bid_ask` event (custom_feature):
        # asset_id -> {'bid': cents, 'ask': cents, 't': monotonic}. This lightweight
        # quote often lands BEFORE the full `book` snapshot on (re)subscribe, so it
        # lets the readout/chart paint a price early and fills a one-sided depth book.
        # It NEVER overrides a side the depth book already has (see _pm_state_with_quote).
        self._pm_best_quote: dict = {}
        # Which source the sub-second feed is currently streaming for the selected
        # market: 'kalshi' or 'polymarket'. Chosen in _enter_live_mode and via the
        # live feed-source selector when a market is available on both.
        self.live_source = 'kalshi'
        # Polymarket streaming client (lazily started in enable_websocket_updates).
        self.polymarket_stream_client = None
        # Coalesced repaint: buffer every tick, repaint on a fixed cadence so the
        # GUI stays smooth during bursty markets without dropping data.
        self._live_dirty = False

        # Live overlay state / options (only meaningful while live_mode is True)
        self.auto_follow = True
        self.candle_mode = False
        self.candle_bucket_s = 5        # 1 / 5 / 15 ; 0 == "Max" (one candle/tick)
        self.show_spread_band = True
        # --- Liquidity heatmap (Bookmap-style resting-depth ribbon) ---------------
        # When on, each live flush samples the focus series' full depth ladder into
        # a per-cent column (size at each price level) and renders the accumulated
        # columns as a phosphor heat ribbon BEHIND the candles. Reads resting book
        # over time the way the static right-side ladder cannot.
        self.heatmap_mode = False
        self._heat_cols = deque(maxlen=900)   # (epoch_t, np.float32[100]) per column
        self._heat_img = None                 # pg.ImageItem, created lazily in init_ui
        self._heat_last_sample_t = 0.0        # throttle clock (sec)
        self._heat_sample_dt = 1.0            # min seconds between sampled columns
        self._heat_lut = self._build_heat_lut()
        self.live_follow_window_s = 600  # rolling window (sec) for the line view
        self.live_candle_visible_n = 60  # candles to keep in view when following
        # (so candle width stays visually consistent across 1s/5s/15s buckets)
        # Live corner-label running stats (american odds, float; tick-by-tick)
        self._live_lbl_open = None
        self._live_lbl_min = None
        self._live_lbl_max = None
        # Generic live-series model (supersedes primary/secondary): one entry per
        # (venue, outcome) being streamed/rendered. outcome_sel == 'all' shows
        # every outcome; otherwise a single outcome label. Default single (first).
        self.live_series = []
        self.outcome_sel = None  # set to first outcome / 'all' on market select
        # Futures (N-candidate season-long markets) get a distinct render: chart the
        # top-N candidates by price, order books for the top few only. Gated so the
        # head-to-head game path is completely unaffected.
        self._is_future_market = False
        self._FUT_CHART_N = 5   # top-N candidates drawn on the chart
        self._FUT_OB_N = 1      # top-N candidates that get an order-book ladder
        self.live_repaint_timer = QTimer()
        # ~30 fps. The chart (price tick sample + overlay render) updates every
        # tick at this cadence; the heavy panels (ladder HTML, summaries) are
        # TIME-throttled (~5 Hz) in _flush_live_plot — humans can't read numbers
        # faster, and time-based (not flush-count) guarantees the first flush after
        # data always renders the ladder. Tick resolution while trading is bounded
        # only by this interval (dirty-gated, so a quiet market isn't oversampled).
        self.live_repaint_timer.setInterval(33)
        self._flush_n = 0
        self._last_panel_t = 0.0   # wall clock of the last heavy-panel update
        # Per-feed last-message monotonic time (freshness/idle gauge) + last measured
        # ping/pong round-trip latency in ms (None until the first pong lands). RTT is
        # a true user<->exchange round trip (Kalshi: WS protocol ping/pong; PM: app
        # 'PING'/'PONG'), clock-skew-free since both stamps are local.
        self._k_last_msg = 0.0
        self._pm_last_msg = 0.0
        self._k_rtt_ms = None
        self._pm_rtt_ms = None
        self.live_repaint_timer.timeout.connect(self._flush_live_plot)

        self._initialize_kalshi_websocket()
        self._initialize_polymarket_websocket()

        self.init_ui()

    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        # Margins zeroed top & bottom: the controls now float over the plot (no
        # header rows) and the progress bar is gone, so the plot runs flush from
        # the top of the widget to the host banner. Right margin is 0 so the
        # DEPTH/VIEW panel sits flush against the window's right edge (the old 5px
        # left a dead dark strip beside the order book).
        layout.setContentsMargins(5, 0, 0, 0)

        # Controls live in a translucent strip that FLOATS over the plot's top
        # edge (parented to the plot below, after it exists) instead of consuming
        # two header rows — so the chart takes the full widget height. A small
        # always-visible toggle hides the strip for a fully clean chart.
        self.control_overlay = QFrame()
        self.control_overlay.setObjectName("controlOverlay")
        self.control_overlay.setStyleSheet(
            "QFrame#controlOverlay { background: rgba(13,17,23,222);"
            " border: 1px solid #2a3340; border-radius: 4px; }"
            " QFrame#controlOverlay QLabel { color:#7e8794; font-size:11px; }"
            " QFrame#controlOverlay QComboBox,"
            " QFrame#controlOverlay QPushButton { font-size:11px; }")
        header_layout = QHBoxLayout(self.control_overlay)
        header_layout.setContentsMargins(6, 2, 4, 2)
        header_layout.setSpacing(5)

        # Contextual market info. Only shown in TheOddsAPI (table-driven) mode,
        # where the event/market pickers are hidden and this is the sole context;
        # in prediction-market mode the Event dropdown already shows the matchup,
        # so it's hidden to keep the strip as compact as possible. NO stretch —
        # the strip hugs its content instead of spanning the graph width.
        self.market_info = QLabel("")
        self.market_info.setStyleSheet("color:#7e8794; font-style: italic; font-size:11px;")
        self.market_info.hide()
        header_layout.addWidget(self.market_info)

        # No verbose "Event:/Market:/Time:/Interval:" text labels — the dropdowns
        # are self-describing; tooltips carry the meaning and keep the strip tight.
        # Event selector - shows unified events from both sources
        self.event_selector = FilterableEventCombo()
        # Fixed width keeps long event titles from widening the header; the picker
        # popup carries the search/sort/platform filters and shows full titles.
        self.event_selector.setFixedWidth(230)
        self.event_selector.setToolTip("Event — click for search / sort / filter")
        self.event_selector.currentIndexChanged.connect(self.on_event_changed)
        header_layout.addWidget(self.event_selector)

        # Market selector for Kalshi (populated dynamically)
        self.market_selector = QComboBox()
        self.market_selector.setMinimumWidth(120)
        self.market_selector.setToolTip("Market")
        self.market_selector.currentIndexChanged.connect(self.on_market_changed)
        header_layout.addWidget(self.market_selector)

        # Time range + Interval are combined into ONE compact "timeframe" preset
        # dropdown (granularity·window). The two original combos are kept as hidden
        # BACKING state (parented but never shown / never in the layout) so every
        # existing reader — calculate_start_time, load_data, the Live detection,
        # TheOddsAPI min_interval — keeps working unchanged; the preset handler
        # keeps them in sync. This preserves full capability for the paid path.
        self.time_range = QComboBox(self.control_overlay)
        self.time_range.addItems(["1h", "3h", "6h", "12h", "24h", "7d"])
        self.time_range.hide()
        self.kalshi_interval = QComboBox(self.control_overlay)  # name kept for compat
        self.kalshi_interval.addItems([f"{M}m" for M in (1, 60, 1440)])
        self.kalshi_interval.addItem("Live")
        self.kalshi_interval.hide()
        # Default to Live (matches TIMEFRAME_PRESETS[0]) so the backing readers see
        # "Live"/"24h" before any timeframe change; the first market selection then
        # auto-enters live mode (see on_market_changed).
        self.kalshi_interval.setCurrentText("Live")
        self.time_range.setCurrentText("24h")

        # Visible combined control. userData = (range_text, interval_text); the
        # handler writes those into the hidden backing combos, then reloads.
        self.timeframe_combo = QComboBox()
        self.timeframe_combo.setToolTip("Window · candle granularity (Live = sub-second feed)")
        self.timeframe_combo.setFixedWidth(82)
        for label, (rng, itv) in self.TIMEFRAME_PRESETS:
            self.timeframe_combo.addItem(label, userData=(rng, itv))
        self.timeframe_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        header_layout.addWidget(self.timeframe_combo)

        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedWidth(26)
        self.refresh_button.setToolTip("Refresh")
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        header_layout.addWidget(self.refresh_button)

        # WebSocket status indicator
        self.ws_status_label = QLabel("⚪")
        self.ws_status_label.setToolTip("WebSocket: Not connected")
        self.ws_status_label.setFixedWidth(18)
        header_layout.addWidget(self.ws_status_label)

        # Volume Heat Map launcher — opens the Kalshi × Polymarket treemap in its
        # own resizeable window. Additive: does not touch the chart/live path.
        self.volume_map_button = QPushButton("▦")
        self.volume_map_button.setFixedWidth(26)
        self.volume_map_button.setToolTip("Volume Heat Map (Kalshi × Polymarket)")
        self.volume_map_button.clicked.connect(self.open_volume_map)
        header_layout.addWidget(self.volume_map_button)

        # Main content area
        content_layout = QHBoxLayout()
        # No gap/margins so the depth panel sits flush against the graph's right edge.
        content_layout.setSpacing(0)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # Plot section
        self.plot_panel = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_panel)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)

        # https://pyqtgraph.readthedocs.io/en/latest/api_reference/widgets/plotwidget.html
        # Create plot widget with DateAxisItem from the start to avoid scientific notation
        date_axis = pg.DateAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(
            # Near-black terminal screen, matching the order-book ladder and DEPTH
            # panel (#0d1117) instead of the old blue-grey (#29313D) — the whole
            # widget now reads as one CRT surface.
            background="#0d1117",
            axisItems={'bottom': date_axis}
        )
        self.plot_widget.setLabel('left', 'Implied %')
        self.plot_widget.setLabel('bottom', 'Time')
        #self.plot_widget.addLegend()
        # showGrid draws gridlines aligned to the real axis ticks instead of a
        # GridItem, which previously painted raw-unit ghost labels (e.g.
        # "1.7807e+09") over the plot.
        self.plot_widget.showGrid(x=True, y=True, alpha=0.12)
        # Tint the axis frame/graticule to a dim warm slate so the grid reads like
        # a phosphor graticule on black rather than bright default-white lines.
        # Only the line pen is touched — tick label colors are set per-tick as
        # HTML in _update_y_ticks, so they're left alone here.
        for _ax_name in ('left', 'bottom'):
            _ax = self.plot_widget.getAxis(_ax_name)
            _ax.setPen(pg.mkPen('#2a3340'))
            _ax.setTextPen(pg.mkPen('#7e8794'))
        # Fix the %-axis width: its tick labels ("55.0% (-122)") change length as
        # the range moves, and letting the axis auto-size makes the whole plot
        # rect shift sideways on every re-tick — visible as chart wobble. Sized
        # to the widest realistic label instead of auto.
        _lw = QFontMetrics(self.plot_widget.getAxis('left').font()
                           or QFont()).horizontalAdvance("88.88% (-8888)") + 14
        self.plot_widget.getAxis('left').setWidth(_lw)

        # Liquidity heatmap layer — an ImageItem that paints resting book depth as a
        # phosphor ribbon. Sits at the very back (negative z) so candles, the spread
        # band, the start line and the crosshair all draw on top of it. Hidden until
        # the Heat toggle is on; filled per tick in _refresh_heatmap().
        self._ensure_heat_img()
        # Implied chance is a probability: 0% and 100% are HARD floor/ceiling.
        # Bound the Y view so it can never pan/zoom past the valid range (no more
        # scrolling into negative % or above 100%). minYRange keeps a sane floor
        # on zoom-in so a flat market can't be magnified into noise.
        self.plot_widget.getViewBox().setLimits(yMin=0, yMax=100, minYRange=2)
        # Zoom/pan act on the TIME (x) axis only; the %-axis (y) auto-fits to whatever
        # data is visible in the current x-window. This makes the view a deterministic
        # function of the x-range: zooming out and back to the same span reproduces the
        # SAME view (candles keep a readable height) instead of leaving y stuck at some
        # wheel-zoomed range. minYRange=2 (above) keeps a flat market from collapsing to
        # a hairline. See _on_user_range_change, which re-asserts this after each drag.
        _vb0 = self.plot_widget.getViewBox()
        _vb0.setMouseEnabled(x=True, y=False)
        _vb0.setAutoVisible(y=True)
        _vb0.enableAutoRange(axis='y', enable=True)

        # If the user pans/zooms by hand while Live "Follow" is on, drop out of
        # follow so the view stops snapping back each tick (lets them inspect).
        # sigRangeChangedManually fires only for user-driven changes, not our
        # programmatic setXRange/auto-fit, so this won't self-trigger.
        self.plot_widget.getViewBox().sigRangeChangedManually.connect(
            self._on_user_range_change)
        # Manual "Follow live edge" toggle in the plot's right-click context menu
        # (mirrors the DEPTH/VIEW checkbox). Both drive _on_follow_toggled; the
        # action's checked state is kept in sync from there so the menu always shows
        # the true follow state (incl. when a pan auto-released it).
        self._follow_action = QAction("Follow live edge", self)
        self._follow_action.setCheckable(True)
        self._follow_action.setChecked(self.auto_follow)
        self._follow_action.toggled.connect(
            lambda on: self.follow_check.setChecked(on))
        _vb_menu = self.plot_widget.getViewBox().menu
        _vb_menu.addSeparator()
        _vb_menu.addAction(self._follow_action)
        # Regenerate y-axis ticks from the visible range as the user zooms/pans,
        # so the %-axis densifies/coarsens like the time axis (DateAxisItem) does
        # instead of keeping the load-time tick step forever.
        self.plot_widget.getViewBox().sigYRangeChanged.connect(self._update_y_ticks)

        # Team-logo watermarks: one per side (top band = positive-odds team,
        # bottom band = negative-odds team). Parented to the plot so they float
        # in viewport pixels — never pan/zoom/distort. Built on market change,
        # repositioned cheaply per update. See _update_side_logos().
        self._logo_size = 64
        self.logo_label_top = QLabel(self.plot_widget)
        self.logo_label_bottom = QLabel(self.plot_widget)
        for lbl in (self.logo_label_top, self.logo_label_bottom):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setStyleSheet("background: transparent;")
            lbl.hide()

        # Per-band summary overlays (net move + range + last-updated). Also
        # fixed widget-space corner labels, docked just left of each logo, so
        # they can NEVER overlap the graph lines no matter how the odds move.
        # Content set in _draw_series_annotations; positioned in
        # _position_overlays. See also the in-scene peak/trough triangles.
        self.summary_label_top = QLabel(self.plot_widget)
        self.summary_label_bottom = QLabel(self.plot_widget)
        for lbl in (self.summary_label_top, self.summary_label_bottom):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet(
                "background: rgba(18,22,28,210); border:1px solid #39414b;"
                " border-radius:3px; padding:4px 8px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.hide()

        # The current value ("NN% (+am)") is shown inside the corner summary boxes
        # (see _draw_series_annotations / _update_live_summary_label), not as a
        # separate on-chart tag — keeps it off the lines entirely.

        # Time-to-resolution countdown — the one genuinely-new readout vs the corner
        # boxes (which already carry last/move/range/updated). Rendered as a segment
        # of the x-axis legend (next to the Market Post / Event Start swatches, see
        # _rebuild_time_legend) rather than a floating top-left label. Ticks once a
        # second; counts DOWN to first pitch, then flips to "LIVE +elapsed". Hidden
        # when the event start time is unknown (date-only tickers).
        self._countdown_html = None
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_timer.start()

        # --- Crosshair scrubber + hover readout ---------------------------------
        # Mouse-tracking crosshair lines (added to the scene lazily in
        # _ensure_crosshair so they survive plot_widget.clear()) plus a floating
        # readout that reports each registered series' value at the cursor's time.
        self._cross_v = None
        self._cross_h = None
        # Subtle colored vertical marker at the event/game start time (re-added per
        # redraw in _ensure_start_line, like the crosshair, since clear() drops it).
        # Labeled via a legend swatch on the x-axis "Time" label, not on the line.
        self._start_line = None
        # Market Post marker — a second vertical line at the market's open_time (when
        # it was posted/opened for trading on Kalshi). market_post_iso is set on
        # market select from the selected Kalshi market's open_time.
        self._market_post_line = None
        self.market_post_iso = None
        self._kalshi_open_by_ticker = {}
        self._hover_series = []   # list of dicts: name/color/ts/pct/am
        self.hover_label = QLabel(self.plot_widget)
        self.hover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.setTextFormat(Qt.TextFormat.RichText)
        self.hover_label.setStyleSheet(
            "background: rgba(13,17,23,235); border:1px solid #39414b;"
            " border-radius:3px; padding:4px 7px; color:#dcdcdc;")
        self.hover_label.hide()
        # Throttle hover handling: the raw sigMouseMoved fires per mouse event (often
        # >100/s) and each call moves the crosshair (-> repaint) + rescans every
        # series. A SignalProxy rate-limits to ~60 Hz so fast cursor sweeps can't
        # flood the GUI thread. (Proxy delivers args as a 1-tuple.)
        self._hover_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved, rateLimit=60,
            slot=lambda evt: self._on_plot_hover(evt[0]))

        # Reparent the control strip onto the plot so it floats over the top edge,
        # and add the always-visible show/hide toggle. Positioned in
        # _position_overlays; visibility tracked by _controls_visible.
        self.control_overlay.setParent(self.plot_widget)
        self.control_overlay.show()
        self._controls_visible = True
        self.controls_toggle_btn = QPushButton("▴", self.plot_widget)
        self.controls_toggle_btn.setFixedSize(20, 18)
        self.controls_toggle_btn.setToolTip("Hide controls")
        self.controls_toggle_btn.setStyleSheet(
            "QPushButton { background: rgba(13,17,23,222); color:#7e8794;"
            " border:1px solid #2a3340; border-radius:3px; font-size:10px; }"
            " QPushButton:hover { color:#dcdcdc; }")
        self.controls_toggle_btn.clicked.connect(self._toggle_controls)
        self.controls_toggle_btn.show()

        self.plot_layout.addWidget(self.plot_widget)
        content_layout.addWidget(self.plot_panel, 4)

        # ============================================================
        # Right-side expandable DEPTH / VIEW panel
        # Hosts: live controls, the bid/ask/spr/last readout, the live order-book
        # ladder, and the (unchanged) per-bookmaker view toggles.
        # ============================================================
        self.right_panel = QWidget()
        self.right_panel.setObjectName("depthPanel")
        self.right_panel.setStyleSheet("""
            #depthPanel { background:transparent; }
            #depthBody { background:#0d1117; border-left:1px solid #222a35; }
            #depthPanel QLabel { color:#cfd6df; }
            #depthPanel QLabel#sect { color:#e0b050; font-family:monospace;
                font-size:9px; letter-spacing:1px; }
            #depthPanel QCheckBox { color:#cfd6df; font-family:monospace; font-size:11px; }
            #depthPanel QComboBox { background:#161b22; color:#cfd6df; border:1px solid #2b333d;
                font-family:monospace; font-size:11px; padding:1px 3px; }
            #depthHandle { background:transparent; color:#5b6675; border:none;
                font-size:13px; font-weight:bold; }
            #depthHandle:hover { background:rgba(224,176,80,40); color:#e0b050; }
        """)
        self._panel_body_w = 238  # includes room for the vertical scrollbar
        self._handle_w = 14
        # Outer = [thin transparent handle][collapsible body], handle flush against
        # the graph's right edge so toggling expands the body leftward over... no:
        # the body sits to the RIGHT of the handle and pushes the graph left.
        outer = QHBoxLayout(self.right_panel)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        # Always-visible transparent toggle handle (vertical strip at graph edge)
        self.panel_handle = QPushButton("⟨")
        self.panel_handle.setObjectName("depthHandle")
        self.panel_handle.setFixedWidth(self._handle_w)
        self.panel_handle.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.panel_handle.setToolTip("Show depth / view panel")
        self.panel_handle.clicked.connect(self._toggle_right_panel)
        outer.addWidget(self.panel_handle)

        # Collapsible body (content lives inside a scroll area so expanding any
        # section scrolls within the panel instead of growing the whole window).
        self.panel_body = QWidget()
        self.panel_body.setObjectName("depthBody")
        pb = QVBoxLayout(self.panel_body)
        # No right margin: the order book / ladder run flush to the panel's right
        # edge (which is the window's right edge) — the old 6px right margin left a
        # dead dark band beside the numbers.
        pb.setContentsMargins(6, 4, 0, 6); pb.setSpacing(6)
        # Header title + per-feed ping/pong round-trip latency (K / PM)
        title_row = QHBoxLayout(); title_row.setContentsMargins(0, 0, 0, 0); title_row.setSpacing(6)
        self.panel_title = QLabel("DEPTH / VIEW"); self.panel_title.setObjectName("sect")
        self.latency_label = QLabel("")
        self.latency_label.setTextFormat(Qt.TextFormat.RichText)
        self.latency_label.setStyleSheet("font-family:monospace;font-size:9px;")
        self.latency_label.setToolTip(
            "Feed health per source (K=Kalshi, PM=Polymarket).\n"
            "Number: true ping/pong round-trip latency to the exchange (Kalshi uses\n"
            "the WebSocket protocol ping/pong, PM the app-level PING/PONG); falls\n"
            "back to time-since-last-message until the first pong / when the feed goes\n"
            "quiet. Clock-skew-free — both timestamps are local, no NTP dependency.\n"
            "Colour: freshness — green <0.5s, amber <3s, red when stale/idle.")
        title_row.addWidget(self.panel_title)
        title_row.addStretch(1)
        title_row.addWidget(self.latency_label)
        pb.addLayout(title_row)

        # --- Live controls (Follow / Candles+bucket / Spread) ---
        self.live_controls = QWidget()
        lc = QVBoxLayout(self.live_controls)
        lc.setContentsMargins(0, 0, 0, 0); lc.setSpacing(3)
        row1 = QHBoxLayout(); row1.setSpacing(8)
        self.follow_check = QCheckBox("Follow")
        self.follow_check.setToolTip("Auto-scroll the time axis to keep the newest ticks in view")
        self.follow_check.setChecked(True)
        self.follow_check.stateChanged.connect(self._on_follow_toggled)
        self.spread_check = QCheckBox("Spread")
        self.spread_check.setToolTip("Shade the live bid/ask spread band")
        self.spread_check.setChecked(True)
        self.spread_check.stateChanged.connect(self._on_spread_toggled)
        self.heat_check = QCheckBox("Heat")
        self.heat_check.setToolTip("Liquidity heatmap: resting book depth as a phosphor ribbon behind the candles")
        self.heat_check.stateChanged.connect(self._on_heat_toggled)
        row1.addWidget(self.follow_check); row1.addWidget(self.spread_check)
        row1.addWidget(self.heat_check); row1.addStretch(1)
        lc.addLayout(row1)
        # Feed-source selector on its OWN row — keeping it off the Follow/Spread
        # row prevents that row from overflowing the fixed-width (238px) panel and
        # pushing it off-screen. Only shown when the market trades on BOTH Kalshi
        # and Polymarket; otherwise the sole source is used silently.
        self.feed_source_row = QWidget()
        rowfeed = QHBoxLayout(self.feed_source_row)
        rowfeed.setContentsMargins(0, 0, 0, 0); rowfeed.setSpacing(6)
        feed_lbl = QLabel("Feed"); feed_lbl.setStyleSheet("color:#9aa4b0;")
        self.feed_source_combo = QComboBox()
        self.feed_source_combo.setToolTip("Live feed source for this market")
        self.feed_source_combo.setFixedWidth(120)
        self.feed_source_combo.currentIndexChanged.connect(self._on_feed_source_changed)
        rowfeed.addWidget(feed_lbl); rowfeed.addWidget(self.feed_source_combo)
        rowfeed.addStretch(1)
        self.feed_source_row.setVisible(False)
        lc.addWidget(self.feed_source_row)
        # Side/outcome selector — which logical outcome the live overlay follows
        # (per venue), or "All" to overlay every outcome at once. Lists the
        # market's actual outcomes (teams / Over-Under / Home-Draw-Away / Yes-No).
        self.side_row = QWidget()
        rowside = QHBoxLayout(self.side_row)
        rowside.setContentsMargins(0, 0, 0, 0); rowside.setSpacing(6)
        side_lbl = QLabel("Side"); side_lbl.setStyleSheet("color:#9aa4b0;")
        self.side_combo = QComboBox()
        self.side_combo.setToolTip("Outcome the live feed tracks (or All to overlay every outcome)")
        self.side_combo.setFixedWidth(120)
        self.side_combo.currentIndexChanged.connect(self._on_side_changed)
        rowside.addWidget(side_lbl); rowside.addWidget(self.side_combo)
        rowside.addStretch(1)
        self.side_row.setVisible(False)
        lc.addWidget(self.side_row)
        row2 = QHBoxLayout(); row2.setSpacing(8)
        self.candle_check = QCheckBox("Candles")
        self.candle_check.setToolTip("Render the live feed as OHLC candlesticks")
        self.candle_check.stateChanged.connect(self._on_candle_toggled)
        self.candle_bucket = QComboBox()
        self.candle_bucket.addItems(["1s", "5s", "15s", "30s", "1m", "5m", "Max"])
        self.candle_bucket.setCurrentText("5s")
        self.candle_bucket.setFixedWidth(64)
        self.candle_bucket.setToolTip("Candle aggregation window (Max = one candle per tick)")
        self.candle_bucket.currentIndexChanged.connect(self._on_candle_bucket_changed)
        row2.addWidget(self.candle_check); row2.addWidget(self.candle_bucket); row2.addStretch(1)
        lc.addLayout(row2)
        pb.addWidget(self.live_controls)

        # --- Order book section (live only) ---
        self.ob_section = QWidget()
        obl = QVBoxLayout(self.ob_section)
        obl.setContentsMargins(0, 0, 0, 0); obl.setSpacing(2)
        ob_title = QLabel("ORDER BOOK"); ob_title.setObjectName("sect")
        obl.addWidget(ob_title)

        # Readout style reused by the dynamic per-series ladder groups.
        self._ro_style = ("color:#cfd6df; font-family:monospace; font-size:11px;"
                          " background:#11161d; border:1px solid #222a35;"
                          " border-radius:2px; padding:2px 5px;")
        # One (label + bid/ask readout + depth ladder) group per active live series
        # is built into this box by _rebuild_ob_ladders() whenever the series set
        # changes (feed/side/market). Supports 1..N stacked books.
        self.ob_ladders_box = QWidget()
        self.ob_ladders_layout = QVBoxLayout(self.ob_ladders_box)
        self.ob_ladders_layout.setContentsMargins(0, 0, 0, 0)
        self.ob_ladders_layout.setSpacing(4)
        obl.addWidget(self.ob_ladders_box)

        pb.addWidget(self.ob_section)

        # --- FUTURES FIELD (futures only) — the full candidate list with live prices,
        # replacing the unworkable 46-line chart / giant tooltip. Click a row to focus
        # that candidate (charts it + its order book). Populated/updated in the flush.
        self.futures_field_section = QWidget()
        ffl = QVBoxLayout(self.futures_field_section)
        ffl.setContentsMargins(0, 0, 0, 0); ffl.setSpacing(2)
        ff_title = QLabel("FIELD"); ff_title.setObjectName("sect")
        ffl.addWidget(ff_title)
        self.futures_field_list = QListWidget()
        self.futures_field_list.setStyleSheet(
            "QListWidget{background:#0d1117;border:1px solid #222a35;"
            "font-family:monospace;font-size:11px;color:#cfd6df;outline:0;}"
            "QListWidget::item{padding:1px 4px;}"
            "QListWidget::item:selected{background:#1b2430;}")
        self.futures_field_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.futures_field_list.setToolTip(
            "Left-click: chart this candidate (YES).\n"
            "Right-click: chart its NO (bet against them).")
        self.futures_field_list.itemClicked.connect(self._on_futures_field_clicked)
        self.futures_field_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.futures_field_list.customContextMenuRequested.connect(
            self._on_futures_field_context)
        ffl.addWidget(self.futures_field_list)
        pb.addWidget(self.futures_field_section)
        self.futures_field_section.setVisible(False)
        # ticker -> live implied % for EVERY candidate (filled from the lightweight
        # `ticker` channel subscribed across the whole field; the charted ~10 also have
        # full order books). ticker -> QListWidgetItem for in-place price updates.
        self._fut_prices = {}
        self._fut_field_items = {}
        self._fut_field_dirty = False

        # --- ORDER ENTRY (live/kalshi only; collapsible) ---
        self.order_entry_section = self._build_order_entry_section()
        pb.addWidget(self.order_entry_section)

        # --- Bookmaker view toggles (UNCHANGED logic; rehoused in a collapsible tab) ---
        # NOTE (redundancy): this "BOOKS" section (per-bookmaker show/hide
        # checkboxes) overlaps conceptually with the "Feed" selector above
        # (Kalshi/Polymarket/Both) — both gate which venue's data is shown. They are
        # NOT unified on purpose: BOOKS drives the legacy snapshot/polling render
        # pipeline (update_bookmaker_toggles / on_bookmaker_toggled), whereas Feed
        # drives the sub-second Live websocket pipeline (live_source / live_series).
        # Merging them would mean reconciling two different data sources, so they're
        # intentionally left separate for now.
        # No inner scroll — the outer panel scroll handles any overflow.
        self.bookmaker_panel = QWidget()
        self.bookmaker_layout = QVBoxLayout(self.bookmaker_panel)
        self.bookmaker_layout.setContentsMargins(0, 0, 0, 0)
        self.bookmaker_layout.setSpacing(2)
        self.books_section = self._collapsible_section("BOOKS", self.bookmaker_panel, expanded=True)
        pb.addWidget(self.books_section)

        pb.addStretch(1)

        # Wrap the whole body in a scroll area so expanding a section scrolls
        # within the (fixed-width) panel rather than enlarging the top-level window.
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidget(self.panel_body)
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panel_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.panel_scroll.setFixedWidth(self._panel_body_w)
        self.panel_scroll.setMinimumHeight(0)
        outer.addWidget(self.panel_scroll)
        content_layout.addWidget(self.right_panel, 0)

        # Live sections hidden until Live mode is entered
        self.live_controls.setVisible(False)
        self.ob_section.setVisible(False)
        self.order_entry_section.setVisible(False)
        # Start collapsed: just the thin transparent handle shows at the graph edge.
        self._panel_collapsed = True
        self.panel_scroll.setVisible(False)
        self.panel_handle.setText("⟨")

        layout.addLayout(content_layout)

        # Loading indicator: kept as an (unparented, never-shown) QProgressBar so
        # all the existing self.progress_bar.setValue(...) calls still work, but
        # NOT added to the layout — the plot now extends to the widget's bottom
        # edge. Its value is relayed via loading_progress to the host's status
        # banner (EffortOdds bottom strip with Last Update / API QUOTA).
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.valueChanged.connect(self.loading_progress.emit)

        # Initial state
        self.set_enabled(False)

        # Add initial "no data" message
        self.no_data_text = pg.TextItem("Loading events...",
                                      anchor=(0.5, 0.5))
        self.plot_widget.addItem(self.no_data_text)
        self.no_data_text.setPos(0.5, 0.5)

        # Load all sports on initialization
        QTimer.singleShot(100, lambda: asyncio.create_task(self.load_all_sports()))

    def _initialize_kalshi_websocket(self):
        """Initialize Kalshi WebSocket client for live odds updates"""
        try:
            self.kalshi_stream_client = KalshiStreamClient()

            # Connect signals
            self.kalshi_stream_client.connected.connect(self._on_websocket_connected)
            self.kalshi_stream_client.disconnected.connect(self._on_websocket_disconnected)
            self.kalshi_stream_client.error.connect(self._on_websocket_error)
            self.kalshi_stream_client.tick.connect(self._on_websocket_tick)
            # True ping/pong round-trip latency (ms) — fires every ping_interval.
            self.kalshi_stream_client.latency.connect(self._on_k_latency)
            # Sub-second Live feed (additive; only used in "Live" interval mode).
            # Native book: parse the raw frame in C++ (raw_frame) and drive the book
            # + dirty-flag from there — don't also connect the parsed-dict handlers
            # (avoids double-apply / extra cross-thread traffic). Python fallback
            # uses the parsed-dict trade/orderbook handlers.
            if self._k_native:
                self.kalshi_stream_client.raw_frame.connect(self._on_kalshi_raw_frame)
            else:
                self.kalshi_stream_client.trade.connect(self._on_ws_trade)
                self.kalshi_stream_client.orderbook.connect(self._on_ws_orderbook)

            print("✅ Kalshi WebSocket client initialized (not started)")
        except Exception as e:
            print(f"⚠️  Failed to initialize Kalshi WebSocket: {e}")
            self.kalshi_stream_client = None

    def _initialize_polymarket_websocket(self):
        """Initialize the Polymarket CLOB market-channel stream client (public,
        no auth). Mirrors the Kalshi init; started lazily on entering Live mode."""
        try:
            self.polymarket_stream_client = PolymarketStreamClient()
            self.polymarket_stream_client.connected.connect(self._on_pm_ws_connected)
            self.polymarket_stream_client.disconnected.connect(self._on_pm_ws_disconnected)
            self.polymarket_stream_client.error.connect(self._on_pm_ws_error)
            # True app-level PING/PONG round-trip latency (ms).
            self.polymarket_stream_client.latency.connect(self._on_pm_latency)
            # Sub-second Live feed signals. With the native book the raw frame
            # string is parsed in C++ (raw_frame) and the book + dirty-flag are
            # driven entirely from there, so we DON'T also connect the parsed-dict
            # orderbook/trade handlers (avoids ~2x cross-thread GUI signal traffic
            # and any double-apply). Python fallback uses the parsed-dict path.
            if self._pm_native:
                self.polymarket_stream_client.raw_frame.connect(self._on_pm_raw_frame)
            else:
                self.polymarket_stream_client.orderbook.connect(self._on_pm_orderbook)
                self.polymarket_stream_client.trade.connect(self._on_pm_trade)
            # best_bid_ask (custom_feature): a light top-of-book quote used for early
            # paint / one-sided-book fill. Wired in BOTH native and Python modes — the
            # native book parses depth from raw_frame but doesn't track this event, so
            # the quote cache is its only source. Filtered to the market channel; the
            # CLOB best_bid_ask frames aren't part of the raw orderbook parse.
            self.polymarket_stream_client.best_quote.connect(self._on_pm_best_quote)
            print(f"✅ Polymarket WebSocket client initialized "
                  f"({'native C++ book' if self._pm_native else 'Python book'})")
        except Exception as e:
            print(f"⚠️  Failed to initialize Polymarket WebSocket: {e}")
            self.polymarket_stream_client = None

    # ------------------------------------------------------------------
    # Active-source accessors: the sub-second Live render path is shared, so it
    # reads the book/key for whichever source (`self.live_source`) is streaming.
    # ------------------------------------------------------------------
    def _primary_source(self):
        """The primary feed source. In 'both' mode Kalshi is primary (it drives
        the existing flat overlay items, readout and order entry); Polymarket is
        the secondary overlay."""
        return 'polymarket' if self.live_source == 'polymarket' else 'kalshi'

    def _live_color(self, source=None):
        """Series/label color (r,g,b) for a feed source (default: primary)."""
        return BOOKMAKER_LINE_COLORS.get(source or self._primary_source(), (44, 160, 44))

    def _active_sources(self):
        """Sources currently being streamed/rendered (1 for single, 2 for 'both')."""
        if self.live_source == 'both':
            return ['kalshi', 'polymarket']
        return [self.live_source]

    def _pm_token_for_outcome(self, label):
        """YES CLOB token of the Polymarket market matching an outcome label, for
        SPLIT/3-way sports (soccer: separate 'Will <team> win?' + 'Will … draw?'
        markets) where the UnifiedMarket carries no single 2-outcome moneyline.
        Reads the event's per-outcome PM markets directly."""
        ev = getattr(self, 'current_unified_event', None)
        markets = getattr(ev, 'polymarket_markets', None) or []
        nl = EventMatcher._generic_normalize(label or '')
        if not nl:
            return None
        is_draw = ('draw' in nl or 'tie' in nl)
        # PM bundles many prop/sub-period markets under the same matchup (halftime,
        # exact score, leading, corners, cards…). Only the full-time moneyline
        # 'Will <team> win?' / '… end in a draw?' markets are valid outcome handles.
        PROP = ('halftime', 'half time', 'first half', 'second half', 'exact score',
                'leading', 'corner', 'card', 'booking', 'scorer', 'penalty')
        for m in markets:
            toks = getattr(m, 'clob_token_ids', None) or []
            if not toks:
                continue
            q = EventMatcher._generic_normalize(getattr(m, 'question', ''))
            if any(w in q for w in PROP):
                continue
            if is_draw and 'draw' in q:
                return toks[0]
            if not is_draw and 'win' in q and 'draw' not in q and nl in q:
                return toks[0]
        return None

    def _pm_token_for_current_market(self):
        """(token_id, outcome_name) of the Polymarket series we track live — the
        first outcome's CLOB token (matches the first plotted PM series). Returns
        (None, None) when the current market has no usable Polymarket token."""
        um = getattr(self, 'current_unified_market', None)
        m = getattr(um, 'polymarket_market', None) if um else None
        if m and getattr(m, 'clob_token_ids', None):
            outcome = m.outcomes[0] if getattr(m, 'outcomes', None) else None
            return m.clob_token_ids[0], outcome
        # Split / 3-way fallback: resolve from the event's per-outcome PM markets.
        for s in self._build_market_sides():
            if s.get('pm_token'):
                return s['pm_token'], s['label']
        return None, None

    def _build_market_sides(self):
        """Enumerate the selected market's logical OUTCOMES and resolve each to its
        per-venue live handle. Generic over outcome count — 2 teams, Over/Under,
        3-way Home/Draw/Away, Yes/No props.

        Returns a list of dicts (one per outcome):
          {'label': str,                 # outcome display name
           'pm_token': str|None,         # Polymarket CLOB token for this outcome
           'k_ticker': str|None,         # Kalshi market ticker for this outcome
           'k_complement': bool}         # True == track the Kalshi market's NO side
                                         #   (single-ticker binary, second outcome)

        Resolution is by OUTCOME NAME (Kalshi markets self-label via yes_sub_title),
        not list index, since the matcher doesn't guarantee kalshi_tickers and PM
        outcomes share an order. Falls back to index if a name match misses."""
        um = getattr(self, 'current_unified_market', None)
        if um is None:
            return []
        sport = getattr(self.current_unified_event, 'sport', None)
        pm = getattr(um, 'polymarket_market', None)
        pm_outcomes = list(pm.outcomes) if (pm and getattr(pm, 'outcomes', None)) else []
        pm_tokens = list(pm.clob_token_ids) if (pm and getattr(pm, 'clob_token_ids', None)) else []
        k_markets = um.kalshi_markets or []

        # Canonical outcome labels: prefer PM outcomes (clean names, token-aligned);
        # else derive from the Kalshi markets' yes_sub_titles.
        if pm_outcomes:
            labels = pm_outcomes
        elif k_markets:
            labels = [(km.get('yes_sub_title') or km.get('title') or f'Outcome {i+1}')
                      for i, km in enumerate(k_markets)]
        else:
            return []

        def _norm(s):
            try:
                return EventMatcher.normalize_team_name(s or '', sport)
            except Exception:
                return (s or '').strip().lower()

        single_k = len(k_markets) == 1
        sides = []
        for i, label in enumerate(labels):
            pm_token = pm_tokens[i] if i < len(pm_tokens) else None
            if pm_token is None:
                # Split / 3-way PM (soccer): no aligned single moneyline market —
                # resolve this outcome's token from the event's per-outcome markets.
                pm_token = self._pm_token_for_outcome(label)
            k_ticker, k_complement = None, False
            if single_k:
                # One Kalshi binary market: outcome 0 == YES, outcome 1 == NO
                # complement. Outcomes beyond the binary have no Kalshi handle.
                if i < 2:
                    k_ticker = k_markets[0].get('ticker')
                    k_complement = (i == 1)
            elif k_markets:
                nl = _norm(label)
                for km in k_markets:
                    sub = km.get('yes_sub_title') or km.get('title') or ''
                    if nl and (_norm(sub) == nl or nl in sub.lower()):
                        k_ticker = km.get('ticker')
                        break
                if k_ticker is None and i < len(k_markets):  # index fallback
                    k_ticker = k_markets[i].get('ticker')
            sides.append({'label': label, 'pm_token': pm_token,
                          'k_ticker': k_ticker, 'k_complement': k_complement,
                          'no': False})
        # NO sides: each outcome's complement (bet against it). For a single binary
        # market the two outcomes ARE the YES/NO of one market, so a NO side would just
        # duplicate the other outcome — skip. For multi-market events (separate per-team
        # markets) each market has a distinct NO book, so add one NO side per outcome.
        # `no=True` complements BOTH venues' YES data (price -> 100-price, bids<->asks).
        # FUTURES are excluded here: with ~46 candidates this would double the field to
        # YES+NO pairs (charting both, two order books). Futures NO is handled per
        # candidate via the FIELD right-click ('NO::' focus), not as standing sides.
        if not single_k and not self._is_future_market:
            no_sides = []
            for s in sides:
                if not (s['k_ticker'] or s['pm_token']):
                    continue
                no_sides.append({'label': f"{s['label']} · NO", 'pm_token': s['pm_token'],
                                 'k_ticker': s['k_ticker'], 'k_complement': False,
                                 'no': True})
            sides += no_sides
        return sides

    def _build_live_series(self):
        """Cross the feed axis (active venues) with the outcome axis (selected
        outcome, or all) into the list of live series to stream + render. Each:
          {'venue','outcome','key','k_complement','hue','dash','label','ticks','items'}
        Venue sets the hue (Kalshi green / PM blue); outcome sets the line dash so
        the two outcomes of a venue stay distinct (solid / dashed / dotted)."""
        sides = self._build_market_sides()
        if not sides:
            return []
        if self._is_future_market:
            return self._build_futures_live_series(sides)
        # Outcome axis. 'All' shows the base (YES) outcomes only — not the NO mirror
        # lines (they're individually selectable; overlaying them just clutters).
        if self.outcome_sel == 'all':
            chosen = [s for s in sides if not s.get('no')]
        else:
            chosen = [s for s in sides if s['label'] == self.outcome_sel] or sides[:1]
        # Stable dash per outcome label (shared across venues).
        dashes = [Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine,
                  Qt.PenStyle.DotLine, Qt.PenStyle.DashDotLine]
        label_dash = {s['label']: dashes[i % len(dashes)] for i, s in enumerate(sides)}
        venues = self._active_sources()
        series = []
        for s in chosen:
            for v in venues:
                if v == 'polymarket':
                    if not s['pm_token']:
                        continue
                    key, comp = s['pm_token'], False
                else:
                    if not s['k_ticker']:
                        continue
                    key, comp = s['k_ticker'], s['k_complement']
                series.append({
                    'venue': v, 'outcome': s['label'], 'key': key,
                    'k_complement': comp, 'no': s.get('no', False),
                    'hue': BOOKMAKER_LINE_COLORS.get(v, (44, 160, 44)),
                    'dash': label_dash.get(s['label'], Qt.PenStyle.SolidLine),
                    'label': f"{'K' if v == 'kalshi' else 'PM'} · {s['label']}",
                    'ticks': [], 'items': None,
                })
        return series

    # Distinct line colours for the top-N futures candidates (chart legibility).
    _FUT_PALETTE = [(76, 175, 80), (33, 150, 243), (255, 152, 0), (156, 39, 176),
                    (244, 67, 54), (0, 188, 212), (205, 220, 57), (121, 85, 72)]

    # Futures view tiers selectable from the Side combo: how many candidates to chart.
    _FUT_TIERS = {'top5': 5, 'top10': 10, 'all': None}  # None == every candidate
    # HARD cap on charted futures lines. Each line is a separate pyqtgraph curve item
    # repainted on every pan/hover (GUI-thread bound — can't be threaded), so beyond
    # ~10 the chart stutters and the per-line seed 429s Kalshi. The Side combo still
    # lists every candidate for individual focus; the full field belongs in a table.
    _FUT_MAX_CHART = 10

    def _build_futures_live_series(self, sides):
        """Futures live series (Kalshi-only). outcome_sel is either a TIER
        ('top5'/'top10'/'all') -> chart that many candidates by price with order books
        for the top few; or a specific candidate LABEL -> chart the top-5 for context
        plus that candidate, with the order book focused on it.

        Each series carries 'show_ob' so _rebuild_ob_ladders / the flush loop build
        ladders for only the focused/top candidates, not all N."""
        # Price per candidate ticker, to rank.
        price = {m.get('ticker'): self._future_market_price(m)
                 for m in (getattr(self.current_unified_market, 'kalshi_markets', None) or [])}
        ranked = sorted((s for s in sides if s.get('k_ticker')),
                        key=lambda s: price.get(s['k_ticker'], 0.0), reverse=True)
        if not ranked:
            return []
        sel = self.outcome_sel
        # NO::<label> -> focus that candidate's NO side (betting against them): the
        # complement line + the NO order book. Surfaced via right-click in the FIELD.
        no_focus = isinstance(sel, str) and sel.startswith("NO::")
        if no_focus:
            label = sel[4:]
            sel_side = next((s for s in ranked if s['label'] == label), None)
            if sel_side:
                return [{
                    'venue': 'kalshi', 'outcome': f"{label} NO", 'key': sel_side['k_ticker'],
                    'k_complement': False, 'no': True,
                    'hue': self._FUT_PALETTE[0], 'dash': Qt.PenStyle.SolidLine,
                    'label': f"K · {label} NO", 'show_ob': True,
                    'ticks': [], 'items': None,
                }]
            # candidate vanished — fall through to the default top-N tier.
            sel = 'top5'
        if sel in self._FUT_TIERS:
            # Tier overlay: top-N candidates (None == all), order books for the top few.
            n = self._FUT_TIERS[sel]
            top_chart = ranked if n is None else ranked[:n]
            ob_keys = {s['k_ticker'] for s in ranked[:self._FUT_OB_N]}
        else:
            # Specific candidate selected: FOCUS it — chart only that candidate's line
            # and show only its order book. (The tiers are for overlays; an individual
            # pick is "show me just this one".)
            sel_side = next((s for s in ranked if s['label'] == sel), None)
            if sel_side:
                top_chart = [sel_side]
                ob_keys = {sel_side['k_ticker']}
            else:
                top_chart = ranked[:self._FUT_CHART_N]
                ob_keys = {s['k_ticker'] for s in ranked[:self._FUT_OB_N]}
        # Hard cap: never chart more than _FUT_MAX_CHART lines (GUI-thread paint cost).
        top_chart = top_chart[:self._FUT_MAX_CHART]
        series = []
        for i, s in enumerate(top_chart):
            series.append({
                'venue': 'kalshi', 'outcome': s['label'], 'key': s['k_ticker'],
                'k_complement': s['k_complement'], 'no': False,
                'hue': self._FUT_PALETTE[i % len(self._FUT_PALETTE)],
                'dash': Qt.PenStyle.SolidLine,
                'label': f"K · {s['label']}",
                'show_ob': s['k_ticker'] in ob_keys,
                'ticks': [], 'items': None,
            })
        return series

    @staticmethod
    def _complement_state(st):
        """YES->NO complement of a Kalshi state dict (single-ticker binary, NO side):
        best_bid/ask swap-and-complement, last/mid -> 100-x."""
        if not st:
            return st
        def c(v):
            return None if v is None else 100 - v
        return {'best_bid': c(st.get('best_ask')), 'best_ask': c(st.get('best_bid')),
                'mid': c(st.get('mid')), 'last_trade': c(st.get('last_trade')),
                'stale': st.get('stale', False)}

    @staticmethod
    def _complement_ladder(lad):
        """YES->NO complement of a ladder dict: bids<->asks swap, price -> 100-price."""
        if not lad:
            return lad
        bids = [(100 - p, q) for p, q in lad.get('asks', [])]
        asks = [(100 - p, q) for p, q in lad.get('bids', [])]
        last = lad.get('last_trade')
        return {'bids': bids, 'asks': asks,
                'best_bid': bids[0][0] if bids else None,
                'best_ask': asks[0][0] if asks else None,
                'last_trade': (100 - last) if last is not None else None,
                'stale': lad.get('stale', False)}

    def _series_state(self, s):
        """Live state (cents) for one series. Complement applies when this is a NO side
        (`no` — flips BOTH venues' YES data) or the single-market Kalshi NO complement
        (`k_complement` — Kalshi only; the PM token there is the real other outcome)."""
        if s['venue'] == 'polymarket':
            st = self._pm_state_with_quote(s['key'])
            return self._complement_state(st) if (st and s.get('no')) else st
        st = self.live_book.state(s['key'])
        flip = s['k_complement'] or s.get('no')
        return self._complement_state(st) if (st and flip) else st

    def _series_ladder(self, s, depth):
        """Depth ladder for one series; complement on a NO side (both venues) or the
        single-market Kalshi NO complement (Kalshi only). See _series_state."""
        if s['venue'] == 'polymarket':
            lad = self.pm_live_book.ladder(s['key'], depth)
            return self._complement_ladder(lad) if (lad and s.get('no')) else lad
        lad = self.live_book.ladder(s['key'], depth)
        flip = s['k_complement'] or s.get('no')
        return self._complement_ladder(lad) if (lad and flip) else lad

    def _rebuild_live_series(self):
        """Recompute the active live-series set (feed × outcome), tearing down the
        old overlay items and per-series ladders and rebuilding the ladder stack.
        Overlay items are recreated lazily on the next refresh."""
        self._remove_overlay_items()
        self.live_series = self._build_live_series()
        self._rebuild_ob_ladders()
        self._rebuild_futures_field()
        # Force the next flush to repaint the (freshly rebuilt, empty) ladders
        # immediately rather than waiting out the panel throttle.
        self._last_panel_t = 0.0
        # The focus book may have changed (different outcome/feed) — start the
        # heat ribbon fresh so columns from the old book don't splice on.
        self._heat_cols.clear()
        self._heat_last_sample_t = 0.0

    def _rebuild_ob_ladders(self):
        """Rebuild the stacked order-book ladders — one (label + readout + ladder)
        group per active live series. Stores the widgets back on each series."""
        if not hasattr(self, 'ob_ladders_layout'):
            return
        while self.ob_ladders_layout.count():
            item = self.ob_ladders_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for s in self.live_series:
            # Futures chart many candidates but only a flagged few get an order book
            # ('show_ob'). Non-OB series still chart — they just have no ladder widget.
            if not s.get('show_ob', True):
                s['readout'], s['ladder'] = None, None
                continue
            r, g, b = s['hue']
            lbl = QLabel(s['label'].upper())
            lbl.setStyleSheet(f"color:#{r:02x}{g:02x}{b:02x};font-family:monospace;"
                              "font-size:9px;letter-spacing:1px;")
            ro = QLabel("")
            ro.setTextFormat(Qt.TextFormat.RichText)
            ro.setStyleSheet(self._ro_style)
            ro.setMinimumWidth(0)
            ro.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lad = OrderbookLadderWidget(levels=8)
            self.ob_ladders_layout.addWidget(lbl)
            self.ob_ladders_layout.addWidget(ro)
            self.ob_ladders_layout.addWidget(lad)
            s['readout'], s['ladder'] = ro, lad

    def _rebuild_futures_field(self):
        """(Re)build the FIELD list — every candidate of the active futures market,
        ranked by price, click-to-focus. Charted candidates get their line colour; the
        rest are dimmed. Hidden for non-futures markets."""
        if not hasattr(self, 'futures_field_list'):
            return
        show = self._is_future_market
        self.futures_field_section.setVisible(show)
        self.futures_field_list.clear()
        self._fut_field_items = {}
        if not show:
            return
        markets = getattr(self.current_unified_market, 'kalshi_markets', None) or []
        ranked = sorted(markets, key=self._future_market_price, reverse=True)
        for m in ranked:
            tkr = m.get('ticker')
            if not tkr:
                continue
            name = m.get('yes_sub_title') or m.get('title') or tkr
            # Seed the price store from the REST snapshot (live ticks refine it).
            self._fut_prices.setdefault(tkr, self._future_market_price(m) * 100.0)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, (tkr, name))
            self.futures_field_list.addItem(item)
            self._fut_field_items[tkr] = item
        self._fut_field_dirty = True
        self._update_futures_field()

    def _update_futures_field(self):
        """Refresh the FIELD rows in place: live %, charted candidates in their line
        colour (+ the focused one highlighted), the rest dimmed. Cheap — runs on the
        throttled panel cadence only when a field price changed."""
        if not self._is_future_market or not self._fut_field_items:
            return
        chart_color = {s['key']: s['hue'] for s in self.live_series}
        sel = self.outcome_sel
        no_label = sel[4:] if isinstance(sel, str) and sel.startswith("NO::") else None
        for tkr, item in self._fut_field_items.items():
            data = item.data(Qt.ItemDataRole.UserRole)
            name = data[1] if data else tkr
            pct = self._fut_prices.get(tkr)
            ptxt = f"{pct:4.0f}%" if pct is not None else "   —"
            # Mark the candidate when its NO side is the focused view.
            tag = " ·NO" if name == no_label else ""
            item.setText(f"{name[:17]:<17}{tag:<4}{ptxt}")
            hue = chart_color.get(tkr)
            item.setForeground(QColor(*hue) if hue else QColor('#7e8794'))
        self._fut_field_dirty = False

    def _on_futures_field_clicked(self, item):
        """Left-click a FIELD row -> focus that candidate's YES."""
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self._focus_futures_candidate(data[1], no=False)

    def _on_futures_field_context(self, pos):
        """Right-click a FIELD row -> focus that candidate's NO (bet against them)."""
        item = self.futures_field_list.itemAt(pos)
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        if data:
            self._focus_futures_candidate(data[1], no=True)

    def _focus_futures_candidate(self, name, no=False):
        """Focus one futures candidate (YES or NO) — chart its line + its order book.
        Mirrors _on_side_changed's re-subscribe path. NO uses the 'NO::' sentinel that
        _build_futures_live_series resolves to the complement view."""
        if not self.live_mode:
            return
        sel = f"NO::{name}" if no else name
        if sel == self.outcome_sel:
            return
        self._unsubscribe_from_current_market()
        self.outcome_sel = sel
        self._rebuild_live_series()
        self._populate_side_combo()
        self._update_order_entry_visibility()
        self._ensure_active_stream_started()
        self._subscribe_to_current_market()
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self.load_data())

    def _populate_side_combo(self):
        """Fill the Side selector with the market's outcomes + 'All'. Shown only in
        Live mode when there's >1 outcome. Keeps the current selection."""
        sides = self._build_market_sides()
        self.side_combo.blockSignals(True)
        self.side_combo.clear()
        if self._is_future_market:
            # Futures: tier overlays (Top 5 / Top 10 — the chart is capped at
            # _FUT_MAX_CHART lines for performance, so 'chart everything' isn't offered;
            # the full field is reachable by selecting a candidate below). Then every
            # candidate by price (selecting one focuses its line + order book).
            n_cand = sum(1 for s in sides if s.get('k_ticker'))
            self.side_combo.addItem("Top 5", 'top5')
            if n_cand > 5:
                self.side_combo.addItem(f"Top {self._FUT_MAX_CHART}", 'top10')
            price = {m.get('ticker'): self._future_market_price(m)
                     for m in (getattr(self.current_unified_market, 'kalshi_markets', None) or [])}
            by_label = {s['k_ticker']: s for s in sides if s.get('k_ticker')}
            for tkr, s in sorted(by_label.items(),
                                 key=lambda kv: price.get(kv[0], 0.0), reverse=True):
                pct = price.get(tkr, 0.0) * 100.0
                self.side_combo.addItem(f"{s['label']}  {pct:.0f}%", s['label'])
        else:
            for s in sides:
                self.side_combo.addItem(s['label'], s['label'])
            if len(sides) > 1:
                self.side_combo.addItem("All", 'all')
        idx = self.side_combo.findData(self.outcome_sel)
        if idx >= 0:
            self.side_combo.setCurrentIndex(idx)
        self.side_combo.blockSignals(False)
        self.side_row.setVisible(self.live_mode and (self._is_future_market or len(sides) > 1))

    @qasync.asyncSlot()
    async def _on_side_changed(self):
        """User picked a different outcome (or All) to follow live."""
        if not self.live_mode:
            return
        sel = self.side_combo.currentData()
        if not sel or sel == self.outcome_sel:
            return
        print(f"🎯 Live outcome -> {sel}")
        # Re-subscribe in place — DON'T stop/start the stream clients (stop() does
        # a blocking close that stalls the GUI). Unsubscribe old keys, rebuild the
        # series, ensure the needed clients are running, subscribe the new keys.
        self._unsubscribe_from_current_market()
        self.outcome_sel = sel
        self._rebuild_live_series()
        self._populate_side_combo()
        self._update_order_entry_visibility()
        self._ensure_active_stream_started()
        self._subscribe_to_current_market()
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self.load_data())

    def _available_live_sources(self):
        """Sources the currently selected market can stream a live feed from.

        Kalshi is available whenever a ticker is selected (incl. the legacy
        Kalshi-only selection path with no UnifiedMarket); Polymarket requires a
        UnifiedMarket carrying a usable CLOB token."""
        um = getattr(self, 'current_unified_market', None)
        srcs = []
        if self.kalshi_market_ticker and (um is None or um.has_kalshi()):
            srcs.append('kalshi')
        # PM available whenever a usable token resolves — covers the 2-outcome
        # moneyline (um.polymarket_market) AND split/3-way per-outcome markets.
        if um and self._pm_token_for_current_market()[0]:
            srcs.append('polymarket')
        return srcs

    def _populate_feed_sources(self):
        """Fill the feed-source selector from the current market's sources. The
        combo is only shown when both are available; the active source is selected
        without re-triggering a switch."""
        sources = self._available_live_sources()
        # Offer a "Both" option (dual orderbook + both lines) only when the market
        # trades on both venues.
        options = list(sources)
        if len(sources) > 1:
            options.append('both')
        self.feed_source_combo.blockSignals(True)
        self.feed_source_combo.clear()
        labels = {'kalshi': 'Kalshi', 'polymarket': 'Polymarket', 'both': 'Both'}
        for s in options:
            self.feed_source_combo.addItem(labels[s], s)
        idx = self.feed_source_combo.findData(self.live_source)
        if idx >= 0:
            self.feed_source_combo.setCurrentIndex(idx)
        self.feed_source_combo.blockSignals(False)
        self.feed_source_row.setVisible(len(sources) > 1)

    def _update_order_entry_visibility(self):
        """Show the order-entry panel when the active feed has a tradeable venue.
        Orders route to whichever venue _primary_source() resolves to (Kalshi, or
        Polymarket when Feed=Polymarket), so the panel is shown for either."""
        if hasattr(self, 'order_entry_section'):
            tradeable = any(s['venue'] in ('kalshi', 'polymarket')
                            for s in self.live_series)
            self.order_entry_section.setVisible(self.live_mode and tradeable)

    def _ensure_active_stream_started(self):
        """Start the active source(s) stream client(s) if not already running
        (used when switching markets while already in Live mode)."""
        for src in self._active_sources():
            if src == 'polymarket':
                if self.polymarket_stream_client and not self.polymarket_stream_client._is_running:
                    self.polymarket_stream_client.start()
            elif self.kalshi_stream_client and not self.kalshi_stream_client._is_running:
                self.kalshi_stream_client.start()

    @qasync.asyncSlot()
    async def _on_feed_source_changed(self):
        """User picked a different live feed source for the current market."""
        if not self.live_mode:
            return
        sel = self.feed_source_combo.currentData()
        if not sel or sel == self.live_source:
            return
        print(f"🔀 Switching live feed source -> {sel}")
        # Re-subscribe in place (no stop/start — stop() blocks the GUI on close).
        self._unsubscribe_from_current_market()
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        self.live_source = sel
        if sel in ('polymarket', 'both'):
            self.polymarket_token_id, self.polymarket_outcome_name = \
                self._pm_token_for_current_market()
        self._rebuild_live_series()
        self._populate_side_combo()
        self._update_order_entry_visibility()
        self._ensure_active_stream_started()
        self._subscribe_to_current_market()
        # Full reload clears the plot and rebuilds the chart + overlay items.
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self.load_data())

    def _on_websocket_connected(self):
        """Handle WebSocket connection"""
        print("🔌 Kalshi WebSocket connected")
        self.ws_status_label.setText("🟢")
        self.ws_status_label.setToolTip("WebSocket: Connected (Live)")
        self.ws_status_label.setStyleSheet("color: green")
        # Re-subscribe to current market if any
        if self.kalshi_market_ticker and self.websocket_enabled:
            self._subscribe_to_current_market()

    def _on_websocket_disconnected(self):
        """Handle WebSocket disconnection"""
        print("🔌 Kalshi WebSocket disconnected")
        self.ws_status_label.setText("🔴")
        self.ws_status_label.setToolTip("WebSocket: Disconnected")
        self.ws_status_label.setStyleSheet("color: red")

    def _on_websocket_error(self, error_data):
        """Handle WebSocket error"""
        print(f"⚠️  Kalshi WebSocket error: {error_data}")
        self.ws_status_label.setText("🟠")
        self.ws_status_label.setToolTip(f"WebSocket: Error - {error_data.get('action', 'unknown')}")

    def _on_websocket_tick(self, tick_data):
        """
        Handle incoming tick data from Kalshi WebSocket `ticker` channel.

        Current Kalshi schema (all prices are DOLLAR strings, 0-1, not cents):
        {
            "type": "ticker",
            "msg": {
                "market_ticker": "...",
                "price_dollars": "0.520",      # last traded YES price (may be absent)
                "yes_bid_dollars": "0.510",
                "yes_ask_dollars": "0.530",
                "ts_ms": 1234567890123,         # preferred (ms); also "ts" (sec)
                "time": "2026-05-30T..."        # deprecated RFC3339 fallback
            }
        }

        The previous implementation read `yes_price`/`timestamp`, which no longer
        exist on the message, so every live tick was silently dropped and the Kalshi
        line never moved during a game.
        """
        try:
            msg = tick_data.get('msg', {})
            market_ticker = msg.get('market_ticker')

            def _to_float(v):
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            # FUTURES FIELD: capture EVERY candidate's price (the whole field is
            # subscribed to the lightweight `ticker` channel), not just the focused
            # market, so the FIELD list shows live prices for all candidates.
            if self._is_future_market and market_ticker in self._fut_field_items:
                p = _to_float(msg.get('price_dollars'))
                if p is None:
                    b = _to_float(msg.get('yes_bid_dollars'))
                    a = _to_float(msg.get('yes_ask_dollars'))
                    p = ((b + a) / 2.0 if b is not None and a is not None
                         else (b if b is not None else a))
                if p is not None:
                    self._fut_prices[market_ticker] = p * 100.0
                    self._fut_field_dirty = True
                    self._live_dirty = True  # ensure the throttled flush runs

            # Only process the rest (chart line tick) for the current market.
            if market_ticker != self.kalshi_market_ticker:
                return

            # Prefer last traded price; fall back to the bid/ask mid so the line
            # still moves between trades during a live game. Keep legacy field
            # names as a last resort in case the schema flips back.
            price_dollars = _to_float(msg.get('price_dollars'))
            if price_dollars is None:
                price_dollars = _to_float(msg.get('price'))  # legacy
            if price_dollars is None:
                bid = _to_float(msg.get('yes_bid_dollars'))
                ask = _to_float(msg.get('yes_ask_dollars'))
                if bid is not None and ask is not None:
                    price_dollars = (bid + ask) / 2.0
                elif bid is not None:
                    price_dollars = bid
                elif ask is not None:
                    price_dollars = ask
            if price_dollars is None:
                # Last-ditch legacy cents field
                legacy_cents = msg.get('yes_price')
                if legacy_cents is not None:
                    price_dollars = _to_float(legacy_cents)
                    if price_dollars is not None and price_dollars > 1:
                        price_dollars = price_dollars / 100.0

            # Timestamp: ts_ms (ms) -> ts (sec) -> RFC3339 time -> now
            from datetime import datetime, timezone
            ts_ms = msg.get('ts_ms')
            ts_sec = msg.get('ts')
            if ts_ms is not None:
                tick_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            elif ts_sec is not None:
                tick_dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
            elif msg.get('time'):
                tick_dt = datetime.fromisoformat(msg['time'].replace('Z', '+00:00'))
            else:
                tick_dt = datetime.now(timezone.utc)

            if price_dollars is None or not (0.0 < price_dollars < 1.0):
                return

            price_cents = int(round(price_dollars * 100))
            american_odds = kalshi_cents_to_american_odds(price_cents)
            if american_odds is None:
                return

            print(f"📊 Live update: {market_ticker} = ${price_dollars:.3f} ({american_odds:+d})")

            now_iso = tick_dt.isoformat().replace('+00:00', 'Z')
            outcome_name = market_ticker.split('-')[-1] if '-' in market_ticker else 'Yes'

            # Match the market key the candlestick path uses so the live point lands
            # on the same series instead of spawning a stray h2h line.
            market_key = 'h2h'
            market_type = getattr(getattr(self, 'current_unified_market', None), 'market_type', None)
            if market_type == 'spread':
                market_key = 'spreads'
            elif market_type == 'total':
                market_key = 'totals'

            synthetic_snapshot = {
                'timestamp': now_iso,
                'data': {
                    'bookmakers': [{
                        'key': 'kalshi',
                        'title': 'Kalshi',
                        'markets': [{
                            'key': market_key,
                            'outcomes': [{
                                'name': outcome_name,
                                'price': american_odds,
                                'kalshi_cents': price_cents,
                            }]
                        }]
                    }]
                }
            }

            # Append to current snapshots
            if hasattr(self, 'current_snapshots') and self.current_snapshots:
                self.current_snapshots.append(synthetic_snapshot)

                # Throttle plot updates - only update every 10 seconds max
                if not hasattr(self, '_last_tick_plot_time'):
                    self._last_tick_plot_time = 0

                import time
                current_time = time.time()
                if current_time - self._last_tick_plot_time >= 10.0:
                    self._last_tick_plot_time = current_time
                    # Schedule async plot update
                    asyncio.create_task(self.update_plot(self.current_snapshots))

        except Exception as e:
            print(f"Error processing websocket tick: {e}")
            import traceback
            traceback.print_exc()

    def _subscribe_to_current_market(self):
        """Subscribe to live updates for the active source(s). In sub-second Live
        mode each active source subscribes its trade+book feed; outside Live mode
        only Kalshi's slow `ticker` channel applies (Polymarket has no such poll)."""
        if not self.live_mode:
            # Non-live ticker polling is Kalshi-only.
            if (self.kalshi_stream_client and self.kalshi_market_ticker
                    and self.live_source != 'polymarket'):
                print(f"📡 Subscribing to live updates for {self.kalshi_market_ticker}")
                self.kalshi_stream_client.subscribe_ticker([self.kalshi_market_ticker])
            return

        # Sub-second Live: clear per-series ticks + summary, then subscribe every
        # distinct key across the active series (N Kalshi tickers + N PM tokens).
        for s in self.live_series:
            s['ticks'] = []
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        k_tickers = sorted({s['key'] for s in self.live_series if s['venue'] == 'kalshi'})
        pm_tokens = sorted({s['key'] for s in self.live_series if s['venue'] == 'polymarket'})
        if k_tickers and self.kalshi_stream_client:
            for t in k_tickers:
                self.live_book.reset(t)
            print(f"📡 Kalshi sub-second feed: {len(k_tickers)} ticker(s)")
            # ONE combined subscription for all tickers. Kalshi serves only a single
            # active orderbook_delta subscription per connection (a second, separate
            # subscribe silently starves the first — confirmed live: SEA orderbook went
            # dead the instant PIT's separate sub was made). All tickers therefore share
            # one sid and one `seq` counter; the book validates seq PER-SID (not per
            # market) so the interleaved multi-market stream isn't seen as gaps.
            self.kalshi_stream_client.subscribe_live(k_tickers)
            # FUTURES: also subscribe the WHOLE field to the lightweight `ticker`
            # channel (last/bid/ask only, no order-book deltas) so the FIELD list shows
            # live prices for every candidate — not just the charted ~10.
            if self._is_future_market:
                field = sorted({m.get('ticker')
                                for m in (getattr(self.current_unified_market,
                                                  'kalshi_markets', None) or [])
                                if m.get('ticker')})
                if field:
                    self.kalshi_stream_client.subscribe_ticker(field)
        if pm_tokens and self.polymarket_stream_client:
            for t in pm_tokens:
                self.pm_live_book.reset(t)
                self._pm_best_quote.pop(t, None)  # drop stale top-of-book for re-sub
            print(f"📡 Polymarket sub-second feed: {len(pm_tokens)} token(s)")
            self.polymarket_stream_client.set_assets(pm_tokens)
            # PM lacks WS snapshot-recovery — seed the book(s) from REST right now so
            # the ladder populates immediately instead of waiting on a WS snapshot
            # that may not come until the next reconnect.
            asyncio.ensure_future(self._seed_pm_books_rest(pm_tokens))
        # Re-seed the (new) series' history so the overlay line/candles span the
        # market life, not just live ticks (runs in both line and candle mode).
        asyncio.ensure_future(self._apply_live_history_seed())

    def _unsubscribe_from_current_market(self):
        """Unsubscribe every active series' key from its venue feed."""
        k_tickers = sorted({s['key'] for s in self.live_series if s['venue'] == 'kalshi'})
        pm_tokens = {s['key'] for s in self.live_series if s['venue'] == 'polymarket'}
        for t in pm_tokens:
            # Local book teardown only. The PM server-side sub is changed by the next
            # set_assets, which diffs and sends explicit dynamic unsubscribe ops for
            # the tokens that actually drop out (cumulative subscriptions don't clear
            # by re-sending the initial payload).
            self.pm_live_book.reset(t)
            self._pm_best_quote.pop(t, None)
        if self.kalshi_stream_client and k_tickers:
            try:
                channels = ["trade", "orderbook_delta"] if self.live_mode else ["ticker"]
                self.kalshi_stream_client.unsubscribe(channels, k_tickers)
                if self.live_mode:
                    # All tickers share one sid; unsubscribe the distinct sid(s) once.
                    sids = {self.live_book.current_sid(t) for t in k_tickers}
                    self.kalshi_stream_client.unsubscribe_sids([s for s in sids if s is not None])
            except Exception as e:
                print(f"Failed to unsubscribe Kalshi: {e}")

    # ------------------------------------------------------------------
    # Sub-second Live feed (trade + orderbook_delta) — additive to the
    # existing `ticker` path used by _on_websocket_tick.
    # ------------------------------------------------------------------
    def _active_kalshi_keys(self):
        """Tickers of the Kalshi series currently being streamed (1..N)."""
        return {s['key'] for s in self.live_series if s['venue'] == 'kalshi'}

    def _on_kalshi_raw_frame(self, raw):
        """Native path: parse a Kalshi trade/orderbook frame in C++ and apply it to
        the native book in one call. ingest() returns the affected market's state
        (or None); we flag a repaint when it's one of the active Kalshi tickers.
        A sequence gap triggers on_gap (_on_live_book_gap) from inside ingest."""
        if not self.live_mode:
            return
        self._k_last_msg = _time.monotonic()
        try:
            st = self.live_book.ingest(raw)
            if not st:
                return
            tkr = st.get('market_ticker')
            # Native ingest surfaces a seq gap via the 'gap' flag (no C++ callback);
            # resubscribe for a fresh snapshot, same as the Python on_gap path.
            if st.get('gap'):
                self._on_live_book_gap(tkr)
            if tkr in self._active_kalshi_keys():
                self._live_dirty = True
        except Exception as e:
            print(f"Error processing Kalshi raw frame: {e}")

    def _on_k_latency(self, rtt_ms):
        """Record the Kalshi ping/pong round-trip (ms). Emitted from the stream
        client's `on_pong` every ping_interval — a genuine round trip, not a
        clock-skew-dependent server-timestamp diff."""
        self._k_rtt_ms = rtt_ms

    def _on_pm_latency(self, rtt_ms):
        """Record the Polymarket app-level PING/PONG round-trip (ms)."""
        self._pm_rtt_ms = rtt_ms

    def _on_ws_trade(self, msg):
        """Handle a Kalshi `trade` message (true per-execution tick). Accepts any
        ticker among the active Kalshi series ('All' outcomes streams several).

        Native mode: the book is fed via _on_kalshi_raw_frame instead — skip here."""
        inner = msg.get('msg', {}) or {}
        if self._k_native:
            return
        if not self.live_mode:
            return
        try:
            if inner.get('market_ticker') not in self._active_kalshi_keys():
                return
            self.live_book.apply(msg)
            self._live_dirty = True
        except Exception as e:
            print(f"Error processing live trade: {e}")

    def _on_ws_orderbook(self, msg):
        """Handle a Kalshi `orderbook_snapshot`/`orderbook_delta` message (any of
        the active Kalshi series' tickers)."""
        inner = msg.get('msg', {}) or {}
        if self._k_native:
            return  # native book fed via _on_kalshi_raw_frame
        if not self.live_mode:
            return
        try:
            if inner.get('market_ticker') not in self._active_kalshi_keys():
                return
            self.live_book.apply(msg)
            self._live_dirty = True
        except Exception as e:
            print(f"Error processing live orderbook: {e}")

    def _on_live_book_gap(self, ticker):
        """Sequence gap on the orderbook stream -> request a fresh snapshot.

        Primary recovery is Kalshi's `get_snapshot`: it re-baselines the affected
        books WITHOUT tearing down the subscription, so the delta stream keeps
        flowing and interleaved sibling markets on the same sid aren't disturbed —
        the fragile case for the old unsubscribe+resubscribe (both-sides YES, the
        multi-contract field). Only when snapshots don't stick (gaps recurring in a
        short window) do we ESCALATE to a full unsub+resub, the proven heavy hammer.

        Debounced: the book keeps applying deltas best-effort meanwhile, so an
        occasional resync suffices rather than one per message."""
        # Recover ANY active Kalshi series, not just the primary ticker. When both
        # sides of a market are streamed the non-primary book (e.g. the opposite
        # outcome) gaps too, and was previously never recovered -> it froze.
        if not self.live_mode or ticker not in self._active_kalshi_keys():
            return
        import time
        now = time.time()
        # Global debounce: all active tickers share one sid, so a gap is a whole-
        # subscription event — one resync covers every market on it.
        last = getattr(self, '_last_gap_resub', 0)
        if not isinstance(last, (int, float)):
            last = 0
        if now - last < 5.0:
            return
        self._last_gap_resub = now
        # Escalation streak: consecutive gaps within ~20s mean get_snapshot isn't
        # healing the stream, so fall back to the full teardown. A clean gap resets it.
        streak = getattr(self, '_gap_streak', 0)
        streak = streak + 1 if (now - getattr(self, '_last_gap_t', 0) < 20.0) else 1
        self._gap_streak = streak
        self._last_gap_t = now
        try:
            active = sorted(self._active_kalshi_keys())
            sids = [s for s in {self.live_book.current_sid(t) for t in active}
                    if s is not None]
            if streak >= 3:
                # get_snapshot isn't recovering — drop the sid(s) and re-subscribe the
                # whole set in one command so they land back on one sid. The fresh
                # snapshot rebinds every book; straggler deltas from the old sid are
                # ignored.
                print(f"⚠️  Orderbook gap x{streak} (sid={sids}) — full resubscribe {active}")
                if sids:
                    self.kalshi_stream_client.unsubscribe_sids(sids)
                self.kalshi_stream_client.subscribe_orderbook(active)
                self._gap_streak = 0
            else:
                print(f"⚠️  Orderbook seq gap (sid={sids}) — get_snapshot resync {active}")
                self.kalshi_stream_client.get_snapshot(active, sids)
        except Exception as e:
            print(f"Failed to recover after gap: {e}")

    # ------------------------------------------------------------------
    # Polymarket sub-second Live feed (CLOB market channel). Symmetric to the
    # Kalshi trade/orderbook handlers; both funnel into the shared live book +
    # _flush_live_plot render path via the active-source accessors.
    # ------------------------------------------------------------------
    def _on_pm_ws_connected(self):
        if self.live_source != 'polymarket':
            return
        print("🔌 Polymarket WebSocket connected")
        self.ws_status_label.setText("🟢")
        self.ws_status_label.setToolTip("WebSocket: Connected (Polymarket Live)")
        self.ws_status_label.setStyleSheet("color: green")

    def _on_pm_ws_disconnected(self):
        if self.live_source != 'polymarket':
            return
        print("🔌 Polymarket WebSocket disconnected")
        self.ws_status_label.setText("🔴")
        self.ws_status_label.setToolTip("WebSocket: Disconnected")
        self.ws_status_label.setStyleSheet("color: red")

    def _on_pm_ws_error(self, error_data):
        if self.live_source != 'polymarket':
            return
        print(f"⚠️  Polymarket WebSocket error: {error_data}")
        self.ws_status_label.setText("🟠")
        self.ws_status_label.setToolTip(
            f"WebSocket: Error - {error_data.get('action', 'unknown') if isinstance(error_data, dict) else error_data}")

    def _active_pm_keys(self):
        """Tokens of the Polymarket series currently being streamed (1..N)."""
        return {s['key'] for s in self.live_series if s['venue'] == 'polymarket'}

    def _on_pm_raw_frame(self, raw):
        """Native path: parse the raw market-channel frame in C++ and apply it to
        the native book in one call. ingest() returns the touched asset_ids, so we
        flag a repaint when any active PM series token changed."""
        if self._pm_debug and not getattr(self, '_pm_dbg_fired', False):
            self._pm_dbg_fired = True
            print(f"[pm-dbg] raw_frame FIRED (live_mode={self.live_mode} "
                  f"live_source={self.live_source!r}) — signal delivery OK")
        if not self.live_mode or self.live_source not in ('polymarket', 'both'):
            return
        self._pm_last_msg = _time.monotonic()
        try:
            touched = self.pm_live_book.ingest(raw)
            active = self._active_pm_keys()
            hit = bool(touched and (set(touched) & active))
            if hit:
                self._live_dirty = True
            if self._pm_debug:
                # Bisect a paused PM book: are frames arriving, do their asset_ids
                # match the active series keys, and does the native book hold depth?
                n = getattr(self, '_pm_dbg_n', 0) + 1
                self._pm_dbg_n = n
                if n % 25 == 0 or ('"event_type":"book"' in raw):
                    def _depth(k):
                        lad = self.pm_live_book.ladder(k, 50)
                        return (len(lad['bids']), len(lad['asks'])) if lad else None
                    rows = "; ".join(f"…{k[-6:]} depth={_depth(k)}" for k in active)
                    print(f"[pm-dbg] n={n} hit={hit} "
                          f"touched={[t[-6:] for t in (touched or [])]} "
                          f"active={[k[-6:] for k in active]} | {rows}")
        except Exception as e:
            print(f"Error processing Polymarket raw frame: {e}")

    def _on_pm_orderbook(self, ev):
        """Handle a Polymarket `book`/`price_change` event (orderbook update). The
        book is always applied (keyed by asset_id); a repaint is flagged when the
        event touches any active PM series token ('All' tracks several).

        Native mode: the book is fed via _on_pm_raw_frame instead — skip here so
        the native book isn't double-applied (it also has no dict apply())."""
        if self._pm_native:
            return
        if not self.live_mode or self.live_source not in ('polymarket', 'both'):
            return
        try:
            self.pm_live_book.apply(ev)
            if self._pm_event_matches(ev):
                self._live_dirty = True
        except Exception as e:
            print(f"Error processing Polymarket orderbook: {e}")

    def _on_pm_trade(self, ev):
        """Handle a Polymarket `last_trade_price` event (any active PM token)."""
        if self._pm_native:
            return  # native book fed via _on_pm_raw_frame
        if not self.live_mode or self.live_source not in ('polymarket', 'both'):
            return
        try:
            self.pm_live_book.apply(ev)
            if ev.get('asset_id') in self._active_pm_keys():
                self._live_dirty = True
        except Exception as e:
            print(f"Error processing Polymarket trade: {e}")

    def _on_pm_best_quote(self, ev):
        """Handle a Polymarket `best_bid_ask` event (custom_feature top-of-book).

        Independent of the depth book: it frequently arrives BEFORE the full `book`
        snapshot on (re)subscribe and again whenever the top moves. We cache it per
        asset (cents) so _pm_state_with_quote can paint a price early or fill a
        one-sided book — it never overrides a side the depth book already holds.
        Wired in both native and Python modes (the book parsers ignore this event)."""
        if not self.live_mode or self.live_source not in ('polymarket', 'both'):
            return
        if not isinstance(ev, dict):
            return
        asset_id = ev.get('asset_id')
        if not asset_id:
            return

        def _cents(v):  # PM dollar price (0-1) -> cents at 0.1¢, matching the book
            try:
                return round(float(v) * 100.0, 1)
            except (TypeError, ValueError):
                return None
        bid, ask = _cents(ev.get('best_bid')), _cents(ev.get('best_ask'))
        if bid is None and ask is None:
            return
        self._pm_best_quote[asset_id] = {'bid': bid, 'ask': ask, 't': _time.monotonic()}
        self._pm_last_msg = _time.monotonic()
        if asset_id in self._active_pm_keys():
            self._live_dirty = True

    def _pm_state_with_quote(self, key):
        """PM depth-book state, augmented from the cached `best_bid_ask` top-of-book
        when the book is absent or one-sided. Depth is authoritative for any side it
        has; the quote only fills a missing side (early paint / thin book). Returns a
        state dict shaped exactly like PolymarketLiveBook.state (so _complement_state
        and the flush path treat it identically)."""
        st = self.pm_live_book.state(key)
        bid = st.get('best_bid') if st else None
        ask = st.get('best_ask') if st else None
        # Book already two-sided -> it's complete; don't touch it.
        if bid is not None and ask is not None:
            return st
        q = self._pm_best_quote.get(key)
        if not q:
            return st
        fill_bid = bid if bid is not None else q.get('bid')
        fill_ask = ask if ask is not None else q.get('ask')
        if fill_bid is None and fill_ask is None:
            return st
        if fill_bid is not None and fill_ask is not None:
            mid = (fill_bid + fill_ask) / 2.0
        else:
            mid = fill_bid if fill_bid is not None else fill_ask
        return {'asset_id': key, 'best_bid': fill_bid, 'best_ask': fill_ask, 'mid': mid,
                'last_trade': (st.get('last_trade') if st else None),
                'stale': (st.get('stale', False) if st else False)}

    def _pm_event_matches(self, ev):
        """True if the event touches any active PM series token (price_change
        bundles per-asset changes under price_changes[])."""
        keys = self._active_pm_keys()
        if ev.get('asset_id') in keys:
            return True
        for ch in ev.get('price_changes', []) or []:
            if ch.get('asset_id') in keys:
                return True
        return False

    def _collapsible_section(self, title, content_widget, expanded=True):
        """Wrap content in a section with a clickable ▾/▸ header that toggles it.
        Reusable so more panel sections can be added concisely."""
        section = QWidget()
        v = QVBoxLayout(section)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(2)
        header = QPushButton(("▾ " if expanded else "▸ ") + title)
        header.setObjectName("sectHeader")
        header.setStyleSheet(
            "#sectHeader{background:transparent;border:none;color:#e0b050;"
            "font-family:monospace;font-size:9px;letter-spacing:1px;text-align:left;padding:2px 0;}"
            "#sectHeader:hover{color:#f0c060;}")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        content_widget.setVisible(expanded)

        def _toggle():
            vis = not content_widget.isVisible()
            content_widget.setVisible(vis)
            header.setText(("▾ " if vis else "▸ ") + title)
        header.clicked.connect(_toggle)
        v.addWidget(header)
        v.addWidget(content_widget)
        return section

    def _build_order_entry_section(self):
        """Concise Kalshi order-entry + open-orders, wrapped in a collapsible
        section. Real orders are placed only after an explicit confirmation."""
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
        content.setStyleSheet(
            "QSpinBox{background:#161b22;color:#cfd6df;border:1px solid #2b333d;"
            "font-family:monospace;font-size:11px;padding:1px 2px;}"
            "QPushButton#oePlace{background:#1c2530;color:#e0b050;border:1px solid #3a4452;"
            "font-family:monospace;font-weight:bold;padding:3px;}"
            "QPushButton#oePlace:hover{background:#27313e;}"
            "QListWidget{background:#11161d;color:#cfd6df;border:1px solid #222a35;"
            "font-family:monospace;font-size:10px;}")

        # Side + type
        row1 = QHBoxLayout(); row1.setSpacing(4)
        self.oe_side = QComboBox()
        self.oe_side.addItems(["Buy YES", "Buy NO", "Sell YES", "Sell NO"])
        self.oe_side.currentIndexChanged.connect(self._prefill_order_price)
        row1.addWidget(self.oe_side, 1)
        v.addLayout(row1)

        # Price + qty
        row2 = QHBoxLayout(); row2.setSpacing(4)
        pl = QLabel("¢"); pl.setStyleSheet("color:#8893a2;font-family:monospace;")
        self.oe_price = QSpinBox(); self.oe_price.setRange(1, 99); self.oe_price.setValue(50)
        self.oe_price.setToolTip("Limit price (cents)")
        self.oe_qty = QSpinBox(); self.oe_qty.setRange(1, 1000000); self.oe_qty.setValue(1)
        self.oe_qty.setToolTip("Contracts")
        row2.addWidget(pl); row2.addWidget(self.oe_price, 1)
        ql = QLabel("×"); ql.setStyleSheet("color:#8893a2;font-family:monospace;")
        row2.addWidget(ql); row2.addWidget(self.oe_qty, 1)
        v.addLayout(row2)

        self.oe_place_btn = QPushButton("PLACE ORDER")
        self.oe_place_btn.setObjectName("oePlace")
        self.oe_place_btn.clicked.connect(self._on_place_order)
        v.addWidget(self.oe_place_btn)

        # Open orders
        oo_row = QHBoxLayout(); oo_row.setSpacing(4)
        oo_lbl = QLabel("OPEN"); oo_lbl.setObjectName("sect")
        self.oe_refresh_btn = QPushButton("↻"); self.oe_refresh_btn.setFixedWidth(22)
        self.oe_refresh_btn.setStyleSheet("background:transparent;border:none;color:#8893a2;")
        self.oe_refresh_btn.clicked.connect(self._on_refresh_open_orders)
        oo_row.addWidget(oo_lbl); oo_row.addStretch(1); oo_row.addWidget(self.oe_refresh_btn)
        v.addLayout(oo_row)
        self.open_orders_list = QListWidget()
        self.open_orders_list.setFixedHeight(80)
        self.open_orders_list.setToolTip("Resting orders — double-click to cancel")
        self.open_orders_list.itemDoubleClicked.connect(self._on_cancel_order_item)
        v.addWidget(self.open_orders_list)

        return self._collapsible_section("ORDER ENTRY", content, expanded=True)

    def _prefill_order_price(self):
        """Seed the limit price from the live book for the chosen side."""
        if not self.kalshi_market_ticker:
            return
        state = self.live_book.state(self.kalshi_market_ticker)
        if not state:
            return
        text = self.oe_side.currentText()
        # YES price for yes-side; for no-side, NO price = 100 - yes price.
        yb, ya = state.get('best_bid'), state.get('best_ask')
        px = None
        if "YES" in text:
            px = (yb if text.startswith("Buy") else ya)
        else:  # NO side
            if ya is not None and text.startswith("Buy"):
                px = 100 - ya
            elif yb is not None:
                px = 100 - yb
        if px is not None and 1 <= px <= 99:
            self.oe_price.setValue(int(px))

    def _on_place_order(self):
        """Confirm (modal, SYNC) then submit the order on a background task.

        Kept a plain slot, NOT an @asyncSlot: a modal QMessageBox spins a nested
        Qt event loop, and under qasync the Qt loop *is* the asyncio loop — so
        opening it inside a running task makes qasync re-enter other pending
        tasks and raise "Cannot enter into task while another is being executed".
        Running the confirm in sync context (no task on the stack) avoids that;
        the actual order placement is scheduled as its own task below."""
        action, side = self.oe_side.currentText().lower().split(" ")  # buy/sell, yes/no
        price = self.oe_price.value()
        qty = self.oe_qty.value()

        # Route to Polymarket when that's the active feed; otherwise Kalshi.
        if self._primary_source() == 'polymarket':
            self._place_pm_order_flow(action, side, price, qty)
            return

        ticker = self.kalshi_market_ticker
        if not ticker:
            QMessageBox.warning(self, "No market", "No Kalshi market selected.")
            return
        cost = price * qty  # cents of max exposure for a buy
        # Show the EXACT translated V2 order (bid/ask + dollar price) the API will
        # receive, so a mis-mapped side/price is caught before risking real money.
        v2_side, _yc, v2_price = KalshiClient.legacy_to_v2_order(action, side, price)
        if QMessageBox.question(
                self, "Confirm order",
                f"{action.upper()} {qty} × {side.upper()} @ {price}¢\n"
                f"→ Kalshi V2: side={v2_side.upper()}  price=${v2_price}  count={qty}\n"
                f"Market: {ticker}\n"
                f"Max cost ≈ ${cost/100:,.2f}\n\nPlace this REAL order?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.oe_place_btn.setEnabled(False)
        asyncio.ensure_future(self._submit_order(ticker, action, side, qty, price))

    async def _submit_order(self, ticker, action, side, qty, price):
        """Place the order in a worker thread, refresh the resting list, and
        report failures via a DEFERRED dialog (QTimer.singleShot keeps the modal
        out of this task, where a nested loop would re-enter the event loop)."""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: self.kalshi_client.kalshi_client.create_order(
                    ticker=ticker, action=action, side=side, count=qty, price_cents=price))
            print(f"✅ Order placed: {resp}")
            await self._refresh_open_orders()
        except Exception as e:
            print(f"⚠️  Order failed: {e}")
            QTimer.singleShot(0, lambda e=e: QMessageBox.critical(self, "Order failed", f"{e}"))
        finally:
            self.oe_place_btn.setEnabled(True)

    def _resolve_pm_order_token(self, yn):
        """(token_id, outcome_label) the slip should trade. YES -> the selected PM
        outcome; NO -> the complementary outcome's token (NO of an outcome IS the
        other side's YES on Polymarket). Returns (None, None) if unavailable."""
        pm = [s for s in self.live_series if s['venue'] == 'polymarket']
        if not pm:
            return None, None
        sel = self.outcome_sel
        chosen = next((s for s in pm
                       if s.get('label') == sel or s.get('outcome') == sel), pm[0])
        if yn == 'yes':
            return chosen['key'], chosen.get('label')
        comp = next((s for s in pm
                     if s.get('outcome') != chosen.get('outcome')), None)
        return (comp['key'], comp.get('label')) if comp else (None, None)

    def _place_pm_order_flow(self, action, side, price_cents, qty):
        """Confirm + submit a Polymarket order. The entered cents is the price of
        the token being traded (the selected outcome for YES, the complement for
        NO); action maps directly to BUY/SELL."""
        token, outcome_lbl = self._resolve_pm_order_token(side)
        if not token:
            QMessageBox.warning(
                self, "No token",
                "No Polymarket token for that side. For a NO order, the opposite "
                "outcome must also be streaming (try Side: All).")
            return
        price_dollars = price_cents / 100.0
        cost = price_dollars * qty if action == 'buy' else 0.0
        if QMessageBox.question(
                self, "Confirm Polymarket order",
                f"{action.upper()} {qty} × {outcome_lbl} @ ${price_dollars:.2f}\n"
                f"(from slip: {action.upper()} {side.upper()} @ {price_cents}¢)\n"
                f"Token: …{token[-8:]}\n"
                f"Max cost ≈ ${cost:,.2f}\n\nPlace this REAL order?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.oe_place_btn.setEnabled(False)
        asyncio.ensure_future(
            self._submit_pm_order(token, action, qty, price_dollars))

    async def _submit_pm_order(self, token, side, qty, price_dollars):
        """Place a PM order off the GUI thread (py_clob_client is blocking)."""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: place_pm_order(token, price_dollars, qty, side))
            print(f"✅ Polymarket order placed: {resp}")
            await self._refresh_open_orders()
        except Exception as e:
            print(f"⚠️  Polymarket order failed: {e}")
            QTimer.singleShot(0, lambda e=e: QMessageBox.critical(
                self, "Polymarket order failed", f"{e}"))
        finally:
            self.oe_place_btn.setEnabled(True)

    @qasync.asyncSlot()
    async def _on_refresh_open_orders(self):
        await self._refresh_open_orders()

    async def _refresh_open_orders(self):
        """List resting orders for the active venue. Each item stores (venue,
        order_id) so cancel routes to the right API."""
        if self._primary_source() == 'polymarket':
            await self._refresh_pm_open_orders()
            return
        ticker = self.kalshi_market_ticker
        if not ticker:
            return
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: self.kalshi_client.kalshi_client.get_orders(
                    ticker=ticker, status="resting"))
        except Exception as e:
            print(f"⚠️  Open-orders fetch failed: {e}")
            return
        self.open_orders_list.clear()
        for o in resp.get("orders", []):
            side = o.get("side", "?")
            cnt = o.get("remaining_count", o.get("count", "?"))
            # V2 orders carry a single dollar `price` + bid/ask side; legacy carried
            # yes_price/no_price (cents) + action. Render either shape.
            if o.get("price") is not None and side in ("bid", "ask"):
                try:
                    px = f"{round(float(o['price']) * 100)}¢"
                except (TypeError, ValueError):
                    px = str(o.get("price"))
                label = f"{side.upper()} {cnt} @ {px}"
            else:
                action = o.get("action", "?")
                px = o.get("yes_price") if side == "yes" else o.get("no_price")
                label = f"{action[:1].upper()} {cnt}×{side.upper()} @ {px}¢"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ('kalshi', o.get("order_id")))
            self.open_orders_list.addItem(item)

    async def _refresh_pm_open_orders(self):
        """Resting Polymarket orders for the active PM token(s)."""
        tokens = self._active_pm_keys()
        if not tokens:
            return
        try:
            loop = asyncio.get_event_loop()
            orders = await loop.run_in_executor(
                None, lambda: get_pm_open_orders(None))
        except Exception as e:
            print(f"⚠️  PM open-orders fetch failed: {e}")
            return
        self.open_orders_list.clear()
        for o in (orders or []):
            # py_clob_client returns dict-ish orders: asset_id, side (BUY/SELL),
            # price (dollar str), original_size/size_matched, id.
            if o.get('asset_id') not in tokens:
                continue
            side = str(o.get('side', '?'))
            try:
                px = f"{round(float(o.get('price', 0)) * 100)}¢"
            except (TypeError, ValueError):
                px = str(o.get('price'))
            size = o.get('original_size', o.get('size', '?'))
            item = QListWidgetItem(f"{side} {size} @ {px}")
            item.setData(Qt.ItemDataRole.UserRole, ('polymarket', o.get('id')))
            self.open_orders_list.addItem(item)

    def _on_cancel_order_item(self, item):
        """Confirm (modal, SYNC) then cancel on a background task — same qasync
        re-entrancy avoidance as _on_place_order (no modal inside a running task)."""
        data = item.data(Qt.ItemDataRole.UserRole)
        # Items store (venue, order_id); tolerate a bare id from older state.
        venue, order_id = data if isinstance(data, tuple) else ('kalshi', data)
        if not order_id:
            return
        if QMessageBox.question(
                self, "Cancel order", f"Cancel {venue} order {str(order_id)[:8]}…?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        asyncio.ensure_future(self._submit_cancel(venue, order_id))

    async def _submit_cancel(self, venue, order_id):
        try:
            loop = asyncio.get_event_loop()
            if venue == 'polymarket':
                await loop.run_in_executor(None, lambda: cancel_pm_order(order_id))
            else:
                await loop.run_in_executor(
                    None, lambda: self.kalshi_client.kalshi_client.cancel_order(order_id))
            print(f"🗑️  Cancelled {venue} {order_id}")
        except Exception as e:
            QTimer.singleShot(0, lambda e=e: QMessageBox.critical(self, "Cancel failed", f"{e}"))
        await self._refresh_open_orders()

    def _toggle_right_panel(self):
        """Show/hide the DEPTH/VIEW body. Collapsed = just the thin transparent
        handle at the graph's right edge; expanding pushes the graph left."""
        self._panel_collapsed = not getattr(self, '_panel_collapsed', True)
        if self._panel_collapsed:
            self.panel_scroll.setVisible(False)
            self.panel_handle.setText("⟨")
            self.panel_handle.setToolTip("Show depth / view panel")
        else:
            self.panel_scroll.setVisible(True)
            self.panel_handle.setText("⟩")
            self.panel_handle.setToolTip("Hide depth / view panel")

    # ---- Live control handlers ----
    def _on_follow_toggled(self, state):
        self.auto_follow = bool(state)
        # Keep the right-click menu action in lockstep with the checkbox (and with
        # auto-releases from a pan). blockSignals avoids the toggle bouncing back.
        act = getattr(self, '_follow_action', None)
        if act is not None and act.isChecked() != self.auto_follow:
            act.blockSignals(True)
            act.setChecked(self.auto_follow)
            act.blockSignals(False)
        vb = self.plot_widget.getViewBox()
        if self.auto_follow:
            # Resume scrolling/auto-fit on the next tick.
            if self.live_mode:
                self._refresh_live_items()
        else:
            # Freeze the TIME axis where it is for inspection; the %-axis keeps
            # fitting to the visible data via _fit_live_y (mouse can't touch y).
            vb.enableAutoRange(x=False)
            if self.live_mode:
                (vxmin, vxmax), _ = vb.viewRange()
                self._fit_live_y(vxmin, vxmax, smooth=False)

    def _on_user_range_change(self, *args):
        """User panned/zoomed by hand -> switch to MANUAL view control.

        Follow (auto-scroll to the live edge) is nothing more than an auto-scroll
        MODE; the instant the user drives the view themselves it turns off, so the
        view stops snapping back and zoom/pan behave as plain, native pyqtgraph —
        IDENTICALLY whether Follow was on or off, and anywhere on the chart (incl.
        empty space past the last tick). Re-enable auto-scroll via the DEPTH/VIEW
        checkbox or the right-click menu.

        The overlay is also re-rendered for the new window (debounced): the overlays
        are clipped to the visible range, so without this the candles outside the old
        window stay blank until the next live flush (the zoom-out lag)."""
        if not self.live_mode:
            return
        if self.auto_follow:
            # Taking manual control of the TIME axis -> stop auto-scroll.
            self.follow_check.setChecked(False)
        # Re-fit the %-axis to the new x-window IMMEDIATELY (not just in the
        # debounced re-render): the view stays a pure function of the x-window —
        # reversible — and the fit carries the 0-100 overzoom blend, which
        # pyqtgraph's own autorange (capped at the data's peak/trough) cannot do.
        (vxmin, vxmax), _ = self.plot_widget.getViewBox().viewRange()
        self._fit_live_y(vxmin, vxmax, smooth=False)
        if not hasattr(self, '_zoom_render_timer'):
            self._zoom_render_timer = QTimer(self)
            self._zoom_render_timer.setSingleShot(True)
            self._zoom_render_timer.timeout.connect(
                lambda: self.live_mode and self._refresh_live_items())
        self._zoom_render_timer.start(40)  # coalesce a drag into one re-render

    def _on_spread_toggled(self, state):
        self.show_spread_band = bool(state)
        if self.live_mode:
            self._refresh_live_items()

    def _on_candle_toggled(self, state):
        self.candle_mode = bool(state)
        self.candle_bucket.setEnabled(self.candle_mode)
        if self.live_mode:
            # The overlay already carries seeded history in BOTH modes (the seed runs
            # on every subscribe now, not just in candle mode), and update_plot never
            # draws the K/PM lines while live — so toggling is a pure in-place visibility
            # flip (line <-> candles) on the SAME overlay items, no stale static layer to
            # collide with. Re-fit the axis when turning candles ON so the 1-min candle
            # window is readable; a defensive re-seed covers a series that somehow has no
            # history yet (idempotent — merges by timestamp).
            if self.candle_mode:
                self._axes_configured = False
                asyncio.ensure_future(self._apply_live_history_seed())
            self._refresh_live_items()

    def _on_candle_bucket_changed(self):
        self.candle_bucket_s = self._parse_bucket_seconds(self.candle_bucket.currentText())
        if self.live_mode and self.candle_mode:
            self._refresh_live_items()

    def _on_heat_toggled(self, state):
        self.heatmap_mode = bool(state)
        if self._heat_img is not None:
            self._heat_img.setVisible(self.heatmap_mode)
        if not self.heatmap_mode:
            # Drop accumulated columns so re-enabling starts a fresh ribbon rather
            # than splicing a stale gap onto the leading edge.
            self._heat_cols.clear()
            self._heat_last_sample_t = 0.0
        elif self.live_mode:
            self._refresh_heatmap()

    @staticmethod
    def _parse_bucket_seconds(text):
        """Candle bucket label -> seconds. 'Max' == 0 (one candle/tick); supports
        both 's' (seconds) and 'm' (minutes) suffixes."""
        if text == "Max":
            return 0
        if text.endswith('m'):
            return int(text[:-1]) * 60
        return int(text.removesuffix('s'))

    def _update_ob_readout(self, state, label):
        """Refresh a per-series inline bid/ask/spread/last readout label."""
        if label is None:
            return
        def c(v):
            return f"{v}¢" if v is not None else "—"
        bid, ask, last = state.get('best_bid'), state.get('best_ask'), state.get('last_trade')
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        stale = " <span style='color:#d08770'>·resync</span>" if state.get('stale') else ""
        label.setText(
            f"bid <span style='color:#8fbf7f'>{c(bid)}</span>  "
            f"ask <span style='color:#cf6f6f'>{c(ask)}</span>  "
            f"spr {c(spread)}  "
            f"last <span style='color:#cfd6df'>{c(last)}</span>{stale}")

    def _flush_live_plot(self):
        """Coalesced per-tick repaint. Records one tick and mutates the persistent
        Live overlay items in place (setData/setOpts) — never calls the heavy full
        update_plot, so the GUI thread stays responsive during fast markets."""
        if not self._live_dirty:
            return
        self._live_dirty = False
        if not self.live_series:
            return

        from datetime import datetime
        now_t = datetime.now().timestamp()  # local time matches the seed-path axis
        self._flush_n += 1
        # Heavy panels (order-book ladder HTML, corner summaries, latency text)
        # update at most ~5 Hz, TIME-based — humans can't read numbers at 30 Hz.
        # Time-based (not flush-count) is critical: it guarantees the FIRST flush
        # after data arrives renders the ladder (a flush-count gate could skip the
        # only flush that has data, leaving the book blank). Price-tick sampling +
        # chart render run EVERY flush for fine candles.
        panels_due = (now_t - self._last_panel_t) >= 0.18
        if panels_due:
            self._last_panel_t = now_t

        # When the widget is hidden (toggled off / window minimized) keep recording
        # ticks so the history stays continuous, but skip ALL the invisible visual
        # work (readouts, ladders, summaries, overlay render). The chart catches up
        # on the next live tick once it's shown again.
        vis = self.isVisible()

        any_ticks = False
        for idx, s in enumerate(self.live_series):
            st = self._series_state(s)
            if not st:
                any_ticks = any_ticks or bool(s['ticks'])
                continue
            # Prefer last executed trade; fall back to the bid/ask MID only when
            # both sides are present (a one-sided book yields a garbage price).
            px = st.get('last_trade')
            if px is None and st.get('best_bid') is not None and st.get('best_ask') is not None:
                px = st['mid']
            if px is not None and 0 < px < 100:
                s['ticks'].append({'t': now_t, 'price': px,
                                   'bid': st.get('best_bid'), 'ask': st.get('best_ask')})
                if len(s['ticks']) > 90000:  # cap ~50min of 33ms ticks
                    s['ticks'] = s['ticks'][-90000:]
            any_ticks = any_ticks or bool(s['ticks'])
            # Per-series readout + depth ladder — throttled (panels_due).
            if vis and panels_due and s.get('readout') is not None:
                self._update_ob_readout(st, s['readout'])
            if vis and panels_due and s.get('ladder') is not None:
                lad = self._series_ladder(s, s['ladder']._levels)
                if lad:
                    s['ladder'].set_data(lad['asks'], lad['bids'],
                                         lad.get('last_trade'), lad.get('stale', False))

        if not vis:
            return
        # Liquidity heatmap: sample the focus book into a depth column (self-throttled).
        if self.heatmap_mode:
            self._sample_heat_column(now_t)
        if panels_due:
            # Corner summaries + feed-latency readout (throttled).
            self._update_live_summaries(now_t)
            self._update_latency_label()
            if self._fut_field_dirty:
                self._update_futures_field()

        if not any_ticks:
            return
        # Ensure persistent items exist (first tick after a series-set change), then
        # do the cheap in-place refresh — EVERY flush (~30 fps) for fine candles.
        if not self._overlays_built():
            self._rebuild_live_items()
        self._refresh_live_items()

    def _update_latency_label(self):
        """Show the true ping/pong round-trip latency to each exchange next to the
        DEPTH/VIEW header. The NUMBER is RTT (user<->exchange); the COLOUR is feed
        freshness (idle age since the last frame) — green fresh, amber idle, red
        stale. The two are independent: a live feed can have low RTT but you still
        want red if frames stop arriving."""
        lbl = getattr(self, 'latency_label', None)
        if lbl is None:
            return
        import time as _t
        now = _t.monotonic()

        def chip(name, last, rtt_ms, active):
            if not active or last <= 0:
                return f"<span style='color:#5b6675'>{name} —</span>"
            idle_ms = (now - last) * 1000.0
            # Colour from freshness (how long since ANY frame): a quiet/dead feed
            # goes amber->red regardless of the measured round-trip time.
            color = '#5bd075' if idle_ms < 500 else ('#e0b050' if idle_ms < 3000 else '#cf6f6f')
            # Number = true round-trip latency once a pong has landed. Fall back to
            # idle age when the feed is stale (>3s) or no pong seen yet, since a held
            # RTT reading would be misleading on a dead/just-opened feed.
            if idle_ms >= 3000 or rtt_ms is None:
                txt = f"{idle_ms:.0f}ms" if idle_ms < 10000 else f"{idle_ms/1000:.0f}s"
            else:
                txt = f"{rtt_ms:.0f}ms"
            return f"<span style='color:{color}'>{name} {txt}</span>"

        k_on = self.live_source in ('kalshi', 'both')
        pm_on = self.live_source in ('polymarket', 'both')
        lbl.setText(chip('K', self._k_last_msg, self._k_rtt_ms, k_on) + " &nbsp; "
                    + chip('PM', self._pm_last_msg, self._pm_rtt_ms, pm_on))

    @staticmethod
    def _am_from_cents_float(cents):
        """American odds as a FLOAT from a (possibly fractional) cent price, so the
        live label can show sub-integer movement to one decimal place."""
        if cents is None:
            return None
        p = cents / 100.0
        if not (0.0 < p < 1.0):
            return None
        if p >= 0.5:
            return -100.0 * p / (1.0 - p)
        return 100.0 * (1.0 - p) / p

    def _update_live_summaries(self, now_t):
        """Update the corner range boxes — one per band (pos >=50% / neg <50%).

        Each box shows EVERY active venue's current price for that band side by
        side (venue-coloured, e.g. Kalshi 71.5% green next to Poly 71.5% blue), plus
        the open->cur move / range / updated-time for the band's first series.
        With Side='All' both bands are populated (favourite + underdog)."""
        from datetime import datetime
        updated = datetime.fromtimestamp(now_t).strftime('%H:%M:%S')

        def _track(s):
            """Current price (cents) for a series, updating its running open/min/max.
            Returns None when the series has no usable price yet."""
            st = self._series_state(s)
            if not st:
                return None
            mid = st.get('mid')
            cents = mid if mid is not None else st.get('last_trade')
            if cents is None or not (0.0 < cents < 100.0):
                return None
            cents = float(cents)
            if s.get('_lbl_open') is None:
                s['_lbl_open'] = s['_lbl_min'] = s['_lbl_max'] = cents
            else:
                s['_lbl_min'] = min(s['_lbl_min'], cents)
                s['_lbl_max'] = max(s['_lbl_max'], cents)
            return cents

        def _move_html(s, cents, lead):
            """Corner-box HTML: a bold coloured lead line + open→cur move/range/time."""
            r, g, b = s['hue']
            open_v = s['_lbl_open']
            delta = cents - open_v
            rng = s['_lbl_max'] - s['_lbl_min']
            if delta > 0:
                arrow, dcolor = '▲', (80, 200, 120)
            elif delta < 0:
                arrow, dcolor = '▼', (220, 90, 90)
            else:
                arrow, dcolor = '■', (170, 170, 170)
            dhex = '#%02x%02x%02x' % dcolor
            return (
                f"<div style='font-size:11pt'>{lead}</div>"
                f"<div style='font-size:9pt;color:#c8c8c8'>{open_v:.1f}%&#8594;{cents:.1f}% "
                f"<span style='color:{dhex}'>{arrow}{abs(delta):.1f}</span> "
                f"<span style='color:#8a93a0'>· rng {rng:.1f}%</span></div>"
                f"<div style='font-size:7pt;color:#7e8794'>updated {updated}</div>")

        # Futures: too many candidates for the favourite/underdog split — just show the
        # top-2 by current price, one per corner (named, line-coloured).
        if self._is_future_market:
            scored = [(s, c) for s in self.live_series if (c := _track(s)) is not None]
            scored.sort(key=lambda sc: sc[1], reverse=True)
            for idx, band in ((0, 'pos'), (1, 'neg')):
                label = self.summary_label_top if band == 'pos' else self.summary_label_bottom
                if idx >= len(scored):
                    label.hide()
                    continue
                s, cents = scored[idx]
                r, g, b = s['hue']
                mark = self._venue_marker(s.get('venue'), s['hue'])
                lead = (f"{mark}&nbsp;<span style='color:#{r:02x}{g:02x}{b:02x};"
                        f"font-weight:bold'>{s['outcome']} {cents:.1f}%</span>")
                self._set_summary_label(band, _move_html(s, cents, lead))
            return

        bands = {'pos': [], 'neg': []}
        for s in self.live_series:
            cents = _track(s)
            if cents is None:
                continue
            bands['pos' if cents >= 50.0 else 'neg'].append((s, cents))

        for band in ('pos', 'neg'):
            label = self.summary_label_top if band == 'pos' else self.summary_label_bottom
            items = bands[band]
            if not items:
                label.hide()
                continue
            # Big line: each venue's % tagged with its brand logo + coloured by
            # venue, so which price is Kalshi vs Polymarket reads at a glance.
            spans = []
            for s, cents in items:
                r, g, b = s['hue']
                mark = self._venue_marker(s.get('venue'), s['hue'])
                spans.append(f"{mark}&nbsp;<span style='color:#{r:02x}{g:02x}{b:02x};"
                             f"font-weight:bold'>{cents:.1f}%</span>")
            big = "&nbsp;&nbsp;&nbsp;&nbsp;".join(spans)
            # Move/range line tracks the band's first series.
            s0, c0 = items[0]
            self._set_summary_label(band, _move_html(s0, c0, big))

    def enable_websocket_updates(self, enable: bool = True):
        """
        Enable or disable WebSocket live updates

        Args:
            enable: True to use WebSocket, False to use polling
        """
        self.websocket_enabled = enable

        if enable:
            # Start every active source's stream client (one for single, both for
            # 'both' mode), then subscribe the current market on each.
            for src in self._active_sources():
                if src == 'polymarket':
                    if self.polymarket_stream_client and not self.polymarket_stream_client._is_running:
                        print("🚀 Starting Polymarket WebSocket...")
                        self.polymarket_stream_client.start()
                elif self.kalshi_stream_client and not self.kalshi_stream_client._is_running:
                    print("🚀 Starting Kalshi WebSocket...")
                    self.kalshi_stream_client.start()
            self._subscribe_to_current_market()
            # Stop polling timer
            self.refresh_timer.stop()
            # Start coalesced repaint cadence while in sub-second Live mode
            if self.live_mode and not self.live_repaint_timer.isActive():
                self.live_repaint_timer.start()
        else:
            self.live_repaint_timer.stop()
            # Stop whichever stream clients are running (source may have changed).
            if self.kalshi_stream_client and self.kalshi_stream_client._is_running:
                print("🛑 Stopping Kalshi WebSocket...")
                self.kalshi_stream_client.stop()
            if self.polymarket_stream_client and self.polymarket_stream_client._is_running:
                print("🛑 Stopping Polymarket WebSocket...")
                self.polymarket_stream_client.stop()
            # Resume polling if enabled
            if self.auto_refresh_enabled and self.data_source == 'kalshi':
                self.refresh_timer.start(self.refresh_interval_ms)

    def set_enabled(self, enabled):
        """Enable or disable the widget controls"""
        self.timeframe_combo.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.plot_widget.setEnabled(enabled)
        self.event_selector.setEnabled(enabled)
        self.market_selector.setEnabled(enabled)

    async def load_all_sports(self):
        """Gather the cross-platform event list and populate the event selector."""
        all_unified_events = await self.gather_unified_events()
        # Cache the loaded events so the volume heat map can reuse them instead of
        # re-running the (heavy, rate-limited) full gather — see _refresh_volume_map.
        self._last_unified_events = all_unified_events
        # Populate event selector with all events
        self.event_selector.blockSignals(True)
        self.event_selector.clear()
        for event in all_unified_events:
            self.event_selector.addItem(event.get_display_title(), userData=event)
        self.event_selector.blockSignals(False)

        # Enable controls + update the no-data message (original tail of this method).
        self.set_enabled(True)
        if all_unified_events:
            self.no_data_text.setText("Select an event to view historical odds")
        else:
            self.no_data_text.setText("No events available")

    async def gather_unified_events(self):
        """Build the cross-platform UnifiedEvent list (NO UI side effects) and
        return it past-filtered. Also stashes self.last_sport_by_league
        ({league_label: sport_category}) so the volume heat map can group leagues
        under their sport. Shared by load_all_sports and the heat map controller."""
        print(f"\n{'='*80}")
        print(f"Loading all sports events...")
        print(f"{'='*80}\n")

        # Configuration for filtering past events
        SHOW_PAST_EVENTS = False  # Set to True to show all events including past ones
        PAST_EVENT_CUTOFF_HOURS = 36  # Keep events from last N hours (generous to handle midnight UTC parsing)

        all_unified_events = []

        # Volume-ranked event-series config from KalshiClient's discovery cache
        # (data-driven, season-aware). No cache yet -> immediate big-4 menu + a
        # background scan that builds the cache and reloads with the full set.
        import time as _tp_all, sys as _sp_all
        _all_t0 = _tp_all.perf_counter()
        ranked = self.kalshi_client.kalshi_client.read_cached_event_series()
        if not ranked:
            ranked = [{'game': g, 'tag': t} for g, t in (
                ('KXNFLGAME', 'Football'), ('KXNBAGAME', 'Basketball'),
                ('KXMLBGAME', 'Baseball'), ('KXNHLGAME', 'Hockey'))]
            asyncio.ensure_future(self._refresh_series_cache_bg())

        # Load every ACTIVE league (matched + unmatched) — the filterable picker
        # makes a big list navigable. Drop only truly-dead (zero-volume) series;
        # the per-series timeouts below keep a slow league from blocking the menu.
        MAX_SERIES = 40
        ranked = [r for r in ranked
                  if r.get('volume') is None or r['volume'] >= 1][:MAX_SERIES]

        # big-4 GAME tickers get the full Kalshi+PM merge (they have PM series +
        # team aliases); every other league loads Kalshi-only for now.
        PM_SPORT_BY_GAME = {'KXNFLGAME': 'NFL', 'KXNBAGAME': 'NBA',
                            'KXMLBGAME': 'MLB', 'KXNHLGAME': 'NHL'}
        # Auto-map non-big-4 Kalshi leagues -> PM gamma series_id (World Cup, etc.)
        # so they merge cross-platform via the generic matcher.
        pm_series_map = self._build_kalshi_pm_series_map(ranked)
        # League -> sport-category map (from the ranked-series tags) so the volume
        # heat map can put each league under its sport. Built here where the big-4
        # labels (PM_SPORT_BY_GAME) and non-big-4 series labels are both known.
        self.last_sport_by_league = {}
        for _r in ranked:
            _gt = _r.get('game')
            _label = PM_SPORT_BY_GAME.get(_gt) or self._series_label(_r)
            if _label and _r.get('tag'):
                self.last_sport_by_league[_label] = _r['tag']
        # Kalshi Basic tier = ~20 read req/s (200 tokens/s ÷ 10 per request); PM
        # Gamma /events = 50 req/s. Each series fires ~1-2 Kalshi requests (~0.3s),
        # so 8 concurrent ≈ 20-26 req/s peak — at the Kalshi ceiling, with
        # _make_request's exponential backoff absorbing transient 429s. Kalshi is
        # the binding constraint (PM has 2.5x more headroom).
        sem = asyncio.Semaphore(8)

        async def _load_one(r):
            async with sem:
                gt = r.get('game')
                try:
                    # Per-series timeout so one slow/hung league can never block
                    # the whole menu from populating.
                    if gt in PM_SPORT_BY_GAME:
                        return await asyncio.wait_for(
                            self._load_unified_events_for_sport(PM_SPORT_BY_GAME[gt]),
                            timeout=25)
                    return await asyncio.wait_for(
                        self._load_kalshi_only_events_for_series(
                            self._series_label(r), gt, pm_series_id=pm_series_map.get(gt)),
                        timeout=25)
                except asyncio.TimeoutError:
                    print(f"[allsports] {gt} timed out (skipped)")
                    return []

        results = await asyncio.gather(*[_load_one(r) for r in ranked],
                                       return_exceptions=True)
        for r, res in zip(ranked, results):
            if isinstance(res, Exception):
                print(f"[allsports-probe] {r.get('game')} FAILED: {res}", file=_sp_all.stderr)
            else:
                all_unified_events.extend(res)  # series volume order preserved

        # Season-long futures (Kalshi-only) — additive, never blocks the game menu.
        fut_results = await asyncio.gather(
            *[self._load_kalshi_futures(sp) for sp in self.KALSHI_FUTURES_SERIES],
            return_exceptions=True)
        for res in fut_results:
            if isinstance(res, Exception):
                print(f"[allsports-probe] futures FAILED: {res}", file=_sp_all.stderr)
            else:
                all_unified_events.extend(res)
        if PERF_DIAG:
            print(f"[allsports-probe] {len(ranked)} series total={(_tp_all.perf_counter()-_all_t0)*1000:.0f}ms",
                  file=_sp_all.stderr)

        print(f"\n📊 Total events loaded from all sports: {len(all_unified_events)}")
        import time as _tp_post, sys as _sp_post
        _post_t0 = _tp_post.perf_counter()

        # Filter out past events if configured to do so
        if not SHOW_PAST_EVENTS:
            from datetime import datetime, timezone, timedelta

            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=PAST_EVENT_CUTOFF_HOURS)

            original_count = len(all_unified_events)
            filtered_events = []

            for event in all_unified_events:
                if event.start_time:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event.start_time.replace('Z', '+00:00'))

                        # If time is exactly midnight (00:00:00), this likely means we only have
                        # the date (from Kalshi ticker parsing). Add 24 hours buffer for such events.
                        if event_dt.hour == 0 and event_dt.minute == 0 and event_dt.second == 0:
                            # For midnight times, add 24 hours to account for games later in the day
                            event_dt_adjusted = event_dt + timedelta(hours=24)
                            if event_dt_adjusted >= cutoff:
                                filtered_events.append(event)
                        else:
                            # For events with actual time info, use standard cutoff
                            if event_dt >= cutoff:
                                filtered_events.append(event)
                    except Exception as e:
                        # If we can't parse the date, include it to be safe
                        print(f"⚠️  Could not parse date for {event.away_team} @ {event.home_team}: {event.start_time}")
                        filtered_events.append(event)
                else:
                    # If no start_time, include it to be safe
                    filtered_events.append(event)

            all_unified_events = filtered_events
            filtered_count = original_count - len(all_unified_events)

            if filtered_count > 0:
                print(f"🗑️  Filtered out {filtered_count} past events (older than {PAST_EVENT_CUTOFF_HOURS} hours)")
            print(f"📊 Showing {len(all_unified_events)} events")

        if PERF_DIAG:
            print(f"[post-probe] filter={(_tp_post.perf_counter()-_post_t0)*1000:.0f}ms "
                  f"(n={len(all_unified_events)})", file=_sp_post.stderr)
        return all_unified_events

    # ----- Volume heat map (Kalshi × Polymarket treemap) ----------------------
    # Additive feature in its OWN window: aggregates trailing-24h USD notional
    # across both platforms and rolls it up Sport->League->Event->Market. Reuses
    # gather_unified_events() (existing cross-platform mapping) for data + the
    # VolumeHeatmap data layer for the roll-up. NOTE: distinct _volmap_* namespace
    # — unrelated to the liquidity-ribbon _heat*/heatmap_mode state.
    def open_volume_map(self):
        """Open (or re-show) the volume heat map window and start live refresh."""
        if getattr(self, '_volmap_view', None) is None:
            from volume_heatmap_view import VolumeHeatmapView
            self._volmap_view = VolumeHeatmapView()
            self._volmap_view.refresh_requested.connect(self._kick_volume_map_refresh)
            self._volmap_view.closed.connect(self._on_volume_map_closed)
            self._volmap_timer = QTimer(self)
            self._volmap_timer.setInterval(90_000)   # 90s REST snapshot cadence
            self._volmap_timer.timeout.connect(self._kick_volume_map_refresh)
        self._volmap_view.show()
        self._volmap_view.raise_()
        self._volmap_view.activateWindow()
        self._volmap_timer.start()
        self._kick_volume_map_refresh()

    def _on_volume_map_closed(self):
        if getattr(self, '_volmap_timer', None) is not None:
            self._volmap_timer.stop()

    def _kick_volume_map_refresh(self):
        """Schedule the async refresh; guarded so ticks can't overlap a slow fetch."""
        if getattr(self, '_volmap_view', None) is None:
            return
        if getattr(self, '_volmap_busy', False):
            return
        asyncio.ensure_future(self._refresh_volume_map())

    async def _refresh_volume_map(self):
        self._volmap_busy = True
        try:
            self._volmap_view.set_status("loading…")
            # Reuse the events the widget already loaded for the selector — do NOT
            # re-run the full multi-league gather here (that doubled Kalshi calls and
            # could starve the main event load with 429s). Only fall back to a gather
            # if nothing has loaded yet. The Kalshi candle $ (the live-moving part) is
            # still re-fetched fresh inside VolumeHeatmap.compute below.
            events = getattr(self, '_last_unified_events', None)
            if not events:
                events = await self.gather_unified_events()
                self._last_unified_events = events
            # Recover finished games' Kalshi side (settled markets drop out of the
            # 'open' fetch, leaving finals reading 100% Polymarket). Best-effort;
            # operates on COPIES so the shared event list / dropdown is untouched.
            events = await self._enrich_settled_kalshi(events)
            kalshi = self.kalshi_client.kalshi_client   # underlying KalshiClient
            hm = await VolumeHeatmap(kalshi).compute(
                events, getattr(self, 'last_sport_by_league', None))
            self._volmap_view.set_records(hm.to_records())
            from datetime import datetime as _dt
            self._volmap_view.set_status(
                f"updated {_dt.now():%H:%M:%S} · {len(events)} events")
        except Exception as e:
            import traceback
            traceback.print_exc()
            if getattr(self, '_volmap_view', None) is not None:
                self._volmap_view.set_status(f"refresh failed: {e}")
        finally:
            self._volmap_busy = False

    # Big-4 league -> Kalshi GAME series (for settled-event recovery).
    _SETTLED_RECOVERY_SERIES = {'NFL': 'KXNFLGAME', 'NBA': 'KXNBAGAME',
                                'MLB': 'KXMLBGAME', 'NHL': 'KXNHLGAME'}

    async def _enrich_settled_kalshi(self, events):
        """Heatmap-only: a finished game drops out of the Kalshi 'open' fetch, so it
        reaches us PM-only and reads 100% Polymarket. Pull recently-CLOSED/SETTLED
        Kalshi events (bounded by min_close_ts) for the big-4 leagues that have such
        PM-only games, match them with the EXISTING EventMatcher, and attach the
        Kalshi markets to COPIES of those events — the shared list / dropdown is left
        untouched. Best-effort: any failure returns events unchanged.

        Caveat: dollarization uses volume_24h_fp, which decays to 0 once a game closed
        >24h ago; this recovers today's finished slate, not yesterday's."""
        from dataclasses import replace
        pm_only = [e for e in events
                   if getattr(e, 'polymarket_game_id', None)
                   and not getattr(e, 'kalshi_event_ticker', None)
                   and e.sport in self._SETTLED_RECOVERY_SERIES]
        if not pm_only:
            return events
        pm_only_ids = {id(e) for e in pm_only}
        leagues = sorted({e.sport for e in pm_only})
        min_close = int(_time.time()) - 36 * 3600   # match the 36h past-event window
        kc = self.kalshi_client.kalshi_client
        loop = asyncio.get_event_loop()

        def fetch_closed(series):
            out, seen = [], set()
            for st in ('closed', 'settled'):
                cursor = None
                for _ in range(4):
                    try:
                        r = kc.get_events(series_ticker=series, status=st,
                                          with_nested_markets=True,
                                          min_close_ts=min_close, limit=200,
                                          cursor=cursor)
                    except Exception:
                        break
                    for ev in r.get('events', []):
                        et = ev.get('event_ticker', '')
                        if et and et not in seen:
                            seen.add(et)
                            out.append(ev)
                    cursor = r.get('cursor')
                    if not cursor:
                        break
            return out

        try:
            results = await asyncio.gather(*[
                loop.run_in_executor(None, fetch_closed,
                                     self._SETTLED_RECOVERY_SERIES[lg])
                for lg in leagues], return_exceptions=True)
        except Exception as e:
            print(f"[heatmap] settled-event fetch failed: {e}")
            return events
        closed_by_league = {
            lg: (res if not isinstance(res, Exception) else [])
            for lg, res in zip(leagues, results)}

        enriched, used = [], set()
        for e in events:
            if id(e) not in pm_only_ids:
                enriched.append(e)
                continue
            p1, p2 = EventMatcher.parse_polymarket_title(e.polymarket_title or '')
            match = None
            for ke in closed_by_league.get(e.sport, []):
                ket = ke.get('event_ticker', '')
                if ket in used:
                    continue
                ka, kh = EventMatcher.parse_kalshi_title(ke.get('title', ''))
                if (EventMatcher.events_match(ka, kh, p1, p2, e.sport)
                        and EventMatcher.dates_compatible(ket, e.start_time)):
                    match = ke
                    break
            if match:
                used.add(match.get('event_ticker', ''))
                enriched.append(replace(
                    e, kalshi_event_ticker=match.get('event_ticker'),
                    kalshi_series_ticker=match.get('series_ticker'),
                    kalshi_markets=match.get('markets', []) or []))
            else:
                enriched.append(e)
        recovered = sum(1 for a, b in zip(events, enriched) if a is not b)
        if recovered:
            print(f"[heatmap] recovered Kalshi side for {recovered} finished game(s)")
        return enriched

    @staticmethod
    def _series_label(r):
        """Readable league label for a discovered series row (e.g. 'World Cup',
        'NPB', 'WNBA') from its title, falling back to the ticker."""
        t = (r.get('title') or '').replace(' Game', '').replace(' Match', '').strip()
        return t or r.get('game', '').replace('KX', '').replace('GAME', '')

    async def _refresh_series_cache_bg(self):
        """Build/refresh the volume-ranked event-series cache off-thread, then
        reload the menu with the full set. Runs once when no cache exists yet."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self.kalshi_client.kalshi_client.load_ranked_event_series(refresh=True))
            print("✅ Kalshi event-series cache refreshed — reloading menu")
            await self.load_all_sports()
        except Exception as e:
            print(f"⚠️  series cache refresh failed: {e}")

    def _build_kalshi_pm_series_map(self, kalshi_ranked):
        """Map each non-big-4 Kalshi GAME-series ticker -> a Polymarket gamma
        series_id by matching the discovered league titles (token subset). E.g.
        Kalshi 'World Cup' -> PM 'FIFA World Cup' (soccer-fifwc). Empty when no PM
        cache yet."""
        # self.polymarket_client is the PolymarketHistoricalOddsClient wrapper; the
        # real PolymarketSportsClient (with the discovery cache) is its inner
        # .polymarket_client. Reach through, tolerating either shape.
        out = {}
        try:
            pm_client = getattr(self.polymarket_client, 'polymarket_client',
                                self.polymarket_client)
            if not hasattr(pm_client, 'read_cached_event_series'):
                return {}
            pm_ranked = pm_client.read_cached_event_series()
            for kr in kalshi_ranked:
                ktok = {t for t in EventMatcher._generic_normalize(
                    self._series_label(kr)).split() if len(t) >= 3}
                if not ktok:
                    continue
                for pr in pm_ranked:
                    ptok = {t for t in EventMatcher._generic_normalize(
                        pr.get('title', '')).split() if len(t) >= 3}
                    if ptok and (ktok <= ptok or ptok <= ktok):
                        out[kr['game']] = pr.get('series_id')
                        break
        except Exception as e:
            # Mapping is an enhancement — never let it block the menu load.
            print(f"[allsports] PM series map skipped: {e}")
            return {}
        return out

    # Season-long futures series (Kalshi-only): each series resolves to one open
    # event whose markets are the candidates (one market per team/player). Scope:
    # MLB championship + awards. (label is the human title shown in the picker.)
    KALSHI_FUTURES_SERIES = {
        'MLB': [
            ('KXMLB',      'World Series'),
            ('KXMLBAL',    'AL Pennant'),
            ('KXMLBNL',    'NL Pennant'),
            ('KXMLBALMVP', 'AL MVP'),
            ('KXMLBNLMVP', 'NL MVP'),
            ('KXMLBALCY',  'AL Cy Young'),
            ('KXMLBNLCY',  'NL Cy Young'),
        ],
    }

    async def _load_kalshi_futures(self, sport_label):
        """Load season-long futures events for one sport into UnifiedEvents.

        Each configured series -> one open event with N candidate markets (one per
        team/player). Kalshi-only. Markets carry the newer `*_dollars`/`*_fp` price
        schema; the price ranking for display happens at render time, not here."""
        catalog = self.KALSHI_FUTURES_SERIES.get(sport_label, [])
        if not catalog or not self.kalshi_client:
            return []
        loop = asyncio.get_event_loop()

        def fetch_one(series_ticker):
            # with_nested_markets so the candidate markets come back in the event.
            return self.kalshi_client.kalshi_client.get_events(
                series_ticker=series_ticker, limit=50, status='open',
                with_nested_markets=True)

        out = []
        for series_ticker, label in catalog:
            try:
                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, fetch_one, series_ticker), timeout=15)
            except Exception as e:
                print(f"  ⚠️  futures {series_ticker}: {e}")
                continue
            for k_event in resp.get('events', []):
                markets = k_event.get('markets', []) or []
                if not markets:
                    continue
                et = k_event.get('event_ticker', '')
                # Year suffix (…-26) -> a 'season' label so multiple years stay distinct.
                yr = et.rsplit('-', 1)[-1] if '-' in et else ''
                disp = f"{label}" + (f" '{yr}" if yr.isdigit() and len(yr) == 2 else '')
                out.append(UnifiedEvent(
                    sport=sport_label, home_team=label, away_team='',
                    start_time=None,  # season-long; no game time -> past-filter keeps it
                    kalshi_event_ticker=et,
                    kalshi_series_ticker=k_event.get('series_ticker') or series_ticker,
                    kalshi_markets=markets,
                    is_future=True, future_label=disp))
        if out:
            print(f"  🏆 {sport_label} futures: {len(out)} market(s)")
        return out

    async def _load_kalshi_only_events_for_series(self, sport_label, game_ticker,
                                                  pm_series_id=None):
        """Load open Kalshi events for one GAME series and build UnifiedEvents.
        When pm_series_id is given, also fetch that PM series' games and MERGE via
        the generic (non-big-4) matcher; otherwise Kalshi-only. Companion
        spread/total markets are fetched lazily on event open."""
        loop = asyncio.get_event_loop()

        def fetch_k():
            cursor, events = None, []
            while True:
                resp = self.kalshi_client.kalshi_client.get_events(
                    series_ticker=game_ticker, limit=200, cursor=cursor, status='open')
                events.extend(resp.get('events', []))
                cursor = resp.get('cursor')
                if not cursor:
                    break
            return events

        try:
            if pm_series_id:
                kalshi_events, pm_games = await asyncio.gather(
                    loop.run_in_executor(None, fetch_k),
                    self.polymarket_client.get_sport_games(series_id=pm_series_id))
            else:
                kalshi_events = await loop.run_in_executor(None, fetch_k)
                pm_games = []
        except Exception as e:
            print(f"  ⚠️  {game_ticker}: {e}")
            return []

        active_pm = [g for g in pm_games
                     if getattr(g, 'active', True) and not getattr(g, 'closed', False)]
        out, matched = [], set()
        for k_event in kalshi_events:
            k_away, k_home = EventMatcher.parse_kalshi_title(k_event.get('title', ''))
            et = k_event.get('event_ticker', '')
            # ALL team+time-matching PM events for this matchup. Polymarket splits a
            # single game into SEPARATE events (the 'Will X win?' moneyline, the
            # halftime markets, the exact-score markets) — all same teams/time. We
            # must aggregate their markets so the win markets (needed for the 3-way
            # moneyline + live feed) are present regardless of which event is the
            # closest-time one. Closest-time is the PRIMARY (for id/title/start).
            related, primary, best = [], None, float('inf')
            for g in active_pm:
                if g.id in matched:
                    continue
                p1, p2 = EventMatcher.parse_polymarket_title(g.title)
                if (EventMatcher.events_match(k_away, k_home, p1, p2, sport_label)
                        and EventMatcher.dates_compatible(et, g.start_time)):
                    related.append(g)
                    d = EventMatcher.start_time_delta_hours(et, g.start_time)
                    if d < best or primary is None:
                        primary, best = g, d
            # Prefer the FULL datetime from the ticker (KX…26JUN260530… -> 05:30)
            # so the chart frames + Event Start line work; fall back to date-only.
            _dt = EventMatcher.parse_kalshi_event_datetime(et)
            k_start = _dt.isoformat() if _dt else EventMatcher.parse_kalshi_event_date(et)
            ue = UnifiedEvent(
                sport=sport_label, home_team=k_home, away_team=k_away,
                start_time=k_start,
                kalshi_event_ticker=et,
                kalshi_series_ticker=k_event.get('series_ticker'),
                kalshi_markets=k_event.get('markets', []))
            if primary:
                agg_markets = []
                for g in related:
                    matched.add(g.id)
                    agg_markets.extend(g.markets or [])
                ue.polymarket_game_id = primary.id
                ue.polymarket_title = primary.title
                ue.polymarket_markets = agg_markets
                if primary.start_time:
                    ue.start_time = primary.start_time
            out.append(ue)
        # Second pass: unmatched (non-stale) PM games as PM-only rows.
        for g in active_pm:
            if g.id in matched or EventMatcher.poly_game_is_stale(g.start_time):
                continue
            p1, p2 = EventMatcher.parse_polymarket_title(g.title)
            out.append(UnifiedEvent(
                sport=sport_label, home_team=p2, away_team=p1,
                start_time=g.start_time, polymarket_game_id=g.id,
                polymarket_title=g.title, polymarket_markets=g.markets))
        return out

    async def _load_unified_events_for_sport(self, sport):
        """
        Load and merge events from both Kalshi and Polymarket for a single sport.
        Returns list of UnifiedEvent objects.

        Args:
            sport: Sport key ('NFL', 'NBA', 'MLB', 'NHL')
        """
        # Map sport to the MONEYLINE series (base games only)
        KALSHI_MONEYLINE_SERIES = {
            'NFL': 'KXNFLGAME',
            'NBA': 'KXNBAGAME',
            'MLB': 'KXMLBGAME',
            'NHL': 'KXNHLGAME',
            'NCAAF': 'KXNCAAFGAME',
        }

        series_ticker = KALSHI_MONEYLINE_SERIES.get(sport)
        if not series_ticker:
            print(f"⚠️  No Kalshi moneyline series found for {sport}")
            return []

        loop = asyncio.get_event_loop()

        # Fetch from both sources concurrently
        print(f"  Fetching {sport} from Kalshi and Polymarket in parallel...")

        # Fetch Kalshi moneyline events only (base games)
        # Filter for only active/open events
        def fetch_kalshi_moneylines():
            cursor = None
            events = []
            while True:
                response = self.kalshi_client.kalshi_client.get_events(
                    series_ticker=series_ticker,
                    limit=200,
                    cursor=cursor,
                    status='open'  # Only get open (active) events
                )
                events.extend(response.get('events', []))
                cursor = response.get('cursor')
                if not cursor:
                    break
            return {'events': events, 'count': len(events)}

        import time as _tp_sync, sys as _sp_sync
        _kt0 = _tp_sync.perf_counter()
        kalshi_data, polymarket_games = await asyncio.gather(
            loop.run_in_executor(None, fetch_kalshi_moneylines),
            self.polymarket_client.get_sport_games(sport)
        )
        if PERF_DIAG:
            print(f"[task-probe] {sport} concurrent_await={(_tp_sync.perf_counter()-_kt0)*1000:.0f}ms",
                  file=_sp_sync.stderr)
        _sync_t0 = _tp_sync.perf_counter()

        kalshi_events = kalshi_data.get('events', [])

        # Filter Polymarket games to only include active (not closed) games
        active_polymarket_games = [g for g in polymarket_games if g.active and not g.closed]

        print(f"  ✅ Kalshi: {len(kalshi_events)} open events")
        print(f"  ✅ Polymarket: {len(active_polymarket_games)} active games (filtered from {len(polymarket_games)} total)")

        # Create unified events by matching
        unified_events = []
        matched_poly_ids = set()

        # First pass: match Kalshi events with Polymarket
        for k_event in kalshi_events:
            k_title = k_event.get('title', '')
            k_away, k_home = EventMatcher.parse_kalshi_title(k_title)
            event_ticker = k_event.get('event_ticker', '')

            # Try to find matching Polymarket game. Among all team-matching
            # candidates take the CLOSEST start time, not the first hit:
            # PM lists every game of a series/doubleheader separately under
            # identical team names, and first-hit could bind this Kalshi
            # event to a different day's game.
            matched_poly_game = None
            best_delta = float('inf')
            for p_game in active_polymarket_games:
                if p_game.id in matched_poly_ids:
                    continue

                p_title = p_game.title
                p_away, p_home = EventMatcher.parse_polymarket_title(p_title)

                # Match on teams AND start time. The date guard prevents pairing
                # the live game's Polymarket market with a different day's
                # (untraded) Kalshi event for the same matchup.
                if (EventMatcher.events_match(k_away, k_home, p_away, p_home, sport)
                        and EventMatcher.dates_compatible(event_ticker, p_game.start_time)):
                    delta = EventMatcher.start_time_delta_hours(
                        event_ticker, p_game.start_time)
                    if delta < best_delta or matched_poly_game is None:
                        matched_poly_game = p_game
                        best_delta = delta

            if matched_poly_game:
                matched_poly_ids.add(matched_poly_game.id)

            # Create unified event
            # Parse start_time from event_ticker since strike_date doesn't exist
            start_time = EventMatcher.parse_kalshi_event_date(event_ticker)

            unified_event = UnifiedEvent(
                sport=sport,
                home_team=k_home,
                away_team=k_away,
                start_time=start_time,
                kalshi_event_ticker=event_ticker,
                kalshi_series_ticker=k_event.get('series_ticker'),
                kalshi_markets=k_event.get('markets', [])
            )

            if matched_poly_game:
                unified_event.polymarket_game_id = matched_poly_game.id
                unified_event.polymarket_title = matched_poly_game.title
                unified_event.polymarket_markets = matched_poly_game.markets
                # Prefer Polymarket's start_time as it includes actual time of day
                # Kalshi's parsed date from ticker only has date (set to midnight UTC)
                if matched_poly_game.start_time:
                    unified_event.start_time = matched_poly_game.start_time

            unified_events.append(unified_event)

        # Second pass: add unmatched Polymarket games. Skip ones that
        # started >8h ago — gamma keeps settled games "active" for a
        # while, and with no open Kalshi side anchoring them they'd show
        # as stale P-only rows for yesterday's games.
        for p_game in active_polymarket_games:
            if p_game.id not in matched_poly_ids:
                if EventMatcher.poly_game_is_stale(p_game.start_time):
                    continue
                p_away, p_home = EventMatcher.parse_polymarket_title(p_game.title)
                unified_event = UnifiedEvent(
                    sport=sport,
                    home_team=p_home,
                    away_team=p_away,
                    start_time=p_game.start_time,
                    polymarket_game_id=p_game.id,
                    polymarket_title=p_game.title,
                    polymarket_markets=p_game.markets
                )
                unified_events.append(unified_event)

        # Sort events by start_time (chronological order)
        # Events without start_time go to the end
        unified_events.sort(key=lambda e: e.start_time if e.start_time else 'Z' * 30)

        if PERF_DIAG:
            print(f"[sportmatch-probe] {sport} sync match/build="
                  f"{(_tp_sync.perf_counter()-_sync_t0)*1000:.0f}ms "
                  f"(k={len(kalshi_events)} p={len(active_polymarket_games)})",
                  file=_sp_sync.stderr)
        return unified_events

    async def load_unified_events(self, sport= None):
        """
        Load and merge events from both Kalshi and Polymarket.

        Args:
            sport: Sport key ('NFL', 'NBA', 'MLB', 'NHL')
        """
        print(f"\n{'='*80}")
        print(f"Loading unified events for {sport}")
        print(f"{'='*80}\n")

        # Map sport to the MONEYLINE series (base games only)
        KALSHI_MONEYLINE_SERIES = {
            'NFL': 'KXNFLGAME',
            'NBA': 'KXNBAGAME',
            'MLB': 'KXMLBGAME',
            'NHL': 'KXNHLGAME',
            'NCAAF': 'KXNCAAFGAME',
        }

        series_ticker = KALSHI_MONEYLINE_SERIES.get(sport)
        if not series_ticker:
            print(f"⚠️  No Kalshi moneyline series found for {sport}")
            return

        loop = asyncio.get_event_loop()

        # Fetch from both sources concurrently
        print("Fetching from Kalshi and Polymarket in parallel...")

        # Fetch Kalshi moneyline events only (base games)
        # Filter for only active/open events
        def fetch_kalshi_moneylines():
            cursor = None
            events = []
            while True:
                response = self.kalshi_client.kalshi_client.get_events(
                    series_ticker=series_ticker,
                    limit=200,
                    cursor=cursor,
                    status='open'  # Only get open (active) events
                )
                events.extend(response.get('events', []))
                cursor = response.get('cursor')
                if not cursor:
                    break
            return {'events': events, 'count': len(events)}

        kalshi_task = loop.run_in_executor(None, fetch_kalshi_moneylines)
        polymarket_task = self.polymarket_client.get_sport_games(sport)

        kalshi_data, polymarket_games = await asyncio.gather(kalshi_task, polymarket_task)

        kalshi_events = kalshi_data.get('events', [])

        # Filter Polymarket games to only include active (not closed) games
        active_polymarket_games = [g for g in polymarket_games if g.active and not g.closed]

        print(f"✅ Kalshi: {len(kalshi_events)} open events")
        print(f"✅ Polymarket: {len(active_polymarket_games)} active games (filtered from {len(polymarket_games)} total)")

        # Create unified events by matching
        unified_events = []
        matched_poly_ids = set()

        # First pass: match Kalshi events with Polymarket
        for k_event in kalshi_events:
            k_title = k_event.get('title', '')
            k_away, k_home = EventMatcher.parse_kalshi_title(k_title)
            event_ticker = k_event.get('event_ticker', '')

            # Try to find matching Polymarket game. Among all team-matching
            # candidates take the CLOSEST start time, not the first hit
            # (PM lists each series/doubleheader game separately under
            # identical team names).
            matched_poly_game = None
            best_delta = float('inf')
            for p_game in active_polymarket_games:
                if p_game.id in matched_poly_ids:
                    continue

                p_title = p_game.title
                p_away, p_home = EventMatcher.parse_polymarket_title(p_title)

                # Match on teams AND start time. The date guard prevents pairing
                # the live game's Polymarket market with a different day's
                # (untraded) Kalshi event for the same matchup.
                if (EventMatcher.events_match(k_away, k_home, p_away, p_home)
                        and EventMatcher.dates_compatible(event_ticker, p_game.start_time)):
                    delta = EventMatcher.start_time_delta_hours(
                        event_ticker, p_game.start_time)
                    if delta < best_delta or matched_poly_game is None:
                        matched_poly_game = p_game
                        best_delta = delta

            if matched_poly_game:
                matched_poly_ids.add(matched_poly_game.id)

            # Create unified event
            # Parse start_time from event_ticker since strike_date doesn't exist
            start_time = EventMatcher.parse_kalshi_event_date(event_ticker)

            unified_event = UnifiedEvent(
                sport=sport,
                home_team=k_home,
                away_team=k_away,
                start_time=start_time,
                kalshi_event_ticker=event_ticker,
                kalshi_series_ticker=k_event.get('series_ticker'),
                kalshi_markets=k_event.get('markets', [])
            )

            if matched_poly_game:
                unified_event.polymarket_game_id = matched_poly_game.id
                unified_event.polymarket_title = matched_poly_game.title
                unified_event.polymarket_markets = matched_poly_game.markets
                # Only use Polymarket's start_time as fallback if Kalshi doesn't have one
                # Kalshi's parsed date from ticker is more accurate for game date
                if not unified_event.start_time and matched_poly_game.start_time:
                    unified_event.start_time = matched_poly_game.start_time
                print(f"  [K+P] Matched: {k_home} vs {k_away}")
            else:
                print(f"  [K  ] No Polymarket match: {k_home} vs {k_away}")

            unified_events.append(unified_event)

        # Second pass: add unmatched Polymarket games. Skip ones that
        # started >8h ago — gamma keeps settled games "active" for a
        # while, and with no open Kalshi side anchoring them they'd show
        # as stale P-only rows for yesterday's games.
        for p_game in active_polymarket_games:
            if p_game.id not in matched_poly_ids:
                if EventMatcher.poly_game_is_stale(p_game.start_time):
                    continue
                p_away, p_home = EventMatcher.parse_polymarket_title(p_game.title)
                unified_event = UnifiedEvent(
                    sport=sport,
                    home_team=p_home,
                    away_team=p_away,
                    start_time=p_game.start_time,
                    polymarket_game_id=p_game.id,
                    polymarket_title=p_game.title,
                    polymarket_markets=p_game.markets
                )
                unified_events.append(unified_event)
                print(f"  [  P] Polymarket only: {p_home} vs {p_away}")

        # Sort events by start_time (chronological order)
        # Events without start_time go to the end
        unified_events.sort(key=lambda e: e.start_time if e.start_time else 'Z' * 30)

        print(f"\n📊 Total unified events: {len(unified_events)}")

        # Filter out past events if configured to do so
        SHOW_PAST_EVENTS = True  # Set to True to show all events including past ones
        PAST_EVENT_CUTOFF_HOURS = 12  # Keep events from last N hours (to include live/recent games)

        if not SHOW_PAST_EVENTS:
            from datetime import datetime, timezone, timedelta

            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=PAST_EVENT_CUTOFF_HOURS)

            original_count = len(unified_events)
            filtered_events = []

            for event in unified_events:
                if event.start_time:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event.start_time.replace('Z', '+00:00'))

                        # Keep event if it's in the future or within the cutoff window
                        if event_dt >= cutoff:
                            filtered_events.append(event)
                    except Exception as e:
                        # If we can't parse the date, include it to be safe
                        print(f"⚠️  Could not parse date for {event.away_team} @ {event.home_team}: {event.start_time}")
                        filtered_events.append(event)
                else:
                    # If no start_time, include it to be safe
                    filtered_events.append(event)

            unified_events = filtered_events
            filtered_count = original_count - len(unified_events)

            if filtered_count > 0:
                print(f"🗑️  Filtered out {filtered_count} past events (older than {PAST_EVENT_CUTOFF_HOURS} hours)")
            print(f"📊 Showing {len(unified_events)} events")

        # Populate event selector
        self.event_selector.blockSignals(True)
        self.event_selector.clear()

        for event in unified_events:
            display_title = event.get_display_title()
            self.event_selector.addItem(display_title, userData=event)

        self.event_selector.blockSignals(False)

        # Auto-select first event
        if unified_events:
            await self.on_event_changed()

    async def load_kalshi_events(self, sport=None):
        """
        Load and populate Kalshi events for all sports or a specific sport.

        Args:
            sport: Sport key ('NFL', 'NBA', 'MLB', 'NHL', etc.) - if None, loads all sports
        """
        # All available Kalshi sports including moneylines, spreads, totals
        # Note: Props are loaded separately when an event is selected (they have different ticker formats)
        all_sports = [
            # NFL
            'NFL', 'KXNFLSPREAD', 'KXNFLTOTAL',
            # NBA
            'NBA', 'KXNBASPREAD', 'KXNBATOTAL',
            # MLB
            'MLB', 'KXMLBSPREAD', 'KXMLBTOTAL', 'MLB_SERIES',
            # NHL
            'NHL', 'KXNHLSPREAD', 'KXNHLTOTAL',
            # College Football
            'NCAAF',
            # Soccer
            'EPL', 'UCL', 'LA_LIGA', 'BUNDESLIGA', 'SERIE_A', 'LIGUE_1', 'MLS',
            # Esports
            'LOL'
        ]

        sports_to_load = [sport] if sport else all_sports

        print(f"Loading Kalshi events for: {', '.join(sports_to_load)}...")

        try:
            loop = asyncio.get_event_loop()

            # Calculate time thresholds for filtering events
            # NFL: 1 week ahead, Others: 2 days ahead
            now_ts = int(datetime.now().timestamp())

            # Determine the appropriate time threshold based on sport
            def get_max_close_ts(sport_key: str) -> int:
                """Calculate max close timestamp for event filtering."""
                # Extract base sport from series ticker
                base_sport = sport_key.split('KX')[-1][:3] if sport_key.startswith('KX') else sport_key[:3]

                if 'NFL' in sport_key.upper() or sport_key == 'NFL':
                    # NFL: 1 week ahead
                    return now_ts + (7 * 24 * 60 * 60)
                else:
                    # All other sports: 2 days ahead
                    return now_ts + (2 * 24 * 60 * 60)

            # Fetch events for all sports concurrently
            fetch_tasks = []
            for sport_key in sports_to_load:
                # Check if this is a direct series ticker (SPREAD/TOTAL) or a game series
                if sport_key.startswith('KX'):
                    # Direct series ticker - use get_events
                    task = loop.run_in_executor(
                        None,
                        lambda s=sport_key: self.kalshi_client.kalshi_client.get_events(
                            series_ticker=s,
                            status='open',
                            with_nested_markets=True,
                            limit=200,
                            min_close_ts=now_ts  # Filter out already-closed events
                        )
                    )
                else:
                    # Game series - use get_game_events
                    task = loop.run_in_executor(
                        None,
                        lambda s=sport_key: self.kalshi_client.kalshi_client.get_game_events(
                            sport=s,
                            min_close_ts=now_ts  # Filter out already-closed events
                        )
                    )
                fetch_tasks.append((sport_key, task))

            # Wait for all tasks to complete
            all_events = []
            for sport_key, task in fetch_tasks:
                try:
                    events_data = await task
                    events = events_data.get('events', [])

                    # Filter events by close time based on sport
                    max_close_ts = get_max_close_ts(sport_key)
                    filtered_events = []
                    for event in events:
                        # Check if any market in the event closes within our time window
                        markets = event.get('markets', [])
                        if markets:
                            # Get the earliest close time among all markets
                            close_times = [m.get('close_time') for m in markets if m.get('close_time')]
                            if close_times:
                                # Parse ISO datetime strings to timestamps
                                earliest_close_ts = min([
                                    int(datetime.fromisoformat(ct.replace('Z', '+00:00')).timestamp())
                                    for ct in close_times
                                ])
                                # Include event if it closes within our time window
                                if earliest_close_ts <= max_close_ts:
                                    filtered_events.append(event)
                        else:
                            # No markets, include anyway (will be filtered later if no data)
                            filtered_events.append(event)

                    print(f"  {sport_key}: {len(filtered_events)} events (filtered from {len(events)})")
                    all_events.extend(filtered_events)
                except Exception as e:
                    print(f"  {sport_key}: Error - {e}")
                    continue

            print(f"Total events loaded: {len(all_events)}")

            # Group events by base game (remove market type suffix like ": Spread", ": Total Points")
            # This prevents duplicate entries for the same game
            unique_events = {}
            for event in all_events:
                event_title = event.get('title', 'Unknown Event')
                event_ticker = event.get('event_ticker')
                series_ticker = event.get('series_ticker')

                # Extract base game title (remove ": Spread", ": Total Points", etc.)
                base_title = event_title.split(':')[0].strip()

                # Use base title as key to group related events
                if base_title not in unique_events:
                    unique_events[base_title] = {
                        'title': base_title,
                        'event_ticker': event_ticker,
                        'series_ticker': series_ticker,
                        'sort_key': event_ticker
                    }

            # Convert to list and sort
            unique_event_list = list(unique_events.values())
            unique_event_list.sort(key=lambda x: x.get('sort_key', ''), reverse=True)

            print(f"Unique events after grouping: {len(unique_event_list)}")

            # Populate event selector
            self.event_selector.blockSignals(True)
            self.event_selector.clear()

            for event in unique_event_list:
                event_title = event['title']
                event_ticker = event['event_ticker']
                series_ticker = event['series_ticker']

                # Extract sport from series ticker for prefix
                # KXNFLSPREAD -> NFL, KXNBATOTAL -> NBA, etc.
                sport_prefix = ''
                if series_ticker:
                    if 'NFL' in series_ticker:
                        sport_prefix = 'NFL'
                    elif 'NBA' in series_ticker:
                        sport_prefix = 'NBA'
                    elif 'MLB' in series_ticker:
                        sport_prefix = 'MLB'
                    elif 'NHL' in series_ticker:
                        sport_prefix = 'NHL'
                    elif 'NCAAF' in series_ticker:
                        sport_prefix = 'NCAAF'
                    else:
                        sport_prefix = series_ticker.replace('KX', '').replace('GAME', '')[:6]

                if sport_prefix:
                    display_title = f"[{sport_prefix}] {event_title}"
                else:
                    display_title = event_title

                # Store event info as user data
                self.event_selector.addItem(display_title, userData=(event_ticker, series_ticker, event_title))

            self.event_selector.blockSignals(False)

            # Auto-select first event if available
            if all_events:
                await self.on_event_changed()

        except Exception as e:
            print(f"Error loading Kalshi events: {e}")
            import traceback
            traceback.print_exc()

    @staticmethod
    def _future_market_price(m):
        """Implied price (0..1) of a futures candidate market, for ranking. Prefers
        last trade, then mid of bid/ask, then bid. Returns 0.0 when untraded."""
        def f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        last = f(m.get('last_price_dollars'))
        if last is not None:
            return last
        bid, ask = f(m.get('yes_bid_dollars')), f(m.get('yes_ask_dollars'))
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else 0.0

    def _futures_candidates_ranked(self):
        """Active futures market's candidate markets, sorted by implied price desc.
        Each entry: the raw Kalshi market dict (has ticker + yes_sub_title + prices)."""
        um = getattr(self, 'current_unified_market', None)
        mks = (um.kalshi_markets if um else None) or []
        return sorted(mks, key=self._future_market_price, reverse=True)

    async def _load_futures_markets(self, unified_event: UnifiedEvent):
        """Build the single N-candidate futures market and enter live view.

        The futures event already carries every candidate market (one per team/
        player). We wrap them in ONE UnifiedMarket so the existing outcome/side
        machinery enumerates the candidates; futures-specific rendering (top-N chart,
        top-few order books, Side-combo focus) is gated by self._is_future_market."""
        # Remember the feed choice we're leaving (once, on the way INTO futures) so a
        # game opened afterward isn't stuck on Kalshi-only — futures forces 'kalshi'
        # below since they don't trade on Polymarket (yet). Restored in the game path.
        if getattr(self, '_pre_future_live_source', None) is None:
            self._pre_future_live_source = self.live_source
        self._is_future_market = True
        self.current_unified_event = unified_event
        markets = unified_event.kalshi_markets or []
        ranked = sorted(markets, key=self._future_market_price, reverse=True)
        # kalshi_markets keeps the FULL field (side combo lists every candidate), but
        # kalshi_tickers/titles — which drive the historical-candle seed — are capped
        # to the charted top-N so we don't fire 30 sequential candle fetches. Off-top
        # candidates picked via the Side combo still stream live (just no seed).
        top = ranked[:self._FUT_CHART_N]
        um = UnifiedMarket(
            market_type='future',
            display_name=unified_event.future_label or 'Futures',
            kalshi_markets=markets,
            kalshi_tickers=[m.get('ticker') for m in top if m.get('ticker')],
            kalshi_titles=[m.get('yes_sub_title') or m.get('title') for m in top],
            kalshi_event_ticker=unified_event.kalshi_event_ticker)

        # Market selector holds a single entry: the award itself. Selecting it drives
        # on_market_changed -> live view, exactly like a game's moneyline market.
        self.market_selector.blockSignals(True)
        self.market_selector.clear()
        n = len(markets)
        self.market_selector.addItem(
            f"{unified_event.future_label} · {n} candidates", userData=um)
        self.market_selector.blockSignals(False)
        self.market_selector.setEnabled(True)
        # Trigger selection (index 0) -> on_market_changed sets up the live series.
        self.market_selector.setCurrentIndex(0)
        await self.on_market_changed()

    async def load_markets_for_unified_event(self, unified_event: UnifiedEvent):
        """
        Load markets from both Kalshi and Polymarket for a unified event.

        Args:
            unified_event: UnifiedEvent containing data from both sources
        """
        # Futures: dedicated, much simpler path (N-candidate single market).
        if getattr(unified_event, 'is_future', False):
            await self._load_futures_markets(unified_event)
            return
        # Normal head-to-head game from here on.
        self._is_future_market = False
        # Returning from a futures view: restore the feed source futures overrode, so
        # a K+P game isn't left stuck on Kalshi-only. Validity is re-checked downstream.
        if getattr(self, '_pre_future_live_source', None) is not None:
            self.live_source = self._pre_future_live_source
            self._pre_future_live_source = None
        print(f"\nLoading markets for unified event: {unified_event.away_team} @ {unified_event.home_team}")

        # Block signals to prevent triggering during population
        self.market_selector.blockSignals(True)
        self.market_selector.clear()

        # Store the current unified event
        self.current_unified_event = unified_event

        # Collect all markets from both sources
        all_markets = []

        # === KALSHI MARKETS ===
        if unified_event.has_kalshi():
            print(f"  Loading Kalshi markets...")

            # Check if markets are already loaded, if not fetch them
            kalshi_markets = unified_event.kalshi_markets or []

            if not kalshi_markets:
                # Markets not loaded yet - fetch them from all related series
                print(f"    Fetching markets from Kalshi API...")
                ticker_parts = unified_event.kalshi_event_ticker.split('-')
                if len(ticker_parts) >= 2:
                    ticker_id = '-'.join(ticker_parts[1:])
                else:
                    ticker_id = unified_event.kalshi_event_ticker

                # Determine sport and construct series tickers to check
                sport = unified_event.sport
                series_to_check = []

                if sport == 'NFL':
                    series_to_check = [
                        # Game lines
                        'KXNFLGAME',           # Moneylines
                        'KXNFLSPREAD',         # Spreads
                        'KXNFLTOTAL',          # Totals
                        'KXNFLTEAMTOTAL',      # Team Totals
                        # Player props
                        'KXNFLRECYDS',         # Receiving Yards
                        'KXNFLRSHYDS',         # Rushing Yards
                        'KXNFLPASSYDS',        # Passing Yards
                        'KXNFLFIRSTTD',        # First TD scorer
                        'KXNFLANYTD',          # Anytime TD scorer
                        'KXNFL2TD',            # Multiple TDs
                        'KXNFLREC',            # Receptions
                        # Note: KXMVENFLSINGLEGAME contains only user-created parlays, not individual props
                    ]
                elif sport == 'NBA':
                    series_to_check = [
                        # Game lines
                        'KXNBAGAME',           # Moneylines
                        'KXNBASPREAD',         # Spreads
                        'KXNBATOTAL',          # Totals
                        # Player props
                        'KXNBAPTS',            # Player Points
                        'KXNBAAST',            # Player Assists
                        'KXNBAREB',            # Player Rebounds
                        'KXNBA3PT',            # Player 3-Pointers
                    ]
                elif sport == 'MLB':
                    series_to_check = [
                        # Game lines
                        'KXMLBGAME',           # Moneylines
                        'KXMLBSPREAD',         # Run line (spread)
                        'KXMLBTOTAL',          # Totals

                    ]
                elif sport == 'NHL':
                    series_to_check = [
                        # Game lines
                        'KXNHLGAME',           # Moneylines
                        'KXNHLSPREAD',         # Puck line (spread)
                        'KXNHLTOTAL',          # Goal Total
                        # Player props
                        'KXNHLGOAL',           # Player Goal (specific count)
                        'KXNHLANYGOAL',        # Anytime Goalscorer
                        'KXNHLFIRSTGOAL',      # First Goal
                        'KXNHLSAVES',          # Goalie Saves
                    ]
                else:
                    # Generic (any expanded sport): derive the companion market
                    # series from this event's GAME series — KX<LEAGUE>GAME plus
                    # KX<LEAGUE>{SPREAD,TOTAL,TEAMTOTAL}. Non-existent ones simply
                    # return no markets, so over-listing is harmless.
                    game_series = (unified_event.kalshi_series_ticker
                                   or unified_event.kalshi_event_ticker.split('-')[0])
                    if game_series.endswith('GAME'):
                        base = game_series[:-4]
                        series_to_check = [base + 'GAME', base + 'SPREAD',
                                           base + 'TOTAL', base + 'TEAMTOTAL']
                    else:
                        series_to_check = [game_series]

                # Fetch markets from all related series
                loop = asyncio.get_event_loop()
                for series in series_to_check:
                    if series.startswith('KXMVE'):
                        # Props series - fetch by team names
                        try:
                            props_markets = await self._fetch_props_markets_for_teams(
                                series, unified_event.home_team, unified_event.away_team
                            )
                            if props_markets:
                                kalshi_markets.extend(props_markets)
                                print(f"      {series}: {len(props_markets)} markets")
                        except Exception as e:
                            pass  # No props available
                    else:
                        # Standard series - use ticker pattern
                        try:
                            markets = await loop.run_in_executor(
                                None,
                                lambda s=series, t=ticker_id: self._fetch_event_markets_sync(s, t)
                            )
                            if markets:
                                kalshi_markets.extend(markets)
                                print(f"      {series}: {len(markets)} markets")
                        except Exception as e:
                            pass  # Series doesn't exist

                # Cache the fetched markets in the unified event
                unified_event.kalshi_markets = kalshi_markets

        # Get Polymarket markets
        poly_markets = []
        if unified_event.has_polymarket():
            print(f"  Loading Polymarket markets...")
            poly_markets = unified_event.polymarket_markets or []
            print(f"  Found {len(poly_markets)} Polymarket markets:")
            for pm in poly_markets:
                pm_type = MarketMatcher.get_market_type(poly_question=pm.question)
                print(f"    - {pm.question} (type: {pm_type})")

        # === MATCH MARKETS ACROSS SOURCES ===
        unified_markets = []
        matched_poly_indices = set()
        matched_kalshi_indices = set()

        # Special handling for moneyline: Group both Kalshi moneyline markets together
        # Kalshi has 2 markets (one per team), Polymarket has 1 market (with 2 outcomes)
        moneyline_markets_kalshi = []
        if unified_event.has_kalshi():
            for idx, k_market in enumerate(kalshi_markets):
                k_type = MarketMatcher.get_market_type(kalshi_title=k_market.get('title', ''))
                if k_type == 'moneyline':
                    moneyline_markets_kalshi.append((idx, k_market))

        # Create unified moneyline market if we have Kalshi moneylines
        if moneyline_markets_kalshi:
            # Find matching Polymarket moneyline
            poly_moneyline = None
            poly_moneyline_idx = None
            for p_idx, p_market in enumerate(poly_markets):
                if MarketMatcher.get_market_type(poly_question=p_market.question) == 'moneyline':
                    poly_moneyline = p_market
                    poly_moneyline_idx = p_idx
                    matched_poly_indices.add(p_idx)
                    break

            # Extract all Kalshi moneyline markets
            k_markets_list = [m[1] for m in moneyline_markets_kalshi]
            k_tickers_list = [m[1].get('ticker') for m in moneyline_markets_kalshi]
            k_titles_list = [m[1].get('title', '') for m in moneyline_markets_kalshi]

            # Mark these Kalshi markets as matched
            for idx, _ in moneyline_markets_kalshi:
                matched_kalshi_indices.add(idx)

            # Get event ticker from first market
            first_ticker = k_tickers_list[0]
            if first_ticker and '-' in first_ticker:
                k_event_ticker = first_ticker.rsplit('-', 1)[0]
            else:
                k_event_ticker = first_ticker or unified_event.kalshi_event_ticker

            # Create unified moneyline market
            display_name = poly_moneyline.question if poly_moneyline else k_titles_list[0]

            unified_moneyline = UnifiedMarket(
                market_type='moneyline',
                display_name=display_name,
                kalshi_markets=k_markets_list,
                kalshi_tickers=k_tickers_list,
                kalshi_titles=k_titles_list,
                kalshi_event_ticker=k_event_ticker,
                polymarket_market=poly_moneyline,
                polymarket_id=poly_moneyline.id if poly_moneyline else None,
                polymarket_question=poly_moneyline.question if poly_moneyline else None,
                polymarket_game_id=unified_event.polymarket_game_id if poly_moneyline else None
            )
            unified_markets.append(unified_moneyline)
            print(f"    [MONEYLINE] Combined {len(k_markets_list)} Kalshi markets{' + Polymarket' if poly_moneyline else ''}")

        # Now process non-moneyline markets individually
        # For spreads, each Kalshi market is independent (not grouped like moneylines)
        # because Kalshi spread markets are binary: YES = team covers, NO = team doesn't cover
        # First pass: Match Kalshi markets with Polymarket
        if unified_event.has_kalshi():
            for k_idx, k_market in enumerate(kalshi_markets):
                # Skip if already matched (moneylines)
                if k_idx in matched_kalshi_indices:
                    continue
                k_title = k_market.get('title', '')
                k_ticker = k_market.get('ticker')

                # CRITICAL FIX: Extract event ticker from the market ticker
                # Market ticker format: KXNFLSPREAD-25NOV23TBLA-TB6
                # Event ticker should be: KXNFLSPREAD-25NOV23TBLA (everything before last dash)
                if k_ticker and '-' in k_ticker:
                    ticker_parts = k_ticker.rsplit('-', 1)  # Split from right, only once
                    k_event_ticker = ticker_parts[0]  # Everything except the market suffix
                else:
                    k_event_ticker = k_ticker or unified_event.kalshi_event_ticker

                # Try to find matching Polymarket market
                matched_p_market = None
                matched_p_idx = None

                k_type = MarketMatcher.get_market_type(kalshi_title=k_title)
                print(f"    Kalshi: {k_title[:60]} (type: {k_type}, ticker: {k_ticker})")

                for p_idx, p_market in enumerate(poly_markets):
                    if p_idx in matched_poly_indices:
                        continue

                    p_type = MarketMatcher.get_market_type(poly_question=p_market.question)

                    # Use MarketMatcher to check if these represent the same bet
                    matches = MarketMatcher.markets_match(
                        k_market, p_market,
                        unified_event.home_team,
                        unified_event.away_team,
                        unified_event.sport
                    )

                    if matches:
                        matched_p_market = p_market
                        matched_p_idx = p_idx
                        matched_poly_indices.add(p_idx)
                        print(f"      ✅ MATCHED with Polymarket: {p_market.question[:60]}")
                        break
                    else:
                        # Debug: show why it didn't match
                        if k_type == p_type and p_idx < 3:  # Only show first few attempts
                            print(f"        ❌ No match with: {p_market.question[:50]} (same type: {k_type})")

                # Determine market type and display name
                market_type = k_type

                if matched_p_market:
                    # Both sources available - use Polymarket's more descriptive name if available
                    if matched_p_market.question and len(matched_p_market.question) > len(k_title):
                        display_name = matched_p_market.question
                    else:
                        # For Kalshi totals, extract the line value from ticker and add to display
                        if market_type == 'total' and k_ticker:
                            total_value = MarketMatcher.extract_total_value(k_title, ticker=k_ticker)
                            if total_value:
                                display_name = f"{k_title.replace('?', '')} O/U {total_value + 0.5}"
                            else:
                                display_name = k_title
                        else:
                            display_name = k_title

                    unified_market = UnifiedMarket(
                        market_type=market_type or 'unknown',
                        display_name=display_name,
                        kalshi_markets=[k_market],  # Single market in a list
                        kalshi_tickers=[k_ticker],
                        kalshi_titles=[k_title],
                        kalshi_event_ticker=k_event_ticker,
                        polymarket_market=matched_p_market,
                        polymarket_id=matched_p_market.id,
                        polymarket_question=matched_p_market.question,
                        polymarket_game_id=unified_event.polymarket_game_id
                    )
                    print(f"      → Created unified market: {display_name[:60]}")
                else:
                    # Kalshi only
                    # For totals, add the line value to display name
                    if market_type == 'total' and k_ticker:
                        total_value = MarketMatcher.extract_total_value(k_title, ticker=k_ticker)
                        if total_value:
                            display_name = f"{k_title.replace('?', '')} O/U {total_value + 0.5}"
                        else:
                            display_name = k_title
                    else:
                        display_name = k_title
                    unified_market = UnifiedMarket(
                        market_type=market_type or 'unknown',
                        display_name=display_name,
                        kalshi_markets=[k_market],  # Single market in a list
                        kalshi_tickers=[k_ticker],
                        kalshi_titles=[k_title],
                        kalshi_event_ticker=k_event_ticker
                    )
                    print(f"      → Kalshi only market")

                unified_markets.append(unified_market)

        # Second pass: Add unmatched Polymarket markets
        if unified_event.has_polymarket():
            for p_idx, p_market in enumerate(poly_markets):
                if p_idx not in matched_poly_indices:
                    # Polymarket only
                    p_question = p_market.question
                    market_type = MarketMatcher.get_market_type(poly_question=p_question)

                    unified_market = UnifiedMarket(
                        market_type=market_type or 'unknown',
                        display_name=p_question,
                        polymarket_market=p_market,
                        polymarket_id=p_market.id,
                        polymarket_question=p_question,
                        polymarket_game_id=unified_event.polymarket_game_id
                    )
                    unified_markets.append(unified_market)

        # Sort markets: matched first, then by type, then by name
        # Order:
        #   1. Matched markets (has both Kalshi and Polymarket) - priority 0
        #   2. Single-source markets - priority 1
        # Within each priority group, sort by: moneyline, spread, total, props, unknown
        type_order = {'moneyline': 0, 'spread': 1, 'total': 2, 'prop': 3, 'unknown': 4}

        def sort_key(m):
            # Props always sort below the game lines (they live under a
            # section separator, same pattern as the LiquidityWidget
            # event menu's FUTURES header). Within each section: matched
            # markets first, then by type, then by name.
            is_prop = 1 if m.market_type == 'prop' else 0
            has_both = 0 if (m.has_kalshi() and m.has_polymarket()) else 1
            market_type_order = type_order.get(m.market_type, 4)
            return (is_prop, has_both, market_type_order, m.display_name)

        unified_markets.sort(key=sort_key)

        # Populate market selector. A disabled header item marks the
        # game-lines / player-props boundary (only when both sections are
        # present, so it's never the first row).
        props_separator_added = False
        for unified_market in unified_markets:
            if (unified_market.market_type == 'prop'
                    and not props_separator_added
                    and self.market_selector.count() > 0):
                self.market_selector.addItem("──────  PLAYER PROPS  ──────")
                sep_item = self.market_selector.model().item(
                    self.market_selector.count() - 1)
                if sep_item is not None:
                    sep_item.setEnabled(False)
                props_separator_added = True
            source_indicator = unified_market.get_source_indicator()
            display_text = f"{source_indicator} {unified_market.display_name}"
            self.market_selector.addItem(display_text, userData=unified_market)

        self.market_selector.blockSignals(False)

        print(f"  Loaded {len(unified_markets)} unified markets")
        kalshi_count = sum(1 for m in unified_markets if m.has_kalshi())
        poly_count = sum(1 for m in unified_markets if m.has_polymarket())
        both_count = sum(1 for m in unified_markets if m.has_kalshi() and m.has_polymarket())
        print(f"    Kalshi: {kalshi_count}, Polymarket: {poly_count}, Both: {both_count}")

        # Enable widget if we have markets
        if unified_markets:
            self.set_enabled(True)
            # Auto-select first market
            await self.on_market_changed()
        else:
            # No markets available - keep disabled
            self.set_enabled(False)

    async def load_all_markets_for_event(self, base_event_ticker, base_title, home_team, away_team):
        """
        Load all markets for an event from all related series (moneyline, spread, total).

        Args:
            base_event_ticker: Base event ticker (e.g., 'KXNFLGAME-25NOV03ARIDAL')
            base_title: Base event title (e.g., 'Arizona at Dallas')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Loading all markets for: {base_title}")

        # Extract the base ticker ID (e.g., '25NOV03ARIDAL' from 'KXNFLGAME-25NOV03ARIDAL')
        ticker_parts = base_event_ticker.split('-')
        if len(ticker_parts) >= 2:
            ticker_id = '-'.join(ticker_parts[1:])  # Everything after first dash
        else:
            ticker_id = base_event_ticker

        # Determine sport and construct series tickers to check
        # Include props series where available (currently only NFL has props)
        sport = None
        if 'NFL' in base_event_ticker:
            sport = 'NFL'
            series_to_check = [
                'KXNFLGAME',           # Moneylines
                'KXNFLSPREAD',         # Spreads
                'KXNFLTOTAL',          # Totals
                'KXMVENFLSINGLEGAME'   # Single game props
            ]
        elif 'NBA' in base_event_ticker:
            sport = 'NBA'
            series_to_check = [
                'KXNBAGAME',           # Moneylines
                'KXNBASPREAD',         # Spreads
                'KXNBATOTAL'           # Totals
                # 'KXMVENBASINGLEGAME' would go here when available
            ]
        elif 'MLB' in base_event_ticker:
            sport = 'MLB'
            series_to_check = [
                'KXMLBGAME',           # Moneylines
                'KXMLBSPREAD',         # Spreads
                'KXMLBTOTAL'           # Totals
                # 'KXMVEMLBSINGLEGAME' would go here when available
            ]
        elif 'NHL' in base_event_ticker:
            sport = 'NHL'
            series_to_check = [
                'KXNHLGAME',           # Moneylines
                'KXNHLSPREAD',         # Spreads
                'KXNHLTOTAL'           # Totals
                # 'KXMVENHLSINGLEGAME' would go here when available
            ]
        else:
            # Fallback to single event
            await self.set_kalshi_event(base_event_ticker, base_event_ticker.split('-')[0], home_team, away_team)
            return

        # Fetch markets from all related series
        all_markets = []
        loop = asyncio.get_event_loop()

        for series in series_to_check:
            # Props series have different ticker formats - they need to be fetched differently
            if series.startswith('KXMVE'):
                # For props, we need to fetch all events in the series and filter by team names
                # since they don't follow the same ticker pattern
                try:
                    props_markets = await self._fetch_props_markets_for_teams(series, home_team, away_team)
                    if props_markets:
                        all_markets.extend(props_markets)
                        print(f"  {series}: {len(props_markets)} props markets")
                except Exception as e:
                    print(f"  {series}: No props markets found ({e})")
            else:
                # Standard game/spread/total series - use ticker pattern
                event_ticker = f"{series}-{ticker_id}"
                try:
                    # Fetch event markets
                    markets = await loop.run_in_executor(
                        None,
                        lambda s=series, t=ticker_id: self._fetch_event_markets_sync(s, t)
                    )
                    if markets:
                        all_markets.extend(markets)
                        print(f"  {series}: {len(markets)} markets")
                except Exception as e:
                    print(f"  {series}: No markets found ({e})")

        print(f"Total markets loaded: {len(all_markets)}")

        # Set up the event
        self.data_source = 'kalshi'
        self.client = self.kalshi_client
        self.kalshi_event_ticker = base_event_ticker
        self.kalshi_series_ticker = series_to_check[0]  # Use primary series
        self.home_team = home_team
        self.away_team = away_team

        # Update market info
        self.market_info.setText(f"{away_team} @ {home_team}")

        # Populate market selector
        self.market_selector.blockSignals(True)
        self.market_selector.clear()

        for market in all_markets:
            market_title = market.get('yes_sub_title', market.get('subtitle', market.get('ticker')))
            market_ticker = market.get('ticker')
            self.market_selector.addItem(market_title, userData=market_ticker)

        self.market_selector.blockSignals(False)

        # Select first market and load data
        if all_markets:
            self.kalshi_market_ticker = all_markets[0].get('ticker')
            # Market Post marker source: the selected market's Kalshi open_time.
            self._kalshi_open_by_ticker = {
                m.get('ticker'): m.get('open_time') for m in all_markets
                if isinstance(m, dict) and m.get('ticker')}
            self.market_post_iso = self._kalshi_open_by_ticker.get(self.kalshi_market_ticker)
            self.set_enabled(True)

            # Load data for first market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self.load_data())

    def _fetch_event_markets_sync(self, series_ticker, ticker_id):
        """Synchronous helper to fetch event markets"""
        try:
            event_ticker = f"{series_ticker}-{ticker_id}"
            event_data = self.kalshi_client.kalshi_client.get_event(
                event_ticker=event_ticker,
                with_nested_markets=True
            )
            return event_data.get('event', {}).get('markets', [])
        except:
            return []

    async def _fetch_props_markets_for_teams(self, series_ticker, home_team, away_team):
        """
        Fetch props markets for a specific game by matching team names.
        Props markets have different event structures and need to be matched by team names.
        Filters out parlay/multi-leg props (which contain multiple player names).

        Args:
            series_ticker: Props series ticker (e.g., 'KXMVENFLSINGLEGAME')
            home_team: Home team name to match
            away_team: Away team name to match

        Returns:
            List of markets for this game's props (excluding parlays)
        """
        try:
            loop = asyncio.get_event_loop()

            # Fetch all events in the props series
            events_data = await loop.run_in_executor(
                None,
                lambda: self.kalshi_client.kalshi_client.get_events(
                    series_ticker=series_ticker,
                    status='open',
                    with_nested_markets=True,
                    limit=200
                )
            )

            events = events_data.get('events', [])

            # Filter events that match our game by checking if both teams are in the title
            matching_markets = []
            parlay_keywords = ['yes', '+yes', 'and', '&', ',']  # Indicators of multi-leg parlays

            for event in events:
                event_title = event.get('title', '').lower()

                # Check if both team names appear in the event title
                # Handle various formats: "Team1 at Team2", "Team1 vs Team2", etc.
                home_match = home_team.lower() in event_title
                away_match = away_team.lower() in event_title

                if home_match and away_match:
                    # This event is for our game - filter the markets
                    markets = event.get('markets', [])

                    for market in markets:
                        # Get market subtitle/title to check for parlay indicators
                        market_subtitle = market.get('yes_sub_title', market.get('subtitle', '')).lower()
                        market_title = market.get('title', '').lower()

                        # Enhanced parlay detection
                        # Check for multiple "yes" statements (most parlays have multiple "yes X, yes Y" patterns)
                        yes_count = market_subtitle.count('yes ')

                        # Check for comma-separated player names (e.g., "yes Player1, yes Player2")
                        comma_count = market_subtitle.count(',')

                        # Check for multiple colons (often separate different prop types in parlays)
                        colon_count = market_subtitle.count(':')

                        # Check for "+" which separates different legs in parlays
                        plus_count = market_subtitle.count('+')

                        # STRICT FILTERING: Only accept single-player TD scorer props
                        # Skip if:
                        # - Multiple "yes" statements (yes Player1, yes Player2)
                        # - Multiple commas (lists multiple players/props)
                        # - Multiple colons (combines different prop types)
                        # - Contains game outcome props mixed with player props (e.g., "yes Player: 50+ and Dallas wins")
                        is_parlay = (
                            yes_count > 1 or          # Multiple "yes" = parlay
                            comma_count > 0 or        # Commas separate legs
                            colon_count > 1 or        # Multiple colons = multiple prop types
                            plus_count > 1 or         # Multiple + signs = parlay legs
                            'over ' in market_subtitle and ':' in market_subtitle  # Game total mixed with player prop
                        )

                        if is_parlay:
                            continue

                        # Skip if title explicitly mentions multiple outcomes or team outcomes
                        skip_keywords = ['and', ' & ', 'both', 'wins by', 'spread', 'total points']
                        if any(keyword in market_title for keyword in skip_keywords):
                            continue
                        if any(keyword in market_subtitle for keyword in skip_keywords):
                            continue

                        # Only include if this is a simple single-player prop
                        # Accepted prop types: TD scorer, yards (rushing/receiving/passing), receptions
                        prop_keywords = ['td', 'touchdown', 'yard', 'reception', 'pass', 'rush', 'receiv']

                        if any(keyword in market_subtitle for keyword in prop_keywords):
                            matching_markets.append(market)

            print(f"Filtered props: {len(matching_markets)} single props (excluded parlays)")
            return matching_markets

        except Exception as e:
            print(f"Error fetching props markets: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def set_kalshi_event(self, event_ticker, series_ticker, home_team, away_team):
        """
        Set a Kalshi event and load available markets.

        Args:
            event_ticker: Kalshi event ticker (e.g., 'KXNFLGAME-25OCT27WASKC')
            series_ticker: Kalshi series ticker (e.g., 'KXNFLGAME')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Setting Kalshi event: {event_ticker}")
        self.data_source = 'kalshi'
        self.client = self.kalshi_client
        self.kalshi_event_ticker = event_ticker
        self.kalshi_series_ticker = series_ticker
        self.home_team = home_team
        self.away_team = away_team

        # Update market info
        if home_team and away_team:
            self.market_info.setText(f"{away_team} @ {home_team}")
        else:
            self.market_info.setText(f"Event: {event_ticker}")

        # Fetch available markets
        try:
            markets = await self.kalshi_client.get_event_markets(event_ticker)
            self.kalshi_available_markets = markets

            # Populate market selector
            self.market_selector.blockSignals(True)
            self.market_selector.clear()

            for market in markets:
                market_title = market.get('yes_sub_title', market.get('subtitle', market.get('ticker')))
                market_ticker = market.get('ticker')
                self.market_selector.addItem(market_title, userData=market_ticker)

            self.market_selector.blockSignals(False)

            # Select first market and load data
            if markets:
                self.kalshi_market_ticker = markets[0].get('ticker')
                self.set_enabled(True)

                # Load data for first market
                if self._load_task and not self._load_task.done():
                    self._load_task.cancel()
                self._load_task = asyncio.create_task(self.load_data())

        except Exception as e:
            print(f"Error setting Kalshi event: {e}")
            import traceback
            traceback.print_exc()

    def set_market(self, sport_key, event_id, market_key, home_team, away_team):
        """
        Set the market to display using TheOddsAPI.

        Args:
            sport_key: Sport key for TheOddsAPI
            event_id: Event ID for TheOddsAPI
            market_key: Market key (e.g., 'h2h', 'spreads')
            home_team: Home team name
            away_team: Away team name
        """
        print(f"Setting TheOddsAPI market: {sport_key}, {event_id}, {market_key}")

        self.data_source = 'theoddsapi'
        self.client = self.theoddsapi_client
        self.sport_key = sport_key
        self.event_id = event_id
        self.market_key = market_key
        self.home_team = home_team
        self.away_team = away_team

        # Show/hide appropriate controls. TheOddsAPI is table-driven, so the
        # event/market pickers are hidden (the market is fixed by the table click)
        # and the time-range picker is shown. market_info carries the context.
        self.event_selector.setVisible(False)
        self.market_selector.setVisible(False)
        # (time_range is a hidden backing combo now; the visible timeframe combo
        # stays visible. Don't setVisible(True) on the backing combo or it would
        # pop up as a stray floating widget.)

        # Update UI
        if home_team and away_team:
            self.market_info.setText(f"{home_team} vs {away_team} - {market_key}")
        else:
            self.market_info.setText(f"Market: {market_key}")

        self.set_enabled(True)

        # Cancel any existing task
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()

        # Start new data load
        self._load_task = asyncio.create_task(self.load_data())

    @qasync.asyncSlot()
    async def on_event_changed(self):
        """Handle event selector change - supports unified events from both sources"""
        selected_index = self.event_selector.currentIndex()
        if selected_index < 0:
            return

        event_data = self.event_selector.itemData(selected_index)
        if not event_data:
            return

        # Default OFF; _load_futures_markets turns it on. Guarantees switching from a
        # futures market to any game/legacy market restores normal rendering.
        self._is_future_market = False

        # Check if this is a UnifiedEvent object
        if isinstance(event_data, UnifiedEvent):
            unified_event = event_data
            print(f"\nEvent changed to: {unified_event.get_display_title()}")
            if unified_event.is_future:
                print(f"  🏆 Futures '{unified_event.future_label}' — "
                      f"{len(unified_event.kalshi_markets or [])} candidates")
            else:
                print(f"  Kalshi: {'✓' if unified_event.has_kalshi() else '✗'}")
                print(f"  Polymarket: {'✓' if unified_event.has_polymarket() else '✗'}")

            # Populate market selector with markets (futures take a dedicated branch
            # inside load_markets_for_unified_event).
            await self.load_markets_for_unified_event(unified_event)

        else:
            # Legacy Kalshi-only format: (event_ticker, series_ticker, base_title)
            if self.data_source != 'kalshi':
                return

            event_ticker, series_ticker, base_title = event_data

            # Remove sport prefix from display title
            display_title = self.event_selector.currentText()
            if display_title.startswith('['):
                # Remove prefix like "[NFL] "
                event_title = display_title.split('] ', 1)[-1]
            else:
                event_title = display_title

            print(f"Event changed to: {event_title} ({event_ticker})")

            # Parse team names from event title (e.g., "Washington at Kansas City")
            if ' at ' in event_title:
                away_team, home_team = [t.strip() for t in event_title.split(' at ', 1)]
            elif ' vs ' in event_title:
                home_team, away_team = [t.strip() for t in event_title.split(' vs ', 1)]
            else:
                home_team = event_title
                away_team = event_title

            # Load markets for this event from ALL related series (moneyline, spread, total)
            await self.load_all_markets_for_event(event_ticker, base_title, home_team, away_team)

    @qasync.asyncSlot()
    async def on_market_changed(self):
        """Handle market selector change - supports both Kalshi and Polymarket"""
        selected_index = self.market_selector.currentIndex()
        if selected_index < 0:
            return

        market_data = self.market_selector.itemData(selected_index)
        if not market_data:
            return

        # Check if this is a UnifiedMarket object
        if isinstance(market_data, UnifiedMarket):
            unified_market = market_data

            # Unsubscribe from the OLD market's live feed BEFORE retargeting the
            # tracked tickers/tokens (uses the current live_source + keys).
            if self.websocket_enabled:
                self._unsubscribe_from_current_market()

            self.current_unified_market = unified_market

            # Futures have no historical fetch, so update_plot — which clears the
            # canvas — never runs for them. Wipe the PREVIOUS market's plotted lines +
            # hover registry NOW, synchronously, before the live overlay/seed render —
            # otherwise a prior game's lines/points bleed under the futures chart (and
            # the async load_data clear races with the seed). The live overlay rebuilds
            # on the next flush.
            if self._is_future_market:
                self._clear_plotted_series()

            # Retarget per-source keys for the new market.
            source_info = []
            if unified_market.has_kalshi():
                source_info.append("Kalshi")
                self.kalshi_market_ticker = unified_market.kalshi_tickers[0] if unified_market.kalshi_tickers else None
            else:
                self.kalshi_market_ticker = None
            # Market Post marker source: the selected Kalshi market's open_time.
            self.market_post_iso = self._kalshi_open_time_for(
                unified_market, self.kalshi_market_ticker)
            if unified_market.has_polymarket():
                source_info.append("Polymarket")
                self.polymarket_token_id, self.polymarket_outcome_name = \
                    self._pm_token_for_current_market()
            else:
                self.polymarket_token_id = self.polymarket_outcome_name = None

            print(f"Market changed to: [{' + '.join(source_info)}] {unified_market.display_name}")

            # Live is the default timeframe — auto-enter live mode on the first market
            # selection (when not already live). _enter_live_mode enables the websocket
            # itself (and reverts if no stream client), and does the full live setup
            # (series, subscribe, seed, data load), so we're done here.
            if (self.kalshi_interval.currentText() == "Live" and not self.live_mode
                    and self._available_live_sources()):
                await self._enter_live_mode()
                return

            # Re-target the live feed for the new market.
            if self.live_mode and self.websocket_enabled:
                avail = self._available_live_sources()
                if avail:
                    # Keep 'both' if both venues are still available; otherwise if
                    # the current single source is gone, fall back to what's there.
                    if self.live_source == 'both':
                        if len(avail) < 2:
                            self.live_source = avail[0]
                    elif self.live_source not in avail:
                        self.live_source = avail[0]
                    # Reset the outcome selection to the new market's first outcome
                    # (the previous label likely doesn't exist here). Futures default
                    # to 'all' = the top-N overlay rather than a single candidate.
                    _sides = self._build_market_sides()
                    if self._is_future_market:
                        self.outcome_sel = 'top5'  # initial tier; Side combo offers more
                    else:
                        self.outcome_sel = _sides[0]['label'] if _sides else None
                    # Switching between a game and a futures market changes the right
                    # view mode (candles vs lines + follow). Re-apply BEFORE subscribe so
                    # the scheduled history seed runs with the correct candle_mode.
                    if self._is_future_market != getattr(self, '_live_view_is_future', None):
                        self._apply_live_view_defaults()
                    self._rebuild_live_series()
                    self._populate_feed_sources()
                    self._populate_side_combo()
                    self._update_order_entry_visibility()
                    self._ensure_active_stream_started()
                    self._subscribe_to_current_market()
            elif self.websocket_enabled and unified_market.has_kalshi():
                # Non-live ticker-polling path (Kalshi only) — existing behavior.
                self._subscribe_to_current_market()

            # Reload data for new market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self.load_data())

        # Check if this is the old dict format (dict with 'source' key)
        elif isinstance(market_data, dict) and 'source' in market_data:
            self.selected_market_data = market_data
            source = market_data['source']

            if source == 'kalshi':
                self.kalshi_market_ticker = market_data['ticker']
                print(f"Market changed to: [Kalshi] {market_data['title']}")

                # Handle WebSocket subscription
                if self.websocket_enabled:
                    self._unsubscribe_from_current_market()
                    self._subscribe_to_current_market()

            elif source == 'polymarket':
                print(f"Market changed to: [Polymarket] {market_data['question']}")

            # Reload data for new market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self.load_data())

        else:
            # Legacy Kalshi-only format (just the ticker string)
            if self.data_source != 'kalshi':
                return

            market_ticker = market_data
            self.kalshi_market_ticker = market_ticker
            print(f"Market changed to: {market_ticker}")

            # Handle WebSocket subscription
            if self.websocket_enabled:
                self._unsubscribe_from_current_market()
                self._subscribe_to_current_market()

            # Reload data for new market
            if self._load_task and not self._load_task.done():
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self.load_data())

    @qasync.asyncSlot()
    async def on_refresh_clicked(self):
        """Handle refresh button click"""
        if self._load_task and not self._load_task.done():
            return  # Skip if already loading

        try:
            self._load_task = asyncio.create_task(self.load_data())
            await self._load_task
        except Exception as e:
            print(f"Refresh error: {e}")

    @qasync.asyncSlot()
    async def on_auto_refresh(self):
        """Handle automatic refresh timer"""
        # Refresh for Kalshi-only views and for unified (Kalshi + Polymarket) markets.
        has_unified = getattr(self, 'current_unified_market', None) is not None
        if not self.auto_refresh_enabled or (self.data_source != 'kalshi' and not has_unified):
            return

        if self._load_task and not self._load_task.done():
            return  # Skip if already loading

        print("Auto-refreshing Kalshi market data...")

        try:
            # Reload the data
            self._load_task = asyncio.create_task(self.load_data())
            await self._load_task
        except Exception as e:
            print(f"Auto-refresh error: {e}")

    def start_live_updates(self):
        """Start the auto-refresh timer"""
        # WebSocket streaming, when enabled, drives live updates instead of polling.
        if self.websocket_enabled:
            return
        has_unified = getattr(self, 'current_unified_market', None) is not None
        if (self.data_source == 'kalshi' or has_unified) and self.auto_refresh_enabled:
            if not self.refresh_timer.isActive():
                self.refresh_timer.start(self.refresh_interval_ms)
                print(f"Live updates enabled (refresh every {self.refresh_interval_ms/1000}s)")

    def stop_live_updates(self):
        """Stop the auto-refresh timer"""
        self.refresh_timer.stop()
        print("Live updates disabled")

    async def load_unified_market_data(self):
        """
        Load historical data for a unified market (can be from Kalshi, Polymarket, or both).
        Handles both UnifiedMarket objects and legacy dict format.
        """
        # Determine which format we're using
        unified_market = None
        selected_market_data = None

        if hasattr(self, 'current_unified_market') and self.current_unified_market:
            unified_market = self.current_unified_market
        elif hasattr(self, 'selected_market_data') and self.selected_market_data:
            selected_market_data = self.selected_market_data
        else:
            print("No market selected")
            return

        # Calculate time range
        end_time = datetime.now()
        start_time = self.calculate_start_time(end_time)
        # "Live" is the sub-second websocket mode; for the seed historical pull
        # treat it as the finest candlestick resolution (1m).
        _interval_text = self.kalshi_interval.currentText()
        kalshi_interval_value = 1 if _interval_text == "Live" else int(_interval_text.removesuffix('m'))
        # At 1-minute granularity the Kalshi candlestick endpoint caps results at
        # ~5000 candles. A wide range (e.g. 7d) silently truncates/errors the Kalshi
        # request while Polymarket still returns data, leaving the Kalshi line blank
        # or stale. Clamp the lookback so the live 1m series always comes back.
        if kalshi_interval_value == 1:
            start_time = max(start_time, end_time - timedelta(days=2))

        self.progress_bar.setValue(10)
        self.refresh_button.setEnabled(False)

        # Remove "no data" message if it exists
        try:
            self.plot_widget.removeItem(self.no_data_text)
        except:
            pass

        try:
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=60)

            all_snapshots = []

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # === UNIFIED MARKET PATH (NEW) ===
                if unified_market:
                    print(f"\n{'='*80}")
                    print(f"Loading unified market data")
                    print(f"Market: {unified_market.display_name}")
                    print(f"Sources: {unified_market.get_source_indicator()}")
                    print(f"Time range: {start_time} to {end_time}")
                    print(f"{'='*80}\n")

                    # Fetch every Kalshi ticker + Polymarket outcome CONCURRENTLY.
                    # These were awaited one-at-a-time (up to 4 sequential round-trips
                    # for a Both moneyline: 2 Kalshi sides + 2 PM outcomes), which is
                    # the bulk of a market/event switch's latency. gather collapses the
                    # phase to ~1 round-trip; each fetch isolates its own errors so one
                    # failure doesn't sink the rest, and all results land in the same
                    # all_snapshots (draw order is timestamp-sorted downstream).
                    fetch_coros = []

                    # Kalshi (may be multiple markets for a moneyline). Futures SKIP
                    # this: the live overlay's per-candidate seed (_apply_live_history_seed)
                    # supplies palette-coloured history for the whole tier — drawing
                    # update_plot's venue-coloured, top-5-capped lines here would just
                    # overlap them with cyan duplicates.
                    if unified_market.has_kalshi() and not self._is_future_market:
                        k_series = unified_market.kalshi_event_ticker.split('-')[0]

                        async def _fetch_kalshi(k_ticker, k_title):
                            print(f"Fetching Kalshi data for: {k_title} ({k_ticker})")
                            try:
                                snaps = await self.kalshi_client.get_historical_candlesticks(
                                    session, k_ticker, k_series, start_time, end_time,
                                    period_interval=kalshi_interval_value,
                                    market_type=unified_market.market_type)
                                print(f"  ✅ Got {len(snaps)} Kalshi snapshots ({k_ticker})")
                                return snaps
                            except Exception as e:
                                print(f"  ❌ Error fetching Kalshi data ({k_ticker}): {e}")
                                import traceback
                                traceback.print_exc()
                                return []

                        for idx, k_ticker in enumerate(unified_market.kalshi_tickers):
                            fetch_coros.append(
                                _fetch_kalshi(k_ticker, unified_market.kalshi_titles[idx]))

                    # Polymarket (may have multiple outcomes, e.g. both teams).
                    if unified_market.has_polymarket():
                        market_obj = unified_market.polymarket_market
                        if market_obj.outcome_prices and len(market_obj.clob_token_ids) > 0:
                            print(f"Fetching Polymarket data for: {market_obj.question}")

                            async def _fetch_poly(token_id, outcome_name):
                                print(f"  Outcome: {outcome_name} ({token_id})")
                                try:
                                    snaps = await self.polymarket_client.get_historical_candlesticks(
                                        session, token_id, outcome_name, start_time, end_time,
                                        fidelity=kalshi_interval_value,  # align with Kalshi
                                        market_type=unified_market.market_type)
                                    print(f"    ✅ Got {len(snaps)} PM snapshots ({outcome_name})")
                                    return snaps
                                except Exception as e:
                                    print(f"  ❌ Error fetching Polymarket data ({outcome_name}): {e}")
                                    import traceback
                                    traceback.print_exc()
                                    return []

                            for outcome_idx, token_id in enumerate(market_obj.clob_token_ids):
                                if hasattr(market_obj, 'outcomes') and outcome_idx < len(market_obj.outcomes):
                                    outcome_name = market_obj.outcomes[outcome_idx]
                                else:
                                    outcome_name = f"{market_obj.question} - Outcome {outcome_idx + 1}"
                                fetch_coros.append(_fetch_poly(token_id, outcome_name))
                        else:
                            print("  ⚠️  No token IDs available for Polymarket market")

                    if fetch_coros:
                        for snaps in await asyncio.gather(*fetch_coros):
                            all_snapshots.extend(snaps)

                # === LEGACY DICT PATH (OLD) ===
                elif selected_market_data:
                    source = selected_market_data['source']
                    print(f"\n{'='*80}")
                    print(f"Loading market data from: {source}")
                    print(f"Time range: {start_time} to {end_time}")
                    print(f"{'='*80}\n")

                    if source == 'kalshi':
                        snapshots = await self.kalshi_client.get_historical_candlesticks(
                            session,
                            selected_market_data['ticker'],
                            selected_market_data['event_ticker'].split('-')[0],
                            start_time,
                            end_time,
                            period_interval=kalshi_interval_value
                        )
                        all_snapshots = snapshots

                    elif source == 'polymarket':
                        market_obj = selected_market_data['market_obj']
                        if market_obj.outcome_prices and len(market_obj.clob_token_ids) > 0:
                            token_id = market_obj.clob_token_ids[0]
                            outcome_name = market_obj.question.split('?')[0].strip()

                            snapshots = await self.polymarket_client.get_historical_candlesticks(
                                session,
                                token_id,
                                outcome_name,
                                start_time,
                                end_time,
                                fidelity=kalshi_interval_value  # Use same interval as Kalshi for alignment
                            )
                            all_snapshots = snapshots
                        else:
                            print("⚠️  No token IDs available for this Polymarket market")

            self.progress_bar.setValue(50)
            print(f"Total snapshots received: {len(all_snapshots)}")
            snapshots = all_snapshots

            if snapshots:
                self.current_snapshots = snapshots
                # Process UI updates
                await asyncio.gather(
                    self.update_bookmaker_toggles(snapshots),
                    self.update_plot(snapshots)
                )
                self.progress_bar.setValue(100)

                # Start live updates - the unified (Kalshi + Polymarket) path is what
                # the dual-line chart uses, so it MUST start the auto-refresh timer too.
                # Without this the chart freezes after the initial load and never picks
                # up in-game odds moves (the legacy load_data path started it, this one
                # did not).
                self.start_live_updates()
            elif self.live_mode:
                # Live market with no historical snapshots. update_plot — which clears
                # the canvas via plot_widget.clear() — is skipped here, so the PREVIOUS
                # event's plotted lines would persist under the new live overlay. Futures
                # were already cleared synchronously at market-switch time (on_market_changed,
                # to avoid racing the seed); for a non-futures market whose candlestick fetch
                # simply came back empty, clear the stale series NOW so switching events
                # actually flushes the old lines. The live overlay rebuilds on the next flush.
                if not self._is_future_market:
                    self._clear_plotted_series()
                self._ensure_market_post_line()
                self._ensure_start_line()
                self.start_live_updates()
                self.progress_bar.setValue(100)
            else:
                print("No data returned")
                self.progress_bar.setValue(0)

        except Exception as e:
            print(f"Error loading unified market data: {e}")
            import traceback
            traceback.print_exc()
            self.progress_bar.setValue(0)
        finally:
            self.refresh_button.setEnabled(True)

    async def load_data(self):
        """Load historical odds data and populate the graph - supports both Kalshi and Polymarket"""

        # A new market load re-arms a single axis auto-fit (see update_plot).
        self._axes_configured = False

        # Check if we have a unified market (new path with UnifiedMarket object)
        if hasattr(self, 'current_unified_market') and self.current_unified_market:
            return await self.load_unified_market_data()

        # Check if we have a unified event with market data (dict-based path)
        if hasattr(self, 'selected_market_data') and self.selected_market_data:
            return await self.load_unified_market_data()

        # Legacy validation for old Kalshi-only path
        if self.data_source == 'kalshi':
            if not all([self.kalshi_event_ticker, self.kalshi_series_ticker, self.kalshi_market_ticker]):
                print("Missing required Kalshi market info")
                return
        else:  # theoddsapi
            if not all([self.sport_key, self.event_id, self.market_key]):
                print("Missing required TheOddsAPI market info")
                return

        if not self.client:
            print("Client not initialized!")
            return

        # Calculate time range
        end_time = datetime.now()
        start_time = self.calculate_start_time(end_time)
        # "Live" is the sub-second websocket mode; for the seed historical pull
        # treat it as the finest candlestick resolution (1m).
        _interval_text = self.kalshi_interval.currentText()
        kalshi_interval_value = 1 if _interval_text == "Live" else int(_interval_text.removesuffix('m'))
        # if the interval is set to 1-minute, the start-time needs to be reduced to avoid the 5000-candlestick (API-side) limit
        if ((self.data_source == 'kalshi') and (kalshi_interval_value == 1)):
            start_time = max(start_time, (end_time - timedelta(days=2)))
            # only reduce start_time - 'max', not 'min' - because it's back in time

        self.progress_bar.setValue(10)
        self.refresh_button.setEnabled(False)

        # Make sure we remove the "no data" message if it exists
        try:
            self.plot_widget.removeItem(self.no_data_text)
        except:
            pass  # It's fine if it doesn't exist

        try:
            print(f"Fetching historical data from {start_time} to {end_time}")
            print(f"Data source: {self.data_source}")

            # Set up the session with connection pooling for better performance
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            timeout = aiohttp.ClientTimeout(total=60)  # 60 second timeout

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                if self.data_source == 'kalshi': # Fetch from Kalshi
                    snapshots = await self.client.get_historical_candlesticks(
                        session,
                        self.kalshi_market_ticker,
                        self.kalshi_series_ticker,
                        start_time,
                        end_time,
                        period_interval=kalshi_interval_value
                    )
                else:
                    # Fetch from TheOddsAPI
                    snapshots = await self.client.get_historical_snapshots(
                        session,
                        self.sport_key,
                        self.event_id,
                        self.market_key,
                        start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                    )

            print(f"Processing {len(snapshots)} snapshots")
            self.progress_bar.setValue(50)

            if snapshots:
                self.current_snapshots = snapshots
                # Process UI updates concurrently
                await asyncio.gather(
                    self.update_bookmaker_toggles(snapshots),
                    self.update_plot(snapshots)
                )
                self.progress_bar.setValue(100)

                # Start live updates for Kalshi data
                if self.data_source == 'kalshi':
                    self.start_live_updates()
            else:
                print("No valid historical data available")
                await self._show_no_data_message("No historical data available")

        except asyncio.CancelledError:
            print("Data loading was cancelled")
            # Clean exit for cancelled tasks
            await self._show_no_data_message("Loading cancelled")
        except Exception as e:
            print(f"Error loading historical odds: {str(e)}")
            import traceback
            traceback.print_exc()
            await self._show_no_data_message(f"Error: {str(e)}")
        finally:
            self.progress_bar.setValue(0)
            self.refresh_button.setEnabled(True)
            QTimer.singleShot(1000, lambda: self.progress_bar.setValue(0))

    def calculate_start_time(self, end_time):
        """
        Calculate start time based on selected time range.
        controlled by the 'Time' dropdown top-right of the graph
        """
        range_text = self.time_range.currentText()

        if range_text == "1h":
            return end_time - timedelta(hours=1)
        elif range_text == "3h":
            return end_time - timedelta(hours=3)
        elif range_text == "6h":
            return end_time - timedelta(hours=6)
        elif range_text == "12h":
            return end_time - timedelta(hours=12)
        elif range_text == "24h":
            return end_time - timedelta(hours=24)
        elif range_text == "7d":
            return end_time - timedelta(days=7)
        else:
            return end_time - timedelta(hours=6)  # Default

    def create_bookmaker_toggle(self, bookmaker_name):
        """Create a toggle handler for a specific bookmaker"""
        # Convert this to use qasync.asyncSlot
        @qasync.asyncSlot(int)
        async def toggle_handler(state):
            await self.on_bookmaker_toggled(bookmaker_name, state)
        return toggle_handler

    async def update_bookmaker_toggles(self, snapshots):
        """Update the bookmaker toggle checkboxes based on available data"""
        # Clear existing toggles
        while self.bookmaker_layout.count():
            item = self.bookmaker_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Extract unique bookmakers from all snapshots
        all_bookmakers = set()
        for snapshot in snapshots:
            for bookmaker in snapshot.get("data", {}).get("bookmakers", []):
                all_bookmakers.add(bookmaker["key"])

        print(f"Found bookmakers: {all_bookmakers}")

        # Add "All" checkbox
        select_all = QCheckBox("All")
        select_all.setChecked(True)
        select_all.stateChanged.connect(self.toggle_all_bookmakers)
        self.bookmaker_layout.addWidget(select_all)

        # Add a checkbox for each bookmaker
        for bookmaker_name in sorted(all_bookmakers):
            checkbox = QCheckBox(bookmaker_name)
            checkbox.setChecked(True)
            self.bookmaker_visible[bookmaker_name] = True
            # Use the function factory to avoid lambda capture issues
            checkbox.stateChanged.connect(self.create_bookmaker_toggle(bookmaker_name))
            self.bookmaker_layout.addWidget(checkbox)

        # Add stretch to push all checkboxes to the top
        self.bookmaker_layout.addStretch(1)

    @qasync.asyncSlot(int)
    async def toggle_all_bookmakers(self, state):
        """Toggle all bookmaker checkboxes to the given state"""
        # @asyncSlot(int) delivers `state` as a plain int (0/2); comparing it to
        # the Qt.CheckState enum is always False in PyQt6, so use bool() like the
        # other toggle handlers.
        checked = bool(state)
        # Skip the first checkbox (which is "All") and last item (which is stretch)
        for i in range(1, self.bookmaker_layout.count() - 1):
            item = self.bookmaker_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QCheckBox):
                item.widget().setChecked(checked)

    async def on_bookmaker_toggled(self, bookmaker, state):
        """Handle bookmaker toggle checkbox changes"""
        print(f"Toggling bookmaker: {bookmaker} to {state}")
        # bool(state): @asyncSlot(int) gives an int; `== Qt.CheckState.Checked`
        # is always False in PyQt6, which is why a rechecked book never returned.
        self.bookmaker_visible[bookmaker] = bool(state)
        # Refresh the plot with current visibility settings
        await self.update_plot(self.current_snapshots)

    @qasync.asyncSlot()
    async def _on_timeframe_changed(self):
        """Combined timeframe preset picked: push (window, granularity) into the
        hidden backing combos, then run the normal reload path. asyncSlot makes
        this schedulable from the Qt signal; on_time_range_changed is a plain
        coroutine, so we await it directly."""
        data = self.timeframe_combo.currentData()
        if not data:
            return
        rng, itv = data
        # Backing combos are disconnected, so these don't recurse.
        self.time_range.setCurrentText(rng)
        self.kalshi_interval.setCurrentText(itv)
        await self.on_time_range_changed()

    def _set_timeframe_silent(self, label):
        """Set the visible timeframe combo without triggering a reload (used when
        the Live attempt reverts to a candle interval)."""
        self.timeframe_combo.blockSignals(True)
        i = self.timeframe_combo.findText(label)
        if i >= 0:
            self.timeframe_combo.setCurrentIndex(i)
        self.timeframe_combo.blockSignals(False)

    async def on_time_range_changed(self):
        """Handle time range / interval dropdown changes"""
        range_text = self.time_range.currentText()
        interval_text = self.kalshi_interval.currentText()
        print(f"Time range changed to: {range_text} (interval: {interval_text})")

        # --- Sub-second Live mode toggle (Interval == "Live") ---
        want_live = (interval_text == "Live")
        if want_live and not self.live_mode:
            await self._enter_live_mode()
            return
        if not want_live and self.live_mode:
            self._exit_live_mode()
            # fall through to a normal reload below

        # Only update min_interval for TheOddsAPI client
        if self.data_source == 'theoddsapi' and hasattr(self.theoddsapi_client, 'min_interval'):
            # Extract numeric value from range text
            if 'h' in range_text:
                current_time_range = int(range_text.removesuffix('h'))
                self.theoddsapi_client.min_interval = timedelta(minutes=(current_time_range * 10))
                print(f"TheOddsAPI interval: {self.theoddsapi_client.min_interval}")

        # Reload data with new time range
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self.load_data())

    def _apply_live_view_defaults(self):
        """Candle/Follow view defaults for the current market kind. Games -> 1-min
        candles + follow ON; futures -> LINES (many overlaid candidates make candles
        an unreadable mess). Called on live entry AND when switching between a game and
        a futures market while already live — otherwise the previous kind's mode (e.g.
        futures' candles-off) sticks and the game renders as a jumbled tick line.
        Tracks _live_view_is_future so the caller only re-applies on an actual change."""
        want_candles = not self._is_future_market
        self.candle_check.blockSignals(True)
        self.candle_check.setChecked(want_candles)
        self.candle_check.blockSignals(False)
        self.candle_mode = want_candles
        # Futures DEFAULT to lines (a Top-N overlay of candle stacks is unreadable and
        # heavy), but the checkbox stays ENABLED so a single focused candidate can be
        # viewed as candles — at ≤10 series the per-flush candle rebuild is affordable.
        self.candle_check.setEnabled(True)
        self.candle_bucket.blockSignals(True)
        self.candle_bucket.setCurrentText("1m")
        self.candle_bucket.blockSignals(False)
        self.candle_bucket_s = 60
        self.candle_bucket.setEnabled(want_candles)
        # Follow ON: a readable RECENT window (the follow logic sizes X to the last
        # ~N candles) instead of the whole seeded day where 1-min candles are ~1px.
        self.auto_follow = True
        self.follow_check.blockSignals(True)
        self.follow_check.setChecked(True)
        self.follow_check.blockSignals(False)
        self._axes_configured = False
        self._live_view_is_future = self._is_future_market
        # Leaving futures clears the futures X-bounds / Y-lock so games get free X
        # zoom back, but the implied-% Y axis keeps its HARD [0, 100] floor/ceiling
        # (probability can't pan/zoom past its valid range). Futures re-apply their
        # own full Y-lock in the seed.
        if not self._is_future_market:
            vb = self.plot_widget.getViewBox()
            xlo, xhi = self._live_x_limits()
            # maxXRange caps wheel zoom-out at the allowed span outright (limits
            # alone would let scaleBy overshoot and clamp back).
            vb.setLimits(xMin=xlo, xMax=xhi,
                         maxXRange=(xhi - xlo) if xhi is not None else None,
                         yMin=0, yMax=100, minYRange=2, maxYRange=None)

    async def _enter_live_mode(self):
        """Switch the widget into sub-second websocket Live mode (Kalshi or
        Polymarket). The source is chosen from what the selected market offers;
        when it trades on both, Kalshi is the default (its OHLC seed + order entry
        are fully wired) and the feed selector lets the user switch to Polymarket."""
        def _revert():
            self.kalshi_interval.setCurrentText("1m")
            self._set_timeframe_silent("1m · 1h")

        sources = self._available_live_sources()
        if not sources:
            print("⚠️  Live mode needs a selected Kalshi or Polymarket market — reverting to 1m")
            _revert()
            return

        self.live_source = 'kalshi' if 'kalshi' in sources else 'polymarket'
        if self.live_source == 'polymarket':
            if self.polymarket_stream_client is None:
                print("⚠️  Live mode unavailable: Polymarket WebSocket failed to init")
                _revert()
                return
            self.polymarket_token_id, self.polymarket_outcome_name = \
                self._pm_token_for_current_market()
        else:
            if self.kalshi_stream_client is None:
                print("⚠️  Live mode unavailable: Kalshi WebSocket client failed to init")
                _revert()
                return

        print(f"🔴 Entering Live (sub-second) mode [{self.live_source}]")
        self.live_mode = True
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        # Default to following the first outcome (single series) — matches prior
        # behavior; the Side selector lets the user pick another or "All".
        _sides = self._build_market_sides()
        # Futures default to the top-N overlay; a head-to-head game to its first side.
        self.outcome_sel = 'top5' if self._is_future_market else (
            _sides[0]['label'] if _sides else None)

        # Candle/Follow view defaults (games -> 1m candles + follow; futures -> lines).
        self._apply_live_view_defaults()

        self.live_controls.setVisible(True)
        self.ob_section.setVisible(True)
        # Build the active series (feed × outcome) + the ladder stack, then the
        # selectors reflect the current choice.
        self._rebuild_live_series()
        self._populate_feed_sources()
        self._populate_side_combo()
        self._update_order_entry_visibility()  # routes to the active venue (K or PM)
        self.open_orders_list.clear()
        if any(s['venue'] == 'kalshi' for s in self.live_series):
            asyncio.create_task(self._refresh_open_orders())
        # Auto-expand the panel so the live depth is visible on entry.
        if getattr(self, '_panel_collapsed', False):
            self._toggle_right_panel()
        self.ws_status_label.setText("🟡")
        self.ws_status_label.setToolTip("WebSocket: Live mode — connecting…")

        # Hand off to the websocket: subscribing (in _subscribe_to_current_market)
        # resets live_ticks and then schedules _apply_live_history_seed, so the
        # historical candles are (re)seeded here and on every market switch.
        self.enable_websocket_updates(True)

        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        await self.load_data()

    async def _apply_live_history_seed(self):
        """Seed each live series' candles with the last day of history so the chart
        shows history up to now — Kalshi from real 1-min OHLC, Polymarket from CLOB
        prices-history (both -> 4 ticks/min via the shared aggregator). Merges with
        any live ticks newer than the seed, time-sorted. Called on live entry and on
        every feed/side/market change (which clear per-series ticks)."""
        # Seed whenever live: the live overlay is now the SOLE renderer of the K/PM
        # series in Live mode (update_plot no longer draws their history lines — that
        # caused a stale dotted layer to overlap the candles). So the overlay must
        # carry the market's history in BOTH line and candle mode, not just candles/
        # futures — otherwise switching to line mode would collapse the series to just
        # the handful of live ticks since the widget opened.
        if not self.live_mode:
            return
        # Fetch every series' seed history CONCURRENTLY (was sequential — painfully
        # slow for futures with up to ~40 candidates). Concurrency is LOW: Kalshi's
        # candlestick endpoint rate-limits hard, and 8-wide bursts 429'd the top
        # candidates (losing their seed -> empty ticks -> blank tooltip). 3 keeps it
        # under the limit while still ~3x faster than sequential.
        sem = asyncio.Semaphore(3)

        async def _fetch(s):
            async with sem:
                if s['venue'] == 'polymarket':
                    seed = await self._fetch_pm_seed_ticks(s['key'])
                    if seed and s.get('no'):  # NO side -> complement the PM seed too
                        seed = [{**t, 'price': 100 - t['price']} for t in seed]
                else:
                    seed = self._seed_ticks_from_ohlc(await self._fetch_seed_ohlc(s['key']))
                    if seed and (s['k_complement'] or s.get('no')):
                        seed = [{**t, 'price': 100 - t['price']} for t in seed]
            return s, seed

        # Cap the seed to the top series by price for a large futures field: seeding
        # all ~46 is slow and 429-prone, and the long-tail longshots (<~2%) don't
        # warrant a request each. live_series is price-ranked, so the head is the
        # favourites (which also fill the tooltip + order books). The tail still charts
        # from live ticks — it just lacks back-history.
        seed_series = self.live_series
        if self._is_future_market and len(seed_series) > 15:
            seed_series = seed_series[:15]
        results = await asyncio.gather(*[_fetch(s) for s in seed_series],
                                       return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                print(f"⚠️  seed fetch failed: {res}")
                continue
            s, seed = res
            if seed:
                last_t = seed[-1]['t']
                tail = [t for t in s['ticks'] if t['t'] > last_t]
                s['ticks'] = sorted(seed + tail, key=lambda t: t['t'])

        if not any(s['ticks'] for s in self.live_series):
            return
        if not self._overlays_built():
            self._rebuild_live_items()
        # Frame the ENTIRE market life: Market Post (or first tick) -> Event Start
        # (or last tick). Only when not following (follow keeps its scroll window).
        all_t = [t['t'] for s in self.live_series for t in s['ticks']]
        if not self.auto_follow and all_t:
            first, last = min(all_t), max(all_t)
            post = self._market_post_epoch()
            start_x = min(first, post) if post is not None else first
            es = self._event_start_epoch()
            end_x = max(last, es) if es is not None else last
            pad = max((end_x - start_x) * 0.02, 30)
            vb = self.plot_widget.getViewBox()
            vb.setXRange(start_x - pad, end_x + pad, padding=0)
            self._fit_live_y(start_x - pad, end_x + pad, smooth=False)
        # Futures: constrain the view so zoom/pan can't escape into nonsense. Implied %
        # is always 0-100 (lock the Y axis — no reason to zoom probability), and X is
        # bounded to the market's life span so you can't wander off to empty years.
        # Without this, free zoom left the chart stuck on a 31-37% Y / 2023-2029 X view
        # with no clean way back.
        vb = self.plot_widget.getViewBox()
        if self._is_future_market and all_t:
            span = max(all_t) - min(all_t)
            xpad = max(span * 0.05, 3600)
            vb.setLimits(xMin=min(all_t) - xpad, xMax=max(all_t) + xpad,
                         yMin=0, yMax=100, minYRange=100, maxYRange=100)
        else:
            # Games: bound X to the market life (Market Post -> live edge, now
            # refined by the seeded ticks) with small scroll pads on both sides,
            # and cap zoom-out at that span (maxXRange). Y keeps its HARD [0, 100]
            # floor/ceiling — probability can't scroll past its valid range.
            xlo, xhi = self._live_x_limits()
            vb.setLimits(xMin=xlo, xMax=xhi,
                         maxXRange=(xhi - xlo) if xhi is not None else None,
                         yMin=0, yMax=100, minYRange=2, maxYRange=None)
        # Re-evaluate the Market Post / Event Start markers: futures skip update_plot
        # (where these normally refresh), so without this they'd linger from the last
        # regular game. Both epoch sources return None for futures -> the lines hide.
        self._ensure_market_post_line()
        self._ensure_start_line()
        self._refresh_live_items()

    def _seed_window(self, end):
        """(start_epoch, kalshi_period_interval, pm_fidelity_min) for seeding the
        FULL market life — from the Market Post (Kalshi open_time) to now — instead
        of a fixed 24h. Granularity adapts to the span so the request stays bounded
        (Kalshi caps candles/request): <=~3.4d -> 1-min, else hourly, else daily."""
        # Use the raw open time (NOT _market_post_epoch, which is None for futures —
        # that would collapse the seed to the 3-day fallback and lock zoom to ~2 days).
        post = self._market_open_epoch()
        # Fall back to 3 days when the open time is unknown (typical game window).
        start = post if post is not None else (end - 3 * 24 * 3600)
        start = min(start, end - 60)  # guard against a future/garbage open time
        span = end - start
        if span <= 3.4 * 24 * 3600:
            return start, 1, 1        # 1-minute
        if span <= 200 * 24 * 3600:
            return start, 60, 60      # hourly
        return start, 1440, 1440      # daily

    async def _fetch_seed_ohlc(self, ticker=None):
        """Fetch the FULL Kalshi OHLC history (Market Post -> now) for `ticker`,
        at a span-appropriate interval, so the chart shows the entire market life."""
        mkt = ticker or self.kalshi_market_ticker
        if not mkt:
            return []
        # Derive the series the SAME way the unified load path does
        # (event_ticker prefix) instead of relying on self.kalshi_series_ticker,
        # which isn't set in the unified-market flow.
        series = (self.kalshi_series_ticker if not ticker else None) or mkt.split('-')[0]
        from datetime import datetime
        end = datetime.now().timestamp()
        start, interval, _ = self._seed_window(end)
        try:
            return await self.kalshi_client.get_ohlc_candles(
                mkt, series, start, end, period_interval=interval)
        except Exception as e:
            print(f"⚠️  seed OHLC fetch failed: {e}")
            return []

    async def _seed_pm_books_rest(self, tokens):
        """Seed each PM token's order book from the CLOB REST /book endpoint on
        subscribe. PM has no snapshot-recovery (unlike Kalshi's on_gap resubscribe),
        and the WS market channel doesn't reliably re-send a `book` snapshot on a
        re-subscribe over an existing socket — so a side/event switch can leave the
        book empty for 10-30s until deltas slowly rebuild it. This pulls the current
        book immediately instead. The WS `book` event stays authoritative and
        overwrites this the moment it arrives."""
        if not tokens:
            return
        import aiohttp
        import json as _json
        url = "https://clob.polymarket.com/book"

        def _depth(token):
            """Total resting levels currently in the book for `token`. Used to tell
            a real snapshot (many levels) from a few stray WS deltas that trickled
            in during the re-subscribe window — only the latter should be seeded."""
            lad = self.pm_live_book.ladder(token, 30)
            return (len(lad['bids']) + len(lad['asks'])) if lad else 0

        SNAPSHOT_MIN = 6  # >= this many levels ⇒ a real book already landed, skip
        for token in tokens:
            # Skip tokens no longer active (user switched again) or already holding a
            # real snapshot (a WS book arrived, e.g. from a trade). A handful of
            # stray delta levels does NOT count — we still want the full REST seed.
            if token not in self._active_pm_keys():
                continue
            if _depth(token) >= SNAPSHOT_MIN:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params={'token_id': token},
                                           timeout=10) as resp:
                        if resp.status != 200:
                            print(f"⚠️  PM book seed HTTP {resp.status} for …{token[-6:]}")
                            continue
                        data = await resp.json()
            except Exception as e:
                print(f"⚠️  PM book seed fetch failed (…{token[-6:]}): {e}")
                continue
            bids = data.get('bids') or []
            asks = data.get('asks') or []
            if not bids and not asks:
                continue
            # Re-check after the await — the user may have switched, or a real WS
            # book snapshot may have landed meanwhile (don't clobber fresher data).
            if token not in self._active_pm_keys():
                continue
            if _depth(token) >= SNAPSHOT_MIN:
                continue
            frame = {"event_type": "book", "asset_id": token,
                     "bids": bids, "asks": asks}
            try:
                if self._pm_native:
                    self.pm_live_book.ingest(_json.dumps(frame))
                else:
                    self.pm_live_book.apply(frame)
                self._live_dirty = True
                print(f"📖 PM book seeded from REST: …{token[-6:]} "
                      f"({len(bids)} bids / {len(asks)} asks)")
            except Exception as e:
                print(f"⚠️  PM book seed apply failed (…{token[-6:]}): {e}")

    async def _fetch_pm_seed_ticks(self, token=None):
        """Seed Live candles with the last day of Polymarket price history for the
        tracked token.

        Unlike Kalshi (real OHLC bars), CLOB prices-history gives ONE price per
        minute. Emitting one tick per point would make every seeded candle a flat
        doji. Instead we candle-ize the series the standard way: each minute's
        candle OPENS at the previous minute's price and CLOSES at this minute's
        price, so the seeded bars show real directional bodies. The open/close are
        placed at +1s/+59s within the minute, matching _seed_ticks_from_ohlc's
        1-minute-bucket convention (live entry forces the 1m bucket)."""
        token = token or self.polymarket_token_id
        if not token:
            return []
        from datetime import datetime
        end = datetime.now().timestamp()
        start, _, fidelity = self._seed_window(end)
        try:
            import aiohttp
            url = "https://clob.polymarket.com/prices-history"
            params = {'market': token, 'startTs': int(start),
                      'endTs': int(end), 'fidelity': fidelity}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=15) as resp:
                    if resp.status != 200:
                        print(f"⚠️  PM seed history HTTP {resp.status}")
                        return []
                    data = await resp.json()
            pts = []
            for pt in data.get('history', []):
                t, p = pt.get('t'), pt.get('p')
                if t is None or p is None:
                    continue
                pf = float(p)
                cents = pf * 100.0 if pf <= 1.0 else pf
                if 0 < cents < 100:
                    pts.append((float(t), round(cents, 1)))
            if not pts:
                return []
            # Resample onto a CONTINUOUS 1-minute grid (carry the last known price
            # across minutes that prices-history skips), then emit 4 ticks/min at
            # the SAME offsets as the Kalshi OHLC seed (_seed_ticks_from_ohlc). This
            # makes PM seeded candles continuous + identically spaced to Kalshi
            # instead of gapped/sparse where PM's history has no point for a minute.
            pts.sort(key=lambda x: x[0])
            start_m = (int(pts[0][0]) // 60) * 60
            end_m = (int(pts[-1][0]) // 60) * 60
            grid, j, last_price = {}, 0, pts[0][1]
            for m in range(start_m, end_m + 60, 60):
                while j < len(pts) and (int(pts[j][0]) // 60) * 60 <= m:
                    last_price = pts[j][1]; j += 1
                grid[m] = last_price
            ticks, prev = [], None
            for m in sorted(grid):
                c = grid[m]
                o = prev if prev is not None else c
                hi, lo = max(o, c), min(o, c)
                for off, price in ((1, o), (20, hi), (40, lo), (59, c)):
                    ticks.append({'t': m + off, 'price': price, 'bid': None, 'ask': None})
                prev = c
            return ticks
        except Exception as e:
            print(f"⚠️  PM seed history fetch failed: {e}")
            return []

    @staticmethod
    def _seed_ticks_from_ohlc(candles):
        """Expand each historical 1-min OHLC candle into 4 synthetic ticks
        (open→high→low→close, spaced within its minute) so the live 60s candle
        aggregator reconstructs the real bar. Returns a live_ticks-shaped list."""
        ticks = []
        for cd in candles:
            bs = cd['t'] - 60  # start of the 1-min period ending at end_period_ts
            for off, price in ((1, cd['o']), (20, cd['h']), (40, cd['l']), (59, cd['c'])):
                if price is None or not (0 < price < 100):
                    continue
                ticks.append({'t': bs + off, 'price': price, 'bid': None, 'ask': None})
        return ticks

    def _exit_live_mode(self):
        """Leave Live mode and return to the normal polling/candlestick path."""
        print("⚪ Exiting Live mode")
        self.live_mode = False
        self._live_dirty = False
        self.live_repaint_timer.stop()
        self.live_controls.setVisible(False)
        self.ob_section.setVisible(False)
        self.futures_field_section.setVisible(False)
        self.order_entry_section.setVisible(False)
        self.open_orders_list.clear()
        # Drop overlay items + the per-series ladders; the next live entry rebuilds.
        self._remove_overlay_items()
        self.live_series = []
        self._rebuild_ob_ladders()  # clears the ladder stack
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        self.enable_websocket_updates(False)
        self.live_book.reset()
        self.pm_live_book.reset()
        self.feed_source_row.setVisible(False)
        self.side_row.setVisible(False)
        self.live_source = 'kalshi'  # default for the next live entry
        self.outcome_sel = None
        # Drop the heat ribbon — its book is gone until the next live entry.
        self._heat_cols.clear()
        self._heat_last_sample_t = 0.0
        if self._heat_img is not None:
            self._heat_img.setVisible(False)

    async def update_plot(self, snapshots):
        """Enhanced plotting with point change visualization"""
        self.plot_widget.clear()
        # Grid is drawn by showGrid() on the axes (set once in init_ui) and
        # survives clear(), so no GridItem re-add is needed here.
        # Track which outcome bands already got peak/trough + net-move
        # annotations so the kalshi and polymarket series of the same band
        # don't double them up (reset each redraw; clear() wiped the old items).
        self._annotated_bands = set()
        # Hover registry is rebuilt each redraw (clear() wiped the plotted series).
        self._hover_series = []
        self._hide_hover()
        # Corner summary overlays are widget-space (clear() doesn't touch them),
        # so hide them up front; bands that are still present re-show their own.
        self._hide_summaries()
        if not snapshots:
            self._hide_side_logos()
            await self._show_no_data_message()
            return

        # Some Legend options for graph display
        #self.plot_widget.addLegend(offset=(10, 10), labelTextSize='8pt')

        # Group data by bookmaker and outcome
        plot_data = await self._organize_plot_data(snapshots)

        # Plot each series with proper point change handling
        for bm_idx, (bookmaker, outcomes) in enumerate(plot_data.items()):
            if not self.bookmaker_visible.get(bookmaker, True):
                continue

            # In LIVE mode the persistent live overlay OWNS Kalshi/Polymarket
            # rendering — it draws them as a line OR candles (seeded with history)
            # and mutates in place every tick. Drawing them HERE too would (a) double
            # the line, and (b) — because this static layer is only rebuilt on full
            # redraws — leave a STALE dotted-line layer overlaying the candles the
            # instant candle_mode flips without an update_plot (the candles+lines
            # overlap bug). So suppress K/PM here whenever the live overlay is active,
            # regardless of candle_mode. Lines here are reserved for the sportsbook
            # (TheOddsAPI/ProphetX) series and the non-live polling path.
            if (self.live_mode or self.candle_mode) and bookmaker in ('kalshi', 'polymarket'):
                continue

            color = self._color_for_bookmaker(bookmaker, bm_idx)
            for outcome_key, points_data in outcomes.items():
                await self._plot_outcome_series(bookmaker, outcome_key, points_data, color)

        # Auto-range the view only once per freshly loaded market, not on every
        # refresh/tick — constantly re-fitting the axes made the chart "jump"
        # (especially in Live mode). After the first frame the user owns the view
        # (pan/zoom freely); a new market load re-arms a single auto-fit.
        if not getattr(self, '_axes_configured', False):
            await self.configure_plot_axes(snapshots)
            self._axes_configured = True

        # Position team-logo watermarks for the two outcomes (moneyline only;
        # auto-hidden for Over/Under or spread markets where sides aren't teams).
        self._update_side_logos(plot_data)

        # Live overlays use persistent items so the hot tick path never calls this
        # heavy full redraw. clear() above removed them, so recreate them here on
        # the (infrequent) full redraws — load / market change / candle toggle.
        if getattr(self, 'live_mode', False):
            self._rebuild_live_items()
            self._refresh_live_items()

        # Crosshair lines + the event-start marker are scene items, so clear()
        # removed them — re-add.
        self._ensure_crosshair()
        self._ensure_start_line()
        self._ensure_market_post_line()
        self._ensure_heat_img()

    @staticmethod
    def _color_for_bookmaker(bookmaker, idx):
        """Series color by bookmaker key: kalshi=green, polymarket=blue, other
        paid-OddsAPI books fall back to the palette by plot order."""
        c = BOOKMAKER_LINE_COLORS.get((bookmaker or '').lower())
        if c is not None:
            return c
        return FALLBACK_LINE_COLORS[idx % len(FALLBACK_LINE_COLORS)]

    @staticmethod
    def _bookmaker_logo_path(bookmaker):
        """Absolute path to a bookmaker's brand logo, or None if we don't have one."""
        fn = BOOKMAKER_LOGO_FILE.get((bookmaker or '').lower())
        if not fn:
            return None
        p = SPORTSBOOK_LOGO_DIR / fn
        return str(p) if p.exists() else None

    def _venue_marker(self, venue, hue, size=14):
        """Small inline brand-logo <img> tagging which exchange a price belongs to
        (Kalshi / Polymarket), for the corner summary boxes. Falls back to a
        hue-coloured dot when a venue has no logo asset."""
        logo = self._bookmaker_logo_path(venue)
        if logo:
            return (f"<img src='{logo}' width='{size}' height='{size}' "
                    f"style='vertical-align:middle'>")
        r, g, b = hue
        return f"<span style='color:#{r:02x}{g:02x}{b:02x}'>&#9679;</span>"

    @staticmethod
    def _cents_to_am(cents):
        if cents is None or not (0 < cents < 100):
            return None
        return kalshi_cents_to_american_odds(cents)

    @staticmethod
    def _cents_to_pct(cents):
        """Kalshi price in cents IS the implied chance in %, so this is a guarded
        passthrough. Live overlays plot in implied % to match the seed series."""
        if cents is None or not (0 < cents < 100):
            return None
        return float(cents)

    def _make_overlay_set(self, line_color, dash=None):
        """Create one series' persistent overlay items (line + spread band + candle
        bodies/wicks), add them to the plot, and return them as a dict. `dash` (a
        Qt.PenStyle) distinguishes outcomes within a venue's hue; the candle wick
        carries the venue hue so overlapping candle series stay identifiable."""
        r, g, b = line_color
        pen = pg.mkPen(line_color, width=2)
        if dash is not None:
            pen.setStyle(dash)
        line = pg.PlotDataItem([], [], pen=pen)
        band_lo = pg.PlotDataItem([], [], pen=pg.mkPen((r, g, b, 90), width=1))
        band_hi = pg.PlotDataItem([], [], pen=pg.mkPen((r, g, b, 90), width=1))
        band_fill = pg.FillBetweenItem(band_lo, band_hi, brush=pg.mkBrush(r, g, b, 40))
        cwicks = pg.PlotDataItem([], [], connect='finite',
                                 pen=pg.mkPen(r, g, b, 220))
        cbodies = CandleBodyItem(x=[], width=[], y0=[], height=[])
        # z-order: band (back) -> line -> candles (front)
        for it in (band_lo, band_hi, band_fill, line, cwicks, cbodies):
            self.plot_widget.addItem(it)
        return {'line': line, 'band_lo': band_lo, 'band_hi': band_hi,
                'band_fill': band_fill, 'cwicks': cwicks, 'cbodies': cbodies}

    def _overlays_built(self):
        """True once every active live series has its overlay items created."""
        return bool(self.live_series) and all(s.get('items') for s in self.live_series)

    def _remove_overlay_items(self):
        """Remove all series overlay items from the plot (used before a rebuild)."""
        for s in self.live_series:
            it = s.get('items')
            if not it:
                continue
            for obj in it.values():
                try:
                    self.plot_widget.removeItem(obj)
                except Exception:
                    pass
            s['items'] = None

    def _clear_plotted_series(self):
        """Wipe the previous market's plotted line/point series + hover registry
        from the canvas, then re-add the scene chrome (crosshair, start/post markers,
        heat image) that plot_widget.clear() drops. Used on the paths where the heavy
        update_plot — which normally does exactly this via clear() — does NOT run:
        futures (no historical fetch) and live markets whose historical fetch came
        back empty. Without it a prior event's lines linger under the new live
        overlay. Live overlay items are removed too and rebuild on the next flush."""
        self._remove_overlay_items()
        self.plot_widget.clear()
        self._hover_series = []
        self._hide_hover()
        self._hide_summaries()
        self._ensure_crosshair()
        self._ensure_start_line()
        self._ensure_market_post_line()
        self._ensure_heat_img()

    def _rebuild_live_items(self):
        """(Re)create the persistent overlay items for every active live series and
        add them to the plot. Called on full redraws (after update_plot's clear())
        and on the first tick after the series set changes; mutated in place per
        tick via setData/setOpts in _refresh_live_items."""
        for s in self.live_series:
            s['items'] = self._make_overlay_set(s['hue'], s['dash'])

    def _render_overlay_set(self, ticks, items, xmin=None, xmax=None):
        """Update one series' overlay items (line, spread band, candles) from its
        ticks, CLIPPED to the visible [xmin, xmax] window so the per-frame cost is
        bounded by what's on screen rather than the full (seed + live) history.
        Ticks are time-sorted, so the window is found by bisection."""
        to_pct = self._cents_to_pct
        if xmin is not None and ticks:
            import bisect
            i0 = bisect.bisect_left(ticks, xmin, key=lambda tk: tk['t'])
            i1 = bisect.bisect_right(ticks, xmax, key=lambda tk: tk['t'])
            sub = ticks[i0:i1]
        else:
            sub = ticks

        # --- Line (hidden in candle mode -> skip building its array entirely) ---
        if not self.candle_mode:
            # Cap EACH line to ~500 points: the Python loop here (building lx/ly +
            # per-point _cents_to_pct) and the subsequent curve paint both scale with
            # point count, and run on every pan/zoom frame. A line zoomed out over a
            # full market life can be thousands of ticks; striding to ~500 keeps pan/
            # hover fluid with no visible loss (sub-pixel detail anyway). Independent of
            # series count — even 2 game lines stutter when each is thousands of points.
            line_sub = sub
            if len(sub) > 500:
                stride = len(sub) // 500 + 1
                line_sub = sub[::stride]
            lx, ly = [], []
            for tk in line_sub:
                pct = to_pct(tk['price'])
                if pct is not None:
                    lx.append(tk['t']); ly.append(pct)
            items['line'].setData(lx, ly)
            items['line'].setVisible(len(lx) > 0)
        else:
            items['line'].setVisible(False)

        # --- Spread band (bid/ask in implied %) ---
        show_band = self.show_spread_band
        if show_band:
            bx, b_lo, b_hi = [], [], []
            for tk in sub:
                lo, hi = to_pct(tk['bid']), to_pct(tk['ask'])
                if lo is None or hi is None:
                    continue
                bx.append(tk['t']); b_lo.append(lo); b_hi.append(hi)
            items['band_lo'].setData(bx, b_lo)
            items['band_hi'].setData(bx, b_hi)
        for k in ('band_lo', 'band_hi', 'band_fill'):
            items[k].setVisible(show_band)

        # --- Candles --- (gets the FULL tick list + window: it re-clips on a
        # bucket-aligned boundary so the first visible candle aggregates its
        # complete bucket and carries the true prev-close, keeping candle shapes
        # stable while the window slides instead of morphing at the clip edge.)
        if self.candle_mode:
            self._refresh_live_candles(ticks, to_pct, items['cbodies'], items['cwicks'],
                                       xmin, xmax)
        items['cbodies'].setVisible(self.candle_mode)
        items['cwicks'].setVisible(self.candle_mode)

    def _live_render_window(self):
        """The x-range to render: the follow window when following, else the current
        view range. Returns (xmin, xmax, follow_target_or_None)."""
        last_t = None
        for s in self.live_series:
            if s['ticks'] and (last_t is None or s['ticks'][-1]['t'] > last_t):
                last_t = s['ticks'][-1]['t']
        if self.auto_follow and last_t is not None:
            if self.candle_mode:
                eff = self.candle_bucket_s if self.candle_bucket_s > 0 else 0.25
                window = max(self.live_candle_visible_n * eff, 20)
                right_pad = eff * 2
            else:
                window = self.live_follow_window_s
                right_pad = 5
            # Clamp the follow window to the allowed x-span (maxXRange): early in
            # a market's life the preferred window can exceed it, and a too-wide
            # setXRange gets shrunk about its CENTER — sliding the live edge off
            # screen. Keep the right edge pinned instead.
            if not self._is_future_market:
                xlo, xhi = self._live_x_limits()
                if xhi is not None:
                    window = max(min(window, (xhi - xlo) - right_pad), 30)
            return last_t - window, last_t + right_pad, (last_t, right_pad)
        (vxmin, vxmax), _ = self.plot_widget.getViewBox().viewRange()
        return vxmin, vxmax, None

    def _refresh_live_items(self):
        """Cheap per-tick update: setData on every series' overlay items (clipped to
        the visible window), then apply auto-follow once."""
        if not self._overlays_built():
            return
        xmin, xmax, follow = self._live_render_window()
        # Render a bit beyond the visible edges so a small pan still shows data.
        span = max(xmax - xmin, 1.0)
        rxmin, rxmax = xmin - span * 0.25, xmax + span * 0.25
        for s in self.live_series:
            self._render_overlay_set(s['ticks'], s['items'], rxmin, rxmax)

        # --- Auto-follow: scroll the TIME axis to the newest ticks, then fit the
        # %-axis to the visible ticks OURSELVES (smoothed glide toward the target,
        # instant only for containment). Re-enabling pyqtgraph's y-autorange here
        # re-fitted the axis from item bounds on every ~30fps flush, so any
        # sub-percent bounds wobble rescaled the whole chart — the candle-mode
        # vertical jitter. ---
        # Keep the X bounds tracking the live edge: the right limit must advance
        # with new ticks / wall clock, or follow mode would catch up to the clamp
        # and freeze. setLimits is a cheap state update; it only acts on range set.
        if not self._is_future_market:
            xlo, xhi = self._live_x_limits()
            if xhi is not None:
                self.plot_widget.getViewBox().setLimits(
                    xMin=xlo, xMax=xhi, maxXRange=xhi - xlo)

        if follow is not None:
            last_t, right_pad = follow
            vb = self.plot_widget.getViewBox()
            vb.setXRange(xmin, xmax, padding=0)
            self._fit_live_y(xmin, xmax)
        else:
            # Manual view: the x-window is frozen, but keep the %-axis fitted to
            # what's visible (incl. the 0-100 overzoom blend) as new ticks land;
            # the smoothed glide damps the per-flush wiggle.
            self._fit_live_y(xmin, xmax)

        # Repaint the liquidity heatmap from the columns inside the new window.
        if self.heatmap_mode:
            self._refresh_heatmap()

    # How far past the data span the x-view must zoom out before the %-axis
    # reaches the full 0-100 probability box (3.0 == view spans 3x the data).
    _Y_OVERZOOM_FULL = 3.0

    def _fit_live_y(self, xmin, xmax, smooth=True):
        """Own the %-axis: fit it to the data inside the visible [xmin, xmax].

        Two regimes, both pure functions of the x-window (so zooming is
        reversible):
        - view within the data span -> fit to the visible values, padded;
        - view zoomed OUT past the data span -> blend the fitted range toward
          the full 0-100% probability box, reaching it at ~3x the data span.
          The old autorange capped the y-axis at the series' peak/trough no
          matter how far out you zoomed; this lets the wheel keep going to the
          whole probability range and come back in to a data-fitted view.

        smooth=True (per-flush ticks): glide the current range toward the target
        by a fixed fraction per flush (~30fps exponential ease, like animated
        rescaling in mainstream charting libs), expanding instantly only as far
        as needed to keep visible data on screen. The earlier design snapped the
        whole range once a hysteresis threshold tripped, which read as a drastic
        interval jump and momentarily distorted the candles. smooth=False (user
        zoom/pan): apply the target directly so the view tracks the wheel
        deterministically.
        """
        to_pct = self._cents_to_pct
        lo = hi = None
        t_first = t_last = None
        for s in self.live_series:
            ticks = s['ticks']
            if not ticks:
                continue
            if t_first is None or ticks[0]['t'] < t_first:
                t_first = ticks[0]['t']
            if t_last is None or ticks[-1]['t'] > t_last:
                t_last = ticks[-1]['t']
            i0 = bisect.bisect_left(ticks, xmin, key=lambda tk: tk['t'])
            i1 = bisect.bisect_right(ticks, xmax, key=lambda tk: tk['t'])
            sub = ticks[i0:i1]
            # Bound the scan: stride dense windows to ~2000 samples (a missed
            # 1-tick extreme is sub-pixel at that density) but always keep the
            # newest tick so the live edge is never cut off.
            if len(sub) > 2000:
                stride = len(sub) // 2000 + 1
                sub = sub[::stride] + [ticks[i1 - 1]]
            include_band = self.show_spread_band
            for tk in sub:
                vals = ((tk['price'], tk['bid'], tk['ask']) if include_band
                        else (tk['price'],))
                for v in vals:
                    p = to_pct(v)
                    if p is None:
                        continue
                    if lo is None or p < lo:
                        lo = p
                    if hi is None or p > hi:
                        hi = p
        # Static sportsbook lines (drawn by update_plot, registered for hover)
        # count toward the fit too — else the fit to K/PM ticks alone could clip
        # them off the top/bottom of the view.
        for s in getattr(self, '_hover_series', []):
            ts, pct = s.get('ts'), s.get('pct')
            if ts is None or getattr(ts, 'size', 0) == 0:
                continue
            i0 = int(np.searchsorted(ts, xmin))
            i1 = int(np.searchsorted(ts, xmax, side='right'))
            if i1 <= i0:
                continue
            seg = pct[i0:i1]
            seg = seg[np.isfinite(seg)]
            if seg.size == 0:
                continue
            slo, shi = float(seg.min()), float(seg.max())
            lo = slo if lo is None else min(lo, slo)
            hi = shi if hi is None else max(hi, shi)
        if lo is None:
            return
        pad = max(hi - lo, 0.5) * 0.10
        tlo, thi = max(lo - pad, 0.0), min(hi + pad, 100.0)

        # Overzoom blend toward the full probability box. With the x-view clamped
        # to the market life on BOTH sides (game markets), full 0-100 is reached
        # exactly when the view fills the ALLOWED x-range — the y-axis absorbs
        # the zoom-out the confined x can't, in that small horizontal space.
        # Unbounded x (no limits set) falls back to the fixed 3x-data-span ramp.
        vb = self.plot_widget.getViewBox()
        if t_first is not None and t_last is not None:
            ds = max(t_last - t_first, 60.0)
            xlim = vb.state['limits']['xLimits']
            # pyqtgraph stores "no limit" as ±1e307 (not None) — treat as unbounded.
            if all(v is not None and abs(v) < 1e306 for v in xlim):
                max_vs = xlim[1] - xlim[0]
            else:
                max_vs = ds * self._Y_OVERZOOM_FULL
            f = ((xmax - xmin) - ds) / max(max_vs - ds, 1e-9)
            if f > 0:
                f = min(f, 1.0)
                tlo, thi = tlo * (1.0 - f), thi + (100.0 - thi) * f

        # This method owns the y-range outright — leaving pyqtgraph's autorange
        # armed alongside it would refit from item bounds per flush anyway.
        vb.enableAutoRange(axis='y', enable=False)
        if not smooth:
            vb.setYRange(tlo, thi, padding=0)
            return
        cur_lo, cur_hi = vb.viewRange()[1]
        # Exponential glide: step a fixed fraction of the remaining distance each
        # flush (~0.18 @ 30fps ≈ 150ms time constant) so rescaling reads as one
        # continuous motion instead of a snap.
        ALPHA = 0.18
        new_lo = cur_lo + (tlo - cur_lo) * ALPHA
        new_hi = cur_hi + (thi - cur_hi) * ALPHA
        # Containment: while gliding, never leave visible data outside the view —
        # jump exactly as far as the data edge, and let the padding ease in after.
        if lo < new_lo:
            new_lo = lo
        if hi > new_hi:
            new_hi = hi
        # Deadband: skip sub-0.1%-of-span moves — ends the glide near the target
        # instead of asymptotically re-painting forever.
        cur_span = max(cur_hi - cur_lo, 1e-9)
        if (abs(new_lo - cur_lo) < cur_span * 0.001
                and abs(new_hi - cur_hi) < cur_span * 0.001):
            return
        vb.setYRange(new_lo, new_hi, padding=0)

    # Adaptive-coarsening bucket ladder (seconds). Snapping the effective bucket
    # to fixed steps keeps candle widths/edges STABLE while zooming — a raw
    # span/N bucket changes continuously with every wheel notch and pan frame,
    # which made every candle morph (re-bucketed) on each zoom step.
    _CANDLE_BUCKET_LADDER = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900,
                             1800, 3600, 7200, 14400, 43200, 86400)

    def _refresh_live_candles(self, ticks, to_pct, cbodies, cwicks,
                              wxmin=None, wxmax=None):
        """Aggregate ticks into OHLC (implied %) and push into the candle items.

        Candles are drawn in implied-% space, which is linear, so bodies don't
        distort near 50% the way they did on the american-odds axis.

        Receives the FULL tick list plus the render window [wxmin, wxmax] and
        clips internally on a BUCKET-ALIGNED left edge: the first visible candle
        always aggregates its complete bucket, and its open carries over from the
        tick just before the window. Clipping candles mid-bucket (the old way)
        re-shaped the edge candle and re-seeded the prev-close chain on every
        pan/follow step, so candle bodies flickered as the window slid.
        """
        bucket = self.candle_bucket_s
        # Adaptive coarsening: cap the rendered candle count so a zoomed-out
        # full-history view doesn't draw thousands of sub-pixel bars. Based on
        # the WINDOW span (stable under panning), snapped to the ladder.
        if wxmin is not None and wxmax is not None and wxmax > wxmin:
            MAX_CANDLES = 700
            eff = (wxmax - wxmin) / MAX_CANDLES
            if eff > (bucket if bucket > 0 else 0.25):
                bucket = next((s for s in self._CANDLE_BUCKET_LADDER if s >= eff),
                              None)
                if bucket is None:  # beyond the ladder: whole days
                    bucket = math.ceil(eff / 86400.0) * 86400.0

        # Bucket-aligned clip + previous-close carry-in.
        eff_bucket = bucket if bucket > 0 else 0.25
        prev_close = None
        if wxmin is not None and ticks:
            import bisect
            t0 = math.floor(wxmin / eff_bucket) * eff_bucket
            i0 = bisect.bisect_left(ticks, t0, key=lambda tk: tk['t'])
            i1 = bisect.bisect_right(ticks, wxmax, key=lambda tk: tk['t'])
            if i0 > 0:
                prev_close = to_pct(ticks[i0 - 1]['price'])
            ticks = ticks[i0:i1]

        groups, order = {}, []
        for tk in ticks:
            pct = to_pct(tk['price'])
            if pct is None:
                continue
            key = tk['t'] if bucket <= 0 else int(tk['t'] // bucket) * bucket
            if key not in groups:
                groups[key] = []; order.append(key)
            groups[key].append(pct)
        if not order:
            cbodies.setOpts(x=[], width=[], y0=[], height=[])
            cwicks.setData([], [])
            return

        # Minimum body height (doji): scale to the visible y-span so a flat bar
        # stays a thin sliver at every zoom level. The old flat 0.5% became a
        # huge slab once the y-axis was fitted to a quiet 2-3% band.
        yr = self.plot_widget.getViewBox().viewRange()[1]
        yspan = yr[1] - yr[0]
        min_h = max(yspan * 0.006, 0.02) if yspan > 0 else 0.1

        # Width tracks the bucket (Max -> tick cadence ~0.25s), filling ~88% of the
        # slot so neighbouring candles read as distinct but chunky.
        width = eff_bucket * 0.88
        centers, body_y0, body_h, brushes = [], [], [], []
        wick_x, wick_y = [], []
        up, down = (80, 200, 120), (220, 90, 90)
        # Open each candle at the PREVIOUS candle's close (continuous candles), so
        # the body color reflects the bar-to-bar directional move. With true
        # intra-bar opens, these markets often sit flat within a minute
        # (open==close -> all green) even while the series steps DOWN between bars;
        # prev-close opens make declines render red as expected. The wick still
        # spans the intra-bar high/low (extended to include the open).
        for key in order:
            vals = groups[key]
            c = vals[-1]
            o = prev_close if prev_close is not None else vals[0]
            hi = max(max(vals), o)
            lo = min(min(vals), o)
            cx = key + (eff_bucket / 2.0 if bucket > 0 else 0)
            centers.append(cx)
            body_y0.append(min(o, c))
            body_h.append(max(abs(c - o), min_h))
            brushes.append(pg.mkBrush(*(up if c >= o else down)))
            wick_x += [cx, cx, float('nan')]
            wick_y += [lo, hi, float('nan')]
            prev_close = c
        cbodies.setOpts(x=centers, width=width, y0=body_y0,
                        height=body_h, brushes=brushes,
                        pen=pg.mkPen(0, 0, 0, 60))
        cwicks.setData(wick_x, wick_y)

    # ---- Liquidity heatmap -------------------------------------------------
    def _ensure_heat_img(self):
        """Create the heatmap ImageItem if needed and (re)add it to the scene —
        like _ensure_crosshair, it must be re-added after plot_widget.clear()."""
        if self._heat_img is None:
            self._heat_img = pg.ImageItem()
            # col-major: array axis0 -> x. We pass (n_time_cols, 100), so axis0 is
            # time (x) and axis1 is price cent (y), matching setRect in _refresh.
            self._heat_img.setOpts(axisOrder='col-major')
            self._heat_img.setZValue(-100)  # behind candles, band, lines, crosshair
            self._heat_img.setLookupTable(self._heat_lut)
        self._heat_img.setVisible(self.heatmap_mode)
        self.plot_widget.addItem(self._heat_img, ignoreBounds=True)

    @staticmethod
    def _build_heat_lut():
        """256-entry RGBA lookup table: a phosphor ramp from transparent (empty
        book) through faint teal -> teal -> amber -> hot near-white (deepest
        resting size). Teal/amber keep it in the same family as the venue hues and
        the UI accent, and the transparent low end lets the black screen and the
        candles read straight through thin parts of the book."""
        stops = np.array([0.0, 0.12, 0.40, 0.72, 1.0])
        cols = np.array([
            [0,   0,   0,   0],     # empty -> fully transparent
            [22,  64,  62,  70],    # faint teal
            [72,  197, 190, 150],   # teal
            [224, 176, 80,  210],   # amber (UI accent)
            [255, 245, 215, 255],   # hot near-white
        ], dtype=float)
        xs = np.linspace(0.0, 1.0, 256)
        lut = np.zeros((256, 4), dtype=np.ubyte)
        for ch in range(4):
            lut[:, ch] = np.interp(xs, stops, cols[:, ch]).astype(np.ubyte)
        return lut

    def _heat_focus_series(self):
        """Series whose depth feeds the heatmap: the primary outcome, including
        every venue quoting it (their cent prices share the 0-100 scale, so depth
        sums cleanly). Restricting to ONE outcome avoids smearing complementary
        sides (e.g. 72c favourite vs 28c underdog) into one ribbon."""
        if not self.live_series:
            return []
        focus = self.live_series[0].get('outcome')
        return [s for s in self.live_series if s.get('outcome') == focus]

    def _sample_heat_column(self, now_t):
        """Snapshot the focus book into one per-cent depth column, throttled to
        ~_heat_sample_dt so the ribbon advances at a steady cadence regardless of
        tick rate. Bids and asks both contribute resting size at their price."""
        if now_t - self._heat_last_sample_t < self._heat_sample_dt:
            return
        series = self._heat_focus_series()
        if not series:
            return
        col = np.zeros(100, dtype=np.float32)
        got = False
        for s in series:
            lad = self._series_ladder(s, 80)
            if not lad:
                continue
            for price, qty in lad.get('bids', []) + lad.get('asks', []):
                b = int(price)
                if 0 <= b < 100:
                    col[b] += float(qty); got = True
        if not got:
            return
        self._heat_cols.append((now_t, col))
        self._heat_last_sample_t = now_t

    def _refresh_heatmap(self):
        """Rebuild the heat ImageItem from the columns inside the visible window.
        Columns map uniformly across [t0, t1] in x and 0-100c in y; contrast is
        scaled to the 97th percentile of nonzero depth so one giant wall can't wash
        the rest of the book to black."""
        if not self.heatmap_mode or self._heat_img is None:
            return
        if len(self._heat_cols) < 2:
            self._heat_img.setVisible(False)
            return
        xmin, _xmax, _ = self._live_render_window()
        cols = list(self._heat_cols)
        import bisect
        i0 = bisect.bisect_left([t for t, _ in cols], xmin - 5.0)
        sub = cols[i0:]
        if len(sub) < 2:
            self._heat_img.setVisible(False)
            return
        t0, t1 = sub[0][0], sub[-1][0]
        arr = np.stack([c for _, c in sub], axis=0)  # (n_cols, 100) == (x, y)
        pos = arr[arr > 0]
        if pos.size == 0:
            self._heat_img.setVisible(False)
            return
        vmax = float(np.quantile(pos, 0.97)) or float(pos.max())
        self._heat_img.setVisible(True)
        self._heat_img.setImage(arr, levels=(0.0, max(vmax, 1.0)),
                                autoLevels=False, lut=self._heat_lut)
        self._heat_img.setRect(QRectF(t0, 0.0, max(t1 - t0, 1.0), 100.0))

    def _latest_value_by_team(self, plot_data):
        """Collapse plot_data to {canonical_team: (display_name, latest_value)}.

        Aggregates across bookmakers, keeping the most recent numeric American
        value per team. Only outcomes that resolve to a known team are kept, so
        non-team markets (totals/spreads) naturally yield <2 teams.
        """
        sport = getattr(self.current_unified_event, 'sport', None)
        latest = {}  # canonical -> (display_name, timestamp, value)
        for outcomes in plot_data.values():
            for (name, _desc), data in outcomes.items():
                if not name:
                    continue
                canonical = EventMatcher.normalize_team_name(name, sport)
                if canonical not in LOGO_FILE_BY_TEAM.get((sport or '').upper(), {}):
                    continue
                ts = data.get('timestamps') or []
                vals = data.get('american_prices') or []
                if not ts or not vals:
                    continue
                try:
                    value = float(str(vals[-1]).replace('+', ''))
                except (ValueError, TypeError):
                    continue
                prev = latest.get(canonical)
                if prev is None or ts[-1] >= prev[1]:
                    latest[canonical] = (name, ts[-1], value)
        return {c: (n, v) for c, (n, _t, v) in latest.items()}

    def _update_side_logos(self, plot_data):
        """Show each team's logo in its band: top = higher implied % (favorite),
        bottom = lower implied % (underdog) — matching the %-axis where high sits
        on top and the corner summary boxes (>=50% -> top).

        Falls back to a static top/bottom split for neutral (pick'em) markets.
        Resolution is cached, so the only per-call cost is two setPixmap/move.
        """
        sport = getattr(self.current_unified_event, 'sport', None)
        teams = self._latest_value_by_team(plot_data)
        if len(teams) != 2:
            self._hide_side_logos()
            return

        (ca, (na, va)), (cb, (nb, vb)) = teams.items()
        # va/vb are AMERICAN odds. Compare by IMPLIED % so the higher-probability
        # team lands on top (the line plots high in %-space). Earlier this keyed
        # on raw american ordering, which put the underdog on top — wrong since
        # the axis flipped from american odds to implied %.
        pa, pb = american_to_implied_pct(va), american_to_implied_pct(vb)
        if pa is not None and pb is not None and abs(pa - pb) > 1e-9:
            top_team, bottom_team = (na, nb) if pa > pb else (nb, na)
        else:
            top_team, bottom_team = (na, nb) if na <= nb else (nb, na)

        pm_top = resolve_team_logo(top_team, sport, self._logo_size)
        pm_bottom = resolve_team_logo(bottom_team, sport, self._logo_size)

        self._apply_logo(self.logo_label_top, pm_top)
        self._apply_logo(self.logo_label_bottom, pm_bottom)
        self._position_side_logos()

    def _apply_logo(self, label, pixmap):
        if pixmap is None:
            label.hide()
            return
        label.setPixmap(pixmap)
        label.resize(pixmap.size())
        label.show()

    def _toggle_controls(self):
        """Show/hide the floating control strip for a fully clean chart."""
        self._controls_visible = not getattr(self, '_controls_visible', True)
        self.control_overlay.setVisible(self._controls_visible)
        self.controls_toggle_btn.setText("▴" if self._controls_visible else "▾")
        self.controls_toggle_btn.setToolTip(
            "Hide controls" if self._controls_visible else "Show controls")
        self._position_overlays()

    def _position_overlays(self):
        """Pin the floating control strip + toggle, the corner logos, and dock
        each summary just left of its logo. All widget-space, so they stay clear
        of the graph lines. The top logo/summary drop below the strip when it's
        shown so nothing stacks on the controls.
        """
        margin, gap = 10, 8
        w = self.plot_widget.width()
        h = self.plot_widget.height()

        # --- Floating control strip + its toggle (top-CENTER, HUGGING its content
        # so it never spans the graph width). The toggle sits just right of the
        # strip when shown, or centered alone when the strip is hidden. ---
        btn = getattr(self, 'controls_toggle_btn', None)
        ov = getattr(self, 'control_overlay', None)
        if ov is not None and getattr(self, '_controls_visible', False):
            # market_info only in TheOddsAPI mode (event picker hidden); in
            # prediction mode the Event dropdown already shows the matchup.
            if getattr(self, 'market_info', None) is not None:
                self.market_info.setVisible(not self.event_selector.isVisibleTo(ov))
            ov.adjustSize()           # shrink to fit current (mode-dependent) content
            sx = max(6, (w - ov.width()) // 2)
            ov.move(sx, 6)
            ov.raise_()
            if btn is not None:
                btn.move(sx + ov.width() + 3, 6 + max(0, (ov.height() - btn.height()) // 2))
                btn.raise_()
        elif btn is not None:
            btn.move((w - btn.width()) // 2, 6)
            btn.raise_()

        # Bottom of the plotting area = above the x-axis, so the bottom logo /
        # summary box don't sit on top of the time-axis labels.
        axis_h = 0
        bax = self.plot_widget.getAxis('bottom')
        if bax is not None and bax.isVisible():
            axis_h = bax.height()
        # getAxis().height() is a float (GraphicsWidget) -> keep an int for moves.
        data_bottom = int(h - axis_h)

        # Top logo/summary stay top-right; bottom ones dock just ABOVE the x-axis.
        logo_top_left = w - margin
        if self.logo_label_top.isVisible() and self.logo_label_top.pixmap():
            pm = self.logo_label_top.pixmap()
            logo_top_left = w - pm.width() - margin
            self.logo_label_top.move(logo_top_left, margin)
        logo_bot_left = w - margin
        if self.logo_label_bottom.isVisible() and self.logo_label_bottom.pixmap():
            pm = self.logo_label_bottom.pixmap()
            logo_bot_left = w - pm.width() - margin
            self.logo_label_bottom.move(logo_bot_left, data_bottom - pm.height() - margin)

        if self.summary_label_top.isVisible():
            s = self.summary_label_top
            s.move(logo_top_left - gap - s.width(), margin)
        if self.summary_label_bottom.isVisible():
            s = self.summary_label_bottom
            s.move(logo_bot_left - gap - s.width(), data_bottom - s.height() - margin)


    def _update_countdown(self):
        """1s tick: refresh the countdown segment of the x-axis legend. Counts down
        pre-game ('⏱ 2:14:08 to start'); after start flips to 'LIVE +H:MM:SS'.
        Lives in the axis label alongside the Market Post / Event Start swatches
        (see _rebuild_time_legend); only rebuilds the label when the text changes."""
        epoch = self._event_start_epoch()
        html = None
        if epoch is not None:
            from datetime import datetime as _dt
            # _event_start_epoch() returns a POSIX timestamp; datetime.now().timestamp()
            # is the matching POSIX 'now', so the difference is correct regardless of tz.
            rem = epoch - _dt.now().timestamp()

            def _hms(sec):
                sec = int(abs(sec)); h, r = divmod(sec, 3600); m, s = divmod(r, 60)
                return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

            if rem > 0:
                html = (f"<span style='color:#7e8794'>&#9201; </span>"
                        f"<span style='color:#e0b050;font-weight:bold'>{_hms(rem)}</span>"
                        f"<span style='color:#7e8794'> to start</span>")
            else:
                html = (f"<span style='color:#5bd075;font-weight:bold'>&#9679; LIVE</span>"
                        f"<span style='color:#7e8794'> +{_hms(rem)}</span>")
        if html != self._countdown_html:
            self._countdown_html = html
            self._rebuild_time_legend()

    # Back-compat alias: older call sites used the logo-only name.
    _position_side_logos = _position_overlays

    def _hide_side_logos(self):
        self.logo_label_top.hide()
        self.logo_label_bottom.hide()

    def _hide_summaries(self):
        self.summary_label_top.hide()
        self.summary_label_bottom.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the control strip, watermarks + summaries pinned when the widget
        # (and plot) resizes.
        self._position_overlays()

    def showEvent(self, event):
        super().showEvent(event)
        # First reveal (the widget starts hidden) may not emit a resize, so pin
        # the floating overlays now that real geometry exists.
        self._position_overlays()
        # Live render is skipped while hidden (ticks still accumulate); on re-show,
        # render immediately so the chart reflects everything that arrived rather
        # than waiting for the next live tick.
        if getattr(self, 'live_mode', False) and self.live_series:
            if not self._overlays_built():
                self._rebuild_live_items()
            self._refresh_live_items()

    async def _organize_plot_data(self, snapshots):
        """Organize snapshot data using American odds directly"""
        plot_data = {}

        for snapshot in snapshots:
            timestamp = datetime.fromisoformat(snapshot['timestamp'].replace('Z', '')).timestamp()

            for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
                bm_key = bookmaker['key']

                if bm_key not in plot_data:
                    plot_data[bm_key] = {}

                for market in bookmaker.get('markets', []):
                    for outcome in market.get('outcomes', []):
                        outcome_key = (outcome.get('name'), outcome.get('description', ''))

                        if outcome_key not in plot_data[bm_key]:
                            plot_data[bm_key][outcome_key] = {
                                'timestamps': [],
                                'american_prices': [],
                                'points': []
                            }

                        # Add timestamp
                        plot_data[bm_key][outcome_key]['timestamps'].append(timestamp)

                        # Add American price exactly as it comes from the API
                        if 'price' in outcome:
                            american_price = outcome['price']
                            # Format with sign if it's a number
                            if isinstance(american_price, (int, float)):
                                if american_price > 0:
                                    american_price = f"+{american_price}"
                                else:
                                    american_price = f"{american_price}"
                            plot_data[bm_key][outcome_key]['american_prices'].append(american_price)
                        else:
                            plot_data[bm_key][outcome_key]['american_prices'].append(None)

                        # Add point if available
                        if 'point' in outcome:
                            plot_data[bm_key][outcome_key]['points'].append(outcome['point'])
                        else:
                            plot_data[bm_key][outcome_key]['points'].append(None)

        # Clean up data structure - remove None values
        for bm_key, outcomes in plot_data.items():
            for outcome_key, data in outcomes.items():
                # Keep only entries with valid American prices
                valid_indices = []
                for i, price in enumerate(data['american_prices']):
                    if price is not None:
                        valid_indices.append(i)

                if valid_indices:
                    data['timestamps'] = [data['timestamps'][i] for i in valid_indices]
                    data['american_prices'] = [data['american_prices'][i] for i in valid_indices]

                    # Only keep points that have corresponding valid prices
                    if 'points' in data:
                        data['points'] = [
                            data['points'][i] if i < len(data['points']) else None
                            for i in valid_indices
                        ]

        return plot_data

    async def _plot_outcome_series(self, bookmaker, outcome_key, points_data, color):
        """Plot a single outcome series using American odds directly"""
        if not points_data['timestamps'] or len(points_data['timestamps']) == 0:
            return

        timestamps = np.array(points_data['timestamps'])

        # Check if we have price data
        if 'american_prices' in points_data and points_data['american_prices'] and len(points_data['american_prices']) > 0:
            american_prices = np.array(points_data['american_prices'])

            # Convert to numeric values for plotting
            american_values = []
            for price in american_prices:
                try:
                    if isinstance(price, str) and price.startswith('+'):
                        american_values.append(float(price[1:]))
                    elif isinstance(price, str) and price.startswith('-'):
                        american_values.append(float(price))
                    else:
                        american_values.append(float(price))
                except (ValueError, TypeError):
                    # Default to a safe value if conversion fails
                    american_values.append(-110.0)

            american_values = np.array(american_values)

            # Primary y-axis is implied chance (%). American odds are carried
            # alongside (same index) only for parenthetical label text.
            implied_values = np.array(
                [american_to_implied_pct(v) if v is not None else np.nan
                 for v in american_values], dtype=float)

            name = f"{bookmaker} - {outcome_key[0]}"
            if outcome_key[1]:
                name += f" ({outcome_key[1]})"

            # Plot the implied-% line; american odds never touch the axis.
            line = self.plot_widget.plot(
                timestamps,
                implied_values,
                pen=pg.mkPen(color=color, width=2),
                name=name,
                symbol='o',
                symbolSize=6,
                symbolBrush=color,
                connect='finite',
            )

            # Register this series for the crosshair hover readout (value at the
            # cursor's time). Skipped for the dashed points-only path below.
            self._hover_series.append({
                'name': outcome_key[0],
                'bookmaker': bookmaker,
                'color': color,
                'ts': np.asarray(timestamps, dtype=float),
                'pct': np.asarray(implied_values, dtype=float),
                'am': np.asarray(american_values, dtype=float),
            })

            # Labels are deliberately sparse now: the per-change swarm is gone.
            # _draw_series_annotations places peak / trough / last labels only
            # (in implied %, with American odds in parens), plus the corner badge.
            self._draw_series_annotations(
                timestamps, implied_values, american_values, color,
                outcome_key[0], bookmaker)

        # If we only have points data (no prices), plot those instead
        elif 'points' in points_data and points_data['points'] and len(points_data['points']) > 0:
            points = np.array(points_data['points'])
            name = f"{bookmaker} - {outcome_key[0]} (Points)"
            if outcome_key[1]:
                name += f" ({outcome_key[1]})"

            line = self.plot_widget.plot(
                timestamps,
                points,
                pen=pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine),
                name=name,
                symbol='s',
                symbolSize=6,
                symbolBrush=color
            )

    # Minimum swing (in implied-% points) before peak/trough markers + labels
    # are drawn. Below this a band is treated as flat and gets no markers, which
    # suppresses minute-level jitter and dead-quiet markets.
    PEAK_TROUGH_MIN_SWING = 3.0

    def _draw_series_annotations(self, timestamps, implied_values, american_values,
                                 color, outcome_name, bookmaker='kalshi'):
        """Draw peak/trough marker triangles, a color-coded last-value tag at the
        series end, and a fixed corner net-move summary badge — per BAND.

        No on-chart text except the end tag (fill = series color, 'NN% (+am)').
        Deduped per band so kalshi/poly series of the same side don't double up.
        Safe to no-op on short/degenerate series.
        """
        try:
            ts = np.asarray(timestamps, dtype=float)
            vals = np.asarray(implied_values, dtype=float)
            am = np.asarray(american_values, dtype=float)
        except (ValueError, TypeError):
            return
        # Drop NaNs (unusable conversions) keeping ts/vals/am aligned.
        finite = np.isfinite(ts) & np.isfinite(vals)
        ts, vals, am = ts[finite], vals[finite], am[finite]
        n = vals.size
        if n < 2:
            return

        # Dedup by BAND: in implied-% space the favorite sits HIGH (>=50%) and
        # the underdog LOW (<50%), so the side relative to 50% is the stable
        # per-band key. 'pos' -> top corner overlay, 'neg' -> bottom.
        cur_v = float(vals[-1])
        band_key = 'pos' if cur_v >= 50.0 else 'neg'
        bands = getattr(self, '_annotated_bands', set())
        if band_key in bands:
            return
        bands.add(band_key)

        # --- Net move since open (density-agnostic: first vs last) ---
        open_v = float(vals[0])
        delta = cur_v - open_v
        if delta > 0:
            arrow, dcolor = '▲', (80, 200, 120)
        elif delta < 0:
            arrow, dcolor = '▼', (220, 90, 90)
        else:
            arrow, dcolor = '■', (170, 170, 170)

        # --- Peak/trough (noise-gated, smoothed) ---
        win = max(1, n // 50)
        smoothed = np.convolve(vals, np.ones(win) / win, mode='same') if win > 1 else vals

        def _snap(center, want_max):
            lo = max(0, center - win)
            hi = min(n, center + win + 1)
            seg = vals[lo:hi]
            return lo + (int(np.argmax(seg)) if want_max else int(np.argmin(seg)))

        hi_i = _snap(int(np.argmax(smoothed)), True)
        lo_i = _snap(int(np.argmin(smoothed)), False)
        peak_v, trough_v = float(vals[hi_i]), float(vals[lo_i])
        swing = peak_v - trough_v

        if swing >= self.PEAK_TROUGH_MIN_SWING:
            # Triangles mark WHERE the extremes are; labels sit just off the line
            # (peak above, trough below) so they don't overlap the curve.
            markers = pg.ScatterPlotItem(
                x=[float(ts[hi_i]), float(ts[lo_i])],
                y=[peak_v, trough_v],
                symbol=['t1', 't'],  # up-triangle = peak, down-triangle = trough
                size=12,
                brush=pg.mkBrush(color),
                pen=pg.mkPen('w', width=1),
            )
            self.plot_widget.addItem(markers)

        # Summary -> fixed corner overlay (top-right for the high-% band,
        # bottom-right for the low-% band). Widget-space, can't collide with graph.
        # The current value ("NN% (+am)") is the bold header line, color-coded to
        # the series; net move / range / updated follow underneath.
        val_txt = format_pct_with_american(am[-1] if am.size else None, cur_v) or f"{cur_v:.0f}%"
        chex = '#%02x%02x%02x' % (int(color[0]), int(color[1]), int(color[2]))
        updated = datetime.fromtimestamp(float(ts[-1])).strftime('%H:%M:%S')
        dhex = '#%02x%02x%02x' % dcolor
        summary_html = (
            f"<div style='font-size:11pt;color:{chex};font-weight:bold'>{val_txt}</div>"
            f"<div style='font-size:9pt;color:#c8c8c8'>{open_v:.0f}%&#8594;{cur_v:.0f}% "
            f"<span style='color:{dhex}'>{arrow}{abs(delta):.0f}</span> "
            f"<span style='color:#8a93a0'>· rng {swing:.0f}%</span></div>"
            f"<div style='font-size:7pt;color:#7e8794'>updated {updated}</div>"
        )
        self._set_summary_label(band_key, summary_html)

    def _set_summary_label(self, band_key, html):
        label = self.summary_label_top if band_key == 'pos' else self.summary_label_bottom
        # Centre every line within the box: the box sizes to its WIDEST line (the
        # move/range row), so left-aligned lines left the % row hugging the left edge
        # with dead space to its right. text-align:center balances all rows.
        label.setText(f"<div style='text-align:center'>{html}</div>")
        label.adjustSize()
        label.show()
        self._position_overlays()

    # ---- Crosshair scrubber + hover readout --------------------------------
    def _ensure_crosshair(self):
        """Create the crosshair lines if needed and (re)add them to the scene.

        Called at the end of update_plot, after clear() removed the old ones.
        ignoreBounds keeps the infinite lines out of autorange calculations."""
        if self._cross_v is None:
            pen = pg.mkPen((150, 160, 175, 130), width=1, style=Qt.PenStyle.DashLine)
            self._cross_v = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            self._cross_h = pg.InfiniteLine(angle=0, movable=False, pen=pen)
            self._cross_v.setZValue(200)
            self._cross_h.setZValue(200)
        self._cross_v.hide()
        self._cross_h.hide()
        self.plot_widget.addItem(self._cross_v, ignoreBounds=True)
        self.plot_widget.addItem(self._cross_h, ignoreBounds=True)

    def _event_start_epoch(self):
        """Epoch (UTC) of the selected event's start, from the unified event's
        ISO start_time. None if unknown or date-only (midnight = no real time)."""
        if self._is_future_market:
            return None  # season-long futures have no game start
        ev = getattr(self, 'current_unified_event', None)
        st = getattr(ev, 'start_time', None) if ev else None
        if not st:
            return None
        try:
            s = str(st)
            if s.endswith('Z'):
                s = s.replace('Z', '+00:00')
            elif '+' not in s and s.count(':') >= 2:
                s = s + '+00:00'  # naive ISO from Polymarket is UTC
            dt = datetime.fromisoformat(s)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return None  # date-only (e.g. Kalshi-from-ticker) — no real start
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _kalshi_open_time_for(unified_market, ticker):
        """open_time ISO of the selected Kalshi market in a UnifiedMarket (its
        kalshi_markets hold the raw dicts). Falls back to the first market's
        open_time, else None."""
        mkts = [m for m in (getattr(unified_market, 'kalshi_markets', None) or [])
                if isinstance(m, dict)]
        for m in mkts:
            if m.get('ticker') == ticker and m.get('open_time'):
                return m.get('open_time')
        for m in mkts:
            if m.get('open_time'):
                return m.get('open_time')
        return None

    def _market_open_epoch(self):
        """Epoch (UTC) of the selected market's Kalshi open_time, regardless of market
        kind. Used to bound the history SEED (a futures market opened months ago, so we
        must seed from its real open — distinct from the 'Market Post' LINE, which is
        hidden for futures)."""
        st = getattr(self, 'market_post_iso', None)
        if not st:
            return None
        try:
            s = str(st)
            if s.endswith('Z'):
                s = s.replace('Z', '+00:00')
            elif '+' not in s and s.count(':') >= 2:
                s = s + '+00:00'
            return datetime.fromisoformat(s).timestamp()
        except (ValueError, TypeError):
            return None

    def _market_post_epoch(self):
        """Epoch (UTC) of the 'Market Post' marker line. Same as the open time, except
        hidden (None) for season-long futures where it's meaningless."""
        if self._is_future_market:
            return None
        return self._market_open_epoch()

    def _live_x_limits(self):
        """(xMin, xMax) view limits for a live game market. Left: the Market Post
        (open) time — there is never data before the market was posted. Right: the
        live edge (newest tick / now / the event start, whichever is furthest).
        Both get breathing room (~5% of the market life, min 5 min) so the user
        can scroll slightly past either end without wandering into empty weeks.
        Confining BOTH sides means zooming out quickly runs out of x-room and the
        0-100 y-blend does the rest (see _fit_live_y). (None, None) when no
        anchor time is known. The right limit must be re-applied as ticks arrive
        (see _refresh_live_items) or follow mode would catch up to the clamp."""
        post = self._market_open_epoch()
        first = min((s['ticks'][0]['t'] for s in self.live_series if s['ticks']),
                    default=None)
        known = [v for v in (post, first) if v is not None]
        if not known:
            return None, None
        start = min(known)
        last = max((s['ticks'][-1]['t'] for s in self.live_series if s['ticks']),
                   default=None)
        es = self._event_start_epoch()
        end = max(v for v in (last, es, datetime.now().timestamp()) if v is not None)
        pad = max((end - start) * 0.05, 300)
        return start - pad, end + pad

    def _rebuild_time_legend(self):
        """Set the bottom "Time" axis label with a swatch for each visible marker
        (Market Post, Event Start) plus the start countdown / LIVE-elapsed segment,
        so all the legends coexist instead of clobbering."""
        parts = ["Time"]
        mp = getattr(self, '_market_post_line', None)
        if mp is not None and mp.isVisible():
            hexc = '#%02x%02x%02x' % MARKET_POST_LINE_COLOR
            parts.append(f"<span style='color:{hexc}'>&#9476;&#9476; Market Post</span>")
        sl = getattr(self, '_start_line', None)
        if sl is not None and sl.isVisible():
            hexc = '#%02x%02x%02x' % START_LINE_COLOR
            parts.append(f"<span style='color:{hexc}'>&#9476;&#9476; Event Start</span>")
        if getattr(self, '_countdown_html', None):
            parts.append(self._countdown_html)
        self.plot_widget.setLabel('bottom', "&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))

    def _ensure_start_line(self):
        """Subtle colored dashed vertical marker at the event/game start time.
        Re-added each redraw (clear() drops scene items); ignoreBounds so it
        doesn't affect autorange. Legend handled by _rebuild_time_legend()."""
        if self._start_line is None:
            self._start_line = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen(START_LINE_COLOR + (140,), width=1, style=Qt.PenStyle.DashLine))
            self._start_line.setZValue(55)
        epoch = self._event_start_epoch()
        if epoch is None:
            self._start_line.hide()
            self._rebuild_time_legend()
            return
        self._start_line.setPos(epoch)
        self._start_line.show()
        self.plot_widget.addItem(self._start_line, ignoreBounds=True)
        self._rebuild_time_legend()

    def _ensure_market_post_line(self):
        """Vertical marker at the market's open/post time (Kalshi open_time). Mirror
        of _ensure_start_line; hidden when unknown. Legend via _rebuild_time_legend()."""
        if self._market_post_line is None:
            self._market_post_line = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen(MARKET_POST_LINE_COLOR + (140,), width=1,
                             style=Qt.PenStyle.DashLine))
            self._market_post_line.setZValue(54)
        epoch = self._market_post_epoch()
        if epoch is None:
            self._market_post_line.hide()
            self._rebuild_time_legend()
            return
        self._market_post_line.setPos(epoch)
        self._market_post_line.show()
        self.plot_widget.addItem(self._market_post_line, ignoreBounds=True)
        self._rebuild_time_legend()

    def _hide_hover(self):
        if getattr(self, 'hover_label', None) is not None:
            self.hover_label.hide()
        if getattr(self, '_cross_v', None) is not None:
            self._cross_v.hide()
            self._cross_h.hide()

    def _on_plot_hover(self, pos):
        """Mouse-move handler: position the crosshair and report each series'
        implied % + American odds at the cursor's time (plus the live tape /
        candle OHLC when in Live mode) in a floating tag."""
        if self._cross_v is None:
            return
        vb = self.plot_widget.getViewBox()
        if vb is None or not vb.sceneBoundingRect().contains(pos):
            self._hide_hover()
            return
        mp = vb.mapSceneToView(pos)
        x = float(mp.x())

        rows = []
        for s in self._hover_series:
            ts = s['ts']
            if ts.size == 0:
                continue
            # Skip series whose time span doesn't cover the cursor (+~1 sample of
            # slack). Without this, the live region (past the seed's last point)
            # would show a stale clamped value instead of deferring to the live row.
            pad = float(np.median(np.diff(ts))) if ts.size >= 2 else 0.0
            if x < ts[0] - pad or x > ts[-1] + pad:
                continue
            i = int(np.searchsorted(ts, x))
            if i >= ts.size:
                i = ts.size - 1
            elif i > 0 and abs(ts[i - 1] - x) <= abs(ts[i] - x):
                i -= 1
            pct = s['pct'][i]
            if not np.isfinite(pct):
                continue
            am = s['am'][i]
            c = s['color']
            txt = format_pct_with_american(am if np.isfinite(am) else None, pct)
            # Bookmaker brand logo in place of the old colored dot; fall back to
            # a colored dot for books without a logo asset (paid-OddsAPI books).
            logo = self._bookmaker_logo_path(s.get('bookmaker'))
            if logo:
                marker = (f"<img src='{logo}' width='15' height='15' "
                          f"style='vertical-align:middle'>")
            else:
                chex = '#%02x%02x%02x' % (int(c[0]), int(c[1]), int(c[2]))
                marker = f"<span style='color:{chex}'>&#9679;</span>"
            rows.append(
                f"<div style='font-size:9pt'>{marker} "
                f"<span style='color:#aeb6c0'>{s['name']}</span>&nbsp;&nbsp;"
                f"<span style='color:#f0f0f0'>{txt}</span></div>")

        # Live tape / candle OHLC (only over the live span; defers to seed elsewhere).
        if getattr(self, 'live_mode', False):
            rows.extend(self._live_hover_rows(x))

        if not rows:
            self._hide_hover()
            return

        from datetime import datetime
        tstr = datetime.fromtimestamp(x).strftime('%m/%d %H:%M:%S')
        self.hover_label.setText(
            f"<div style='font-size:8pt;color:#7e8794'>{tstr}</div>" + ''.join(rows))
        self.hover_label.adjustSize()

        self._cross_v.setPos(x)
        self._cross_h.setPos(float(mp.y()))
        self._cross_v.show()
        self._cross_h.show()

        # Position the tag next to the cursor (widget pixels), clamped to the plot.
        vp = self.plot_widget.mapFromScene(pos)
        w, h = self.hover_label.width(), self.hover_label.height()
        pw, ph = self.plot_widget.width(), self.plot_widget.height()
        lx, ly = vp.x() + 14, vp.y() + 12
        if lx + w > pw - 4:
            lx = vp.x() - w - 14
        lx = max(4, min(lx, pw - w - 4))
        ly = max(4, min(ly, ph - h - 4))
        self.hover_label.move(int(lx), int(ly))
        self.hover_label.show()
        self.hover_label.raise_()

    def _hover_row(self, source, ticks, x, label, hue=None):
        """Build one live hover row for a feed source at cursor time x. Branded with
        the venue's logo by default; when `hue` is given (futures, where every line is
        the same venue) the marker + label use that line's colour so the tooltip
        matches the chart. Returns None when the cursor isn't over this feed's span."""
        if not ticks:
            return None
        t0, t1 = ticks[0]['t'], ticks[-1]['t']
        pad = max(2.0, float(self.candle_bucket_s or 1))
        if not (t0 - pad <= x <= t1 + pad):
            return None
        if hue is not None:
            r, g, b = hue
            marker = f"<span style='color:#{r:02x}{g:02x}{b:02x}'>&#9679;</span>"
        else:
            logo = self._bookmaker_logo_path(source)
            r, g, b = self._live_color(source)
            marker = (f"<img src='{logo}' width='15' height='15' style='vertical-align:middle'>"
                      if logo else f"<span style='color:#{r:02x}{g:02x}{b:02x}'>&#9679;</span>")
        if self.candle_mode:
            bucket = self.candle_bucket_s
            if bucket > 0:
                key = int(x // bucket) * bucket
                vals = [tk['price'] for tk in ticks if key <= tk['t'] < key + bucket]
            else:  # "Max" bucket == one candle per tick
                vals = [min(ticks, key=lambda tk: abs(tk['t'] - x))['price']]
            if not vals:
                return None
            o, c = vals[0], vals[-1]
            hi, lo = max(vals), min(vals)
            ccol = '#50c878' if c >= o else '#dc5a5a'
            body = (f"<span style='color:#8a93a0'>O</span> {o}% &nbsp;"
                    f"<span style='color:#8a93a0'>H</span> {hi}% &nbsp;"
                    f"<span style='color:#8a93a0'>L</span> {lo}% &nbsp;"
                    f"<span style='color:{ccol}'>C {c}%</span>")
        else:
            tk = min(ticks, key=lambda t: abs(t['t'] - x))
            pct = tk['price']
            body = (f"<span style='color:#f0f0f0'>"
                    f"{format_pct_with_american(self._cents_to_am(pct), float(pct))}</span>")
        lcol = '#%02x%02x%02x' % (r, g, b)
        return (f"<div style='font-size:9pt'>{marker} "
                f"<span style='color:{lcol}'>{label}</span>&nbsp;&nbsp;{body}</div>")

    def _live_hover_rows(self, x):
        """Hover readout row(s) for the live tape at cursor time x — one row per
        active live series (branded with its venue logo + outcome label). In candle
        mode each reports its hovered candle's O/H/L/C; otherwise its nearest tick."""
        rows = []
        # Cap the futures tooltip: an 'All' tier (~40 lines) builds a giant tooltip on
        # every hover. live_series is price-ranked, so the first N are the favourites.
        series = self.live_series
        capped = self._is_future_market and len(series) > 12
        shown = series[:12] if capped else series
        for s in shown:
            # Full venue name in the tooltip (the compact "K ·"/"PM ·" form is
            # kept for the narrow order-book headers). Futures pass the line hue so
            # each candidate's marker/label matches its chart colour (all are Kalshi,
            # so the venue logo can't distinguish them).
            vlabel = 'Kalshi' if s['venue'] == 'kalshi' else 'Poly'
            hue = s['hue'] if self._is_future_market else None
            row = self._hover_row(s['venue'], s['ticks'], x,
                                  f"{vlabel} · {s['outcome']}", hue=hue)
            if row:
                rows.append(row)
        if capped and rows:
            rows.append(f"<div style='font-size:8pt;color:#7e8794'>"
                        f"+{len(series) - 12} more…</div>")
        return rows

    async def configure_plot_axes(self, snapshots):
        """Configure plot axes optimized for American odds display"""
        if not snapshots:
            return

        # Collect all actual timestamps from the data to find min/max
        all_timestamps = []
        for snapshot in snapshots:
            try:
                ts = datetime.fromisoformat(snapshot['timestamp'].replace('Z', '')).timestamp()
                all_timestamps.append(ts)
            except:
                pass

        if not all_timestamps:
            return

        # Use actual data range for X-axis
        first_time = min(all_timestamps)
        last_time = max(all_timestamps)

        # Add small padding (2%) for visual clarity
        time_range = last_time - first_time
        if time_range > 0:
            padding = time_range * 0.02
        else:
            # If all timestamps are the same, add 1 hour padding on each side
            padding = 3600

        self.plot_widget.setXRange(first_time - padding, last_time + padding)

        # DateAxisItem is already set during initialization, just update the range
        # No need to re-set it here

        # Collect all American prices to determine Y-axis range
        all_american_prices = []

        for snapshot in snapshots:
            for bookmaker in snapshot.get('data', {}).get('bookmakers', []):
                for market in bookmaker.get('markets', []):
                    # For Kalshi, market key is 'h2h', for TheOddsAPI it varies
                    # Just collect all prices regardless of market key match
                    for outcome in market.get('outcomes', []):
                        if 'price' in outcome:
                            american_price = outcome['price']
                            # Convert to numeric for min/max calculations
                            try:
                                if isinstance(american_price, str):
                                    if american_price.startswith('+'):
                                        all_american_prices.append(float(american_price[1:]))
                                    elif american_price.startswith('-'):
                                        all_american_prices.append(float(american_price))
                                    else:
                                        all_american_prices.append(float(american_price))
                                elif isinstance(american_price, (int, float)):
                                    all_american_prices.append(float(american_price))
                            except (ValueError, TypeError):
                                pass  # Skip invalid values

        # Y-axis is implied chance (%). Convert the collected american prices to
        # implied % and fit a padded window, clamped to the natural [0, 100] box —
        # no more ±9900 rail spikes blowing out the scale.
        implied = [p for p in (american_to_implied_pct(a) for a in all_american_prices)
                   if p is not None]
        if implied:
            # The %-axis auto-fits to the visible data (mouse y is disabled), so the
            # candles/lines always fill the vertical space at a readable height and the
            # view stays reversible. Only for the STATIC path — in live mode
            # _fit_live_y owns the y-range (autorange would fight it per flush).
            if not getattr(self, 'live_mode', False):
                self.plot_widget.getViewBox().enableAutoRange(axis='y', enable=True)

            # Y-axis label + ticks: implied % primary, american odds in parens,
            # at round values with fainter minor gridlines. Ticks are (re)built
            # from the visible range here and on every zoom/pan (sigYRangeChanged).
            self.plot_widget.getAxis('left').setLabel('Implied %')
            self._update_y_ticks()

    def _update_y_ticks(self):
        """Rebuild the %-axis ticks from the current visible y-range so zoom/pan
        densifies or coarsens them (mirrors the time axis). Cheap; safe to call
        on every sigYRangeChanged."""
        vb = self.plot_widget.getViewBox()
        if vb is None:
            return
        lo, hi = vb.viewRange()[1]
        if hi <= lo:
            return
        self.plot_widget.getAxis('left').setTicks(self._build_pct_ticks(lo, hi))

    def _build_pct_ticks(self, lo, hi):
        """Round, evenly-spaced %-axis ticks with a fainter minor gridline level.

        Returns [major, minor] for AxisItem.setTicks: major carries 'NN.N% (+am)'
        labels (implied % is a float, so one decimal; two when zoomed in tight) at
        a nice step chosen for ~10 visible ticks, minor draws unlabeled gridlines
        at half that step. Anchored to multiples of the step so lines land on round
        probabilities (55.0%, 55.5%, …) instead of arbitrary values.
        """
        import math
        span = max(hi - lo, 0.2)
        # Aim for ~10 ticks (denser than before) and support sub-1% steps so the
        # axis stays granular when zoomed in. Step list ascends; first that yields
        # <=10 ticks across the span wins.
        steps = (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 25, 50)
        major_step = next((s for s in steps if span / s <= 10), 50)
        minor_step = major_step / 2.0
        dp = 2 if major_step < 0.5 else 1  # decimal places on the % label

        major = []
        v = math.ceil(lo / major_step) * major_step
        while v <= hi + 1e-9:
            am = self._am_from_cents_float(v)
            if am is not None:
                ai = int(round(am))
                label = f"{v:.{dp}f}% ({'+' if ai > 0 else ''}{ai})"
            else:
                label = f"{v:.{dp}f}%"
            major.append((v, label))
            v += major_step

        minor = []
        v = math.ceil(lo / minor_step) * minor_step
        while v <= hi + 1e-9:
            # Skip positions that coincide with a major tick.
            if abs(v / major_step - round(v / major_step)) > 1e-6:
                minor.append((v, ''))
            v += minor_step
        return [major, minor]

    async def _show_no_data_message(self, message="No historical data available"):
        """Show a message when no data is available"""
        self.no_data_text = pg.TextItem(message, anchor=(0.5, 0.5))
        self.plot_widget.addItem(self.no_data_text)
        self.no_data_text.setPos(0.5, 0.5)

    def _american_to_decimal(self, american_odds):
        """Convert American odds to decimal odds with proper handling of extreme values"""
        try:
            if american_odds == 0:
                return 1.0  # Handle zero case

            if american_odds > 0:
                return (american_odds / 100) + 1
            else:
                return (100 / abs(american_odds)) + 1
        except Exception as e:
            print(f"Error converting American odds {american_odds} to decimal: {e}")
            return 1.01  # Return a safe default

    def _decimal_to_american(self, decimal_odds):
        """Convert decimal odds to American odds with safeguards"""
        try:
            if decimal_odds < 1.01:
                return -10000  # Cap at -10000 for very low decimal odds

            if decimal_odds >= 2.0:
                american = round((decimal_odds - 1) * 100)
                return f"+{min(american, 10000)}"  # Cap at +10000
            else:
                # For favorites (decimal odds < 2.0)
                american = round(100 / (decimal_odds - 1))
                return f"-{min(american, 10000)}"  # Cap at -10000
        except Exception as e:
            print(f"Error converting decimal odds {decimal_odds} to American: {e}")
            return "-110"  # Return a safe default

    def _decimal_to_american_int(self, decimal_odds):
        """Convert decimal odds to American odds format as int with safeguards"""
        try:
            if decimal_odds < 1.01:
                return -10000  # Cap at -10000 for very low decimal odds

            if decimal_odds >= 2.0:
                american = round((decimal_odds - 1) * 100)
                return min(american, 10000)  # Cap at +10000
            else:
                american = round(100 / (decimal_odds - 1))
                return -min(american, 10000)  # Cap at -10000
        except Exception as e:
            print(f"Error converting decimal odds {decimal_odds} to American int: {e}")
            return -110  # Return a safe default
