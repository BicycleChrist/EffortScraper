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
from PyQt6.QtGui import QColor

from KalshiClient import KalshiClient
from polymarket_sports_client import PolymarketSportsClient
import re
from dataclasses import dataclass
from typing import Optional


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
                # Kalshi: "2023-11-07T05:31:56Z" (strike_date field)
                # Polymarket: "2025-01-15T19:00:00Z" (start_time field)
                from datetime import datetime
                if 'T' in self.start_time:
                    # Parse ISO datetime - handle both with and without 'Z' suffix
                    dt_str = self.start_time.replace('Z', '+00:00') if self.start_time.endswith('Z') else self.start_time
                    dt = datetime.fromisoformat(dt_str)
                    # Format as MM/DD HH:MMam/pm
                    date_str = dt.strftime("%m/%d %I:%M%p").lstrip('0').replace(' 0', ' ')
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

    # Team name variations for matching
    TEAM_ALIASES = {
        # NFL - All 32 teams
        # City names included - cross-sport conflicts OK since we only match within same sport
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
        'washington commanders': ['washington', 'commanders', 'wash', 'wsh'],

        # NBA
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
        'la clippers': ['los angeles clippers', 'la clippers', 'clippers', 'lac'],
        'la lakers': ['los angeles lakers', 'la lakers', 'lakers', 'lal'],
        'memphis grizzlies': ['memphis', 'grizzlies', 'mem'],
        'milwaukee bucks': ['milwaukee', 'bucks', 'mil'],
        'new york knicks': ['new york', 'knicks', 'nyk'],
        'oklahoma city thunder': ['oklahoma city', 'thunder', 'okc'],
        'orlando magic': ['orlando', 'magic', 'orl'],
        'philadelphia 76ers': ['philadelphia', '76ers', 'sixers', 'phi'],
        'phoenix suns': ['phoenix', 'suns', 'phx'],
        'portland trail blazers': ['portland', 'trail blazers', 'blazers', 'por'],
        'sacramento kings': ['sacramento', 'kings', 'sac'],
        'san antonio spurs': ['san antonio', 'spurs', 'sas'],
        'toronto raptors': ['toronto', 'raptors', 'tor'],
        'utah jazz': ['utah', 'jazz', 'uta'],

        # NHL - All 32 teams (Metropolitan, Atlantic, Central, Pacific)
        # Includes bare city names since NFL/NBA conflicts have been resolved
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
        'utah hockey club': ['utah', 'uta'],
        'vancouver canucks': ['vancouver', 'canucks', 'nucks', 'van'],
        'vegas golden knights': ['vegas', 'las vegas', 'golden knights', 'knights', 'vgk'],

        # MLB
        'yankees': ['new york yankees', 'yankees', 'nyy'],
        'red sox': ['boston red sox', 'red sox', 'bos'],
        'dodgers': ['los angeles dodgers', 'dodgers', 'lad'],
    }

    @staticmethod
    def normalize_team_name(team_name: str) -> str:
        """Normalize team name for matching"""
        if not team_name:
            return ""

        # Convert to lowercase and strip
        normalized = team_name.lower().strip()

        # Remove common suffixes
        normalized = re.sub(r'\s+(football|basketball|hockey|baseball)(\s+team)?$', '', normalized)

        # Check aliases - try exact match first, then substring
        for canonical, aliases in EventMatcher.TEAM_ALIASES.items():
            # Exact match in aliases
            if normalized in aliases:
                return canonical

        # If no exact match, try substring matching (more lenient)
        for canonical, aliases in EventMatcher.TEAM_ALIASES.items():
            # Check if any alias is contained in normalized name
            for alias in aliases:
                if alias in normalized and len(alias) >= 3:  # At least 3 chars to avoid false matches
                    return canonical

        return normalized

    @staticmethod
    def parse_kalshi_title(title: str) -> tuple[str, str]:
        """Parse Kalshi event title into (away_team, home_team)"""
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
    def teams_match(team1: str, team2: str) -> bool:
        """Check if two team names refer to the same team"""
        norm1 = EventMatcher.normalize_team_name(team1)
        norm2 = EventMatcher.normalize_team_name(team2)

        # Exact match
        if norm1 == norm2:
            return True

        # Check if one contains the other
        if norm1 in norm2 or norm2 in norm1:
            return True

        # Check aliases
        for canonical, aliases in EventMatcher.TEAM_ALIASES.items():
            if norm1 in aliases and norm2 in aliases:
                return True

        return False

    @staticmethod
    def events_match(kalshi_away: str, kalshi_home: str, poly_team1: str, poly_team2: str) -> bool:
        """
        Check if Kalshi and Polymarket events represent the same game.

        Since Polymarket doesn't always follow strict home/away conventions,
        we match if both teams are present regardless of order.
        """
        # Check if kalshi_home matches either poly team
        home_matches_1 = EventMatcher.teams_match(kalshi_home, poly_team1)
        home_matches_2 = EventMatcher.teams_match(kalshi_home, poly_team2)

        # Check if kalshi_away matches either poly team
        away_matches_1 = EventMatcher.teams_match(kalshi_away, poly_team1)
        away_matches_2 = EventMatcher.teams_match(kalshi_away, poly_team2)

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

        # Kalshi spread format: "Team wins by over X.X points?"
        kalshi_match = re.search(r'(?:over|under)\s+(\d+(?:\.\d+)?)\s+points?', text.lower())
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
    def get_market_period(text):
        """
        Determine the period/time frame for a market.

        Args:
            text: Market title or question text

        Returns:
            'full_game', '1h', '2h', '1q', '2q', '3q', '4q', or None
        """
        if not text:
            return 'full_game'  # Default to full game

        text_lower = text.lower()

        # First half
        if '1h' in text_lower or 'first half' in text_lower or '1st half' in text_lower or 'half 1' in text_lower:
            return '1h'

        # Second half
        if '2h' in text_lower or 'second half' in text_lower or '2nd half' in text_lower or 'half 2' in text_lower:
            return '2h'

        # Quarters
        if '1q' in text_lower or 'first quarter' in text_lower or '1st quarter' in text_lower:
            return '1q'
        if '2q' in text_lower or 'second quarter' in text_lower or '2nd quarter' in text_lower:
            return '2q'
        if '3q' in text_lower or 'third quarter' in text_lower or '3rd quarter' in text_lower:
            return '3q'
        if '4q' in text_lower or 'fourth quarter' in text_lower or '4th quarter' in text_lower:
            return '4q'

        return 'full_game'

    @staticmethod
    def get_market_type(kalshi_title=None, poly_question=None):
        """
        Determine market type from title/question.

        Returns: 'moneyline', 'spread', 'total', 'prop', or None
        """
        import re

        text = (kalshi_title or poly_question or '').lower()

        # Spread indicators (check first, more specific)
        if 'spread' in text or 'wins by' in text or re.search(r'\([+-]\d+', text):
            return 'spread'

        # Total indicators (check second, also specific)
        if 'total' in text or 'o/u' in text or 'over/under' in text:
            return 'total'

        # Props (check before moneyline, more specific)
        if any(keyword in text for keyword in ['touchdown', 'td', 'yards', 'passing', 'rushing', 'receiving']):
            return 'prop'

        # Moneyline indicators
        # Kalshi: "Dallas at Las Vegas Winner?"
        # Polymarket: "Cowboys vs. Raiders" (just team names with vs/vs./@ separator)
        if 'winner' in text or 'to win' in text:
            return 'moneyline'

        # Polymarket moneyline: Simple format with just teams and vs/@ separator
        # Pattern: "Team1 vs. Team2" or "Team1 vs Team2" or "Team1 @ Team2"
        if re.match(r'^[a-z\s]+\s+(?:vs\.?|@)\s+[a-z\s]+$', text.strip()):
            # Make sure it's not a spread or total (those have extra info)
            if 'spread' not in text and 'o/u' not in text and not re.search(r'\([+-]?\d+', text):
                return 'moneyline'

        return None

    @staticmethod
    def markets_match(kalshi_market, poly_market, home_team, away_team):
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

            # Normalize team names for comparison
            # Check if same team is favored (allowing for Dallas/Cowboys, Las Vegas/Raiders differences)
            from HistoricalOddsClient import EventMatcher
            teams_match = EventMatcher.teams_match(k_team_lower, p_team_lower)

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
                matches = abs(k_total - p_total) < 0.01
                print(f"          DEBUG Total: K={k_total}, P={p_total_raw} (rounded: {p_total}) -> {'MATCH' if matches else 'NO MATCH'}")
                return matches

            # If either is missing a value, can't match
            return False

        # Props: would need more sophisticated matching (not implemented yet)
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
                    limit=50,
                    include_orderbook=False,  # Skip orderbook for faster loading
                    include_trades=False      # Skip trades for faster loading
                )
            )
            return games
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

            for candle in candles:
                price_data = candle.get('price', {})

                # Try to get a price value - prefer close, fall back to previous
                close_price = price_data.get('close')
                if close_price is None:
                    close_price = price_data.get('previous')

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
        
        # TODO: only display for kalshi datasource
        self.kalshi_interval = QComboBox()
        self.kalshi_interval.addItems([f"{M}m" for M in (1, 60, 1440)])
        self.kalshi_interval.setFixedWidth(60)
        self.kalshi_interval.currentIndexChanged.connect(self.on_time_range_changed)
        header_layout.addWidget(self.kalshi_interval)

        self.refresh_button = QPushButton("↻")
        self.refresh_button.setFixedWidth(30)
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        header_layout.addWidget(self.refresh_button)

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
        self.plot_widget.addItem(pg.GridItem())

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
        PAST_EVENT_CUTOFF_HOURS = 12  # Keep events from last N hours (to include live/recent games)

        all_unified_events = []

        # Load events from all 4 major sports
        for sport in ['NFL', 'NBA', 'MLB', 'NHL']:
            print(f"\nLoading {sport}...")
            events = await self._load_unified_events_for_sport(sport)
            all_unified_events.extend(events)

        print(f"\n📊 Total events loaded from all sports: {len(all_unified_events)}")

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

            all_unified_events = filtered_events
            filtered_count = original_count - len(all_unified_events)

            if filtered_count > 0:
                print(f"🗑️  Filtered out {filtered_count} past events (older than {PAST_EVENT_CUTOFF_HOURS} hours)")
            print(f"📊 Showing {len(all_unified_events)} events")

        # Populate event selector with all events
        self.event_selector.blockSignals(True)
        self.event_selector.clear()

        for event in all_unified_events:
            display_title = event.get_display_title()
            self.event_selector.addItem(display_title, userData=event)

        self.event_selector.blockSignals(False)

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

        kalshi_task = loop.run_in_executor(None, fetch_kalshi_moneylines)
        polymarket_task = self.polymarket_client.get_sport_games(sport)

        kalshi_data, polymarket_games = await asyncio.gather(kalshi_task, polymarket_task)

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

            # Try to find matching Polymarket game
            matched_poly_game = None
            for p_game in active_polymarket_games:
                if p_game.id in matched_poly_ids:
                    continue

                p_title = p_game.title
                p_away, p_home = EventMatcher.parse_polymarket_title(p_title)

                if EventMatcher.events_match(k_away, k_home, p_away, p_home):
                    matched_poly_game = p_game
                    matched_poly_ids.add(p_game.id)
                    break

            # Create unified event
            # Parse start_time from event_ticker since strike_date doesn't exist
            event_ticker = k_event.get('event_ticker', '')
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

            unified_events.append(unified_event)

        # Second pass: add unmatched Polymarket games
        for p_game in active_polymarket_games:
            if p_game.id not in matched_poly_ids:
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

            # Try to find matching Polymarket game
            matched_poly_game = None
            for p_game in active_polymarket_games:
                if p_game.id in matched_poly_ids:
                    continue

                p_title = p_game.title
                p_away, p_home = EventMatcher.parse_polymarket_title(p_title)

                if EventMatcher.events_match(k_away, k_home, p_away, p_home):
                    matched_poly_game = p_game
                    matched_poly_ids.add(p_game.id)
                    break

            # Create unified event
            # Parse start_time from event_ticker since strike_date doesn't exist
            event_ticker = k_event.get('event_ticker', '')
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

        # Second pass: add unmatched Polymarket games
        for p_game in active_polymarket_games:
            if p_game.id not in matched_poly_ids:
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
        SHOW_PAST_EVENTS = False  # Set to True to show all events including past ones
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
                        unified_event.away_team
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
            # Priority 0 for matched markets, 1 for single-source
            has_both = 0 if (m.has_kalshi() and m.has_polymarket()) else 1
            market_type_order = type_order.get(m.market_type, 4)
            return (has_both, market_type_order, m.display_name)

        unified_markets.sort(key=sort_key)

        # Populate market selector
        for unified_market in unified_markets:
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
        if not self.auto_refresh_enabled or not self.data_source == 'kalshi':
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
        if self.data_source == 'kalshi' and self.auto_refresh_enabled:
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
                                        fidelity=60,
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
                                fidelity=60
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
        self.plot_widget.addItem(pg.GridItem())
        if not snapshots:
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
