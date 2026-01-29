#!/usr/bin/env python3
"""
Real-time flight tracking service for TravelViz
Integrates with OpenSky Network and correlates with game schedule
"""
import requests
import json
import time
import math
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path
from requests.auth import HTTPBasicAuth
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
from database_manager import DatabaseManager, TeamInfo
from opensky import NBAFlightTracker


# Continental US bounding box for efficient API queries
US_BOUNDING_BOX = {
    'lat_min': 24.0,   # Southern tip of Florida
    'lat_max': 50.0,   # Northern border
    'lon_min': -125.0, # West coast
    'lon_max': -66.0   # East coast
}

# Charter aircraft types used by pro sports teams
CHARTER_AIRCRAFT_TYPES = ['B752', 'B753', 'A21N', 'A321', 'B738', 'B739']


class AdsbLolClient:
    """Client for adsb.lol API - provides aircraft type filtering and direct lookups"""

    BASE_URL = "https://api.adsb.lol/v2"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'application/json'})

    def get_aircraft_by_registration(self, registration: str) -> Optional[Dict]:
        """Get aircraft by tail number/registration (e.g., N801DM)"""
        try:
            # Clean up registration - remove spaces, ensure uppercase
            reg = registration.strip().upper().replace('-', '')
            response = self.session.get(
                f"{self.BASE_URL}/reg/{reg}",
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            aircraft = data.get('ac', [])
            return aircraft[0] if aircraft else None
        except Exception as e:
            print(f"⚠️ adsb.lol registration lookup error for {registration}: {e}")
            return None

    def get_aircraft_by_hex(self, icao24: str) -> Optional[Dict]:
        """Get aircraft by ICAO24 hex code (e.g., a801dm)"""
        try:
            hex_id = icao24.strip().lower()
            response = self.session.get(
                f"{self.BASE_URL}/hex/{hex_id}",
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            aircraft = data.get('ac', [])
            return aircraft[0] if aircraft else None
        except Exception as e:
            print(f"⚠️ adsb.lol hex lookup error for {icao24}: {e}")
            return None

    def get_aircraft_by_callsign(self, callsign: str) -> Optional[Dict]:
        """Get aircraft by callsign (e.g., DAL123, OAE101)"""
        try:
            cs = callsign.strip().upper()
            response = self.session.get(
                f"{self.BASE_URL}/callsign/{cs}",
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            aircraft = data.get('ac', [])
            return aircraft[0] if aircraft else None
        except Exception as e:
            print(f"⚠️ adsb.lol callsign lookup error for {callsign}: {e}")
            return None

    def get_aircraft_by_type(self, type_code: str) -> List[Dict]:
        """Get all airborne aircraft of a specific type (e.g., B752, A21N)"""
        try:
            response = self.session.get(
                f"{self.BASE_URL}/type/{type_code}",
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return data.get('ac', [])
        except Exception as e:
            print(f"⚠️ adsb.lol error for {type_code}: {e}")
            return []

    def get_charter_aircraft(self, types: List[str] = None) -> List[Dict]:
        """Get all charter-type aircraft (B752, A21N, etc.)"""
        types = types or CHARTER_AIRCRAFT_TYPES
        all_aircraft = []
        seen_hex = set()

        for type_code in types:
            aircraft = self.get_aircraft_by_type(type_code)
            for ac in aircraft:
                hex_id = ac.get('hex', '').lower()
                if hex_id and hex_id not in seen_hex:
                    seen_hex.add(hex_id)
                    ac['aircraft_type'] = type_code  # Tag with type
                    all_aircraft.append(ac)

        return all_aircraft

    def filter_us_aircraft(self, aircraft: List[Dict]) -> List[Dict]:
        """Filter to Continental US only"""
        return [
            ac for ac in aircraft
            if ac.get('lat') and ac.get('lon')
            and US_BOUNDING_BOX['lat_min'] <= ac['lat'] <= US_BOUNDING_BOX['lat_max']
            and US_BOUNDING_BOX['lon_min'] <= ac['lon'] <= US_BOUNDING_BOX['lon_max']
        ]


@dataclass
class WatchlistEntry:
    """Entry in the flight watchlist"""
    identifier: str           # The tail number, hex, or callsign
    identifier_type: str      # 'registration', 'hex', or 'callsign'
    label: str               # User-friendly label (e.g., "Mavericks Charter")
    team_id: Optional[str] = None  # Optional team association
    added_at: datetime = field(default_factory=datetime.now)


class DirectFlightTracker(QThread):
    """
    Tracks specific aircraft by tail number, hex code, or callsign.
    Runs in background and emits signals when tracked aircraft are found.
    """

    # Signals
    flightFound = pyqtSignal(dict)      # Aircraft found and airborne
    flightUpdated = pyqtSignal(dict)    # Position update for tracked aircraft
    flightLost = pyqtSignal(str)        # Aircraft no longer visible (landed/out of range)
    statusUpdate = pyqtSignal(str)      # Status messages

    def __init__(self):
        super().__init__()
        self.adsb_client = AdsbLolClient()
        self.watchlist: Dict[str, WatchlistEntry] = {}  # identifier -> WatchlistEntry
        self.active_flights: Dict[str, Dict] = {}       # identifier -> last known position
        self.running = False
        self.update_interval = 10  # seconds

    def add_to_watchlist(self, identifier: str, identifier_type: str,
                         label: str = None, team_id: str = None) -> bool:
        """
        Add an aircraft to the watchlist.

        Args:
            identifier: Tail number (N801DM), hex code (a801dm), or callsign (DAL123)
            identifier_type: 'registration', 'hex', or 'callsign'
            label: Optional friendly name for the aircraft
            team_id: Optional team association

        Returns:
            True if added successfully
        """
        if identifier_type not in ('registration', 'hex', 'callsign'):
            print(f"❌ Invalid identifier type: {identifier_type}")
            return False

        # Normalize the identifier
        if identifier_type == 'registration':
            identifier = identifier.strip().upper().replace('-', '')
        elif identifier_type == 'hex':
            identifier = identifier.strip().lower()
        else:  # callsign
            identifier = identifier.strip().upper()

        entry = WatchlistEntry(
            identifier=identifier,
            identifier_type=identifier_type,
            label=label or identifier,
            team_id=team_id
        )

        self.watchlist[identifier] = entry
        print(f"✅ Added to watchlist: {entry.label} ({identifier_type}: {identifier})")
        self.statusUpdate.emit(f"Added {entry.label} to watchlist")
        return True

    def remove_from_watchlist(self, identifier: str) -> bool:
        """Remove an aircraft from the watchlist"""
        # Try to find by any format
        for key in list(self.watchlist.keys()):
            if key.lower() == identifier.lower() or key.upper() == identifier.upper():
                entry = self.watchlist.pop(key)
                if key in self.active_flights:
                    del self.active_flights[key]
                print(f"🗑️ Removed from watchlist: {entry.label}")
                self.statusUpdate.emit(f"Removed {entry.label} from watchlist")
                return True
        return False

    def get_watchlist(self) -> List[Dict]:
        """Get current watchlist as list of dicts"""
        return [
            {
                'identifier': e.identifier,
                'type': e.identifier_type,
                'label': e.label,
                'team_id': e.team_id,
                'is_active': e.identifier in self.active_flights
            }
            for e in self.watchlist.values()
        ]

    def clear_watchlist(self):
        """Clear all entries from watchlist"""
        self.watchlist.clear()
        self.active_flights.clear()
        self.statusUpdate.emit("Watchlist cleared")

    def _lookup_aircraft(self, entry: WatchlistEntry) -> Optional[Dict]:
        """Look up aircraft based on identifier type"""
        if entry.identifier_type == 'registration':
            return self.adsb_client.get_aircraft_by_registration(entry.identifier)
        elif entry.identifier_type == 'hex':
            return self.adsb_client.get_aircraft_by_hex(entry.identifier)
        else:  # callsign
            return self.adsb_client.get_aircraft_by_callsign(entry.identifier)

    def _parse_aircraft_data(self, ac: Dict, entry: WatchlistEntry) -> Dict:
        """Parse aircraft data into standardized format"""
        try:
            alt_ft = float(ac.get('alt_baro') or ac.get('alt_geom') or 0)
            speed_kts = float(ac.get('gs') or 0)
            heading = float(ac.get('track') or ac.get('true_heading') or 0)
        except (TypeError, ValueError):
            alt_ft, speed_kts, heading = 0, 0, 0

        return {
            'identifier': entry.identifier,
            'identifier_type': entry.identifier_type,
            'label': entry.label,
            'team_id': entry.team_id,
            'icao24': ac.get('hex', '').lower(),
            'callsign': (ac.get('flight') or ac.get('callsign', '')).strip(),
            'registration': ac.get('r', ''),
            'aircraft_type': ac.get('t') or ac.get('type', ''),
            'latitude': ac.get('lat'),
            'longitude': ac.get('lon'),
            'altitude_ft': alt_ft,
            'speed_kts': speed_kts,
            'heading': heading,
            'on_ground': ac.get('on_ground', False) or ac.get('ground', False),
            'timestamp': datetime.now().isoformat(),
            'source': 'direct_tracking'
        }

    def run(self):
        """Main tracking loop"""
        self.running = True
        self.statusUpdate.emit(f"🎯 Direct flight tracker started ({len(self.watchlist)} aircraft)")

        while self.running:
            if not self.watchlist:
                time.sleep(self.update_interval)
                continue

            found_count = 0
            for identifier, entry in list(self.watchlist.items()):
                try:
                    ac = self._lookup_aircraft(entry)

                    if ac and ac.get('lat') and ac.get('lon'):
                        flight_data = self._parse_aircraft_data(ac, entry)

                        if identifier in self.active_flights:
                            # Update existing flight
                            self.active_flights[identifier] = flight_data
                            self.flightUpdated.emit(flight_data)
                        else:
                            # New flight found
                            self.active_flights[identifier] = flight_data
                            self.flightFound.emit(flight_data)
                            status = "airborne" if not flight_data['on_ground'] else "on ground"
                            self.statusUpdate.emit(
                                f"✈️ {entry.label} found ({status})"
                            )

                        found_count += 1
                    else:
                        # Aircraft not visible
                        if identifier in self.active_flights:
                            del self.active_flights[identifier]
                            self.flightLost.emit(identifier)
                            self.statusUpdate.emit(f"📡 Lost signal: {entry.label}")

                except Exception as e:
                    print(f"⚠️ Error looking up {entry.label}: {e}")

            if found_count > 0:
                self.statusUpdate.emit(
                    f"🎯 Tracking {found_count}/{len(self.watchlist)} aircraft"
                )

            time.sleep(self.update_interval)

        self.statusUpdate.emit("🛑 Direct flight tracker stopped")

    def stop(self):
        """Stop the tracking thread"""
        self.running = False

    def check_single(self, identifier: str, identifier_type: str) -> Optional[Dict]:
        """
        One-shot lookup of a specific aircraft (doesn't add to watchlist).
        Useful for quick checks.
        """
        temp_entry = WatchlistEntry(
            identifier=identifier,
            identifier_type=identifier_type,
            label=identifier
        )
        ac = self._lookup_aircraft(temp_entry)
        if ac and ac.get('lat') and ac.get('lon'):
            return self._parse_aircraft_data(ac, temp_entry)
        return None


@dataclass
class LiveFlight:
    """Real-time flight information"""
    icao24: str
    callsign: Optional[str]
    latitude: float
    longitude: float
    altitude_m: float
    velocity_ms: float
    heading: float
    timestamp: datetime

    # Aircraft identification
    aircraft_type: Optional[str] = None  # B752, A21N, etc.
    registration: Optional[str] = None   # N-number

    # Team detection
    team_id: Optional[str] = None
    confidence: int = 0
    detection_reasons: List[str] = field(default_factory=list)

    # Flight path
    origin_airport: Optional[str] = None
    destination_airport: Optional[str] = None
    route_distance_km: Optional[float] = None

    @property
    def altitude_ft(self) -> float:
        return self.altitude_m * 3.28084 if self.altitude_m else 0

    @property
    def speed_kts(self) -> float:
        return self.velocity_ms * 1.94384 if self.velocity_ms else 0


class TeamFlightDetector:
    """Detects team charter flights using multiple heuristics"""

    # Confidence thresholds
    CONFIDENCE_THRESHOLD = 70       # Minimum to be detected at all (raised from 60)
    DISPLAY_THRESHOLD = 80          # Minimum to show on globe (high confidence)

    # Display tiers for UI
    TIER_CONFIRMED = 80             # Show on globe with full visualization
    TIER_LIKELY = 65                # Show in panel, maybe dimmed on globe
    TIER_POSSIBLE = 60              # Panel only

    # Dedicated charter operators - high confidence when detected
    CHARTER_PATTERNS = {
        'Omni Air': ['OAE'],                  # Sports team charter specialist
        'Miami Air': ['BSK'],                 # NBA/MLB charter operator
        'Swift Air': ['SWQ'],                 # Charter specialist
        'iAero Airways': ['WOK'],             # Formerly Swift Air (sports charters)
        'Eastern Airlines': ['EAL'],          # Charter operations
        'Sun Country Charter': ['SCX'],       # Does team charters
        'Atlas Air': ['GTI'],                 # Heavy charter operator
    }

    # Private jet operators - DISABLED: too many false positives
    # These operators have hundreds of aircraft, most unrelated to sports teams
    # Only enable if we have definitive team-owner aircraft registry data
    PRIVATE_JET_PATTERNS = {
        # Disabled - causes too much noise
        # 'NetJets': ['EJA', 'EJM'],
        # 'Flexjet': ['LXJ'],
        # 'VistaJet': ['VJT'],
        # 'Wheels Up': ['GAJ'],
        # 'XO Jet': ['XOJ'],
        # 'Jet Linx': ['JTL'],
    }

    # Major commercial carriers that DO team charters
    # These REQUIRE schedule correlation to be flagged (too many false positives otherwise)
    COMMERCIAL_CHARTER_CARRIERS = {
        'Delta': ['DAL'],                     # Major MLB/NBA/NHL charter partner
        'United': ['UAL'],                    # NBA charter partner
        'American': ['AAL'],                  # Various team contracts
    }

    # Commercial airlines to ALWAYS exclude (never do team charters)
    COMMERCIAL_AIRLINE_PREFIXES = [
        # Low-cost/budget carriers (don't do team charters)
        'SWA', 'JBU', 'NKS', 'FFT', 'ASA', 'HAL', 'VRD', 'AAY',
        # Regional carriers (planes too small)
        'SKW', 'RPA', 'ENY', 'PDT', 'ASH', 'GJS', 'JIA', 'MXY', 'CPZ',
        # Canadian carriers
        'ACA', 'WJA', 'TSC', 'ROU', 'PGT', 'POE',
        # Cargo carriers
        'UPS', 'FDX', 'ABX', 'ATN', 'KFS', 'CJT',
        # International (not doing domestic team charters)
        'BAW', 'AFR', 'DLH', 'KLM', 'EIN', 'VIR',
        # Other
        'FLE', 'TAI', 'VOI', 'TRS',
    ]

    # Aircraft characteristics for charter flights
    CHARTER_ALTITUDE_MIN_FT = 32000  # Typical charter cruise floor
    CHARTER_ALTITUDE_MAX_FT = 43000  # Typical charter cruise ceiling
    CHARTER_SPEED_MIN_KTS = 420      # Minimum cruise speed
    CHARTER_SPEED_MAX_KTS = 540      # Maximum cruise speed

    # Teams with known owned aircraft (rare but high confidence)
    TEAM_OWNED_AIRCRAFT = {
        'DAL': {'icao24': ['a801dm'], 'tail': ['N801DM']},  # Mavericks
        'HOU': {'icao24': ['a625hr'], 'tail': ['N625HR']},  # Rockets
        'LAL': {'icao24': ['a1979l'], 'tail': ['N1979L']},  # Lakers (historical)
    }

    def __init__(self, db: DatabaseManager, league: str = "NBA"):
        self.db = db
        self.league = league
        self.team_locations = {}  # team_id -> {'lat': float, 'lon': float, 'city': str}

        # Persistent connection for thread-safe database access
        # Using WAL mode for better concurrent read/write performance
        self._conn = None
        self._init_connection()

        self.load_team_locations()

    def _init_connection(self):
        """Initialize a persistent database connection with WAL mode"""
        try:
            self._conn = sqlite3.connect(
                self.db.db_path,
                check_same_thread=False,
                timeout=30.0  # Wait up to 30 seconds for locks
            )
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")  # 30 second busy timeout
            print(f"✅ TeamFlightDetector: Persistent DB connection initialized (WAL mode)")
        except Exception as e:
            print(f"⚠️ TeamFlightDetector: Failed to init persistent connection: {e}")
            self._conn = None

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a database connection with retry logic"""
        max_retries = 3
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                if self._conn is None:
                    self._init_connection()

                if self._conn:
                    # Test connection is still valid
                    self._conn.execute("SELECT 1")
                    return self._conn

            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "unable to open" in str(e).lower():
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))
                        self._conn = None  # Force reconnection
                        continue
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    self._conn = None
                    continue
                raise

        # Final fallback: create new connection
        return sqlite3.connect(
            self.db.db_path,
            check_same_thread=False,
            timeout=30.0
        )

    def close(self):
        """Close the persistent connection"""
        if self._conn:
            try:
                self._conn.close()
            except:
                pass
            self._conn = None

    def load_team_locations(self):
        """Load team home locations from database venues"""
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row

            # Get unique home venues for each team in the league
            # A team's home location is where they play home games
            cursor = conn.execute("""
                SELECT DISTINCT
                    g.home_team_id as team_id,
                    t.abbreviation,
                    t.display_name,
                    v.city,
                    v.latitude,
                    v.longitude
                FROM games g
                JOIN teams t ON g.home_team_id = t.team_id AND g.league = t.league
                JOIN venues v ON g.venue_id = v.venue_id
                WHERE g.league = ?
                AND v.latitude IS NOT NULL
                AND v.longitude IS NOT NULL
                GROUP BY g.home_team_id
            """, (self.league,))

            for row in cursor.fetchall():
                self.team_locations[row['team_id']] = {
                    'lat': row['latitude'],
                    'lon': row['longitude'],
                    'city': row['city'],
                    'name': row['display_name'],
                    'abbr': row['abbreviation']
                }

            if self.team_locations:
                print(f"✅ Loaded {len(self.team_locations)} {self.league} team locations from database")
            else:
                print(f"⚠️  No team locations found in database for {self.league}, using fallback")
                self._load_fallback_locations()

        except Exception as e:
            print(f"⚠️  Error loading team locations from DB: {e}")
            self._load_fallback_locations()

    def _load_fallback_locations(self):
        """Fallback static locations if database query fails"""
        # Static mapping as fallback (NBA teams)
        fallback = {
            'ATL': {'lat': 33.7573, 'lon': -84.3963, 'city': 'Atlanta'},
            'BOS': {'lat': 42.3662, 'lon': -71.0621, 'city': 'Boston'},
            'BKN': {'lat': 40.6826, 'lon': -73.9754, 'city': 'Brooklyn'},
            'CHA': {'lat': 35.2251, 'lon': -80.8392, 'city': 'Charlotte'},
            'CHI': {'lat': 41.8807, 'lon': -87.6742, 'city': 'Chicago'},
            'CLE': {'lat': 41.4965, 'lon': -81.6882, 'city': 'Cleveland'},
            'DAL': {'lat': 32.7905, 'lon': -96.8103, 'city': 'Dallas'},
            'DEN': {'lat': 39.7487, 'lon': -105.0077, 'city': 'Denver'},
            'DET': {'lat': 42.3410, 'lon': -83.0552, 'city': 'Detroit'},
            'GSW': {'lat': 37.7680, 'lon': -122.3879, 'city': 'San Francisco'},
            'HOU': {'lat': 29.7508, 'lon': -95.3621, 'city': 'Houston'},
            'IND': {'lat': 39.7640, 'lon': -86.1555, 'city': 'Indianapolis'},
            'LAC': {'lat': 34.0430, 'lon': -118.2673, 'city': 'Los Angeles'},
            'LAL': {'lat': 34.0430, 'lon': -118.2673, 'city': 'Los Angeles'},
            'MEM': {'lat': 35.1382, 'lon': -90.0506, 'city': 'Memphis'},
            'MIA': {'lat': 25.7814, 'lon': -80.1870, 'city': 'Miami'},
            'MIL': {'lat': 43.0451, 'lon': -87.9173, 'city': 'Milwaukee'},
            'MIN': {'lat': 44.9795, 'lon': -93.2760, 'city': 'Minneapolis'},
            'NOP': {'lat': 29.9490, 'lon': -90.0821, 'city': 'New Orleans'},
            'NYK': {'lat': 40.7505, 'lon': -73.9934, 'city': 'New York'},
            'OKC': {'lat': 35.4634, 'lon': -97.5151, 'city': 'Oklahoma City'},
            'ORL': {'lat': 28.5392, 'lon': -81.3839, 'city': 'Orlando'},
            'PHI': {'lat': 39.9012, 'lon': -75.1720, 'city': 'Philadelphia'},
            'PHX': {'lat': 33.4457, 'lon': -112.0712, 'city': 'Phoenix'},
            'POR': {'lat': 45.5316, 'lon': -122.6668, 'city': 'Portland'},
            'SAC': {'lat': 38.5802, 'lon': -121.4997, 'city': 'Sacramento'},
            'SAS': {'lat': 29.4270, 'lon': -98.4375, 'city': 'San Antonio'},
            'TOR': {'lat': 43.6435, 'lon': -79.3791, 'city': 'Toronto'},
            'UTA': {'lat': 40.7683, 'lon': -111.9011, 'city': 'Salt Lake City'},
            'WAS': {'lat': 38.8981, 'lon': -77.0209, 'city': 'Washington'},
        }
        self.team_locations = fallback
        print(f"📍 Loaded {len(self.team_locations)} fallback team locations")

    def get_team_travel_window(self, team_id: str) -> Optional[Dict]:
        """
        Get valid travel window for a team based on their schedule.
        Returns when the team could realistically be traveling.

        Returns:
            {
                'last_game_end': datetime,      # When their last game ended
                'earliest_departure': datetime,  # Earliest possible departure time
                'latest_departure': datetime,    # Latest reasonable departure
                'next_game_start': datetime,     # When their next game starts
                'required_arrival': datetime,    # Must arrive by this time (next_game - 6hrs)
                'origin_city': str,              # Where they're departing from
                'destination_city': str,         # Where they're going
            }
        """
        try:
            now = datetime.now()

            # Determine current season
            current_month = now.month
            current_year = now.year

            if self.league in ['NBA', 'NHL']:
                if current_month >= 10:
                    season_start = current_year
                else:
                    season_start = current_year - 1
                current_season = f"{season_start}-{str(season_start + 1)[2:]}"
            else:
                current_season = str(current_year)

            # Use persistent connection with retry logic
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row

            # Find LAST completed game for this team
            # Game is "completed" if start + 3.5 hours < now
            last_game_cursor = conn.execute("""
                SELECT
                    g.game_id, g.date as game_date, g.venue_id,
                    g.home_team_id, g.away_team_id,
                    v.city as venue_city, v.latitude, v.longitude
                FROM games g
                JOIN venues v ON g.venue_id = v.venue_id
                WHERE g.league = ?
                AND g.season = ?
                AND (g.home_team_id = ? OR g.away_team_id = ?)
                AND datetime(g.date, '+3 hours') < datetime('now', 'localtime')
                ORDER BY g.date DESC
                LIMIT 1
            """, (self.league, current_season, team_id, team_id))

            last_game = last_game_cursor.fetchone()

            # Find NEXT upcoming game for this team
            next_game_cursor = conn.execute("""
                SELECT
                    g.game_id, g.date as game_date, g.venue_id,
                    g.home_team_id, g.away_team_id,
                    v.city as venue_city, v.latitude, v.longitude
                FROM games g
                JOIN venues v ON g.venue_id = v.venue_id
                WHERE g.league = ?
                AND g.season = ?
                AND (g.home_team_id = ? OR g.away_team_id = ?)
                AND g.date > datetime('now', 'localtime')
                ORDER BY g.date ASC
                LIMIT 1
            """, (self.league, current_season, team_id, team_id))

            next_game = next_game_cursor.fetchone()

            if not last_game or not next_game:
                return None

            # Parse game times
            last_game_start = datetime.fromisoformat(last_game['game_date'].replace('Z', '+00:00')) if 'Z' in str(last_game['game_date']) else datetime.fromisoformat(str(last_game['game_date']))
            next_game_start = datetime.fromisoformat(next_game['game_date'].replace('Z', '+00:00')) if 'Z' in str(next_game['game_date']) else datetime.fromisoformat(str(next_game['game_date']))

            # Calculate game end time (start + ~3 hours for game + overtime buffer)
            last_game_end = last_game_start + timedelta(hours=3)

            # Team needs ~2 hours post-game before departure
            # (showers, media, bus to airport, boarding)
            earliest_departure = last_game_end + timedelta(hours=2)

            # Teams typically depart within 6 hours of game end
            # Unless it's a late game, then they might stay overnight
            latest_departure = last_game_end + timedelta(hours=12)

            # Team must arrive at least 6 hours before next game
            # (hotel check-in, shootaround, rest)
            required_arrival = next_game_start - timedelta(hours=6)

            # Determine origin city (where they just played)
            was_home_last = (last_game['home_team_id'] == team_id)
            origin_city = last_game['venue_city']

            # Determine destination city (where next game is)
            is_home_next = (next_game['home_team_id'] == team_id)
            destination_city = next_game['venue_city']

            # If both games are at same venue, no travel needed
            if last_game['venue_id'] == next_game['venue_id']:
                return None

            return {
                'last_game_end': last_game_end,
                'earliest_departure': earliest_departure,
                'latest_departure': latest_departure,
                'next_game_start': next_game_start,
                'required_arrival': required_arrival,
                'origin_city': origin_city,
                'destination_city': destination_city,
                'origin_lat': last_game['latitude'],
                'origin_lon': last_game['longitude'],
                'dest_lat': next_game['latitude'],
                'dest_lon': next_game['longitude'],
            }

        except Exception as e:
            print(f"⚠️  Error getting travel window for {team_id}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def set_league(self, league: str):
        """Change the league and reload team locations"""
        if league != self.league:
            self.league = league
            self.team_locations.clear()
            # Reinitialize connection for new league queries
            if self._conn:
                try:
                    self._conn.close()
                except:
                    pass
                self._conn = None
            self._init_connection()
            self.load_team_locations()

    def detect_team_flight(self, aircraft_state: tuple, upcoming_games: List) -> Optional[LiveFlight]:
        """
        Analyze aircraft state to determine if it's likely a team charter

        Args:
            aircraft_state: OpenSky state vector (17-element tuple)
            upcoming_games: List of games in next 48 hours

        Returns:
            LiveFlight object if detected, None otherwise
        """
        # Parse OpenSky state vector
        icao24 = aircraft_state[0]
        callsign = aircraft_state[1].strip() if aircraft_state[1] else None
        origin_country = aircraft_state[2]
        lon = aircraft_state[5]
        lat = aircraft_state[6]
        altitude = aircraft_state[7]  # barometric altitude in meters
        on_ground = aircraft_state[8]
        velocity = aircraft_state[9]  # m/s
        heading = aircraft_state[10]

        # === STAGE 1: Hard Filters (immediate exclusion) ===
        if on_ground or not lat or not lon:
            return None

        # Must be US-registered (also allows Canada for Toronto)
        if origin_country not in ("United States", "Canada"):
            return None

        # Must be at reasonable cruising altitude (above 15,000 ft / 4572m)
        # This filters out most general aviation and regional turboprops
        if not altitude or altitude < 4572:
            return None

        # Exclude known commercial airlines that never do team charters
        if callsign:
            callsign_upper = callsign.upper()
            for prefix in self.COMMERCIAL_AIRLINE_PREFIXES:
                if callsign_upper.startswith(prefix):
                    return None  # Definitely not a team flight

        # === STAGE 2: Confidence Scoring ===
        confidence = 0
        reasons = []
        detected_team = None

        # Heuristic 1: Known owned aircraft (100% confidence - definitive match)
        for team_id, aircraft_data in self.TEAM_OWNED_AIRCRAFT.items():
            if icao24.lower() in [ac.lower() for ac in aircraft_data['icao24']]:
                confidence = 100
                reasons.append(f"Known {team_id} team-owned aircraft")
                detected_team = team_id
                break

        if not detected_team:
            # Track which types of evidence we have
            has_charter_callsign = False
            has_private_jet_callsign = False
            has_commercial_charter_callsign = False
            has_schedule_match = False
            commercial_carrier_name = None

            callsign_upper = callsign.upper() if callsign else ""

            # === Callsign Classification ===

            # Check 1: Dedicated charter operators (+30) - need schedule match for full confidence
            # These operators also do non-sports charters, so callsign alone isn't definitive
            for charter_company, patterns in self.CHARTER_PATTERNS.items():
                if any(callsign_upper.startswith(p) for p in patterns):
                    confidence += 30
                    reasons.append(f"{charter_company} charter ({callsign})")
                    has_charter_callsign = True
                    break

            # Check 2: Commercial carriers that do team charters (requires schedule match)
            if not has_charter_callsign:
                for carrier_name, patterns in self.COMMERCIAL_CHARTER_CARRIERS.items():
                    if any(callsign_upper.startswith(p) for p in patterns):
                        has_commercial_charter_callsign = True
                        commercial_carrier_name = carrier_name
                        # Don't add confidence yet - requires schedule match
                        break

            # Check 3: Private jets (+15) - likely owner/executive, not full team
            # Lower bonus since private jets are common - require schedule match for high confidence
            if not has_charter_callsign and not has_commercial_charter_callsign:
                for company, patterns in self.PRIVATE_JET_PATTERNS.items():
                    if any(callsign_upper.startswith(p) for p in patterns):
                        confidence += 15
                        reasons.append(f"{company} private jet ({callsign})")
                        has_private_jet_callsign = True
                        break

            # === Proximity Check (do this first for commercial carrier logic) ===
            nearest_team = None
            nearest_distance = float('inf')
            nearest_city = None

            for team_id, location in self.team_locations.items():
                distance = self._haversine_distance(
                    lat, lon, location['lat'], location['lon']
                )
                if distance < nearest_distance:
                    nearest_distance = distance
                    nearest_team = team_id
                    nearest_city = location.get('city', 'Unknown')

            is_near_team_city = nearest_distance < 150  # Within 150km of a team city

            # === Schedule Correlation ===
            schedule_match = self._correlate_with_schedule(
                lat, lon, heading, upcoming_games
            )
            if schedule_match:
                has_schedule_match = True
                detected_team = schedule_match['team_id']

                if has_commercial_charter_callsign:
                    # Commercial carrier requires BOTH schedule match AND near team city
                    # (likely departing from team's home)
                    if is_near_team_city:
                        confidence += 45  # Good confidence for commercial + schedule + proximity
                        reasons.append(f"{commercial_carrier_name} charter ({callsign})")
                        reasons.append(schedule_match['reason'])
                        reasons.append(f"Departing near {nearest_city}")
                    else:
                        # Schedule match but not near team city - likely regular flight
                        return None
                else:
                    # Regular schedule match bonus for non-commercial carriers
                    confidence += schedule_match['confidence']
                    reasons.append(schedule_match['reason'])

            # If commercial carrier WITHOUT schedule match, reject it
            if has_commercial_charter_callsign and not has_schedule_match:
                return None  # Can't distinguish from regular commercial flight

            # Private jets without schedule match need higher threshold
            # (too many private jets flying that aren't team-related)
            if has_private_jet_callsign and not has_schedule_match and not has_charter_callsign:
                if confidence < 75:  # Require higher confidence for unscheduled private jets
                    return None

            # === Proximity Bonus (if not already credited for commercial carriers) ===
            # Add proximity bonus for non-commercial carriers - tighter radius
            if not has_commercial_charter_callsign and nearest_distance < 75:
                confidence += 15
                reasons.append(f"Near {nearest_city} ({nearest_distance:.0f}km)")

            # === Aircraft Characteristics ===
            # Only add for charter callsigns or schedule matches
            # Private jets alone don't get these bonuses (too many false positives)
            if has_charter_callsign or has_schedule_match:
                alt_ft = altitude * 3.28084
                if self.CHARTER_ALTITUDE_MIN_FT <= alt_ft <= self.CHARTER_ALTITUDE_MAX_FT:
                    confidence += 10
                    reasons.append(f"Cruise altitude ({alt_ft:.0f}ft)")

                if velocity:
                    speed_kts = velocity * 1.94384
                    if self.CHARTER_SPEED_MIN_KTS <= speed_kts <= self.CHARTER_SPEED_MAX_KTS:
                        confidence += 10
                        reasons.append(f"Cruise speed ({speed_kts:.0f}kts)")

            # Cap confidence at 100%
            confidence = min(confidence, 100)

            # === FINAL FILTER: Require strong evidence ===
            # Must have EITHER team-owned aircraft OR schedule correlation
            # Charter callsigns alone are not sufficient (too many charter flights)
            if not has_schedule_match and confidence < 100:
                return None  # Only team-owned aircraft (100%) pass without schedule

        # === STAGE 3: Threshold Check ===
        if confidence >= self.CONFIDENCE_THRESHOLD:
            flight = LiveFlight(
                icao24=icao24,
                callsign=callsign,
                latitude=lat,
                longitude=lon,
                altitude_m=altitude,
                velocity_ms=velocity if velocity else 0,
                heading=heading if heading else 0,
                timestamp=datetime.now(),
                team_id=detected_team,
                confidence=confidence,
                detection_reasons=reasons
            )
            return flight

        return None

    def detect_from_adsb_lol(self, ac: Dict, upcoming_games: List) -> Optional[LiveFlight]:
        """
        Detect team flight from adsb.lol aircraft data.
        Since we already filtered by type (B752/A21N), focus on schedule correlation.
        """
        hex_id = ac.get('hex', '').lower()
        callsign = (ac.get('flight') or ac.get('callsign', '')).strip() if ac.get('flight') or ac.get('callsign') else ''
        lat = ac.get('lat')
        lon = ac.get('lon')
        aircraft_type = ac.get('aircraft_type') or ac.get('t') or ac.get('type')
        registration = ac.get('r')

        # Parse numeric fields safely
        try:
            alt_ft = float(ac.get('alt_baro') or ac.get('alt_geom') or 0)
            heading = float(ac.get('track') or ac.get('true_heading') or 0)
            speed_kts = float(ac.get('gs') or 0)
        except (TypeError, ValueError):
            alt_ft, heading, speed_kts = 0, 0, 0

        # Basic validation
        if not lat or not lon or not hex_id:
            return None
        if ac.get('on_ground') or ac.get('ground'):
            return None
        if alt_ft < 15000:  # Must be at cruise altitude
            return None

        # Exclude carriers that don't do US/Canadian team charters
        if callsign:
            excluded = [
                'NKS', 'FFT', 'SWA', 'JBU', 'AAY', 'VRD',  # US budget
                'VIV', 'VOI', 'SCX', 'SLI', 'AMX',  # Mexican
                'TSC', 'WJA', 'ROU', 'PGT',  # Foreign leisure
            ]
            if any(callsign.upper().startswith(p) for p in excluded):
                return None

        # Start with base confidence for charter-type aircraft
        confidence = 30
        reasons = [f"{aircraft_type}"]

        # Check known team-owned aircraft (instant 100%)
        detected_team = None
        for team_id, aircraft_data in self.TEAM_OWNED_AIRCRAFT.items():
            if hex_id in [a.lower() for a in aircraft_data['icao24']]:
                confidence = 100
                reasons = [f"Known {team_id} aircraft"]
                detected_team = team_id
                break

        if not detected_team:
            # Check charter operator callsigns (+20)
            if callsign:
                for operator, patterns in self.CHARTER_PATTERNS.items():
                    if any(callsign.upper().startswith(p) for p in patterns):
                        confidence += 20
                        reasons.append(f"{operator} charter")
                        break

            # Check for charter-like flight numbers (8xxx/9xxx = often charters)
            if callsign and len(callsign) >= 4:
                try:
                    flight_num = int(''.join(c for c in callsign[3:] if c.isdigit()))
                    if flight_num >= 8000:
                        confidence += 10
                        reasons.append("charter-range callsign")
                except ValueError:
                    pass

            # Schedule correlation with ORIGIN verification (+50 max)
            schedule_match = self._correlate_with_schedule_and_origin(
                lat, lon, heading, upcoming_games
            )
            if schedule_match:
                confidence += schedule_match['confidence_boost']
                reasons.append(schedule_match['reason'])
                detected_team = schedule_match['team_id']

        # Threshold: need strong schedule match or be team-owned
        if confidence < 65:
            return None

        return LiveFlight(
            icao24=hex_id,
            callsign=callsign or None,
            latitude=lat,
            longitude=lon,
            altitude_m=alt_ft / 3.28084,
            velocity_ms=speed_kts / 1.94384,
            heading=heading,
            timestamp=datetime.now(),
            aircraft_type=aircraft_type,
            registration=registration,
            team_id=detected_team,
            confidence=min(confidence, 100),
            detection_reasons=reasons
        )

    def _haversine_distance(self, lat1: float, lon1: float,
                           lat2: float, lon2: float) -> float:
        """Calculate great circle distance in km"""
        R = 6371  # Earth radius in km
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c

    def _correlate_with_schedule(self, lat: float, lon: float,
                                 heading: float, upcoming_games: List) -> Optional[Dict]:
        """
        Correlate flight position/heading with upcoming game schedule.
        This is the most powerful heuristic for charter detection.

        Now detects BOTH:
        - Away teams traveling TO away games
        - Home teams returning home from road trips
        """
        if not heading:  # Skip if no heading data
            return None

        # Get games in next 24 hours
        now = datetime.now()
        tomorrow = now + timedelta(days=1)

        best_match = None
        best_confidence = 0

        for game in upcoming_games:
            if not (now <= game.date <= tomorrow):
                continue

            # Check if flight is heading toward game venue
            venue_lat = game.venue.latitude
            venue_lon = game.venue.longitude

            if not venue_lat or not venue_lon:
                continue

            distance_to_venue = self._haversine_distance(lat, lon, venue_lat, venue_lon)

            # Calculate bearing to venue
            bearing = self._calculate_bearing(lat, lon, venue_lat, venue_lon)
            heading_diff = abs(bearing - heading) % 360
            if heading_diff > 180:
                heading_diff = 360 - heading_diff

            # If heading toward venue and within reasonable distance
            if heading_diff < 30 and 150 < distance_to_venue < 1500:
                # Check BOTH teams - away team traveling TO game, home team returning

                # Option 1: Away team traveling to the game
                away_travel_window = self.get_team_travel_window(game.away_team.team_id)
                if away_travel_window:
                    confidence = 40
                    if best_confidence < confidence:
                        best_confidence = confidence
                        best_match = {
                            'confidence': confidence,
                            'reason': f"Heading to {game.venue.city} ({game.home_team.abbreviation} game)",
                            'team_id': game.away_team.team_id
                        }

                # Option 2: Home team returning from road trip
                home_travel_window = self.get_team_travel_window(game.home_team.team_id)
                if home_travel_window:
                    # Check if home team is actually returning (origin != destination)
                    origin_to_venue = self._haversine_distance(
                        home_travel_window['origin_lat'], home_travel_window['origin_lon'],
                        venue_lat, venue_lon
                    )
                    if origin_to_venue > 100:  # Home team IS traveling (returning from road trip)
                        confidence = 40
                        if best_confidence < confidence:
                            best_confidence = confidence
                            best_match = {
                                'confidence': confidence,
                                'reason': f"Returning to {game.venue.city} (home game vs {game.away_team.abbreviation})",
                                'team_id': game.home_team.team_id
                            }

        return best_match

    def _calculate_bearing(self, lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
        """Calculate bearing between two points"""
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = math.atan2(x, y)
        return (math.degrees(bearing) + 360) % 360

    def _correlate_with_schedule_and_origin(self, lat: float, lon: float,
                                            heading: float, upcoming_games: List) -> Optional[Dict]:
        """
        Enhanced schedule correlation that verifies:
        1. Flight is heading toward game venue (destination)
        2. Flight position is consistent with team's actual travel origin
        3. Flight timing aligns with team's travel window (game has ended, enough time to arrive)

        Now detects BOTH:
        - Away teams traveling TO away games
        - Home teams returning home from road trips
        """
        if not heading:
            return None

        now = datetime.now()
        best_match = None
        best_score = 0

        for game in upcoming_games:
            # Game should be within next 36 hours
            hours_until_game = (game.date - now).total_seconds() / 3600
            if hours_until_game < 0 or hours_until_game > 36:
                continue

            venue_lat = game.venue.latitude
            venue_lon = game.venue.longitude
            if not venue_lat or not venue_lon:
                continue

            # Check BOTH teams for this game - away team traveling TO game,
            # and home team potentially returning FROM a road trip
            teams_to_check = [
                (game.away_team.team_id, game.away_team.abbreviation, False),  # Away team
                (game.home_team.team_id, game.home_team.abbreviation, True),   # Home team (returning)
            ]

            for team_id, team_abbr, is_home_team in teams_to_check:
                match_result = self._evaluate_team_travel_match(
                    lat, lon, heading, now,
                    team_id, team_abbr, is_home_team,
                    game, venue_lat, venue_lon
                )

                if match_result and match_result['score'] > best_score:
                    best_score = match_result['score']
                    best_match = match_result['match']

        return best_match

    def _evaluate_team_travel_match(self, lat: float, lon: float, heading: float,
                                    now: datetime, team_id: str, team_abbr: str,
                                    is_home_team: bool, game, venue_lat: float,
                                    venue_lon: float) -> Optional[Dict]:
        """
        Evaluate if a flight matches a specific team's travel pattern.

        For away teams: checks if traveling from their home/last game to game venue
        For home teams: checks if returning from road trip to their home venue
        """
        # Get team's travel window to understand their actual travel needs
        travel_window = self.get_team_travel_window(team_id)

        if not travel_window:
            # No valid travel window means team doesn't need to travel
            return None

        # For home teams, they only need detection if returning from a road trip
        # (i.e., their origin is NOT the game venue)
        if is_home_team:
            # Check if home team is actually traveling (returning from away)
            origin_to_venue_dist = self._haversine_distance(
                travel_window['origin_lat'], travel_window['origin_lon'],
                venue_lat, venue_lon
            )
            if origin_to_venue_dist < 100:
                # Home team's last game was at home, no travel needed
                return None

        # Use the travel window's actual origin (where team last played)
        origin_lat = travel_window['origin_lat']
        origin_lon = travel_window['origin_lon']
        origin_city = travel_window['origin_city']
        dest_city = travel_window['destination_city']

        # Calculate distances
        dist_to_venue = self._haversine_distance(lat, lon, venue_lat, venue_lon)
        dist_from_origin = self._haversine_distance(origin_lat, origin_lon, lat, lon)
        total_route = self._haversine_distance(origin_lat, origin_lon, venue_lat, venue_lon)

        # Skip if route is too short (no flight needed) or too long
        if total_route < 300 or total_route > 4500:
            return None

        # Check if flight position is ON the route (between origin and destination)
        route_progress = dist_from_origin / total_route if total_route > 0 else 0
        expected_dist_to_venue = total_route - dist_from_origin
        dist_deviation = abs(dist_to_venue - expected_dist_to_venue) / total_route if total_route > 0 else 1

        if dist_deviation > 0.25:  # More than 25% off route
            return None
        if route_progress < 0.1 or route_progress > 0.95:  # Too close to endpoints
            return None

        # Verify heading toward venue
        bearing_to_venue = self._calculate_bearing(lat, lon, venue_lat, venue_lon)
        heading_diff = abs(bearing_to_venue - heading) % 360
        if heading_diff > 180:
            heading_diff = 360 - heading_diff
        if heading_diff > 35:  # Must be heading roughly toward venue
            return None

        # === TIMING VALIDATION ===
        timing_bonus = 0
        timing_reason = ""

        # Calculate estimated flight duration (assume ~800 km/h cruise)
        flight_duration_hours = total_route / 800.0

        # Estimate when this flight departed based on progress
        estimated_elapsed_hours = flight_duration_hours * route_progress
        estimated_departure = now - timedelta(hours=estimated_elapsed_hours)

        # Estimate when this flight will arrive
        remaining_hours = flight_duration_hours * (1 - route_progress)
        estimated_arrival = now + timedelta(hours=remaining_hours)

        # VALIDATION 1: Departure should be after earliest possible departure
        if estimated_departure < travel_window['earliest_departure']:
            return None

        # VALIDATION 2: Departure should be before latest reasonable departure
        if estimated_departure > travel_window['latest_departure']:
            return None

        # VALIDATION 3: Arrival should be before required arrival time
        if estimated_arrival > travel_window['required_arrival']:
            return None

        # BONUS: If timing aligns well with travel window, add confidence
        hours_since_game_end = (now - travel_window['last_game_end']).total_seconds() / 3600
        if 2 <= hours_since_game_end <= 8:
            timing_bonus += 15  # Prime travel window
        elif 8 < hours_since_game_end <= 14:
            timing_bonus += 10  # Reasonable window (overnight departure)
        else:
            timing_bonus += 5   # Still valid but less likely

        # Calculate confidence score
        score = 50  # Base for matching route
        if heading_diff < 15:
            score += 10  # Very accurate heading
        if 0.3 < route_progress < 0.7:
            score += 10  # Mid-flight (most certain)
        if dist_deviation < 0.1:
            score += 5   # Very close to great circle
        score += timing_bonus

        # Build reason string
        if is_home_team:
            # Home team returning from road trip
            reason = f"{team_abbr}→{dest_city} (returning home, {route_progress*100:.0f}% enroute)"
        else:
            # Away team traveling to game
            opponent_abbr = game.home_team.abbreviation
            reason = f"{team_abbr}→{dest_city} ({opponent_abbr} game, {route_progress*100:.0f}% enroute)"

        return {
            'score': score,
            'match': {
                'confidence_boost': score,
                'reason': reason,
                'team_id': team_id,
                'route_progress': route_progress,
                'distance_to_venue': dist_to_venue,
                'total_route': total_route,
                'is_returning_home': is_home_team,
            }
        }


class RealTimeFlightTracker(QThread):
    """
    Background service for real-time flight tracking
    Runs continuously and emits signals when team flights are detected
    """

    flightDetected = pyqtSignal(dict)  # Emits when new team flight detected
    flightUpdated = pyqtSignal(dict)   # Emits position updates for tracked flights
    flightLanded = pyqtSignal(str)     # Emits when tracked flight lands (icao24)
    statusUpdate = pyqtSignal(str)     # Status messages

    def __init__(self, db: DatabaseManager, api_keys: Dict, league: str = "NBA"):
        super().__init__()
        self.db = db
        self.league = league
        self.running = False

        # Initialize adsb.lol client (primary - type-based filtering)
        self.adsb_lol = AdsbLolClient()

        # Initialize OpenSky client (fallback)
        self.opensky = NBAFlightTracker(
            client_id=api_keys.get('clientIdOS'),
            client_secret=api_keys.get('clientSecretOS'),
            username=api_keys.get('open_sky_user'),
            password=api_keys.get('open_sky_pwd')
        )

        # Initialize flight detector with league
        self.detector = TeamFlightDetector(db, league)

        # Tracking state
        self.tracked_flights: Dict[str, LiveFlight] = {}  # icao24 -> LiveFlight
        self.update_interval = 15  # seconds

        # Data source preference
        self.use_type_filtering = True  # Use adsb.lol type filtering
        self.aircraft_types = ['B752', 'A21N']  # Primary charter types

        # Schedule cache
        self.upcoming_games = []
        self.schedule_refresh_timer = QTimer()
        self.schedule_refresh_timer.timeout.connect(self.refresh_schedule)
        self.schedule_refresh_timer.start(300000)  # Refresh every 5 minutes

        # Note: refresh_schedule() is called in run() to avoid blocking main thread
        self._initialized = False

    def set_league(self, league: str):
        """Change the tracked league (non-blocking)"""
        if league != self.league:
            self.league = league
            self.detector.set_league(league)
            # Mark as needing re-initialization so schedule refreshes in background
            self._initialized = False
            self.statusUpdate.emit(f"Switched to tracking {league} flights")

    def refresh_schedule(self):
        """Refresh upcoming games schedule from database"""
        try:
            # Determine current season based on league
            now = datetime.now()
            current_year = now.year
            current_month = now.month

            if self.league in ['NBA', 'NHL']:
                # NBA/NHL seasons span two years (e.g., 2024-25)
                # Season runs October to June
                if current_month >= 10:  # October or later - new season
                    season_start = current_year
                else:  # Before October - still in previous season
                    season_start = current_year - 1
                current_season = f"{season_start}-{str(season_start + 1)[2:]}"
            else:  # MLB
                current_season = str(current_year)

            # Load games from database
            all_games = self.db.load_games(current_season, self.league)

            # Filter to next 48 hours
            cutoff = now + timedelta(hours=48)

            self.upcoming_games = [
                g for g in all_games
                if now <= g.date <= cutoff
            ]

            if self.upcoming_games:
                self.statusUpdate.emit(
                    f"📅 {self.league} schedule: {len(self.upcoming_games)} games in next 48h"
                )
            else:
                self.statusUpdate.emit(
                    f"📅 No {self.league} games in next 48h (season: {current_season})"
                )

        except Exception as e:
            self.statusUpdate.emit(f"⚠️  Schedule refresh error: {e}")
            self.upcoming_games = []

    def run(self):
        """Main tracking loop - runs in background thread"""
        self.running = True

        # === ASYNC INITIALIZATION (runs in background thread) ===
        if not self._initialized:
            self.statusUpdate.emit(f"🔄 Initializing {self.league} flight tracker...")

            # Load schedule (database query - usually fast but moved here for safety)
            self.refresh_schedule()

            # Check API connectivity (HTTP request - can be slow)
            self.statusUpdate.emit("🔍 Checking API connectivity...")
            if self.use_type_filtering:
                # Test adsb.lol
                test_data = self.adsb_lol.get_charter_aircraft(['B752'])
                if test_data is not None:
                    self.statusUpdate.emit("✅ adsb.lol API connected")
                else:
                    self.statusUpdate.emit("⚠️ adsb.lol unavailable - will retry")
            else:
                # Test OpenSky
                if self.opensky.check_api_status():
                    self.statusUpdate.emit("✅ OpenSky API connected")
                else:
                    self.statusUpdate.emit("⚠️ OpenSky API unavailable - will retry")

            self._initialized = True

        mode = f"type-filtered ({', '.join(self.aircraft_types)})" if self.use_type_filtering else "OpenSky"
        self.statusUpdate.emit(f"🛫 Real-time {self.league} flight tracker started ({mode})")

        consecutive_errors = 0
        max_consecutive_errors = 3
        backoff_time = 30
        max_backoff = 300

        while self.running:
            try:
                # Collect ALL candidate flights first, then filter to best per team
                candidate_flights: List[LiveFlight] = []

                if self.use_type_filtering:
                    # Primary: adsb.lol type-based filtering
                    aircraft_list = self.adsb_lol.get_charter_aircraft(self.aircraft_types)
                    aircraft_list = self.adsb_lol.filter_us_aircraft(aircraft_list)
                    aircraft_count = len(aircraft_list)

                    if not aircraft_list:
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            self.statusUpdate.emit(f"⚠️ adsb.lol unavailable - retry in {backoff_time}s")
                            time.sleep(backoff_time)
                            backoff_time = min(backoff_time * 2, max_backoff)
                            consecutive_errors = 0
                        continue

                    consecutive_errors = 0
                    backoff_time = 30

                    for ac in aircraft_list:
                        flight = self.detector.detect_from_adsb_lol(ac, self.upcoming_games)
                        if flight:
                            candidate_flights.append(flight)

                else:
                    # Fallback: OpenSky broad scan
                    states_data = self.opensky.get_us_states()
                    if not states_data or 'states' not in states_data:
                        consecutive_errors += 1
                        time.sleep(self.update_interval)
                        continue

                    consecutive_errors = 0
                    aircraft_count = len(states_data['states'])

                    for state in states_data['states']:
                        flight = self.detector.detect_team_flight(state, self.upcoming_games)
                        if flight:
                            candidate_flights.append(flight)

                # === PHASE 1: SINGLE FLIGHT PER TEAM SELECTION ===
                # Group candidates by team_id and pick the best one for each team
                best_flights = self._select_best_flight_per_team(candidate_flights)
                current_icao24s = set()

                for flight in best_flights:
                    current_icao24s.add(flight.icao24)
                    self._process_detected_flight(flight)

                # Check for landed flights
                landed = set(self.tracked_flights.keys()) - current_icao24s
                for icao24 in landed:
                    flight_info = self.tracked_flights.pop(icao24, None)
                    callsign = flight_info.callsign if flight_info else icao24
                    self.statusUpdate.emit(f"🛬 Landed: {callsign}")
                    self.flightLanded.emit(icao24)

                # Status update
                types_str = '/'.join(self.aircraft_types)
                rejected_count = len(candidate_flights) - len(best_flights)
                if self.tracked_flights:
                    self.statusUpdate.emit(
                        f"👁️ Tracking {len(self.tracked_flights)} {self.league} flight(s) | "
                        f"{aircraft_count} {types_str}s in US"
                        + (f" | {rejected_count} duplicate candidates filtered" if rejected_count > 0 else "")
                    )
                else:
                    self.statusUpdate.emit(
                        f"🔍 {aircraft_count} {types_str}s in US | {len(self.upcoming_games)} games upcoming"
                    )

            except Exception as e:
                self.statusUpdate.emit(f"❌ Error: {e}")
                consecutive_errors += 1

            time.sleep(self.update_interval)

        self.statusUpdate.emit("🛑 Flight tracker stopped")

    def _select_best_flight_per_team(self, candidates: List[LiveFlight]) -> List[LiveFlight]:
        """
        Given a list of candidate flights, select only ONE flight per team.
        Uses enhanced scoring to pick the best candidate.

        Scoring factors:
        - Base confidence score (existing)
        - Charter callsign pattern (8xxx/9xxx = +points)
        - Route progress alignment with expected timing
        """
        if not candidates:
            return []

        # Group candidates by team_id
        by_team: Dict[str, List[LiveFlight]] = {}
        no_team: List[LiveFlight] = []  # Flights without team attribution

        for flight in candidates:
            if flight.team_id:
                if flight.team_id not in by_team:
                    by_team[flight.team_id] = []
                by_team[flight.team_id].append(flight)
            else:
                # Keep unattributed flights if they have high confidence
                if flight.confidence >= 80:
                    no_team.append(flight)

        best_flights: List[LiveFlight] = []

        # For each team, pick the best candidate
        for team_id, team_candidates in by_team.items():
            if len(team_candidates) == 1:
                best_flights.append(team_candidates[0])
            else:
                # Score each candidate and pick the best
                scored = []
                for flight in team_candidates:
                    score = self._calculate_flight_score(flight, team_id)
                    scored.append((score, flight))

                # Sort by score descending, pick the best
                scored.sort(key=lambda x: x[0], reverse=True)
                best_flight = scored[0][1]
                best_flights.append(best_flight)

                # Log the selection for debugging
                if len(team_candidates) > 1:
                    rejected_count = len(team_candidates) - 1
                    print(f"📊 {team_id}: Selected {best_flight.callsign or best_flight.icao24} "
                          f"(score: {scored[0][0]:.1f}) from {len(team_candidates)} candidates, "
                          f"rejected {rejected_count}")

        # Add high-confidence unattributed flights
        best_flights.extend(no_team)

        return best_flights

    def _calculate_flight_score(self, flight: LiveFlight, team_id: str) -> float:
        """
        Calculate enhanced score for a flight candidate.

        Scoring components:
        - Base confidence (0-100)
        - Charter callsign bonus (0-15)
        - Timing alignment bonus (0-20)
        - Known aircraft type bonus (0-10)
        """
        score = float(flight.confidence)

        # Charter callsign pattern bonus (8xxx/9xxx flight numbers are often charters)
        if flight.callsign:
            callsign = flight.callsign.upper()
            try:
                # Extract numeric portion after carrier code
                numeric_part = ''.join(c for c in callsign[3:] if c.isdigit())
                if numeric_part:
                    flight_num = int(numeric_part)
                    if flight_num >= 9000:
                        score += 15  # Very likely charter
                    elif flight_num >= 8000:
                        score += 10  # Likely charter
            except (ValueError, IndexError):
                pass

        # Known charter operator bonus
        for operator, patterns in TeamFlightDetector.CHARTER_PATTERNS.items():
            if flight.callsign and any(flight.callsign.upper().startswith(p) for p in patterns):
                score += 10
                break

        # Aircraft type bonus (preferred charter types)
        if flight.aircraft_type:
            if flight.aircraft_type in ['B752', 'B753']:
                score += 10  # Classic team charter aircraft
            elif flight.aircraft_type in ['A21N', 'A321']:
                score += 8   # Modern narrow-body charter
            elif flight.aircraft_type in ['B738', 'B739']:
                score += 5   # Common charter type

        # Timing alignment with team's travel window
        travel_window = self.detector.get_team_travel_window(team_id)
        if travel_window:
            now = datetime.now()
            hours_since_game_end = (now - travel_window['last_game_end']).total_seconds() / 3600

            # Prime departure window: 2-6 hours after game end
            if 2 <= hours_since_game_end <= 6:
                score += 20
            # Reasonable window: 6-10 hours (overnight departure)
            elif 6 < hours_since_game_end <= 10:
                score += 15
            # Late window: 10-14 hours
            elif 10 < hours_since_game_end <= 14:
                score += 10
            # Very late: >14 hours (possible but less likely)
            elif hours_since_game_end > 14:
                score += 5

        return score

    def _process_detected_flight(self, flight: LiveFlight):
        """Handle a detected flight - emit signals as needed"""
        if flight.icao24 in self.tracked_flights:
            self.tracked_flights[flight.icao24] = flight
            self.flightUpdated.emit(self._flight_to_dict(flight))
        else:
            self.tracked_flights[flight.icao24] = flight
            self.flightDetected.emit(self._flight_to_dict(flight))
            type_str = f"[{flight.aircraft_type}]" if flight.aircraft_type else ""
            self.statusUpdate.emit(
                f"✈️ NEW: {flight.team_id or 'Charter'} {type_str} - "
                f"{flight.callsign or flight.icao24} ({flight.confidence}%)"
            )

    def stop(self):
        """Stop the tracking thread"""
        self.running = False

    def _flight_to_dict(self, flight: LiveFlight) -> Dict:
        """Convert LiveFlight to dictionary for signal emission"""
        return {
            'icao24': flight.icao24,
            'callsign': flight.callsign,
            'team_id': flight.team_id,
            'aircraft_type': flight.aircraft_type,
            'registration': flight.registration,
            'latitude': flight.latitude,
            'longitude': flight.longitude,
            'altitude_ft': flight.altitude_ft,
            'speed_kts': flight.speed_kts,
            'heading': flight.heading,
            'confidence': flight.confidence,
            'reasons': flight.detection_reasons,
            'timestamp': flight.timestamp.isoformat()
        }


def test_single_scan(league: str = "NBA", use_type_filter: bool = True):
    """Run a single scan cycle for testing (no threading)"""
    print("=" * 70)
    print(f"LIVE FLIGHT TRACKER - SINGLE SCAN TEST ({league})")
    print("=" * 70)

    db = DatabaseManager()
    detector = TeamFlightDetector(db, league)

    print(f"\n📍 Loaded {len(detector.team_locations)} team locations")
    print(f"✈️  Charter types: {CHARTER_AIRCRAFT_TYPES}")

    # Load upcoming games
    now = datetime.now()
    if league in ['NBA', 'NHL']:
        season_start = now.year if now.month >= 10 else now.year - 1
        current_season = f"{season_start}-{str(season_start + 1)[2:]}"
    else:
        current_season = str(now.year)

    try:
        all_games = db.load_games(current_season, league)
        cutoff = now + timedelta(hours=48)
        upcoming_games = [g for g in all_games if now <= g.date <= cutoff]
        print(f"📅 {len(upcoming_games)} {league} games in next 48h")
    except Exception as e:
        print(f"⚠️  Could not load games: {e}")
        upcoming_games = []

    detected_flights = []

    if use_type_filter:
        # Use adsb.lol type-based filtering
        adsb = AdsbLolClient()
        types_to_scan = ['B752', 'A21N']

        print(f"\n🌐 Fetching {types_to_scan} aircraft from adsb.lol...")

        for type_code in types_to_scan:
            aircraft = adsb.get_aircraft_by_type(type_code)
            us_aircraft = adsb.filter_us_aircraft([{**ac, 'aircraft_type': type_code} for ac in aircraft])
            print(f"   {type_code}: {len(aircraft)} global, {len(us_aircraft)} in US")

            for ac in us_aircraft:
                flight = detector.detect_from_adsb_lol(ac, upcoming_games)
                if flight:
                    detected_flights.append(flight)
    else:
        # Fallback to OpenSky
        try:
            with open('api_keys.json') as f:
                keys = json.load(f)
        except FileNotFoundError:
            keys = {}

        opensky = NBAFlightTracker(
            client_id=keys.get('clientIdOS'),
            client_secret=keys.get('clientSecretOS'),
            username=keys.get('open_sky_user'),
            password=keys.get('open_sky_pwd')
        )

        print(f"\n🌐 Fetching from OpenSky...")
        states_data = opensky.get_us_states()

        if states_data and 'states' in states_data:
            print(f"📡 {len(states_data['states'])} aircraft")
            for state in states_data['states']:
                flight = detector.detect_team_flight(state, upcoming_games)
                if flight:
                    detected_flights.append(flight)

    # Report results
    print(f"\n{'=' * 70}")
    print(f"RESULTS: {len(detected_flights)} potential team flights")
    print("=" * 70)

    if detected_flights:
        detected_flights.sort(key=lambda f: f.confidence, reverse=True)
        for i, flight in enumerate(detected_flights, 1):
            type_str = f"[{flight.aircraft_type}]" if flight.aircraft_type else ""
            print(f"\n[{i}] {flight.callsign or flight.icao24} {type_str} - {flight.confidence}%")
            print(f"    Team: {flight.team_id or 'Unknown'} | Reg: {flight.registration or 'N/A'}")
            print(f"    Position: ({flight.latitude:.2f}, {flight.longitude:.2f})")
            print(f"    Alt: {flight.altitude_ft:,.0f}ft | Speed: {flight.speed_kts:.0f}kts | Hdg: {flight.heading:.0f}°")
            print(f"    Reasons: {', '.join(flight.detection_reasons)}")
    else:
        print("\nNo team flights detected. Normal if no games scheduled.")

    return detected_flights


if __name__ == "__main__":
    import sys

    league = sys.argv[1].upper() if len(sys.argv) > 1 else "NBA"
    use_opensky = "--opensky" in sys.argv

    if league not in ["NBA", "NHL", "MLB"]:
        print(f"Invalid league: {league}. Use NBA, NHL, or MLB.")
        sys.exit(1)

    print(f"Mode: {'OpenSky (legacy)' if use_opensky else 'adsb.lol (type-filtered)'}")
    test_single_scan(league, use_type_filter=not use_opensky)
