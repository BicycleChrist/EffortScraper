import qasync
import asyncio
import aiohttp
from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
    QProgressBar, QCheckBox, QHBoxLayout, QScrollArea
)
from PyQt6.QtGui import QColor, QPixmap, QPainter

from KalshiClient import KalshiClient, KalshiStreamClient
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


class HistoricalOddsWidget(QWidget):
    """Widget for displaying historical odds movement with point change handling"""

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
        self._initialize_kalshi_websocket()

        self.init_ui()

    def init_ui(self):
        """Initialize the UI components"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Header with controls
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Historical Odds")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #7bd419")
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)

        # Event selector - shows unified events from both sources
        self.event_label = QLabel("Event:")
        self.event_selector = QComboBox()
        self.event_selector.setMinimumWidth(200)
        self.event_selector.currentIndexChanged.connect(self.on_event_changed)
        header_layout.addWidget(self.event_label)
        header_layout.addWidget(self.event_selector)

        # Market selector for Kalshi (populated dynamically)
        self.market_label = QLabel("Market:")
        self.market_selector = QComboBox()
        self.market_selector.setMinimumWidth(150)
        self.market_selector.currentIndexChanged.connect(self.on_market_changed)
        header_layout.addWidget(self.market_label)
        header_layout.addWidget(self.market_selector)

        # Time range selector (for TheOddsAPI only)
        self.time_label = QLabel("Time:")
        header_layout.addWidget(self.time_label)

        self.time_range = QComboBox()
        self.time_range.addItems(["1h", "3h", "6h", "12h", "24h", "7d"])
        self.time_range.setFixedWidth(60)
        self.time_range.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.time_range)

        # Interval selector (controls both Kalshi candlestick period and Polymarket fidelity)
        self.interval_label = QLabel("Interval:")
        header_layout.addWidget(self.interval_label)
        self.kalshi_interval = QComboBox()  # Keep variable name for compatibility
        self.kalshi_interval.addItems([f"{M}m" for M in (1, 60, 1440)])
        self.kalshi_interval.setFixedWidth(60)
        self.kalshi_interval.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.kalshi_interval)

        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedWidth(30)
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        header_layout.addWidget(self.refresh_button)

        # WebSocket status indicator
        self.ws_status_label = QLabel("⚪")
        self.ws_status_label.setToolTip("WebSocket: Not connected")
        self.ws_status_label.setFixedWidth(20)
        header_layout.addWidget(self.ws_status_label)

        layout.addLayout(header_layout)

        # Market info label
        self.market_info = QLabel("Select a market to view historical odds")
        self.market_info.setStyleSheet("color: #6c757d; font-style: italic;")
        layout.addWidget(self.market_info)

        # Main content area
        content_layout = QHBoxLayout()

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
        self.plot_widget.setLabel('left', 'Odds')
        self.plot_widget.setLabel('bottom', 'Time')
        #self.plot_widget.addLegend()
        # showGrid draws gridlines aligned to the real axis ticks instead of a
        # GridItem, which previously painted raw-unit ghost labels (e.g.
        # "1.7807e+09") over the plot.
        self.plot_widget.showGrid(x=True, y=True, alpha=0.18)

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

        self.plot_layout.addWidget(self.plot_widget)
        content_layout.addWidget(self.plot_panel, 4)

        # Bookmaker toggle section
        self.bookmaker_panel = QWidget()
        self.bookmaker_layout = QVBoxLayout(self.bookmaker_panel)
        self.bookmaker_layout.setContentsMargins(0, 0, 0, 0)
        self.bookmaker_layout.setSpacing(2)

        scroll_area = QScrollArea()
        scroll_area.setWidget(self.bookmaker_panel)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFixedWidth(100)
        content_layout.addWidget(scroll_area, 1)

        layout.addLayout(content_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(5)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

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
            self.kalshi_stream_client.unsubscribe(["ticker"], [self.kalshi_market_ticker])
        except Exception as e:
            print(f"Failed to unsubscribe: {e}")

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
        else:
            if self.kalshi_stream_client:
                print("🛑 Stopping Kalshi WebSocket...")
                self.kalshi_stream_client.stop()
            # Resume polling if enabled
            if self.auto_refresh_enabled and self.data_source == 'kalshi':
                self.refresh_timer.start(self.refresh_interval_ms)

    def set_enabled(self, enabled):
        """Enable or disable the widget controls"""
        self.time_range.setEnabled(enabled)
        self.kalshi_interval.setEnabled(enabled)
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

        # Show/hide appropriate controls
        self.event_selector.setVisible(False)
        self.event_label.setVisible(False)
        self.market_selector.setVisible(False)
        self.market_label.setVisible(False)
        self.time_range.setVisible(True)
        self.time_label.setVisible(True)

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
        kalshi_interval_value = int(self.kalshi_interval.currentText().removesuffix('m'))
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
        kalshi_interval_value = int(self.kalshi_interval.currentText().removesuffix('m'))
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
        checked = state == Qt.CheckState.Checked
        # Skip the first checkbox (which is "All") and last item (which is stretch)
        for i in range(1, self.bookmaker_layout.count() - 1):
            item = self.bookmaker_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QCheckBox):
                item.widget().setChecked(checked)

    async def on_bookmaker_toggled(self, bookmaker, state):
        """Handle bookmaker toggle checkbox changes"""
        print(f"Toggling bookmaker: {bookmaker} to {state}")
        self.bookmaker_visible[bookmaker] = (state == Qt.CheckState.Checked)
        # Refresh the plot with current visibility settings
        await self.update_plot(self.current_snapshots)

    @qasync.asyncSlot()
    async def on_time_range_changed(self):
        """Handle time range dropdown changes"""
        range_text = self.time_range.currentText()
        print(f"Time range changed to: {range_text}")

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

    async def update_plot(self, snapshots):
        """Enhanced plotting with point change visualization"""
        self.plot_widget.clear()
        # Grid is drawn by showGrid() on the axes (set once in init_ui) and
        # survives clear(), so no GridItem re-add is needed here.
        # Track which outcome bands already got peak/trough + net-move
        # annotations so the kalshi and polymarket series of the same band
        # don't double them up (reset each redraw; clear() wiped the old items).
        self._annotated_bands = set()
        # Corner summary overlays are widget-space (clear() doesn't touch them),
        # so hide them up front; bands that are still present re-show their own.
        self._hide_summaries()
        if not snapshots:
            self._hide_side_logos()
            await self._show_no_data_message()
            return

        colors = [
            (31, 119, 180), (255, 127, 14), (44, 160, 44),
            (214, 39, 40), (148, 103, 189), (140, 86, 75)
        ]

        # Some Legend options for graph display
        #self.plot_widget.addLegend(offset=(10, 10), labelTextSize='8pt')

        # Group data by bookmaker and outcome
        plot_data = await self._organize_plot_data(snapshots)

        # Plot each series with proper point change handling
        for bm_idx, (bookmaker, outcomes) in enumerate(plot_data.items()):
            if not self.bookmaker_visible.get(bookmaker, True):
                continue

            color = colors[bm_idx % len(colors)]
            for outcome_key, points_data in outcomes.items():
                await self._plot_outcome_series(bookmaker, outcome_key, points_data, color)

        await self.configure_plot_axes(snapshots)

        # Position team-logo watermarks for the two outcomes (moneyline only;
        # auto-hidden for Over/Under or spread markets where sides aren't teams).
        self._update_side_logos(plot_data)

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

    def _position_overlays(self):
        """Pin logos to the corners and dock each summary just left of its logo.

        Both are widget-space, so they stay clear of the graph lines regardless
        of the data. Summaries fall back to the corner when a logo is hidden.
        """
        margin, gap = 10, 8
        w = self.plot_widget.width()
        h = self.plot_widget.height()

        # Logos flush to the right edge; remember their left edge for the summary.
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
        # Keep watermarks + summaries pinned when the widget (and plot) resizes.
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

            name = f"{bookmaker} - {outcome_key[0]}"
            if outcome_key[1]:
                name += f" ({outcome_key[1]})"

            # Plot using the American odds values directly
            line = self.plot_widget.plot(
                timestamps,
                american_values,
                pen=pg.mkPen(color=color, width=2),
                name=name,
                symbol='o',
                symbolSize=6,
                symbolBrush=color
            )

            # Add labels only when odds change from previous value
            prev_american_value = None
            for i, ts in enumerate(timestamps):
                if i < len(american_values):
                    current_value = american_values[i]
                    american = american_prices[i]

                    # Only show label if value changed from previous point
                    if prev_american_value is None or current_value != prev_american_value:
                        # Format label based on whether we have point data
                        if ('points' in points_data and
                            points_data['points'] and
                            i < len(points_data['points']) and
                            points_data['points'][i] is not None):
                            pt = points_data['points'][i]
                            label_text = f"{self._decimal_to_american(float(american))} ({pt:.1f})"
                        else:
                            label_text = f"{american}"

                        # Use black color for Kalshi labels, bookmaker color for others
                        label_color = (0, 0, 0) if bookmaker == 'kalshi' else color
                        label = pg.TextItem(label_text, anchor=(0.5, 1.5), color=label_color)
                        self.plot_widget.addItem(label)
                        label.setPos(ts, current_value)

                    prev_american_value = current_value

            # Additive overlays: peak/trough markers (noise-gated) and a
            # net-since-open badge. Drawn once per outcome band, independent of
            # the change-label swarm above.
            self._draw_series_annotations(
                timestamps, american_values, color, outcome_key[0])

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

    # Minimum swing (in American-odds points) before peak/trough markers are
    # drawn. Below this a band is treated as flat and gets no markers, which
    # suppresses minute-level jitter and dead-quiet markets.
    PEAK_TROUGH_MIN_SWING = 6.0

    def _draw_series_annotations(self, timestamps, american_values, color, outcome_name):
        """Draw noise-gated peak/trough markers + a net-move summary badge.

        Additive only — at most one ScatterPlotItem (the two triangles) and one
        summary TextItem per outcome band. Deduped per BAND so the kalshi and
        polymarket series of the same side don't double up. Safe to no-op on
        short/degenerate series; on flat bands it shows the badge but no markers.
        """
        try:
            ts = np.asarray(timestamps, dtype=float)
            vals = np.asarray(american_values, dtype=float)
        except (ValueError, TypeError):
            return
        n = vals.size
        if n < 2 or ts.size != n:
            return

        # Dedup by BAND, not raw outcome name: kalshi/poly label the same side
        # differently ("St. Louis" vs "St. Louis Cardinals"), so keying on the
        # name annotated both. The two visible bands are the positive- and
        # negative-odds clusters, so the sign is a stable per-band key.
        band_key = 'pos' if float(vals[-1]) >= 0 else 'neg'
        bands = getattr(self, '_annotated_bands', set())
        if band_key in bands:
            return
        bands.add(band_key)

        # --- Net move since open (density-agnostic: first vs last) ---
        open_v, cur_v = float(vals[0]), float(vals[-1])
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
            # Triangles mark WHERE the extremes are (on the line, by design); the
            # numbers live in the fixed corner overlay, never over the lines.
            markers = pg.ScatterPlotItem(
                x=[float(ts[hi_i]), float(ts[lo_i])],
                y=[peak_v, trough_v],
                symbol=['t1', 't'],  # up-triangle = peak, down-triangle = trough
                size=12,
                brush=pg.mkBrush(color),
                pen=pg.mkPen('w', width=1),
            )
            self.plot_widget.addItem(markers)

        # Summary -> fixed corner overlay (top-right for +odds band, bottom-right
        # for -odds band). Widget-space, so it can't collide with the graph.
        updated = datetime.fromtimestamp(float(ts[-1])).strftime('%H:%M:%S')
        dhex = '#%02x%02x%02x' % dcolor
        summary_html = (
            f"<div style='font-size:9pt;color:#dcdcdc'>{open_v:+.0f}&#8594;{cur_v:+.0f} "
            f"<span style='color:{dhex}'>{arrow}{abs(delta):.0f}</span></div>"
            f"<div style='font-size:9pt;color:#c8c8c8'>range {swing:.0f}</div>"
            f"<div style='font-size:7pt;color:#7e8794'>updated {updated}</div>"
        )
        self._set_summary_label(band_key, summary_html)

    def _set_summary_label(self, band_key, html):
        label = self.summary_label_top if band_key == 'pos' else self.summary_label_bottom
        label.setText(html)
        label.adjustSize()
        label.show()
        self._position_overlays()

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

        # Set Y-axis range for American odds with proper padding
        # American odds must never be in the range (-100, +100) as this is invalid
        if all_american_prices:
            min_price = min(all_american_prices)
            max_price = max(all_american_prices)

            # Apply padding carefully to avoid crossing into invalid range
            if min_price >= 100:
                # All underdogs (positive odds)
                min_val = max(100, min_price * 0.95)
                max_val = max_price * 1.05
            elif max_price <= -100:
                # All favorites (negative odds)
                min_val = min_price * 1.05  # Make more negative
                max_val = min(-100, max_price * 0.95)  # Less negative but not above -100
            else:
                # Mixed: both favorites and underdogs
                # Handle each side separately
                min_val = min_price * 1.05  # Make more negative
                max_val = max_price * 1.05  # Make more positive

                # Ensure we don't cross into invalid range
                if min_val > -100:
                    min_val = -100
                if max_val < 100:
                    max_val = 100

            # Ensure we don't have identical min/max which would break the axis
            if abs(min_val - max_val) < 10:
                if min_val >= 100:
                    min_val = 100
                    max_val = min_val + 50
                elif max_val <= -100:
                    max_val = -100
                    min_val = max_val - 50
                else:
                    min_val = -200
                    max_val = 200

            self.plot_widget.setYRange(min_val, max_val)

            # Set up Y-axis label and ticks
            y_axis = self.plot_widget.getAxis('left')
            y_axis.setLabel('American Odds')

            # Create appropriate Y-axis ticks for American odds
            # Must avoid the invalid range between -100 and +100
            y_ticks = []

            # Determine if we're crossing the ±100 boundary
            crosses_boundary = min_val < -100 and max_val > 100

            if crosses_boundary:
                # We have both favorites and underdogs - create ticks on both sides
                # Ticks for favorites (negative side)
                neg_range = abs(min_val) - 100
                neg_step = neg_range / 3  # 3 ticks on negative side
                for i in range(4):
                    tick_val = min_val + (i * neg_step)
                    if tick_val <= -100:
                        y_ticks.append((tick_val, f"{int(tick_val)}"))

                # Add boundary ticks at ±100
                y_ticks.append((-100, "-100"))
                y_ticks.append((100, "+100"))

                # Ticks for underdogs (positive side)
                pos_range = max_val - 100
                pos_step = pos_range / 3  # 3 ticks on positive side
                for i in range(1, 4):
                    tick_val = 100 + (i * pos_step)
                    if tick_val >= 100:
                        y_ticks.append((tick_val, f"+{int(tick_val)}"))
            else:
                # All on one side - create evenly spaced ticks
                num_ticks = 5
                step = (max_val - min_val) / num_ticks
                current = min_val

                for i in range(num_ticks + 1):
                    # Ensure tick is in valid American odds range
                    if current >= 100:
                        y_ticks.append((current, f"+{int(current)}"))
                    elif current <= -100:
                        y_ticks.append((current, f"{int(current)}"))
                    # Skip any ticks in invalid range (-100, +100)
                    current += step

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
