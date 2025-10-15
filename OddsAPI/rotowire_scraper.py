#!/usr/bin/env python3
"""
RotoWire Picks API Scraper
Fetches odds and picks data from RotoWire's API endpoint

USAGE:
------
# Fetch and parse data
from rotowire_scraper import fetch_rotowire_data, RotoWireParser

data = fetch_rotowire_data(save_to_file=True)
parser = RotoWireParser(data)

# Get best EV props for NBA
nba_props = parser.get_best_ev_props(sport='NBA', min_diff=1.0, limit=10)
for prop in nba_props:
    print(f"{prop.player_name}: {prop.market_name}")
    print(f"  Projection: {prop.projection:.1f}")
    print(f"  Line: {prop.get_best_line()['line']}")
    print(f"  Diff: {prop.get_projection_diff():+.2f}")

# Filter props by various criteria
props = parser.get_props(
    sport='NFL',
    team='TB',
    market='Passing Yards',
    min_projection_diff=2.0
)

# Access individual prop data
prop = props[0]
print(prop.player_name)           # Player name
print(prop.player_team)           # Team abbreviation
print(prop.player_position)       # Position (QB, WR, etc)
print(prop.market_name)           # Bet type
print(prop.projection)            # RotoWire's projection
print(prop.event_time)            # Game datetime
print(prop.home_team, prop.away_team)  # Matchup
print(prop.weather)               # Weather data (NFL only)
print(prop.lines)                 # All sportsbook lines
print(prop.hit_rates)             # Historical performance

# Helper methods
prop.get_best_line('prizepicks')  # Get line from specific book
prop.get_projection_diff()        # Projection - line
prop.is_positive_ev(threshold=1.0) # Check if EV is positive
prop.format_recent_results()      # Format recent hit/miss history
"""

import requests
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class ParsedProp:
    """Fully parsed prop with all related data joined together"""
    # Prop info
    prop_id: str
    projection: float

    # Player info
    player_name: str
    player_team: str
    player_position: Optional[str]
    player_link: str
    player_photo: Optional[str]

    # Market info
    market_name: str
    sport: str
    category: str

    # Event/Game info
    event_time: datetime
    home_team: str
    away_team: str
    event_name: Optional[str] = None

    # Game odds context
    moneyline: Optional[Dict] = None
    spread: Optional[Dict] = None
    over_under: Optional[float] = None
    weather: Optional[Dict] = None

    # Betting lines from various books
    lines: List[Dict] = field(default_factory=list)

    # Historical performance
    hit_rates: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert ParsedProp to dictionary for JSON serialization"""
        return {
            'prop_id': self.prop_id,
            'projection': self.projection,
            'player_name': self.player_name,
            'player_team': self.player_team,
            'player_position': self.player_position,
            'player_link': self.player_link,
            'player_photo': self.player_photo,
            'market_name': self.market_name,
            'sport': self.sport,
            'category': self.category,
            'event_time': self.event_time.isoformat(),
            'event_time_unix': int(self.event_time.timestamp()),
            'home_team': self.home_team,
            'away_team': self.away_team,
            'event_name': self.event_name,
            'moneyline': self.moneyline,
            'spread': self.spread,
            'over_under': self.over_under,
            'weather': self.weather,
            'lines': self.lines,
            'hit_rates': self.hit_rates,
            # Computed fields
            'projection_diff': self.get_projection_diff(),
            'best_line': self.get_best_line(),
        }

    def get_best_line(self, book: Optional[str] = None) -> Optional[Dict]:
        """Get best line for a specific book or across all books"""
        if not self.lines:
            return None

        if book:
            for line in self.lines:
                if line.get('book') == book:
                    return line
            return None

        # Return first line if no book specified
        return self.lines[0]

    def get_projection_diff(self, book: Optional[str] = None) -> Optional[float]:
        """Calculate difference between projection and line"""
        line_data = self.get_best_line(book)
        if line_data:
            return self.projection - line_data.get('line', 0)
        return None

    def get_hit_rate_for_line(self, line_value: float) -> Optional[Dict]:
        """Get hit rate data for a specific line value"""
        for hr in self.hit_rates:
            if hr.get('line') == line_value:
                return hr
        return None

    def is_positive_ev(self, threshold: float = 0.0, book: Optional[str] = None) -> bool:
        """Check if projection is higher than line by threshold"""
        diff = self.get_projection_diff(book)
        return diff is not None and diff > threshold

    def format_recent_results(self, line_value: Optional[float] = None) -> str:
        """Format recent results string with emojis"""
        if not self.hit_rates:
            return "No data"

        hit_rate = self.hit_rates[0] if line_value is None else self.get_hit_rate_for_line(line_value)
        if not hit_rate:
            return "No data"

        recent = hit_rate.get('recent', '')
        if not recent:
            return "No data"

        # Convert binary string to visual format
        return recent.replace('1', '✓').replace('0', '✗')

    def __str__(self) -> str:
        """String representation for easy printing"""
        lines_str = ', '.join([f"{l['book']}: {l['line']}" for l in self.lines[:3]])
        return (
            f"{self.player_name} ({self.player_team}) - {self.market_name}\n"
            f"  Projection: {self.projection:.1f} | Lines: {lines_str}\n"
            f"  Game: {self.away_team} @ {self.home_team} ({self.event_time.strftime('%m/%d %I:%M%p')})"
        )


class RotoWireParser:
    """Parser for RotoWire API data with lookup tables for efficient joining"""

    def __init__(self, data: Dict[str, Any]):
        """
        Initialize parser with raw API data

        Args:
            data: Raw JSON response from RotoWire API
        """
        self.raw_data = data

        # Build lookup dictionaries for O(1) access
        self.entities_by_id = {e['entityID']: e for e in data.get('entities', [])}
        self.events_by_id = {e['eventID']: e for e in data.get('events', [])}
        self.markets_by_id = {m['marketID']: m for m in data.get('markets', [])}

        # Sport/team indexes for filtering
        self.props_by_sport = defaultdict(list)
        self.props_by_player = defaultdict(list)
        self.props_by_team = defaultdict(list)
        self.props_by_market = defaultdict(list)

        # Parse all props
        self.parsed_props: List[ParsedProp] = []
        self._parse_all_props()

    def _parse_all_props(self):
        """Parse all props and build indexes"""
        for prop_data in self.raw_data.get('props', []):
            try:
                parsed = self._parse_single_prop(prop_data)
                if parsed:
                    self.parsed_props.append(parsed)

                    # Build indexes
                    self.props_by_sport[parsed.sport].append(parsed)
                    self.props_by_player[parsed.player_name].append(parsed)
                    self.props_by_team[parsed.player_team].append(parsed)
                    self.props_by_market[parsed.market_name].append(parsed)
            except Exception as e:
                print(f"Warning: Failed to parse prop {prop_data.get('propID')}: {e}")
                continue

    def _parse_single_prop(self, prop_data: Dict) -> Optional[ParsedProp]:
        """Parse a single prop and join all related data"""

        # Get entity (player) info
        entity_ids = prop_data.get('entities', [])
        if not entity_ids:
            return None

        entity_id = entity_ids[0]  # Props only have single entity
        entity = self.entities_by_id.get(entity_id)
        if not entity:
            return None

        # Get event (game) info
        event_id = entity.get('eventID')
        event = self.events_by_id.get(event_id)
        if not event:
            return None

        # Get market info
        market_id = prop_data.get('marketID')
        market = self.markets_by_id.get(market_id)
        if not market:
            return None

        # Parse event time
        event_time = datetime.fromtimestamp(event.get('eventTime', 0))

        # Determine matchup (handle MMA/individual sports differently)
        if 'homeTeam' in event and 'awayTeam' in event:
            home_team = event['homeTeam']
            away_team = event['awayTeam']
            event_name = None
        else:
            # MMA or individual sport
            home_team = entity.get('name', '')
            away_team = event.get('opponent', 'TBD')
            event_name = event.get('eventName')

        # Create parsed prop
        parsed = ParsedProp(
            prop_id=prop_data.get('propID', ''),
            projection=prop_data.get('projection', 0.0),

            player_name=entity.get('name', ''),
            player_team=entity.get('team', home_team),
            player_position=entity.get('pos'),
            player_link=entity.get('link', ''),
            player_photo=entity.get('photo'),

            market_name=market.get('marketName', ''),
            sport=market.get('sport', entity.get('sport', '')),
            category=market.get('category', ''),

            event_time=event_time,
            home_team=home_team,
            away_team=away_team,
            event_name=event_name,

            moneyline=event.get('ml'),
            spread=event.get('spread'),
            over_under=event.get('ou'),
            weather=event.get('weather'),

            lines=prop_data.get('lines', []),
            hit_rates=prop_data.get('hitRates', [])
        )

        return parsed

    def get_props(
        self,
        sport: Optional[str] = None,
        team: Optional[str] = None,
        player: Optional[str] = None,
        market: Optional[str] = None,
        min_projection_diff: Optional[float] = None,
        book: Optional[str] = None
    ) -> List[ParsedProp]:
        """
        Filter props by various criteria

        Args:
            sport: Filter by sport (e.g., 'NFL', 'NBA', 'MLB')
            team: Filter by team abbreviation
            player: Filter by player name (exact match)
            market: Filter by market name (e.g., 'Passing Yards')
            min_projection_diff: Minimum projection - line difference
            book: Book to use for projection diff calculation

        Returns:
            List of ParsedProp objects matching criteria
        """
        props = self.parsed_props

        # Apply filters
        if sport:
            props = [p for p in props if p.sport == sport]

        if team:
            props = [p for p in props if p.player_team == team]

        if player:
            props = [p for p in props if p.player_name == player]

        if market:
            props = [p for p in props if p.market_name == market]

        if min_projection_diff is not None:
            props = [p for p in props if p.get_projection_diff(book) and
                    p.get_projection_diff(book) >= min_projection_diff]

        return props

    def get_best_ev_props(
        self,
        sport: Optional[str] = None,
        min_diff: float = 1.0,
        book: Optional[str] = None,
        limit: int = 20
    ) -> List[ParsedProp]:
        """
        Get props with best expected value (projection > line)

        Args:
            sport: Filter by sport
            min_diff: Minimum projection - line difference
            book: Book to use for line comparison
            limit: Maximum number of props to return

        Returns:
            List of ParsedProp objects sorted by EV
        """
        props = self.get_props(sport=sport, min_projection_diff=min_diff, book=book)

        # Sort by projection difference (highest first)
        props.sort(key=lambda p: p.get_projection_diff(book) or 0, reverse=True)

        return props[:limit]

    def get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics about the parsed data"""
        return {
            'total_props': len(self.parsed_props),
            'sports': dict([(sport, len(props)) for sport, props in self.props_by_sport.items()]),
            'unique_players': len(self.props_by_player),
            'unique_teams': len(self.props_by_team),
            'unique_markets': len(self.props_by_market),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert all parsed props to dictionary format for JSON serialization"""
        return {
            'metadata': {
                'fetch_time': datetime.now().isoformat(),
                'total_props': len(self.parsed_props),
                'sports': dict([(sport, len(props)) for sport, props in self.props_by_sport.items()]),
                'unique_players': len(self.props_by_player),
                'unique_teams': len(self.props_by_team),
                'unique_markets': len(self.props_by_market),
            },
            'props': [prop.to_dict() for prop in self.parsed_props],
            'sports_index': {sport: [prop.prop_id for prop in props]
                           for sport, props in self.props_by_sport.items()},
        }

    def save_to_json(self, filename: Optional[str] = None) -> str:
        """
        Save parsed props to JSON file

        Args:
            filename: Custom filename (optional)

        Returns:
            Path to saved file
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"rotowire_parsed_{timestamp}.json"

        data = self.to_dict()

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"✓ Parsed data saved to: {filename}")
        print(f"  - {len(self.parsed_props)} props")
        print(f"  - {len(self.props_by_player)} unique players")
        print(f"  - {len(self.props_by_sport)} sports")

        return filename

    @classmethod
    def load_from_json(cls, filename: str) -> 'RotoWireParser':
        """
        Load parsed props from JSON file

        Args:
            filename: Path to saved JSON file

        Returns:
            RotoWireParser instance with loaded data

        Note: This creates a parser from saved parsed data, not raw API data
        """
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Create an empty parser instance
        # We'll reconstruct it from the saved parsed props
        parser = cls.__new__(cls)
        parser.raw_data = {}
        parser.entities_by_id = {}
        parser.events_by_id = {}
        parser.markets_by_id = {}
        parser.props_by_sport = defaultdict(list)
        parser.props_by_player = defaultdict(list)
        parser.props_by_team = defaultdict(list)
        parser.props_by_market = defaultdict(list)
        parser.parsed_props = []

        # Reconstruct ParsedProp objects from dict
        for prop_dict in data.get('props', []):
            # Convert event_time back to datetime
            prop_dict['event_time'] = datetime.fromisoformat(prop_dict['event_time'])

            # Remove computed fields (they'll be regenerated)
            prop_dict.pop('projection_diff', None)
            prop_dict.pop('best_line', None)
            prop_dict.pop('event_time_unix', None)

            # Create ParsedProp object
            prop = ParsedProp(**prop_dict)
            parser.parsed_props.append(prop)

            # Rebuild indexes
            parser.props_by_sport[prop.sport].append(prop)
            parser.props_by_player[prop.player_name].append(prop)
            parser.props_by_team[prop.player_team].append(prop)
            parser.props_by_market[prop.market_name].append(prop)

        print(f"✓ Loaded {len(parser.parsed_props)} props from {filename}")

        return parser


def fetch_rotowire_data(
    save_raw: bool = False,
    save_parsed: bool = True,
    raw_filename: Optional[str] = None,
    parsed_filename: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch data from RotoWire picks API

    Args:
        save_raw: Whether to save the raw API response to JSON file
        save_parsed: Whether to save parsed props to JSON file
        raw_filename: Custom filename for raw data (optional)
        parsed_filename: Custom filename for parsed data (optional)

    Returns:
        Dictionary containing the API response data, or None if request fails
    """

    url = "https://www.rotowire.com/picks/api/lines.php"

    # Headers from the browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Referer': 'https://www.rotowire.com/picks/',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Priority': 'u=4',
        'TE': 'trailers'
    }

    # Note: Cookie has been removed for privacy/security
    # The API may work without authentication cookies, but if not,
    # you can add your session cookie here:
    # headers['Cookie'] = 'PHPSESSID=your_session_id_here; ...'

    try:
        print(f"Fetching data from {url}...")
        response = requests.get(url, headers=headers, timeout=30)

        # Check if request was successful
        response.raise_for_status()

        # Parse JSON response
        data = response.json()

        # Print summary of response
        print("\n✓ Data fetched successfully!")
        print("\nResponse Structure:")
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    print(f"  - {key}: {len(value)} items")
                elif isinstance(value, dict):
                    print(f"  - {key}: {len(value)} keys")
                else:
                    print(f"  - {key}: {type(value).__name__}")

        # Save raw data if requested
        if save_raw:
            if raw_filename is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                raw_filename = f"rotowire_raw_{timestamp}.json"

            with open(raw_filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            print(f"\n✓ Raw data saved to: {raw_filename}")

        # Parse and save parsed data if requested
        if save_parsed:
            print("\nParsing props...")
            parser = RotoWireParser(data)
            parser.save_to_json(parsed_filename)

        return data

    except requests.exceptions.RequestException as e:
        print(f"✗ Error fetching data: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"✗ Error parsing JSON response: {e}")
        return None


def fetch_and_parse(
    save_raw: bool = False,
    save_parsed: bool = True
) -> Optional[RotoWireParser]:
    """
    Fetch data from RotoWire API and return parsed data

    Args:
        save_raw: Whether to save raw API response
        save_parsed: Whether to save parsed props to JSON

    Returns:
        RotoWireParser instance with parsed data, or None if request fails
    """
    data = fetch_rotowire_data(save_raw=save_raw, save_parsed=save_parsed)
    if data:
        return RotoWireParser(data)
    return None


def extract_specific_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract and organize specific data from the API response

    Args:
        data: Full API response dictionary

    Returns:
        Dictionary with organized data
    """
    extracted = {
        'logos': data.get('logos', {}),
        'markets_count': len(data.get('markets', [])),
        'entities_count': len(data.get('entities', [])),
        'events_count': len(data.get('events', [])),
        'props_count': len(data.get('props', [])),
    }

    # Example: Get all events
    events = data.get('events', [])
    if events:
        print(f"\nFound {len(events)} events")
        # Print first event as example
        if len(events) > 0:
            print("\nFirst Event Example:")
            print(json.dumps(events[0], indent=2))

    # Example: Get all markets
    markets = data.get('markets', [])
    if markets:
        print(f"\nFound {len(markets)} markets")

    # Example: Get all props
    props = data.get('props', [])
    if props:
        print(f"\nFound {len(props)} props")

    return extracted


if __name__ == "__main__":
    # Fetch and parse the data (saves parsed JSON by default)
    parser = fetch_and_parse(save_raw=False, save_parsed=True)

    if parser:
        print("\n" + "="*80)
        print("ANALYZING PARSED DATA")
        print("="*80)

        # Get summary stats
        stats = parser.get_summary_stats()
        print(f"\n📊 Summary Statistics:")
        print(f"  Total Props: {stats['total_props']}")
        print(f"  Unique Players: {stats['unique_players']}")
        print(f"  Unique Teams: {stats['unique_teams']}")
        print(f"  Unique Markets: {stats['unique_markets']}")
        print(f"\n  Props by Sport:")
        for sport, count in sorted(stats['sports'].items(), key=lambda x: x[1], reverse=True):
            print(f"    {sport}: {count}")

        # Show best EV props overall
        print("\n" + "="*80)
        print("🔥 TOP 10 BEST EV PROPS (All Sports)")
        print("="*80)
        best_ev = parser.get_best_ev_props(min_diff=0.5, limit=10)
        for i, prop in enumerate(best_ev, 1):
            diff = prop.get_projection_diff()
            line_info = prop.get_best_line()

            print(f"\n{i}. {prop.sport} - {prop.player_name} ({prop.player_team})")
            print(f"   Market: {prop.market_name}")
            print(f"   Projection: {prop.projection:.1f}")
            print(f"   Best Line: {line_info['book']} @ {line_info['line']} (Diff: +{diff:.2f})")
            print(f"   Game: {prop.away_team} @ {prop.home_team} ({prop.event_time.strftime('%m/%d %I:%M%p')})")

            # Show hit rate if available
            if prop.hit_rates:
                hr = prop.hit_rates[0]
                recent = prop.format_recent_results()
                print(f"   Hit Rate: {hr.get('season', 0):.1f}% (season) | Recent: {recent[:10]}{'...' if len(recent) > 10 else ''}")

        # Show NBA props specifically
        print("\n" + "="*80)
        print("🏀 NBA PROPS (Projection > Line by 1.0+)")
        print("="*80)
        nba_props = parser.get_best_ev_props(sport='NBA', min_diff=1.0, limit=5)
        for i, prop in enumerate(nba_props, 1):
            print(f"\n{i}. {prop}")

            # Show all books
            print("   Books:")
            for line in prop.lines:
                diff = prop.projection - line['line']
                print(f"     {line['book']:12s} {line['line']:5.1f} (diff: {diff:+.2f})")

            # Show hit rate
            if prop.hit_rates and len(prop.hit_rates) > 0:
                hr = prop.hit_rates[0]
                season_rate = hr.get('season')
                if season_rate is not None:
                    print(f"   Season Hit Rate: {season_rate:.1f}%")
                    print(f"   Recent: {prop.format_recent_results()}")

        # Show NFL props
        print("\n" + "="*80)
        print("🏈 NFL PROPS (Projection > Line by 1.0+)")
        print("="*80)
        nfl_props = parser.get_best_ev_props(sport='NFL', min_diff=1.0, limit=5)
        if nfl_props:
            for i, prop in enumerate(nfl_props, 1):
                print(f"\n{i}. {prop}")
                if prop.weather:
                    weather = prop.weather.get('atStart', {})
                    print(f"   Weather: {weather.get('desc', 'N/A')}, {weather.get('temp', 'N/A')}°F, Wind: {weather.get('wind', 'N/A')} mph")
        else:
            print("  No NFL props with +1.0 edge found")

        # Example: Get all props for a specific player
        print("\n" + "="*80)
        print("📝 EXAMPLE: All props for first NBA player")
        print("="*80)
        if nba_props:
            first_player = nba_props[0].player_name
            player_props = parser.get_props(player=first_player)
            print(f"\nFound {len(player_props)} props for {first_player}:")
            for prop in player_props:
                diff = prop.get_projection_diff()
                diff_str = f"{diff:+5.2f}" if diff is not None else "  N/A"
                print(f"  - {prop.market_name:20s} Proj: {prop.projection:5.1f}  Diff: {diff_str}")
