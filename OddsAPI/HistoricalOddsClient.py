import qasync
import asyncio
import aiohttp
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QHBoxLayout, QScrollArea, QSizePolicy,
    QSpinBox, QMessageBox, QListWidget, QListWidgetItem, QFrame
)
from PyQt6.QtGui import QColor, QPixmap, QPainter, QFont, QBrush, QPen, QFontMetrics

from KalshiClient import KalshiClient, KalshiStreamClient, KalshiLiveBook
from polymarket_sports_client import PolymarketSportsClient
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

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
    'kalshi': (44, 160, 44),       # green
    'polymarket': (31, 119, 180),  # blue
}
FALLBACK_LINE_COLORS = [
    (255, 127, 14), (214, 39, 40), (148, 103, 189),
    (140, 86, 75), (23, 190, 207), (188, 189, 34),
]

# Filenames under SPORTSBOOK_LOGO_DIR by bookmaker key (for the hover readout).
BOOKMAKER_LOGO_FILE = {
    'kalshi': 'kalshi_alt.png',  # compact "K" mark; renders cleaner at small sizes
    'polymarket': 'polymarket.png',
}

# Canonical team name (as produced by EventMatcher.normalize_team_name) ->
# logo filename under LOGO_DIR/<LEAGUE>/. Verified against the on-disk assets;
# the three teams without a dedicated PNG (Phillies, Kraken, Utah) fall back to
# their league logo. See generation note: built from EventMatcher canonicals.
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
        'philadelphia phillies': 'MLBleague.png', 'pittsburgh pirates': 'Pirates.png',
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

    def get_display_title(self):
        """Get formatted title for display"""
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
    def teams_match(team1: str, team2: str, sport: str = None) -> bool:
        """Check if two team names refer to the same team with sport context"""
        norm1 = EventMatcher.normalize_team_name(team1, sport)
        norm2 = EventMatcher.normalize_team_name(team2, sport)

        # Exact match
        if norm1 == norm2:
            return True

        # Check if one contains the other
        if norm1 in norm2 or norm2 in norm1:
            return True

        # Check aliases with sport context
        if sport and sport in EventMatcher.TEAM_ALIASES_BY_SPORT:
            sport_aliases = EventMatcher.TEAM_ALIASES_BY_SPORT[sport]
            for canonical, aliases in sport_aliases.items():
                if norm1 in aliases and norm2 in aliases:
                    return True

        return False

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

    async def get_sport_games(self, sport: str):
        """
        Get all games for a sport from Polymarket (async, non-blocking).

        Args:
            sport: Sport name (NFL, NBA, NHL, etc.)

        Returns:
            List of Game objects from PolymarketSportsClient
        """
        try:
            # Use run_in_executor for CPU-bound work to avoid blocking Qt event loop
            loop = asyncio.get_event_loop()
            games = await loop.run_in_executor(
                None,
                lambda: self.polymarket_client.get_sport_markets(
                    sport,
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
            print(f"Error fetching Polymarket games for {sport}: {e}")
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


class OrderbookLadderWidget(QWidget):
    """Compact live depth ladder for the YES outcome, Bloomberg-terminal styling.

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


class HistoricalOddsWidget(QWidget):
    """Widget for displaying historical odds movement with point change handling"""

    # Loading progress (0-100). Routed to the host's bottom status banner instead
    # of an in-widget progress bar so the plot can extend to the widget's edge.
    loading_progress = pyqtSignal(int)

    # Combined Time+Interval presets: label -> (time_range_text, interval_text).
    # Drives the hidden backing time_range / kalshi_interval combos. Index 0 is
    # the default and matches their default state (1h window, 1m candles).
    TIMEFRAME_PRESETS = [
        ("1m · 1h",  ("1h",  "1m")),
        ("1m · 6h",  ("6h",  "1m")),
        ("1h · 24h", ("24h", "60m")),
        ("1d · 7d",  ("7d",  "1440m")),
        ("Live",     ("1h",  "Live")),
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
        self.live_book = KalshiLiveBook(on_gap=self._on_live_book_gap)
        # Coalesced repaint: buffer every tick, repaint on a fixed cadence so the
        # GUI stays smooth during bursty markets without dropping data.
        self._live_dirty = False

        # Live overlay state / options (only meaningful while live_mode is True)
        self.auto_follow = True
        self.candle_mode = False
        self.candle_bucket_s = 5        # 1 / 5 / 15 ; 0 == "Max" (one candle/tick)
        self.show_spread_band = True
        self.live_follow_window_s = 600  # rolling window (sec) for the line view
        self.live_candle_visible_n = 60  # candles to keep in view when following
        # (so candle width stays visually consistent across 1s/5s/15s buckets)
        # Per-tick history for the live portion. Each entry:
        #   {'t': epoch_sec, 'price': cents, 'bid': cents|None, 'ask': cents|None}
        self.live_ticks = []
        # Live corner-label running stats (american odds, float; tick-by-tick)
        self._live_lbl_open = None
        self._live_lbl_min = None
        self._live_lbl_max = None
        # Persistent overlay item handles (created lazily; None == not built yet)
        self._li_line = None
        self._li_band_lo = self._li_band_hi = self._li_band_fill = None
        self._li_cbodies = self._li_cwicks = None
        self.live_repaint_timer = QTimer()
        self.live_repaint_timer.setInterval(250)  # ~4 fps repaint while live
        self.live_repaint_timer.timeout.connect(self._flush_live_plot)

        self._initialize_kalshi_websocket()

        self.init_ui()

    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        # Margins zeroed top & bottom: the controls now float over the plot (no
        # header rows) and the progress bar is gone, so the plot runs flush from
        # the top of the widget to the host banner.
        layout.setContentsMargins(5, 0, 5, 0)

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
        self.event_selector = QComboBox()
        self.event_selector.setMinimumWidth(160)
        self.event_selector.setToolTip("Event")
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
            background="#29313D",
            axisItems={'bottom': date_axis}
        )
        self.plot_widget.setLabel('left', 'Implied %')
        self.plot_widget.setLabel('bottom', 'Time')
        #self.plot_widget.addLegend()
        # showGrid draws gridlines aligned to the real axis ticks instead of a
        # GridItem, which previously painted raw-unit ghost labels (e.g.
        # "1.7807e+09") over the plot.
        self.plot_widget.showGrid(x=True, y=True, alpha=0.18)

        # If the user pans/zooms by hand while Live "Follow" is on, drop out of
        # follow so the view stops snapping back each tick (lets them inspect).
        # sigRangeChangedManually fires only for user-driven changes, not our
        # programmatic setXRange/auto-fit, so this won't self-trigger.
        self.plot_widget.getViewBox().sigRangeChangedManually.connect(
            self._on_user_range_change)

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
                " border-radius:3px; padding:3px 5px;")
            lbl.hide()

        # --- Crosshair scrubber + hover readout ---------------------------------
        # Mouse-tracking crosshair lines (added to the scene lazily in
        # _ensure_crosshair so they survive plot_widget.clear()) plus a floating
        # readout that reports each registered series' value at the cursor's time.
        self._cross_v = None
        self._cross_h = None
        self._hover_series = []   # list of dicts: name/color/ts/pct/am
        self.hover_label = QLabel(self.plot_widget)
        self.hover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.setTextFormat(Qt.TextFormat.RichText)
        self.hover_label.setStyleSheet(
            "background: rgba(13,17,23,235); border:1px solid #39414b;"
            " border-radius:3px; padding:4px 7px; color:#dcdcdc;")
        self.hover_label.hide()
        self.plot_widget.scene().sigMouseMoved.connect(self._on_plot_hover)

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
        # Right-side expandable DEPTH / VIEW panel (Bloomberg-terminal-esque).
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
        pb.setContentsMargins(6, 4, 6, 6); pb.setSpacing(6)
        # Header title
        self.panel_title = QLabel("DEPTH / VIEW"); self.panel_title.setObjectName("sect")
        pb.addWidget(self.panel_title)

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
        row1.addWidget(self.follow_check); row1.addWidget(self.spread_check); row1.addStretch(1)
        lc.addLayout(row1)
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
        # Best bid/ask/spr/last readout
        self.ob_readout = QLabel("")
        self.ob_readout.setTextFormat(Qt.TextFormat.RichText)
        self.ob_readout.setToolTip("Live best bid / ask · spread · last trade")
        self.ob_readout.setStyleSheet(
            "color:#cfd6df; font-family:monospace; font-size:11px;"
            " background:#11161d; border:1px solid #222a35;"
            " border-radius:2px; padding:2px 5px;")
        # Allow it to shrink to the panel width instead of forcing the body wider
        # (which, with horizontal scroll off, caused right-edge clipping).
        self.ob_readout.setMinimumWidth(0)
        self.ob_readout.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        obl.addWidget(self.ob_readout)
        # Depth ladder
        self.ob_ladder = OrderbookLadderWidget(levels=8)
        obl.addWidget(self.ob_ladder)
        pb.addWidget(self.ob_section)

        # --- ORDER ENTRY (live/kalshi only; collapsible) ---
        self.order_entry_section = self._build_order_entry_section()
        pb.addWidget(self.order_entry_section)

        # --- Bookmaker view toggles (UNCHANGED logic; rehoused in a collapsible tab) ---
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
            # Sub-second Live feed (additive; only used in "Live" interval mode)
            self.kalshi_stream_client.trade.connect(self._on_ws_trade)
            self.kalshi_stream_client.orderbook.connect(self._on_ws_orderbook)

            print("✅ Kalshi WebSocket client initialized (not started)")
        except Exception as e:
            print(f"⚠️  Failed to initialize Kalshi WebSocket: {e}")
            self.kalshi_stream_client = None

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

            # Only process if it's for the current market
            if market_ticker != self.kalshi_market_ticker:
                return

            def _to_float(v):
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

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
        """Subscribe to live updates for the currently selected Kalshi market"""
        if not self.kalshi_stream_client or not self.kalshi_market_ticker:
            return

        try:
            if self.live_mode:
                print(f"📡 Subscribing to sub-second feed (trade + orderbook) for {self.kalshi_market_ticker}")
                self.live_book.reset(self.kalshi_market_ticker)
                self.live_ticks = []
                self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
                self.kalshi_stream_client.subscribe_live([self.kalshi_market_ticker])
            else:
                print(f"📡 Subscribing to live updates for {self.kalshi_market_ticker}")
                self.kalshi_stream_client.subscribe_ticker([self.kalshi_market_ticker])
        except Exception as e:
            print(f"Failed to subscribe to market: {e}")

    def _unsubscribe_from_current_market(self):
        """Unsubscribe from the current market"""
        if not self.kalshi_stream_client or not self.kalshi_market_ticker:
            return

        try:
            print(f"📡 Unsubscribing from {self.kalshi_market_ticker}")
            channels = ["trade", "orderbook_delta"] if self.live_mode else ["ticker"]
            self.kalshi_stream_client.unsubscribe(channels, [self.kalshi_market_ticker])
            # Also drop the orderbook sid explicitly so the old market's stream
            # actually stops (channel-based unsubscribe leaves the sid running).
            if self.live_mode:
                old_sid = self.live_book.current_sid(self.kalshi_market_ticker)
                if old_sid is not None:
                    self.kalshi_stream_client.unsubscribe_sids([old_sid])
        except Exception as e:
            print(f"Failed to unsubscribe: {e}")

    # ------------------------------------------------------------------
    # Sub-second Live feed (trade + orderbook_delta) — additive to the
    # existing `ticker` path used by _on_websocket_tick.
    # ------------------------------------------------------------------
    def _on_ws_trade(self, msg):
        """Handle a Kalshi `trade` message (true per-execution tick)."""
        if not self.live_mode:
            return
        try:
            inner = msg.get('msg', {}) or {}
            if inner.get('market_ticker') != self.kalshi_market_ticker:
                return
            self.live_book.apply(msg)
            self._live_dirty = True
        except Exception as e:
            print(f"Error processing live trade: {e}")

    def _on_ws_orderbook(self, msg):
        """Handle a Kalshi `orderbook_snapshot`/`orderbook_delta` message."""
        if not self.live_mode:
            return
        try:
            inner = msg.get('msg', {}) or {}
            if inner.get('market_ticker') != self.kalshi_market_ticker:
                return
            self.live_book.apply(msg)
            self._live_dirty = True
        except Exception as e:
            print(f"Error processing live orderbook: {e}")

    def _on_live_book_gap(self, ticker):
        """Sequence gap on the orderbook stream -> resubscribe for a fresh snapshot.

        Debounced: the book keeps applying deltas best-effort in the meantime, so
        we only need an occasional resync rather than one per message."""
        if not self.live_mode or ticker != self.kalshi_market_ticker:
            return
        import time
        now = time.time()
        last = getattr(self, '_last_gap_resub', 0)
        if now - last < 5.0:
            return
        self._last_gap_resub = now
        print(f"⚠️  Orderbook seq gap for {ticker} — resubscribing for fresh snapshot")
        try:
            # Drop the current sid by id (channel-based unsubscribe doesn't), then
            # resubscribe; the fresh snapshot rebinds the book to the new sid and
            # any straggler deltas from the old sid are ignored.
            old_sid = self.live_book.current_sid(ticker)
            if old_sid is not None:
                self.kalshi_stream_client.unsubscribe_sids([old_sid])
            self.kalshi_stream_client.subscribe(["orderbook_delta"], [ticker])
        except Exception as e:
            print(f"Failed to resubscribe after gap: {e}")

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

    @qasync.asyncSlot()
    async def _on_place_order(self):
        """Confirm + submit a Kalshi order off the GUI thread."""
        ticker = self.kalshi_market_ticker
        if not ticker:
            QMessageBox.warning(self, "No market", "No Kalshi market selected.")
            return
        action, side = self.oe_side.currentText().lower().split(" ")  # buy/sell, yes/no
        price = self.oe_price.value()
        qty = self.oe_qty.value()
        cost = price * qty  # cents of max exposure for a buy
        confirm = QMessageBox.question(
            self, "Confirm order",
            f"{action.upper()} {qty} × {side.upper()} @ {price}¢\n"
            f"Market: {ticker}\n"
            f"Max cost ≈ ${cost/100:,.2f}\n\nPlace this REAL order?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.oe_place_btn.setEnabled(False)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: self.kalshi_client.kalshi_client.create_order(
                    ticker=ticker, action=action, side=side, count=qty, price_cents=price))
            print(f"✅ Order placed: {resp}")
        except Exception as e:
            QMessageBox.critical(self, "Order failed", f"{e}")
            print(f"⚠️  Order failed: {e}")
        finally:
            self.oe_place_btn.setEnabled(True)
        await self._refresh_open_orders()

    @qasync.asyncSlot()
    async def _on_refresh_open_orders(self):
        await self._refresh_open_orders()

    async def _refresh_open_orders(self):
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
            action = o.get("action", "?")
            cnt = o.get("remaining_count", o.get("count", "?"))
            px = o.get("yes_price") if side == "yes" else o.get("no_price")
            item = QListWidgetItem(f"{action[:1].upper()} {cnt}×{side.upper()} @ {px}¢")
            item.setData(Qt.ItemDataRole.UserRole, o.get("order_id"))
            self.open_orders_list.addItem(item)

    @qasync.asyncSlot(QListWidgetItem)
    async def _on_cancel_order_item(self, item):
        order_id = item.data(Qt.ItemDataRole.UserRole)
        if not order_id:
            return
        if QMessageBox.question(
                self, "Cancel order", f"Cancel order {order_id[:8]}…?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.kalshi_client.kalshi_client.cancel_order(order_id))
            print(f"🗑️  Cancelled {order_id}")
        except Exception as e:
            QMessageBox.critical(self, "Cancel failed", f"{e}")
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
        vb = self.plot_widget.getViewBox()
        if self.auto_follow:
            # Resume scrolling/auto-fit on the next tick.
            if self.live_mode:
                self._refresh_live_items()
        else:
            # Freeze the view exactly where it is for inspection.
            vb.enableAutoRange(x=False, y=False)

    def _on_user_range_change(self, *args):
        """User panned/zoomed by hand -> stop following so it doesn't snap back."""
        if self.live_mode and self.auto_follow:
            # Uncheck the box (also sets auto_follow False via _on_follow_toggled).
            self.follow_check.setChecked(False)

    def _on_spread_toggled(self, state):
        self.show_spread_band = bool(state)
        if self.live_mode:
            self._refresh_live_items()

    def _on_candle_toggled(self, state):
        self.candle_mode = bool(state)
        self.candle_bucket.setEnabled(self.candle_mode)
        if self.live_mode:
            self._refresh_live_items()

    def _on_candle_bucket_changed(self):
        self.candle_bucket_s = self._parse_bucket_seconds(self.candle_bucket.currentText())
        if self.live_mode and self.candle_mode:
            self._refresh_live_items()

    @staticmethod
    def _parse_bucket_seconds(text):
        """Candle bucket label -> seconds. 'Max' == 0 (one candle/tick); supports
        both 's' (seconds) and 'm' (minutes) suffixes."""
        if text == "Max":
            return 0
        if text.endswith('m'):
            return int(text[:-1]) * 60
        return int(text.removesuffix('s'))

    def _update_ob_readout(self, state):
        """Refresh the discreet inline bid/ask/spread/last readout."""
        def c(v):
            return f"{v}¢" if v is not None else "—"
        bid, ask, last = state.get('best_bid'), state.get('best_ask'), state.get('last_trade')
        spread = (ask - bid) if (bid is not None and ask is not None) else None
        stale = " <span style='color:#d08770'>·resync</span>" if state.get('stale') else ""
        self.ob_readout.setText(
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

        ticker = self.kalshi_market_ticker
        if not ticker:
            return
        state = self.live_book.state(ticker)
        if not state:
            return

        # Prefer last executed trade. Only fall back to the bid/ask MID when both
        # sides are present — a one-sided book (only a bid or only an ask) would
        # otherwise yield an extreme/garbage price.
        price_cents = state.get('last_trade')
        if price_cents is None:
            if state.get('best_bid') is not None and state.get('best_ask') is not None:
                price_cents = int(round(state['mid']))
        if price_cents is None or not (0 < price_cents < 100):
            return

        # Local time to match the candlestick seed path's axis.
        from datetime import datetime
        now_t = datetime.now().timestamp()

        # Record the live tick (price + book sides).
        self.live_ticks.append({
            't': now_t,
            'price': price_cents,
            'bid': state.get('best_bid'),
            'ask': state.get('best_ask'),
        })
        # Cap memory: keep at most ~6h of 250ms ticks.
        if len(self.live_ticks) > 90000:
            self.live_ticks = self.live_ticks[-90000:]

        self._update_ob_readout(state)
        self._update_live_summary_label(state, now_t)
        # Feed the depth ladder (cheap custom-painted widget).
        lad = self.live_book.ladder(ticker, depth=self.ob_ladder._levels)
        if lad:
            self.ob_ladder.set_data(lad['asks'], lad['bids'],
                                    lad.get('last_trade'), lad.get('stale', False))

        # Ensure persistent items exist (first tick after entering live mode), then
        # do the cheap in-place refresh.
        if getattr(self, '_li_line', None) is None:
            self._rebuild_live_items()
        self._refresh_live_items()

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

    def _update_live_summary_label(self, state, now_t):
        """Tick-by-tick update of the corner range label for the live band. Shows
        implied chance (%) to one decimal with the American odds in parens."""
        # Use the mid for smooth sub-integer movement; fall back to last trade.
        mid = state.get('mid')
        cents = mid if mid is not None else state.get('last_trade')
        if cents is None or not (0.0 < cents < 100.0):
            return
        cur = float(cents)  # cents == implied chance %
        am = self._am_from_cents_float(cents)

        if self._live_lbl_open is None:
            self._live_lbl_open = cur
            self._live_lbl_min = cur
            self._live_lbl_max = cur
        else:
            self._live_lbl_min = min(self._live_lbl_min, cur)
            self._live_lbl_max = max(self._live_lbl_max, cur)

        open_v = self._live_lbl_open
        delta = cur - open_v
        rng = self._live_lbl_max - self._live_lbl_min
        if delta > 0:
            arrow, dcolor = '▲', (80, 200, 120)
        elif delta < 0:
            arrow, dcolor = '▼', (220, 90, 90)
        else:
            arrow, dcolor = '■', (170, 170, 170)
        dhex = '#%02x%02x%02x' % dcolor
        from datetime import datetime
        updated = datetime.fromtimestamp(now_t).strftime('%H:%M:%S')
        band_key = 'pos' if cur >= 50.0 else 'neg'
        am_str = ""
        if am is not None:
            am_i = int(round(am))
            am_str = f" <span style='color:#7e8794'>({'+' if am_i > 0 else ''}{am_i})</span>"
        html = (
            f"<div style='font-size:9pt;color:#dcdcdc'>{open_v:.1f}%&#8594;{cur:.1f}%{am_str} "
            f"<span style='color:{dhex}'>{arrow}{abs(delta):.1f}</span></div>"
            f"<div style='font-size:9pt;color:#c8c8c8'>range {rng:.1f}%</div>"
            f"<div style='font-size:7pt;color:#7e8794'>updated {updated}</div>"
        )
        self._set_summary_label(band_key, html)

    def enable_websocket_updates(self, enable: bool = True):
        """
        Enable or disable WebSocket live updates

        Args:
            enable: True to use WebSocket, False to use polling
        """
        self.websocket_enabled = enable

        if enable:
            if self.kalshi_stream_client and not self.kalshi_stream_client._is_running:
                print("🚀 Starting Kalshi WebSocket...")
                self.kalshi_stream_client.start()
                # Subscribe to current market if any
                if self.kalshi_market_ticker:
                    self._subscribe_to_current_market()
            # Stop polling timer
            self.refresh_timer.stop()
            # Start coalesced repaint cadence while in sub-second Live mode
            if self.live_mode and not self.live_repaint_timer.isActive():
                self.live_repaint_timer.start()
        else:
            self.live_repaint_timer.stop()
            if self.kalshi_stream_client:
                print("🛑 Stopping Kalshi WebSocket...")
                self.kalshi_stream_client.stop()
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
        """Load events from all 4 major sports (NFL, NBA, MLB, NHL)"""
        print(f"\n{'='*80}")
        print(f"Loading all sports events...")
        print(f"{'='*80}\n")

        # Configuration for filtering past events
        SHOW_PAST_EVENTS = False  # Set to True to show all events including past ones
        PAST_EVENT_CUTOFF_HOURS = 36  # Keep events from last N hours (generous to handle midnight UTC parsing)

        all_unified_events = []

        # Load events from all 4 major sports concurrently
        import time as _tp_all, sys as _sp_all
        _all_t0 = _tp_all.perf_counter()
        _sports = ['NFL', 'NBA', 'MLB', 'NHL']
        _sport_results = await asyncio.gather(
            *[self._load_unified_events_for_sport(s) for s in _sports],
            return_exceptions=True
        )
        for _sport, _result in zip(_sports, _sport_results):
            if isinstance(_result, Exception):
                print(f"[allsports-probe] {_sport} FAILED: {_result}", file=_sp_all.stderr)
            else:
                all_unified_events.extend(_result)
        if PERF_DIAG:
            print(f"[allsports-probe] all sports total={(_tp_all.perf_counter()-_all_t0)*1000:.0f}ms",
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

        _post_t1 = _tp_post.perf_counter()
        # Populate event selector with all events
        self.event_selector.blockSignals(True)
        self.event_selector.clear()

        for event in all_unified_events:
            display_title = event.get_display_title()
            self.event_selector.addItem(display_title, userData=event)

        self.event_selector.blockSignals(False)
        _post_t2 = _tp_post.perf_counter()
        if PERF_DIAG:
            print(f"[post-probe] filter={(_post_t1-_post_t0)*1000:.0f}ms "
                  f"populate_selector={(_post_t2-_post_t1)*1000:.0f}ms "
                  f"(n={len(all_unified_events)})", file=_sp_post.stderr)

        # Enable controls
        self.set_enabled(True)

        # Update the no data message
        if all_unified_events:
            self.no_data_text.setText("Select an event to view historical odds")
        else:
            self.no_data_text.setText("No events available")

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

    async def load_markets_for_unified_event(self, unified_event: UnifiedEvent):
        """
        Load markets from both Kalshi and Polymarket for a unified event.

        Args:
            unified_event: UnifiedEvent containing data from both sources
        """
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

        # Check if this is a UnifiedEvent object
        if isinstance(event_data, UnifiedEvent):
            unified_event = event_data
            print(f"\nEvent changed to: {unified_event.get_display_title()}")
            print(f"  Kalshi: {'✓' if unified_event.has_kalshi() else '✗'}")
            print(f"  Polymarket: {'✓' if unified_event.has_polymarket() else '✗'}")

            # Populate market selector with markets from both sources
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
            self.current_unified_market = unified_market

            # Show what sources are available
            source_info = []
            if unified_market.has_kalshi():
                source_info.append("Kalshi")
                # Store first ticker for legacy compatibility (some code may still use it)
                self.kalshi_market_ticker = unified_market.kalshi_tickers[0] if unified_market.kalshi_tickers else None
            if unified_market.has_polymarket():
                source_info.append("Polymarket")

            print(f"Market changed to: [{' + '.join(source_info)}] {unified_market.display_name}")

            # Handle WebSocket subscription for Kalshi markets
            if self.websocket_enabled and unified_market.has_kalshi():
                self._unsubscribe_from_current_market()
                # Market ticker is already set above, now subscribe
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

                    # Fetch from Kalshi if available (may be multiple markets for moneyline)
                    if unified_market.has_kalshi():
                        for idx, k_ticker in enumerate(unified_market.kalshi_tickers):
                            try:
                                k_title = unified_market.kalshi_titles[idx]
                                print(f"Fetching Kalshi data for: {k_title}")
                                print(f"  Ticker: {k_ticker}")
                                print(f"  Event ticker: {unified_market.kalshi_event_ticker}")
                                print(f"  Series: {unified_market.kalshi_event_ticker.split('-')[0]}")

                                kalshi_snapshots = await self.kalshi_client.get_historical_candlesticks(
                                    session,
                                    k_ticker,
                                    unified_market.kalshi_event_ticker.split('-')[0],
                                    start_time,
                                    end_time,
                                    period_interval=kalshi_interval_value,
                                    market_type=unified_market.market_type  # Pass market type for correct market key
                                )
                                all_snapshots.extend(kalshi_snapshots)
                                print(f"  ✅ Got {len(kalshi_snapshots)} Kalshi snapshots")
                            except Exception as e:
                                print(f"  ❌ Error fetching Kalshi data: {e}")
                                import traceback
                                traceback.print_exc()

                    # Fetch from Polymarket if available (may have multiple outcomes)
                    if unified_market.has_polymarket():
                        try:
                            market_obj = unified_market.polymarket_market
                            print(f"Fetching Polymarket data for: {market_obj.question}")

                            if market_obj.outcome_prices and len(market_obj.clob_token_ids) > 0:
                                # Fetch data for ALL outcomes (e.g., both teams for moneyline)
                                for outcome_idx, token_id in enumerate(market_obj.clob_token_ids):
                                    # Get the outcome name from the market's outcomes list
                                    if hasattr(market_obj, 'outcomes') and outcome_idx < len(market_obj.outcomes):
                                        outcome_name = market_obj.outcomes[outcome_idx]
                                    else:
                                        # Fallback to using question
                                        outcome_name = f"{market_obj.question} - Outcome {outcome_idx + 1}"

                                    print(f"  Outcome {outcome_idx + 1}: {outcome_name}")
                                    print(f"    Token ID: {token_id}")

                                    poly_snapshots = await self.polymarket_client.get_historical_candlesticks(
                                        session,
                                        token_id,
                                        outcome_name,
                                        start_time,
                                        end_time,
                                        fidelity=kalshi_interval_value,  # Use same interval as Kalshi for alignment
                                        market_type=unified_market.market_type  # Pass market type for correct market key
                                    )
                                    all_snapshots.extend(poly_snapshots)
                                    print(f"    ✅ Got {len(poly_snapshots)} snapshots")
                            else:
                                print("  ⚠️  No token IDs available for Polymarket market")
                        except Exception as e:
                            print(f"  ❌ Error fetching Polymarket data: {e}")
                            import traceback
                            traceback.print_exc()

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

    async def _enter_live_mode(self):
        """Switch the widget into sub-second websocket Live mode (Kalshi only)."""
        if self.kalshi_stream_client is None:
            print("⚠️  Live mode unavailable: Kalshi WebSocket client failed to init")
            self.kalshi_interval.setCurrentText("1m")
            self._set_timeframe_silent("1m · 1h")
            return
        if not self.kalshi_market_ticker:
            print("⚠️  Live mode needs a selected Kalshi market — reverting to 1m")
            self.kalshi_interval.setCurrentText("1m")
            self._set_timeframe_silent("1m · 1h")
            return

        print("🔴 Entering Live (sub-second) mode")
        self.live_mode = True
        self.live_ticks = []
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        self.candle_bucket.setEnabled(self.candle_mode)
        self.live_controls.setVisible(True)
        self.ob_section.setVisible(True)
        self.order_entry_section.setVisible(True)
        self.ob_ladder.clear()
        self.open_orders_list.clear()
        asyncio.create_task(self._refresh_open_orders())
        # Auto-expand the panel so the live depth is visible on entry.
        if getattr(self, '_panel_collapsed', False):
            self._toggle_right_panel()
        self.ws_status_label.setText("🟡")
        self.ws_status_label.setToolTip("WebSocket: Live mode — connecting…")

        # Seed the chart with recent history so it isn't empty (uses the existing
        # candlestick path; the "Live" interval is treated as 1m for this pull).
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        await self.load_data()

        # Hand off to the websocket: subscribes trade + orderbook_delta and
        # starts the coalesced repaint cadence.
        self.enable_websocket_updates(True)

    def _exit_live_mode(self):
        """Leave Live mode and return to the normal polling/candlestick path."""
        print("⚪ Exiting Live mode")
        self.live_mode = False
        self._live_dirty = False
        self.live_repaint_timer.stop()
        self.live_controls.setVisible(False)
        self.ob_section.setVisible(False)
        self.order_entry_section.setVisible(False)
        self.ob_readout.setText("")
        self.ob_ladder.clear()
        self.open_orders_list.clear()
        self.live_ticks = []
        self._live_lbl_open = self._live_lbl_min = self._live_lbl_max = None
        # Drop persistent-item handles; the next normal load's clear() removes the
        # items from the scene and a future live entry recreates them.
        self._li_line = None
        self._li_band_lo = self._li_band_hi = self._li_band_fill = None
        self._li_cbodies = self._li_cwicks = None
        self.enable_websocket_updates(False)
        if self.kalshi_market_ticker:
            self.live_book.reset(self.kalshi_market_ticker)

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

        # Crosshair lines are scene items, so clear() removed them — re-add.
        self._ensure_crosshair()

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

    def _rebuild_live_items(self):
        """(Re)create the persistent Live overlay items and add them to the plot.

        Called only on full redraws (after update_plot's clear()), not per tick.
        Items are then mutated in place via setData/setOpts in _refresh_live_items."""
        # Live feed is always Kalshi -> green, matching the seed-series color map.
        self._li_line = pg.PlotDataItem([], [], pen=pg.mkPen((44, 160, 44), width=2))
        self._li_band_lo = pg.PlotDataItem([], [], pen=pg.mkPen((120, 140, 160, 90), width=1))
        self._li_band_hi = pg.PlotDataItem([], [], pen=pg.mkPen((120, 140, 160, 90), width=1))
        self._li_band_fill = pg.FillBetweenItem(
            self._li_band_lo, self._li_band_hi, brush=pg.mkBrush(120, 140, 160, 45))
        self._li_cwicks = pg.PlotDataItem([], [], connect='finite',
                                          pen=pg.mkPen(170, 170, 170, 200))
        self._li_cbodies = pg.BarGraphItem(x=[], width=[], y0=[], height=[])
        # z-order: band (back) -> line -> candles (front)
        for it in (self._li_band_lo, self._li_band_hi, self._li_band_fill,
                   self._li_line, self._li_cwicks, self._li_cbodies):
            self.plot_widget.addItem(it)

    def _refresh_live_items(self):
        """Cheap per-tick update: setData on the persistent overlay items only."""
        if getattr(self, '_li_line', None) is None:
            return
        ticks = self.live_ticks
        to_pct = self._cents_to_pct

        # --- Line (live price extension in implied %; hidden when candles shown) ---
        lx, ly = [], []
        for tk in ticks:
            pct = to_pct(tk['price'])
            if pct is not None:
                lx.append(tk['t']); ly.append(pct)
        self._li_line.setData(lx, ly)
        self._li_line.setVisible(not self.candle_mode and len(lx) > 0)

        # --- Spread band (bid/ask in implied %) ---
        show_band = self.show_spread_band
        if show_band:
            bx, b_lo, b_hi = [], [], []
            for tk in ticks:
                lo, hi = to_pct(tk['bid']), to_pct(tk['ask'])
                if lo is None or hi is None:
                    continue
                bx.append(tk['t']); b_lo.append(lo); b_hi.append(hi)
            self._li_band_lo.setData(bx, b_lo)
            self._li_band_hi.setData(bx, b_hi)
        for it in (self._li_band_lo, self._li_band_hi, self._li_band_fill):
            it.setVisible(show_band)

        # --- Candles ---
        if self.candle_mode:
            self._refresh_live_candles(ticks, to_pct)
        self._li_cbodies.setVisible(self.candle_mode)
        self._li_cwicks.setVisible(self.candle_mode)

        # --- Auto-follow: scroll X to newest ticks AND auto-fit Y to whatever is
        # visible in that window (seed + live). setAutoVisible(y=True) makes the Y
        # autorange consider only data inside the current X range, so the chart
        # scrolls like a live tape. Disabled the instant the user pans by hand. ---
        if self.auto_follow and ticks:
            vb = self.plot_widget.getViewBox()
            last_t = ticks[-1]['t']
            if self.candle_mode:
                # Keep a fixed number of candles in view so candle width reads
                # consistently across 1s/5s/15s/Max buckets (standard charting).
                eff = self.candle_bucket_s if self.candle_bucket_s > 0 else 0.25
                window = max(self.live_candle_visible_n * eff, 20)
                right_pad = eff * 2
            else:
                window = self.live_follow_window_s
                right_pad = 5
            vb.setXRange(last_t - window, last_t + right_pad, padding=0)
            vb.setAutoVisible(y=True)
            vb.enableAutoRange(axis='y', enable=True)

    def _refresh_live_candles(self, ticks, to_pct):
        """Aggregate ticks into OHLC (implied %) and push into the candle items.

        Candles are now drawn in implied-% space, which is linear, so bodies no
        longer distort near 50% the way they did on the american-odds axis.

        TODO(live-candles): remaining heuristics to revisit:
          - min body height (doji) is a flat 0.5%, not scaled to the visible y-range
          - "Max" bucket assumes a ~0.25s tick cadence; real spacing varies, so
            candles can overlap/gap in bursty/idle stretches
          - width/visible-count are global constants, not user-tunable in the UI
        """
        bucket = self.candle_bucket_s
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
            self._li_cbodies.setOpts(x=[], width=[], y0=[], height=[])
            self._li_cwicks.setData([], [])
            return

        # Width tracks the bucket (Max -> tick cadence ~0.25s), filling ~88% of the
        # slot so neighbouring candles read as distinct but chunky.
        eff_bucket = bucket if bucket > 0 else 0.25
        width = eff_bucket * 0.88
        centers, body_y0, body_h, brushes = [], [], [], []
        wick_x, wick_y = [], []
        up, down = (80, 200, 120), (220, 90, 90)
        for key in order:
            vals = groups[key]
            o, c = vals[0], vals[-1]
            hi, lo = max(vals), min(vals)
            cx = key + (eff_bucket / 2.0 if bucket > 0 else 0)
            centers.append(cx)
            body_y0.append(min(o, c))
            body_h.append(max(abs(c - o), 0.5))
            brushes.append(pg.mkBrush(*(up if c >= o else down)))
            wick_x += [cx, cx, float('nan')]
            wick_y += [lo, hi, float('nan')]
        self._li_cbodies.setOpts(x=centers, width=width, y0=body_y0,
                                 height=body_h, brushes=brushes,
                                 pen=pg.mkPen(0, 0, 0, 60))
        self._li_cwicks.setData(wick_x, wick_y)

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
        """Show each team's logo in its odds band (top=underdog, bottom=favorite).

        Falls back to a static top/bottom split for neutral (pick'em) markets.
        Resolution is cached, so the only per-call cost is two setPixmap/move.
        """
        sport = getattr(self.current_unified_event, 'sport', None)
        teams = self._latest_value_by_team(plot_data)
        if len(teams) != 2:
            self._hide_side_logos()
            return

        (ca, (na, va)), (cb, (nb, vb)) = teams.items()
        # Clear favorite/underdog: opposite signs => the negative (favorite)
        # team sits in the bottom band, matching its line cluster. Otherwise
        # (same sign / near pick'em) keep a stable top/bottom split by name.
        if (va < 0) != (vb < 0):
            top_team, bottom_team = (na, nb) if va > vb else (nb, na)
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

        # --- Floating control strip + its toggle (top-LEFT, HUGGING its content
        # so it never spans the graph width). The toggle sits just right of the
        # strip when shown, or alone at the top-left when the strip is hidden. ---
        btn = getattr(self, 'controls_toggle_btn', None)
        ov = getattr(self, 'control_overlay', None)
        if ov is not None and getattr(self, '_controls_visible', False):
            # market_info only in TheOddsAPI mode (event picker hidden); in
            # prediction mode the Event dropdown already shows the matchup.
            if getattr(self, 'market_info', None) is not None:
                self.market_info.setVisible(not self.event_selector.isVisibleTo(ov))
            ov.adjustSize()           # shrink to fit current (mode-dependent) content
            ov.move(6, 6)
            ov.raise_()
            if btn is not None:
                btn.move(6 + ov.width() + 3, 6 + max(0, (ov.height() - btn.height()) // 2))
                btn.raise_()
        elif btn is not None:
            btn.move(6, 6)
            btn.raise_()

        # Logos/summaries live top-right (strip is top-left, so no collision).
        logo_top_left = w - margin
        if self.logo_label_top.isVisible() and self.logo_label_top.pixmap():
            pm = self.logo_label_top.pixmap()
            logo_top_left = w - pm.width() - margin
            self.logo_label_top.move(logo_top_left, margin)
        logo_bot_left = w - margin
        if self.logo_label_bottom.isVisible() and self.logo_label_bottom.pixmap():
            pm = self.logo_label_bottom.pixmap()
            logo_bot_left = w - pm.width() - margin
            self.logo_label_bottom.move(logo_bot_left, h - pm.height() - margin)

        if self.summary_label_top.isVisible():
            s = self.summary_label_top
            s.move(logo_top_left - gap - s.width(), margin)
        if self.summary_label_bottom.isVisible():
            s = self.summary_label_bottom
            s.move(logo_bot_left - gap - s.width(), h - s.height() - margin)

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

        # Color-coded last-value tag pinned at the series end — the pro-chart
        # standard, replacing the old plain-black on-chart text labels. Fill =
        # series color; text auto-contrasts (dark on light fills, white on dark).
        tag_txt = format_pct_with_american(am[-1] if am.size else None, cur_v)
        if tag_txt:
            r, g, b = int(color[0]), int(color[1]), int(color[2])
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            fg = (15, 17, 20) if luminance > 140 else (245, 245, 245)
            tag = pg.TextItem(tag_txt, anchor=(-0.12, 0.5), color=fg,
                              fill=pg.mkBrush(r, g, b, 235),
                              border=pg.mkPen(r, g, b))
            tag.setZValue(150)
            self.plot_widget.addItem(tag)
            tag.setPos(float(ts[-1]), cur_v)

        # Summary -> fixed corner overlay (top-right for the high-% band,
        # bottom-right for the low-% band). Widget-space, can't collide with graph.
        updated = datetime.fromtimestamp(float(ts[-1])).strftime('%H:%M:%S')
        dhex = '#%02x%02x%02x' % dcolor
        summary_html = (
            f"<div style='font-size:9pt;color:#dcdcdc'>{open_v:.0f}%&#8594;{cur_v:.0f}% "
            f"<span style='color:{dhex}'>{arrow}{abs(delta):.0f}</span></div>"
            f"<div style='font-size:9pt;color:#c8c8c8'>range {swing:.0f}%</div>"
            f"<div style='font-size:7pt;color:#7e8794'>updated {updated}</div>"
        )
        self._set_summary_label(band_key, summary_html)

    def _set_summary_label(self, band_key, html):
        label = self.summary_label_top if band_key == 'pos' else self.summary_label_bottom
        label.setText(html)
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

    def _live_hover_rows(self, x):
        """Hover readout row(s) for the live tape at cursor time x. In candle mode
        it reports the hovered candle's O/H/L/C (implied %); otherwise the nearest
        tick's price. Returns [] when the cursor isn't over the live span."""
        lt = self.live_ticks
        if not lt:
            return []
        t0, t1 = lt[0]['t'], lt[-1]['t']
        pad = max(2.0, float(self.candle_bucket_s or 1))
        if not (t0 - pad <= x <= t1 + pad):
            return []
        logo = self._bookmaker_logo_path('kalshi')
        marker = (f"<img src='{logo}' width='15' height='15' style='vertical-align:middle'>"
                  if logo else "<span style='color:#2ca02c'>&#9679;</span>")

        if self.candle_mode:
            bucket = self.candle_bucket_s
            if bucket > 0:
                key = int(x // bucket) * bucket
                vals = [tk['price'] for tk in lt if key <= tk['t'] < key + bucket]
            else:  # "Max" bucket == one candle per tick
                vals = [min(lt, key=lambda tk: abs(tk['t'] - x))['price']]
            if not vals:
                return []
            o, c = vals[0], vals[-1]
            hi, lo = max(vals), min(vals)
            ccol = '#50c878' if c >= o else '#dc5a5a'
            ohlc = (f"<span style='color:#8a93a0'>O</span> {o}% &nbsp;"
                    f"<span style='color:#8a93a0'>H</span> {hi}% &nbsp;"
                    f"<span style='color:#8a93a0'>L</span> {lo}% &nbsp;"
                    f"<span style='color:{ccol}'>C {c}%</span>")
            body = ohlc
        else:
            tk = min(lt, key=lambda t: abs(t['t'] - x))
            pct = tk['price']
            body = (f"<span style='color:#f0f0f0'>"
                    f"{format_pct_with_american(self._cents_to_am(pct), float(pct))}</span>")
        return [f"<div style='font-size:9pt'>{marker} "
                f"<span style='color:#aeb6c0'>Live</span>&nbsp;&nbsp;{body}</div>"]

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
            lo, hi = min(implied), max(implied)
            pad = max((hi - lo) * 0.10, 3.0)  # at least 3 pts of breathing room
            min_val = max(0.0, lo - pad)
            max_val = min(100.0, hi + pad)
            if max_val - min_val < 5.0:  # degenerate (flat market) -> widen
                mid = (min_val + max_val) / 2.0
                min_val = max(0.0, mid - 5.0)
                max_val = min(100.0, mid + 5.0)

            self.plot_widget.setYRange(min_val, max_val)

            # Y-axis label + ticks: implied % primary, american odds in parens.
            y_axis = self.plot_widget.getAxis('left')
            y_axis.setLabel('Implied %')

            num_ticks = 6
            step = (max_val - min_val) / num_ticks
            y_ticks = []
            for i in range(num_ticks + 1):
                p = min_val + i * step
                am = self._am_from_cents_float(p)
                if am is not None:
                    am_i = int(round(am))
                    label = f"{p:.0f}% ({'+' if am_i > 0 else ''}{am_i})"
                else:
                    label = f"{p:.0f}%"
                y_ticks.append((p, label))
            y_axis.setTicks([y_ticks])

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
