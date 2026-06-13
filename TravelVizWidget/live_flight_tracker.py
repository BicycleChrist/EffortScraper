#!/usr/bin/env python3
"""
Real-time flight tracking service for TravelViz
Integrates with OpenSky Network and correlates with game schedule
"""
import requests
import json
import os
import time
import math
import sqlite3
import threading
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from requests.auth import HTTPBasicAuth
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
from database_manager import DatabaseManager, TeamInfo, CITY_TZ
from opensky import NBAFlightTracker
import logging

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

logger = logging.getLogger(__name__)

# Heatmap record = 16 bytes: hex(i32), lat(i32 deg*1e6), lon(i32), alt(i16), gs(i16)
_HEAT_DT = np.dtype([("hex", "<i4"), ("lat", "<i4"), ("lon", "<i4"),
                     ("alt", "<i2"), ("gs", "<i2")]) if _HAS_NUMPY else None


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _within_km_vec(lat, lon, lat0, lon0, radius_km):
    """Vectorized point-in-radius mask (numpy arrays in)."""
    R = 6371.0
    p1 = np.radians(lat)
    dp = np.radians(lat0 - lat)
    dl = np.radians(lon0 - lon)
    a = (np.sin(dp / 2) ** 2 +
         math.cos(math.radians(lat0)) * np.cos(p1) * np.sin(dl / 2) ** 2)
    return (2 * R * np.arcsin(np.sqrt(a))) <= radius_km


def parse_heatmap(blob: bytes):
    """Vectorized parse of a heatmap slice → (hex24[], lat[], lon[], alt_ft[]).
    Index/marker records (lat==lon==0 / out of range) are dropped. Altitude is
    raw×25 ft (negative raw = on ground → returned as 0). numpy keeps this at
    ~tens of ms per ~1.8M-record slice."""
    if not blob or not _HAS_NUMPY:
        return (np.empty(0, "i4"), np.empty(0), np.empty(0), np.empty(0)) \
            if _HAS_NUMPY else ([], [], [], [])
    arr = np.frombuffer(blob, dtype=_HEAT_DT)
    lat_i, lon_i = arr["lat"], arr["lon"]
    good = ((lat_i != 0) & (lon_i != 0) &
            (np.abs(lat_i) <= 90_000_000) & (np.abs(lon_i) <= 180_000_000))
    a = arr[good]
    alt_ft = np.maximum(a["alt"].astype("i4"), 0) * 25
    return (a["hex"] & 0xFFFFFF), a["lat"] / 1e6, a["lon"] / 1e6, alt_ft


def analyze_trace_leg(trace: Dict, o_lat, o_lon, o_r_km,
                      d_lat, d_lon, d_r_km) -> Optional[Dict]:
    """Find the origin->dest leg in an aircraft trace. Returns
    {dep, arr (datetime), dur_h, max_alt, calls[], landed_dest} or None.
    A point is 'on ground' when alt=='ground' or alt<1000ft."""
    base = trace.get("timestamp", 0)
    pts = trace.get("trace", [])
    if not pts:
        return None

    def is_ground(alt):
        return alt == "ground" or (isinstance(alt, (int, float)) and alt < 1000)

    dep_t = dep_idx = None
    for i, p in enumerate(pts):
        lat, lon, alt = p[1], p[2], p[3]
        if _haversine_km(lat, lon, o_lat, o_lon) <= o_r_km and is_ground(alt):
            dep_t, dep_idx = base + p[0], i
    if dep_idx is None:  # never seen on the ground at origin (coverage gap)
        for i, p in enumerate(pts):
            if _haversine_km(p[1], p[2], o_lat, o_lon) <= o_r_km:
                dep_t, dep_idx = base + p[0], i
                break
    if dep_idx is None:
        return None

    arr_t = None
    landed_dest = False
    for p in pts[dep_idx:]:
        lat, lon, alt = p[1], p[2], p[3]
        if _haversine_km(lat, lon, d_lat, d_lon) <= d_r_km and is_ground(alt):
            arr_t, landed_dest = base + p[0], True
            break
    if arr_t is None:
        best = min(pts[dep_idx:],
                   key=lambda p: _haversine_km(p[1], p[2], d_lat, d_lon),
                   default=None)
        if best is None or _haversine_km(
                best[1], best[2], d_lat, d_lon) > d_r_km * 2:
            return None
        arr_t = base + best[0]

    max_alt = max((p[3] for p in pts[dep_idx:]
                   if isinstance(p[3], (int, float))), default=0)
    calls = []
    for p in pts[dep_idx:]:
        if len(p) > 8 and isinstance(p[8], dict):
            f = (p[8].get("flight") or "").strip()
            if f and f not in calls:
                calls.append(f)
    return {
        "dep": datetime.fromtimestamp(dep_t),
        "arr": datetime.fromtimestamp(arr_t),
        "dur_h": (arr_t - dep_t) / 3600.0,
        "max_alt": max_alt,
        "calls": calls,
        "landed_dest": landed_dest,
    }


# Continental US bounding box for efficient API queries
US_BOUNDING_BOX = {
    'lat_min': 24.0,   # Southern tip of Florida
    'lat_max': 50.0,   # Northern border
    'lon_min': -125.0, # West coast
    'lon_max': -66.0   # East coast
}

# Charter aircraft types used by pro sports teams
CHARTER_AIRCRAFT_TYPES = ['B752', 'B753', 'A21N', 'A321', 'B738', 'B739']

# Aircraft a FULL team (~15 players + staff + media, 40-60 people) can actually
# fly — narrow/wide-body airliners only. A team never charters a business jet
# (Citation/Gulfstream/Challenger won't fit them), so bizjets on the corridor
# are noise, not the charter. Used to filter heatmap-backfill candidates.
TEAM_CAPABLE_TYPES = {
    'B712', 'B722', 'B732', 'B733', 'B734', 'B735', 'B736', 'B737', 'B738',
    'B739', 'B73G', 'B38M', 'B39M', 'B752', 'B753', 'B762', 'B763', 'B764',
    'A318', 'A319', 'A320', 'A321', 'A19N', 'A20N', 'A21N',
    'BCS1', 'BCS3', 'E170', 'E175', 'E190', 'E195', 'E290', 'E75L', 'E75S',
}


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
            logger.warning(f"⚠️ adsb.lol registration lookup error for {registration}: {e}")
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
            logger.warning(f"⚠️ adsb.lol hex lookup error for {icao24}: {e}")
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
            logger.warning(f"⚠️ adsb.lol callsign lookup error for {callsign}: {e}")
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
            logger.warning(f"⚠️ adsb.lol error for {type_code}: {e}")
            return []

    def get_charter_aircraft(self, types: List[str] = None,
                             stop_event: "threading.Event" = None) -> List[Dict]:
        """Get all charter-type aircraft (B752, A21N, etc.).

        Issues one HTTP GET per type serially. `stop_event`, if given, is
        checked between requests so shutdown doesn't have to wait out the
        whole chain of in-flight calls (~3s otherwise)."""
        types = types or CHARTER_AIRCRAFT_TYPES
        all_aircraft = []
        seen_hex = set()

        for type_code in types:
            if stop_event is not None and stop_event.is_set():
                break
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

    def get_aircraft_near_point(self, lat: float, lon: float,
                                radius_nm: float = 250) -> List[Dict]:
        """All airborne aircraft within radius_nm of a point (live /v2/point).
        Area enumeration that, unlike type filtering, doesn't miss off-type
        charters (an A319/A320 team charter the type list would skip)."""
        try:
            r = self.session.get(
                f"{self.BASE_URL}/point/{lat}/{lon}/{int(radius_nm)}", timeout=15)
            r.raise_for_status()
            return r.json().get('ac', [])
        except Exception as e:
            logger.warning(f"⚠️ adsb.lol point query error: {e}")
            return []

    # === Historical reconstruction (folded from charter_replay) ============
    # The live endpoints above are airborne-NOW only. These two sources give
    # history, used to (a) confirm a candidate's actual origin->dest route and
    # detect that it has LANDED, and (b) enumerate flights a team already took
    # earlier in the window (before the app was running) from the day heatmap.

    GLOBE_BASE = "https://globe.adsb.lol"

    def fetch_trace(self, hexid: str) -> Optional[Dict]:
        """Full-day timestamped track for one aircraft + reg/type/operator.
        trace['trace'] points = [dt, lat, lon, alt('ground'|ft), gs, track,...]
        with point[8] a detail dict carrying the callsign when it changed."""
        hexid = hexid.strip().lower()
        try:
            r = self.session.get(
                f"{self.GLOBE_BASE}/data/traces/{hexid[-2:]}/"
                f"trace_full_{hexid}.json", timeout=20)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            logger.debug(f"trace fetch failed for {hexid}: {e}")
            return None

    HEATMAP_CACHE = Path("/tmp/charter_replay_cache")

    def fetch_heatmap_slice(self, d: "date", idx: int) -> bytes:
        """One 30-min historical heatmap slice (idx 0..47, UTC). ~7-17MB each
        — call sparingly (window backfill only, never per poll tick).
        Disk-cached so a window is only ever downloaded once."""
        try:
            self.HEATMAP_CACHE.mkdir(exist_ok=True)
            fn = self.HEATMAP_CACHE / f"{d.isoformat()}_{idx:02d}.bin"
            if fn.exists():
                return fn.read_bytes()
        except Exception:
            fn = None
        try:
            r = self.session.get(
                f"{self.GLOBE_BASE}/globe_history/"
                f"{d.year}/{d.month:02d}/{d.day:02d}/heatmap/{idx:02d}.bin.ttf",
                timeout=60)
            blob = r.content if r.status_code == 200 else b""
            if blob and fn is not None:
                fn.write_bytes(blob)
            return blob
        except Exception as e:
            logger.debug(f"heatmap slice {idx} fetch failed: {e}")
            return b""


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
        # Interruptible sleep so stop() doesn't leave the thread snoozing
        # through QThread.wait() at app shutdown
        self._stop_event = threading.Event()

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
            logger.error(f"❌ Invalid identifier type: {identifier_type}")
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
        logger.info(f"✅ Added to watchlist: {entry.label} ({identifier_type}: {identifier})")
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
                logger.debug(f"🗑️ Removed from watchlist: {entry.label}")
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
        self._stop_event.clear()
        self.statusUpdate.emit(f"🎯 Direct flight tracker started ({len(self.watchlist)} aircraft)")

        while self.running:
            if not self.watchlist:
                self._stop_event.wait(self.update_interval)
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
                    logger.warning(f"⚠️ Error looking up {entry.label}: {e}")

            if found_count > 0:
                self.statusUpdate.emit(
                    f"🎯 Tracking {found_count}/{len(self.watchlist)} aircraft"
                )

            self._stop_event.wait(self.update_interval)

        self.statusUpdate.emit("🛑 Direct flight tracker stopped")

    def stop(self):
        """Stop the tracking thread (interrupts the poll sleep immediately)"""
        self.running = False
        self._stop_event.set()

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
    league: Optional[str] = None
    confidence: int = 0                  # capped 0-100 for display
    raw_score: float = 0.0               # uncapped, for ranking candidates
    detection_reasons: List[str] = field(default_factory=list)

    # Flight path
    origin_airport: Optional[str] = None
    destination_airport: Optional[str] = None
    route_distance_km: Optional[float] = None

    # Inferred route (from schedule correlation) — drives globe route arcs
    origin_city: Optional[str] = None
    dest_city: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    route_progress: Optional[float] = None

    # Charter ledger linkage
    window_id: Optional[str] = None       # which travel window this matches
    timing_bonus: float = 0.0             # per-candidate departure-timing fit
    landed: bool = False                  # trace-confirmed arrival at dest

    @property
    def altitude_ft(self) -> float:
        return self.altitude_m * 3.28084 if self.altitude_m else 0

    @property
    def speed_kts(self) -> float:
        return self.velocity_ms * 1.94384 if self.velocity_ms else 0


class LiveGameStateStore:
    """Observed game states from the flashscore live feed.

    Fed the mapped live-score payload ({league: [event dict, ...]}) each poll
    tick (~30s). The live set is authoritative for "is this team playing right
    now", and a team DROPPING OUT of the set is the live→finished transition —
    i.e. the actual game end, accurate to one tick. Both beat inferring game
    end as start + 2.75h.

    Thread-safe: written from the GUI thread (live feed tick), read from the
    tracker thread (travel-window computation).
    """

    # A live sighting older than this is stale (feed stopped / panel hidden);
    # readers fall back to schedule inference rather than trusting it.
    LIVE_TTL_S = 180

    def __init__(self):
        self._lock = threading.Lock()
        self._live_seen: Dict[tuple, datetime] = {}    # (league, team_id) -> last live sighting
        self._finished_at: Dict[tuple, datetime] = {}  # (league, team_id) -> observed finish

    def update(self, payload: Dict):
        """Ingest one live-score tick: {league: [mapped event dict, ...]}."""
        now = datetime.now()
        live_now = set()
        for league, events in (payload or {}).items():
            for ev in events:
                if ev.get('stage') != 'live':
                    continue
                for tid in (ev.get('home_id'), ev.get('away_id')):
                    if tid:
                        live_now.add((league, tid.lower()))

        with self._lock:
            for key, seen in list(self._live_seen.items()):
                if key in live_now:
                    continue
                del self._live_seen[key]
                # Only a RECENT live→absent transition is a finish; a stale
                # entry (feed was paused) tells us nothing about when it ended
                if (now - seen).total_seconds() < self.LIVE_TTL_S:
                    self._finished_at[key] = now
            for key in live_now:
                self._live_seen[key] = now
                # New game went live (e.g. doubleheader game 2) — the old
                # finish no longer describes the team's current state
                self._finished_at.pop(key, None)

    def is_live(self, league: str, team_id: str, now: datetime = None) -> bool:
        """Is this team in a game right now (per a fresh live-feed sighting)?"""
        now = now or datetime.now()
        with self._lock:
            seen = self._live_seen.get((league, team_id.lower()))
        return seen is not None and (now - seen).total_seconds() < self.LIVE_TTL_S

    def finished_at(self, league: str, team_id: str) -> Optional[datetime]:
        """When the team's most recent game was observed to end, if seen."""
        with self._lock:
            return self._finished_at.get((league, team_id.lower()))


class CharterLedger:
    """Per-travel-window observation ledger (sqlite, in sports_data.db).

    The fix for "several different flights per day": instead of picking the
    best of the CURRENT airborne snapshot every tick, we accumulate evidence
    per (window, aircraft) across the whole window and pick the argmax of
    accumulated+current. A charter seen flying the corridor early banks
    dominant evidence (n_obs, wide route-progress span, prime timing) and
    keeps winning even after it lands — while later same-corridor scheduled
    flights can't displace it. Survives app restarts (it's in the DB), which
    kills the startup-amnesia re-shopping.

    Thread-safe via its own short-lived connections (tracker thread writes).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_table()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init_table(self):
        with self._lock, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS charter_observations (
                    window_id     TEXT NOT NULL,
                    league        TEXT, team_id TEXT,
                    icao24        TEXT NOT NULL,
                    callsign      TEXT, registration TEXT,
                    aircraft_type TEXT, operator TEXT,
                    first_seen    TIMESTAMP, last_seen TIMESTAMP,
                    n_obs         INTEGER DEFAULT 0,
                    max_score     REAL DEFAULT 0,
                    sum_score     REAL DEFAULT 0,
                    min_progress  REAL, max_progress REAL,
                    best_timing   REAL DEFAULT 0,
                    dep_time      TIMESTAMP, arr_time TIMESTAMP,
                    landed        INTEGER DEFAULT 0,
                    confirmed_route INTEGER DEFAULT 0,
                    origin_city   TEXT, dest_city TEXT,
                    origin_lat    REAL, origin_lon REAL,
                    dest_lat      REAL, dest_lon REAL,
                    last_lat      REAL, last_lon REAL,
                    PRIMARY KEY (window_id, icao24)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_obs_window "
                      "ON charter_observations(window_id)")

    def observe(self, window_id: str, league: str, team_id: str,
                flight: "LiveFlight", score: float, timing: float,
                progress: Optional[float]):
        """Record/accumulate one tick's sighting of a candidate aircraft."""
        now = datetime.now()
        prog = progress if progress is not None else 0.0
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT n_obs, min_progress, max_progress, max_score, "
                "best_timing FROM charter_observations "
                "WHERE window_id=? AND icao24=?",
                (window_id, flight.icao24)).fetchone()
            if row is None:
                c.execute("""INSERT INTO charter_observations
                    (window_id, league, team_id, icao24, callsign, registration,
                     aircraft_type, operator, first_seen, last_seen, n_obs,
                     max_score, sum_score, min_progress, max_progress, best_timing,
                     origin_city, dest_city, origin_lat, origin_lon,
                     dest_lat, dest_lon, last_lat, last_lon)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (window_id, league, team_id, flight.icao24, flight.callsign,
                     flight.registration, flight.aircraft_type, None, now, now,
                     1, score, score, prog, prog, timing,
                     flight.origin_city, flight.dest_city,
                     flight.origin_lat, flight.origin_lon,
                     flight.dest_lat, flight.dest_lon,
                     flight.latitude, flight.longitude))
            else:
                c.execute("""UPDATE charter_observations SET
                     last_seen=?, n_obs=n_obs+1,
                     max_score=MAX(max_score,?), sum_score=sum_score+?,
                     min_progress=MIN(min_progress,?), max_progress=MAX(max_progress,?),
                     best_timing=MAX(best_timing,?), callsign=?,
                     last_lat=?, last_lon=?
                     WHERE window_id=? AND icao24=?""",
                    (now, score, score, prog, prog, timing,
                     flight.callsign, flight.latitude, flight.longitude,
                     window_id, flight.icao24))

    def set_route_info(self, window_id: str, icao24: str, *, dep=None, arr=None,
                       landed=None, confirmed=None, operator=None,
                       aircraft_type=None):
        sets, vals = [], []
        for col, v in (("dep_time", dep), ("arr_time", arr),
                       ("landed", landed), ("confirmed_route", confirmed),
                       ("operator", operator), ("aircraft_type", aircraft_type)):
            if v is not None:
                sets.append(f"{col}=?")
                vals.append(int(v) if col in ("landed", "confirmed_route") else v)
        if not sets:
            return
        vals += [window_id, icao24]
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE charter_observations SET {','.join(sets)} "
                      "WHERE window_id=? AND icao24=?", vals)

    def window_rows(self, window_id: str) -> List[dict]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM charter_observations WHERE window_id=?",
                (window_id,)).fetchall()]

    def prune(self, keep_window_ids: Set[str], max_age_days: int = 4):
        """Drop rows for windows no longer active and not seen recently.
        Completed-window rows ARE the charter registry; keep them a few days
        for the team→tail-number history before pruning."""
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM charter_observations WHERE last_seen < ?",
                      (cutoff,))


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

    # Commercial airlines to ALWAYS exclude (never do team charters).
    # NOT excluded on purpose: ASA (Alaska operates the Mariners charter),
    # SCX (Sun Country does team charters, see CHARTER_PATTERNS).
    COMMERCIAL_AIRLINE_PREFIXES = [
        # Low-cost/budget carriers (don't do team charters)
        'SWA', 'JBU', 'NKS', 'FFT', 'HAL', 'VRD', 'AAY',
        # Regional carriers (planes too small)
        'SKW', 'RPA', 'ENY', 'PDT', 'ASH', 'GJS', 'JIA', 'MXY', 'CPZ',
        # Canadian carriers
        'ACA', 'WJA', 'TSC', 'ROU', 'PGT', 'POE',
        # Cargo carriers
        'UPS', 'FDX', 'ABX', 'ATN', 'KFS', 'CJT', 'GEC', 'CLX',
        # Mexican / Central American / leisure (incl. Copa overflights)
        'VIV', 'VOI', 'SLI', 'AMX', 'CMP', 'AVA',
        # International (not doing domestic team charters)
        'BAW', 'AFR', 'DLH', 'KLM', 'EIN', 'VIR',
        # Other
        'FLE', 'TAI', 'TRS',
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

    def __init__(self, db: DatabaseManager, league: str = "NBA",
                 game_state: Optional[LiveGameStateStore] = None):
        self.db = db
        self.league = league
        self.game_state = game_state  # live-feed game states (optional)
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
            logger.info(f"✅ TeamFlightDetector: Persistent DB connection initialized (WAL mode)")
        except Exception as e:
            logger.warning(f"⚠️ TeamFlightDetector: Failed to init persistent connection: {e}")
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
                logger.info(f"✅ Loaded {len(self.team_locations)} {self.league} team locations from database")
            else:
                logger.warning(f"⚠️  No team locations found in database for {self.league}, using fallback")
                self._load_fallback_locations()

        except Exception as e:
            logger.warning(f"⚠️  Error loading team locations from DB: {e}")
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
        logger.debug(f"📍 Loaded {len(self.team_locations)} fallback team locations")

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

            # Find NEXT non-completed game for this team. Exact complement of
            # the last-game query (start + 3h >= now) so an IN-PROGRESS game
            # is never invisible to both queries — when it was, the window
            # spanned right over a live game (last away game → tomorrow's
            # home game) and charters got matched to teams mid-at-bat.
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
                AND datetime(g.date, '+3 hours') >= datetime('now', 'localtime')
                ORDER BY g.date ASC
                LIMIT 1
            """, (self.league, current_season, team_id, team_id))

            next_game = next_game_cursor.fetchone()

            if not last_game or not next_game:
                return None

            # Live feed is authoritative: a team in a game right now cannot
            # be on a plane (covers extra innings past start+3h, rain delays,
            # late starts — cases the schedule inference below gets wrong)
            if self.game_state and self.game_state.is_live(self.league, team_id):
                return None

            # Has the "next" non-completed game already started?
            next_game_started = conn.execute(
                "SELECT datetime(?) <= datetime('now', 'localtime')",
                (next_game['game_date'],)).fetchone()[0]

            observed_end = None
            if next_game_started:
                # SQL thinks this game may still be running (start+3h hasn't
                # passed). If the live feed SAW it finish, it's actually the
                # LAST game — the post-game band opens now, not at start+3h.
                # Otherwise assume the team is still playing: no window.
                observed_end = self._observed_finish(team_id, next_game['game_date'])
                if observed_end is None:
                    return None
                last_game = next_game
                next_game = conn.execute("""
                    SELECT
                        g.game_id, g.date as game_date, g.venue_id,
                        g.home_team_id, g.away_team_id,
                        v.city as venue_city, v.latitude, v.longitude
                    FROM games g
                    JOIN venues v ON g.venue_id = v.venue_id
                    WHERE g.league = ?
                    AND g.season = ?
                    AND (g.home_team_id = ? OR g.away_team_id = ?)
                    AND datetime(g.date) > datetime(?)
                    ORDER BY g.date ASC
                    LIMIT 1
                """, (self.league, current_season, team_id, team_id,
                      last_game['game_date'])).fetchone()
                if not next_game:
                    return None
            else:
                observed_end = self._observed_finish(team_id, last_game['game_date'])

            # Parse game times
            last_game_start = datetime.fromisoformat(last_game['game_date'].replace('Z', '+00:00')) if 'Z' in str(last_game['game_date']) else datetime.fromisoformat(str(last_game['game_date']))
            next_game_start = datetime.fromisoformat(next_game['game_date'].replace('Z', '+00:00')) if 'Z' in str(next_game['game_date']) else datetime.fromisoformat(str(next_game['game_date']))

            # Game end: prefer the live feed's OBSERVED finish (exact to one
            # poll tick). Fall back to per-league typical length — MLB with
            # the pitch clock runs ~2h40; 3h+ overestimates and made the
            # earliest-departure gate reject real getaway-day charters.
            if observed_end is not None:
                last_game_end = observed_end
            else:
                game_hours = {'MLB': 2.75, 'NBA': 2.5, 'NHL': 2.75}.get(self.league, 3.0)
                last_game_end = last_game_start + timedelta(hours=game_hours)

            # Teams are typically wheels-up 1-2 hours after the final out
            # (getaway day: bus is loaded before the game ends)
            earliest_departure = last_game_end + timedelta(hours=1)

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

            # Multi-day gaps (playoff series, off days): teams that don't
            # leave within ~12h of the game travel on the DAY BEFORE the next
            # game instead. Second departure band opens 30h before puck drop.
            pregame_travel_start = None
            if (next_game_start - last_game_end) > timedelta(hours=36):
                pregame_travel_start = next_game_start - timedelta(hours=30)

            return {
                'last_game_end': last_game_end,
                'earliest_departure': earliest_departure,
                'latest_departure': latest_departure,
                'pregame_travel_start': pregame_travel_start,
                'next_game_start': next_game_start,
                'required_arrival': required_arrival,
                'origin_city': origin_city,
                'destination_city': destination_city,
                'origin_lat': last_game['latitude'],
                'origin_lon': last_game['longitude'],
                'dest_lat': next_game['latitude'],
                'dest_lon': next_game['longitude'],
                # Stable window identity for the charter ledger: same key
                # across restarts and ticks, changes only when the team's
                # game pair changes (i.e. they actually played the next game).
                'window_id': (f"{self.league}_{team_id}_"
                              f"{last_game['game_id']}_{next_game['game_id']}"),
            }

        except Exception as e:
            logger.warning(f"⚠️  Error getting travel window for {team_id}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def window_id_for(self, team_id: str) -> Optional[str]:
        w = self.get_team_travel_window(team_id)
        return w['window_id'] if w else None

    def _observed_finish(self, team_id: str, game_date_str) -> Optional[datetime]:
        """Live-feed observed finish time for the team's game starting at
        game_date_str, or None if the feed didn't see that game end.

        The store keeps one finish per team — validate it belongs to THIS
        game (after its start, within 8h) so yesterday's finish never opens
        today's post-game band."""
        if not self.game_state:
            return None
        fin = self.game_state.finished_at(self.league, team_id)
        if not fin:
            return None
        try:
            s = str(game_date_str)
            start = (datetime.fromisoformat(s.replace('Z', '+00:00'))
                     if 'Z' in s else datetime.fromisoformat(s))
        except ValueError:
            return None
        if start <= fin <= start + timedelta(hours=8):
            return fin
        return None

    def window_active_now(self, w: Dict, now: datetime = None) -> bool:
        """Could a charter for this travel window plausibly be AIRBORNE right
        now? Used to pause adsb polling when the answer is no for every team
        (e.g. 2-6 AM after the overnight dead-zone closes)."""
        now = now or datetime.now()
        total = self._haversine_distance(
            w['origin_lat'], w['origin_lon'], w['dest_lat'], w['dest_lon'])
        if total < 300 or total > 4500:
            return False
        dur = timedelta(hours=total / 800.0)

        # A flight airborne now departed within [now - dur, now]; the
        # departure must fall in the post-game band OR the day-before-game
        # travel band, and clear the overnight dead-zone (30-min sampling)
        lo = max(w['earliest_departure'] - timedelta(minutes=45), now - dur)
        hi = now
        if lo > hi:
            return False

        pregame_start = w.get('pregame_travel_start')
        machine_off = now.astimezone().utcoffset()
        origin_off = timedelta(hours=CITY_TZ.get(
            w['origin_city'], round((w['origin_lon'] or 0) / 15.0)))
        t = lo
        while t <= hi:
            in_postgame = t <= w['latest_departure']
            in_pregame = pregame_start is not None and t >= pregame_start
            if t + dur <= w['required_arrival']:
                delay = (t - w['last_game_end']).total_seconds() / 3600
                local = t - machine_off + origin_off
                if in_postgame and (delay <= 4.5 or local.hour >= 7):
                    return True
                # Pregame band: daytime departures only (08-19 origin-local)
                if in_pregame and not in_postgame and 8 <= local.hour <= 19:
                    return True
            t += timedelta(minutes=30)
        return False

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

        # Exclude carriers that don't do US/Canadian team charters (single
        # source of truth — the old inline list here missed all cargo
        # carriers, which fly the same corridors at the same hours)
        if callsign:
            if any(callsign.upper().startswith(p)
                   for p in self.COMMERCIAL_AIRLINE_PREFIXES):
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

            # Charter callsign SIGNATURE: trailing-letter positioning suffix
            # (AAL606P, DAL1234P) is a real charter tell — validated against
            # the Knicks' AAL606P. NOTE: the old "8xxx/9xxx = charter" rule
            # was DROPPED — it's actively wrong for hometown-airline charters
            # (the Twins' real charter SCX346 is low-numbered while scheduled
            # SCX8992/SCX3084 are high), so it favored the impostors.
            if callsign and len(callsign) >= 5:
                cs = callsign.upper()
                if cs[-1].isalpha() and cs[3:-1].isdigit():
                    confidence += 10
                    reasons.append(f"positioning callsign ({cs})")

            # Schedule correlation with ORIGIN verification (+50 max)
            schedule_match = self._correlate_with_schedule_and_origin(
                lat, lon, heading, upcoming_games
            )
            if schedule_match:
                confidence += schedule_match['confidence_boost']
                reasons.append(schedule_match['reason'])
                detected_team = schedule_match['team_id']
        else:
            schedule_match = None

        # Threshold: need strong schedule match or be team-owned
        if confidence < 65:
            return None

        route = schedule_match or {}
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
            raw_score=float(confidence),
            detection_reasons=reasons,
            origin_city=route.get('origin_city'),
            dest_city=route.get('dest_city'),
            origin_lat=route.get('origin_lat'),
            origin_lon=route.get('origin_lon'),
            dest_lat=route.get('dest_lat'),
            dest_lon=route.get('dest_lon'),
            route_progress=route.get('route_progress'),
            window_id=route.get('window_id'),
            timing_bonus=route.get('timing_bonus', 0.0),
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

        # VALIDATION 1+2: departure must fall in one of the two real-world
        # bands: post-game getaway [end+1h-45m, end+12h], or day-before-game
        # travel [next_game - 30h, ...] when there's a multi-day gap.
        # (45 min slack: the constant-cruise-speed estimate ignores
        # taxi/climb, biasing estimated departures early)
        in_postgame = (
            travel_window['earliest_departure'] - timedelta(minutes=45)
            <= estimated_departure <= travel_window['latest_departure'])
        pregame_start = travel_window.get('pregame_travel_start')
        in_pregame = (pregame_start is not None
                      and estimated_departure >= pregame_start)
        if not (in_postgame or in_pregame):
            return None

        # Day-before-game travel is a DAYTIME activity: depart 08:00-19:00
        # origin-local, arrive by evening, sleep at the destination. Without
        # this, late-night corridor red-eyes match all night once the
        # pregame band opens.
        if in_pregame and not in_postgame:
            machine_off = now.astimezone().utcoffset()
            origin_off = timedelta(hours=CITY_TZ.get(
                travel_window['origin_city'],
                round((travel_window['origin_lon'] or 0) / 15.0)))
            local_dep = estimated_departure - machine_off + origin_off
            if not (8 <= local_dep.hour <= 19):
                return None

        # VALIDATION 3: Arrival should be before required arrival time
        if estimated_arrival > travel_window['required_arrival']:
            return None

        # VALIDATION 4: overnight dead-zone. Team departures are bimodal —
        # wheels-up within ~4h of the final out, OR overnight stay and a
        # morning flight. Nobody departs 1-7 AM local hours after the game,
        # but red-eye corridor traffic does, and it kept matching all night.
        dep_delay_for_gate = (estimated_departure -
                              travel_window['last_game_end']).total_seconds() / 3600
        if dep_delay_for_gate > 4.5:
            machine_off = datetime.now().astimezone().utcoffset()
            origin_off = timedelta(hours=CITY_TZ.get(
                travel_window['origin_city'],
                round((travel_window['origin_lon'] or 0) / 15.0)))
            origin_local_dep = estimated_departure - machine_off + origin_off
            # Midnight counts: a 00:30 departure 10h after a day game is
            # corridor noise, not a charter (prompt post-night-game
            # departures have delay < 4.5h and never reach this gate)
            if origin_local_dep.hour < 7:
                return None

        # BONUS: how closely THIS flight's estimated departure matches the
        # charter pattern (wheels-up 1-3.5h after the final out). Must use the
        # per-candidate estimated departure — the old code scored the current
        # wall clock, giving every candidate on the corridor the same bonus.
        dep_delay_hours = (estimated_departure -
                           travel_window['last_game_end']).total_seconds() / 3600
        # Timing is the DOMINANT discriminator (replay lesson: the real
        # charter departs closest to the actual game end; same-corridor
        # scheduled flights don't). Weighted heavily so it outranks the
        # operator/type tiebreakers a flooded corridor can't separate on.
        if 0.75 <= dep_delay_hours <= 3.5:
            timing_bonus += 30  # Prime getaway-day window
        elif dep_delay_hours <= 6:
            timing_bonus += 16  # Plausible late departure
        elif dep_delay_hours <= 12:
            timing_bonus += 6   # Overnight stay, morning flight
        elif in_pregame:
            timing_bonus += 18  # Classic day-before-game travel
        # else: 0 — valid but weak

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
                'timing_bonus': timing_bonus,
                'window_id': travel_window['window_id'],
                'distance_to_venue': dist_to_venue,
                'total_route': total_route,
                'is_returning_home': is_home_team,
                'origin_city': origin_city,
                'dest_city': dest_city,
                'origin_lat': origin_lat,
                'origin_lon': origin_lon,
                'dest_lat': venue_lat,
                'dest_lon': venue_lon,
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

        # Detectors: one per tracked league (multi-league support). self.detector
        # stays as the primary-league alias for back-compat.
        self.db = db
        self.leagues = [league]
        # Shared live game states (fed by the panel's flashscore live feed via
        # update_live_states) — gives detectors authoritative "team is playing
        # now" + exact observed game-end times
        self.game_state = LiveGameStateStore()
        self.detectors = {league: TeamFlightDetector(db, league, self.game_state)}
        self.detector = self.detectors[league]
        self.upcoming_by_league: Dict[str, List] = {}

        # Per-window observation ledger — selection authority. Replaces
        # best-of-current-tick with accumulated window evidence (survives
        # restarts, suppresses the "several flights per day" re-shopping).
        self.ledger = CharterLedger(db.db_path)
        # Aircraft types confirmed via trace this session (avoid re-fetching)
        self._route_confirmed: Set[str] = set()

        # Heatmap backfill: seed the ledger with the flight a team ALREADY took
        # earlier in the window (before the app was running) so a team that
        # flew pre-startup isn't mis-attributed to a later corridor flight.
        # Heavy (downloads bounded heatmap slices, ~10MB each, cached) — runs
        # once per displayed window on a background thread. Disable with
        # TRAVELVIZ_CHARTER_BACKFILL=0.
        self.enable_backfill = os.getenv('TRAVELVIZ_CHARTER_BACKFILL', '1') != '0'
        self._backfilled: Set[str] = set()
        self._backfill_lock = threading.Lock()

        # Tracking state
        self.tracked_flights: Dict[str, LiveFlight] = {}  # icao24 -> LiveFlight
        self.update_interval = 15  # seconds
        # Interruptible sleep (see DirectFlightTracker): stop() must wake the
        # loop, or shutdown stalls in QThread.wait()
        self._stop_event = threading.Event()

        # Data source preference
        self.use_type_filtering = True  # Use adsb.lol type filtering
        # Aircraft types used for team charters: Delta 757/A321neo/767,
        # United 757/737-900, American/charter 737-800s
        self.aircraft_types = ['B752', 'A21N', 'B763', 'B739', 'B738']

        # Schedule cache
        self.upcoming_games = []
        self.schedule_refresh_timer = QTimer()
        self.schedule_refresh_timer.timeout.connect(self.refresh_schedule)
        self.schedule_refresh_timer.start(300000)  # Refresh every 5 minutes

        # Note: refresh_schedule() is called in run() to avoid blocking main thread
        self._initialized = False

    def set_league(self, league: str):
        """Back-compat single-league setter"""
        self.set_leagues([league])

    def update_live_states(self, payload: Dict):
        """Ingest a flashscore live-score tick ({league: [event dict, ...]}).
        Called from the GUI thread; the store is lock-protected. Also drops
        the travel-window cache so live transitions take effect immediately
        (a game ending opens the post-game band NOW, not in 60s)."""
        self.game_state.update(payload)
        self._window_cache = None

    def set_leagues(self, leagues: List[str]):
        """Track charters for several leagues at once (one adsb fetch per
        tick, one schedule-correlation detector per league)."""
        leagues = [lg for lg in dict.fromkeys(leagues) if lg in ('MLB', 'NBA', 'NHL')]
        if not leagues or leagues == self.leagues:
            return
        self.leagues = leagues
        self.league = leagues[0]
        for lg in leagues:
            if lg not in self.detectors:
                self.detectors[lg] = TeamFlightDetector(self.db, lg, self.game_state)
        self.detector = self.detectors[self.league]
        self._initialized = False
        if self.isRunning():
            self.refresh_schedule()
        self.statusUpdate.emit(f"Tracking {', '.join(leagues)} flights")

    @staticmethod
    def _season_for(league: str) -> str:
        now = datetime.now()
        if league in ['NBA', 'NHL']:
            start = now.year if now.month >= 10 else now.year - 1
            return f"{start}-{str(start + 1)[2:]}"
        return str(now.year)

    def refresh_schedule(self):
        """Refresh upcoming games (next 48h) for every tracked league"""
        now = datetime.now()
        cutoff = now + timedelta(hours=48)
        parts = []
        for league in self.leagues:
            try:
                all_games = self.db.load_games(self._season_for(league), league)
                upcoming = [g for g in all_games if now <= g.date <= cutoff]
                self.upcoming_by_league[league] = upcoming
                parts.append(f"{league}:{len(upcoming)}")
            except Exception as e:
                self.statusUpdate.emit(f"⚠️  {league} schedule refresh error: {e}")
                self.upcoming_by_league[league] = []

        # Back-compat alias (primary league)
        self.upcoming_games = self.upcoming_by_league.get(self.league, [])
        self.statusUpdate.emit(f"📅 Games next 48h — {', '.join(parts)}")
        # Age out stale ledger rows (completed windows kept a few days as the
        # team→tail-number charter registry, then dropped)
        try:
            self.ledger.prune(set())
        except Exception as e:
            logger.debug(f"ledger prune skipped: {e}")

    def run(self):
        """Main tracking loop - runs in background thread"""
        self.running = True
        self._stop_event.clear()

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
                # Skip adsb polling entirely when no team could plausibly be
                # in the air (saves API calls overnight / on idle days)
                active_count, sample = self._active_travel_windows()
                if active_count == 0:
                    self.statusUpdate.emit(
                        "💤 No teams in travel windows — flight polling paused "
                        "(rechecks every 20 min)")
                    self._stop_event.wait(1200)
                    continue

                # Collect ALL candidate flights first, then filter to best per team
                candidate_flights: List[LiveFlight] = []

                if self.use_type_filtering:
                    # Primary: adsb.lol type-based filtering
                    aircraft_list = self.adsb_lol.get_charter_aircraft(
                        self.aircraft_types, self._stop_event)
                    if self._stop_event.is_set():
                        break
                    aircraft_list = self.adsb_lol.filter_us_aircraft(aircraft_list)
                    aircraft_count = len(aircraft_list)

                    if not aircraft_list:
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            self.statusUpdate.emit(f"⚠️ adsb.lol unavailable - retry in {backoff_time}s")
                            self._stop_event.wait(backoff_time)
                            backoff_time = min(backoff_time * 2, max_backoff)
                            consecutive_errors = 0
                        continue

                    consecutive_errors = 0
                    backoff_time = 30

                    for ac in aircraft_list:
                        for lg in self.leagues:
                            games = self.upcoming_by_league.get(lg, [])
                            if not games:
                                continue
                            flight = self.detectors[lg].detect_from_adsb_lol(ac, games)
                            if flight:
                                flight.league = lg
                                candidate_flights.append(flight)
                                break  # one attribution per aircraft

                else:
                    # Fallback: OpenSky broad scan
                    states_data = self.opensky.get_us_states()
                    if not states_data or 'states' not in states_data:
                        consecutive_errors += 1
                        self._stop_event.wait(self.update_interval)
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
                        f"🔍 {aircraft_count} {types_str}s in US | watching "
                        f"{active_count} travel window(s): {', '.join(sample)}"
                    )

            except Exception as e:
                self.statusUpdate.emit(f"❌ Error: {e}")
                consecutive_errors += 1

            self._stop_event.wait(self.update_interval)

        self.statusUpdate.emit("🛑 Flight tracker stopped")

    def _active_travel_windows(self):
        """(count, sample team ids) of teams whose charter could be airborne
        right now, across all tracked leagues. Cached for 60s — it's ~2 DB
        queries per team."""
        now = datetime.now()
        cached = getattr(self, '_window_cache', None)
        if cached and (now - cached[0]).total_seconds() < 60:
            return cached[1], cached[2]

        count, sample = 0, []
        for league in self.leagues:
            detector = self.detectors[league]
            try:
                teams = self.db.load_teams(league)
            except Exception:
                continue
            for team in teams:
                w = detector.get_team_travel_window(team.team_id)
                if w and detector.window_active_now(w, now):
                    count += 1
                    if len(sample) < 6:
                        sample.append(f"{league}:{team.team_id.upper()}")

        self._window_cache = (now, count, sample)
        return count, sample

    def _select_best_flight_per_team(self, candidates: List[LiveFlight]) -> List[LiveFlight]:
        """Ledger-driven selection: ONE flight per travel window, chosen by
        ACCUMULATED window evidence, not best-of-this-tick.

        Each candidate is observed into the ledger; then per window we pick
        the argmax of `_window_score` over ALL aircraft ever seen in the
        window. The real charter (early, full-corridor traversal, prime
        timing) leads and stays led — a later same-corridor scheduled flight
        can't displace it, and once the charter lands we show it as ARRIVED
        rather than flipping to an airborne impostor. Survives restarts.
        """
        no_team: List[LiveFlight] = []
        # Observe candidates; index this tick's airborne candidates per window
        cand_by_window: Dict[str, Dict[str, tuple]] = {}  # wid -> icao -> (flight, score)
        for f in candidates:
            if f.team_id and f.window_id:
                score = self._calculate_flight_score(f, f.team_id)
                self.ledger.observe(f.window_id, f.league or '', f.team_id,
                                    f, score, f.timing_bonus, f.route_progress)
                cand_by_window.setdefault(f.window_id, {})[f.icao24] = (f, score)
            elif not f.team_id and f.confidence >= 80:
                no_team.append(f)

        # Windows to resolve: any with an airborne candidate this tick, plus
        # any we're currently displaying (so a leader that just landed gets
        # re-resolved to its ARRIVED state instead of vanishing)
        windows = set(cand_by_window)
        for fl in self.tracked_flights.values():
            if fl.window_id:
                windows.add(fl.window_id)

        best_flights: List[LiveFlight] = []
        for window_id in windows:
            rows = self.ledger.window_rows(window_id)
            if not rows:
                continue
            cur = cand_by_window.get(window_id, {})
            scored = []
            for row in rows:
                icao = row['icao24']
                cur_score = cur.get(icao, (None, 0.0))[1]
                scored.append((self._window_score(row, cur_score), row))
            scored.sort(key=lambda x: x[0], reverse=True)
            lead_ws, lead_row = scored[0]
            icao = lead_row['icao24']

            # First time we resolve a window, backfill it from history (once,
            # background) UNLESS we already hold a confirmed completed flight —
            # catches the charter the team took before the app was running.
            if (self.enable_backfill and window_id not in self._backfilled
                    and not any(r.get('confirmed_route') for r in rows)):
                lg = lead_row.get('league') or self.league
                tid = lead_row.get('team_id')
                if tid:
                    threading.Thread(
                        target=self._backfill_window,
                        args=(window_id, lg, tid),
                        daemon=True, name=f"backfill-{tid}").start()

            if icao in cur:
                # Leader is airborne right now — emit the live candidate,
                # confirm its route via trace once (cheap), tag it
                flight, _ = cur[icao]
                self._maybe_confirm_route(window_id, flight)
                flight.landed = bool(lead_row.get('landed'))
                best_flights.append(flight)
            else:
                # Leader has landed / isn't airborne this tick. Only surface
                # it as ARRIVED if we actually confirmed it landed at dest —
                # otherwise suppress (don't fall back to a corridor impostor)
                if lead_row.get('landed'):
                    arrived = self._flight_from_ledger_row(lead_row)
                    if arrived:
                        best_flights.append(arrived)
                # else: emit nothing for this window this tick

        best_flights.extend(no_team)
        return best_flights

    def _flight_from_ledger_row(self, row: dict) -> Optional[LiveFlight]:
        """Reconstruct a minimal LiveFlight for a landed window leader so the
        globe can render it ARRIVED at the destination."""
        if row.get('dest_lat') is None:
            return None
        return LiveFlight(
            icao24=row['icao24'], callsign=row.get('callsign'),
            latitude=row['dest_lat'], longitude=row['dest_lon'],
            altitude_m=0, velocity_ms=0, heading=0, timestamp=datetime.now(),
            aircraft_type=row.get('aircraft_type'),
            registration=row.get('registration'),
            team_id=row.get('team_id'), league=row.get('league'),
            confidence=90, raw_score=row.get('max_score') or 0,
            detection_reasons=['arrived (trace-confirmed)'],
            origin_city=row.get('origin_city'), dest_city=row.get('dest_city'),
            origin_lat=row.get('origin_lat'), origin_lon=row.get('origin_lon'),
            dest_lat=row.get('dest_lat'), dest_lon=row.get('dest_lon'),
            route_progress=1.0, window_id=row.get('window_id'), landed=True,
        )

    def _maybe_confirm_route(self, window_id: str, flight: LiveFlight):
        """Fetch the leader's trace ONCE to confirm origin→dest and set
        landed/dep/arr in the ledger. Cheap (one ~15KB trace per leader);
        skipped if already done this session."""
        if flight.icao24 in self._route_confirmed:
            return
        self._route_confirmed.add(flight.icao24)
        if flight.origin_lat is None or flight.dest_lat is None:
            return
        trace = self.adsb_lol.fetch_trace(flight.icao24)
        if not trace:
            return
        leg = analyze_trace_leg(trace, flight.origin_lat, flight.origin_lon, 90,
                                flight.dest_lat, flight.dest_lon, 90)
        if not leg:
            return
        # Directness gate: reject obviously indirect legs (the replay's 7h
        # "DTW→MSP" Delta routings) as confirmation evidence
        gc_km = self.detector._haversine_distance(
            flight.origin_lat, flight.origin_lon, flight.dest_lat, flight.dest_lon)
        expected_h = gc_km / 800.0
        confirmed = leg['dur_h'] <= max(2.0, expected_h * 1.6)
        self.ledger.set_route_info(
            window_id, flight.icao24,
            dep=leg['dep'], arr=leg['arr'],
            landed=leg['landed_dest'], confirmed=confirmed,
            operator=(trace.get('ownOp') or None),
            aircraft_type=(trace.get('t') or None))

    @staticmethod
    def _utc_slices(start_utc: datetime, end_utc: datetime,
                    cap: int = 24) -> List[Tuple[date, int]]:
        """(date, heatmap-slice-idx) pairs covering a UTC time range. Slices
        are 30 min; idx = hour*2 + (minute>=30). Capped to the most recent
        `cap` slices to bound bandwidth."""
        out, t = [], start_utc.replace(
            minute=(0 if start_utc.minute < 30 else 30), second=0, microsecond=0)
        while t <= end_utc:
            out.append((t.date(), t.hour * 2 + (1 if t.minute >= 30 else 0)))
            t += timedelta(minutes=30)
        return out[-cap:]

    def _backfill_window(self, window_id: str, league: str, team_id: str):
        """Seed the ledger from the day heatmap with the flight the team
        ALREADY took earlier in the window. Fixes the 'ghost' the user saw
        (NY→SAT charter still shown after the Knicks had landed): without
        this a team that flew before the app started has no ledger evidence,
        so a later corridor flight becomes leader by default. Background-only,
        once per window, bounded + cached."""
        if not (self.enable_backfill and _HAS_NUMPY):
            return
        with self._backfill_lock:
            if window_id in self._backfilled:
                return
            self._backfilled.add(window_id)

        detector = self.detectors.get(league, self.detector)
        w = detector.get_team_travel_window(team_id)
        if not w:
            return
        now = datetime.now()
        # Heatmap slices are real UTC; DB game times are venue-local naive, so
        # converting the window band to UTC slices is tz-fragile. Instead use a
        # generous recent UTC window straight off the wall clock (catches any
        # same-day departure regardless of tz skew); the per-candidate filters
        # (direct, team-capable, recent departure) do the real selecting.
        now_utc = datetime.utcnow()
        slices = self._utc_slices(now_utc - timedelta(hours=16), now_utc, cap=24)
        if not slices:
            return
        # Departure must be recent (same machine-local frame as leg['dep'])
        dep_lo = now - timedelta(hours=16)

        o_lat, o_lon = w['origin_lat'], w['origin_lon']
        d_lat, d_lon = w['dest_lat'], w['dest_lon']
        self.statusUpdate.emit(
            f"🛰️ Backfilling {team_id.upper()} window from history "
            f"({len(slices)} slices)…")

        # Enumerate ARRIVALS at the destination — low-altitude (descending /
        # on-ground) aircraft near the dest airport — rather than the full
        # origin∩dest corridor. The flight may have DEPARTED long before our
        # slice band (day-before travel viewed the next morning), but its
        # ARRIVAL is recent; each candidate's full-day trace then confirms the
        # origin. Far fewer trace fetches than tracing the whole corridor.
        arrivals = set()
        for sd, idx in slices:
            if not self.running:
                return
            blob = self.adsb_lol.fetch_heatmap_slice(sd, idx)
            if not blob:
                continue
            hexes, lat, lon, alt = parse_heatmap(blob)
            m = _within_km_vec(lat, lon, d_lat, d_lon, 40) & (alt < 10000)
            arrivals.update(np.unique(hexes[m]).tolist())

        gc_km = detector._haversine_distance(o_lat, o_lon, d_lat, d_lon)
        found = 0
        for hx in arrivals:
            if not self.running:
                return
            hexid = f"{hx:06x}"
            trace = self.adsb_lol.fetch_trace(hexid)
            if not trace:
                continue
            # A full team flies a narrowbody airliner — bizjets on the corridor
            # are noise (the NetJets/Flexjet false leaders we first saw)
            if (trace.get('t') or '').upper() not in TEAM_CAPABLE_TYPES:
                continue
            leg = analyze_trace_leg(trace, o_lat, o_lon, 90, d_lat, d_lon, 90)
            if not leg:
                continue
            dep = leg['dep']
            # Departure must be recent; flight must be reasonably direct
            if not (dep_lo <= dep <= now + timedelta(hours=1)):
                continue
            if leg['dur_h'] > max(2.0, gc_km / 800.0 * 1.6):
                continue
            # Timing tier vs game end (tz-skew tolerant: tiers are coarse and
            # the positioning-callsign / type signals carry the fine ranking)
            pg = w.get('pregame_travel_start')
            delay_h = (dep - w['last_game_end']).total_seconds() / 3600
            if 0.75 <= delay_h <= 3.5:
                timing = 30
            elif delay_h <= 6:
                timing = 16
            elif pg is not None:
                timing = 18
            else:
                timing = 6
            cs = leg['calls'][0] if leg['calls'] else None
            flight = LiveFlight(
                icao24=hexid, callsign=cs,
                latitude=d_lat, longitude=d_lon, altitude_m=0, velocity_ms=0,
                heading=0, timestamp=now, aircraft_type=trace.get('t'),
                registration=trace.get('r'), team_id=team_id, league=league,
                confidence=85, raw_score=50.0 + timing,
                origin_city=w['origin_city'], dest_city=w['destination_city'],
                origin_lat=o_lat, origin_lon=o_lon, dest_lat=d_lat, dest_lon=d_lon,
                route_progress=1.0, window_id=window_id, timing_bonus=timing)
            score = self._calculate_flight_score(flight, team_id)
            # Two observations spanning the route → full coverage credit; this
            # is a CONFIRMED completed leg, so it gets the big confirmed bonus
            self.ledger.observe(window_id, league, team_id, flight, score, timing, 0.05)
            self.ledger.observe(window_id, league, team_id, flight, score, timing, 0.95)
            self.ledger.set_route_info(
                window_id, hexid, dep=leg['dep'], arr=leg['arr'],
                landed=leg['landed_dest'], confirmed=True,
                operator=trace.get('ownOp'), aircraft_type=trace.get('t'))
            self._route_confirmed.add(hexid)
            found += 1

        self.statusUpdate.emit(
            f"🛰️ Backfill {team_id.upper()}: {found} completed flight(s) "
            f"seeded into ledger")

    def _calculate_flight_score(self, flight: LiveFlight, team_id: str) -> float:
        """Per-TICK candidate score (fed into the ledger's observe()).

        The replay study (Knicks AAL606P, Twins SCX346) reshaped this:
        - raw_score already carries the per-candidate route+timing match from
          _evaluate_team_travel_match — that's the dominant term.
        - DROPPED the 8xxx/9xxx bonus: actively wrong for hometown-airline
          charters (real SCX346 low-numbered, scheduled SCX8992 high).
        - Operator/callsign-pattern bonuses shrink to tiebreakers: useless on
          a corridor flooded with same-operator scheduled flights (22 SCX
          DTW→MSP). Timing (in raw_score) must dominate, not these.
        """
        score = float(flight.raw_score or flight.confidence)

        # Positioning-callsign signature (AAL606P) — small, it's a tiebreaker
        if flight.callsign and len(flight.callsign) >= 5:
            cs = flight.callsign.upper()
            if cs[-1].isalpha() and cs[3:-1].isdigit():
                score += 6

        # Dedicated charter operator (Omni/Sun Country/etc.) — small tiebreaker
        for operator, patterns in TeamFlightDetector.CHARTER_PATTERNS.items():
            if flight.callsign and any(flight.callsign.upper().startswith(p)
                                       for p in patterns):
                score += 4
                break

        # Aircraft type (narrow-body airliner > regional/biz) — tiebreaker
        if flight.aircraft_type in ('B752', 'B753', 'A321', 'A21N'):
            score += 5
        elif flight.aircraft_type in ('B738', 'B739', 'A319', 'A320', 'A20N'):
            score += 3

        return score

    # Window-leader scoring weights (replay-tuned: accumulated evidence
    # dominates so the established leader doesn't flip to a later impostor)
    W_MAXSCORE   = 1.0    # best per-tick score seen
    W_OBS        = 4.0    # × log1p(n_obs): repeatedly seen = real
    W_COVERAGE   = 35.0   # × route-progress span: traversed the corridor
    W_TIMING     = 1.0    # best timing fit
    W_CONFIRMED  = 25.0   # trace confirmed origin→dest
    W_CURRENT    = 0.4    # × current-tick score if airborne right now

    def _window_score(self, row: dict, current_score: float) -> float:
        """Accumulated window evidence for one aircraft (ledger row). A landed
        charter keeps a high score via accumulated terms even though its
        current_score is 0 — so transient airborne impostors can't displace
        it."""
        span = max(0.0, (row.get('max_progress') or 0.0)
                   - (row.get('min_progress') or 0.0))
        return (self.W_MAXSCORE * (row.get('max_score') or 0.0)
                + self.W_OBS * math.log1p(row.get('n_obs') or 0)
                + self.W_COVERAGE * span
                + self.W_TIMING * (row.get('best_timing') or 0.0)
                + self.W_CONFIRMED * (row.get('confirmed_route') or 0)
                + self.W_CURRENT * current_score)

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
        """Stop the tracking thread (interrupts the poll sleep immediately)"""
        self.running = False
        self._stop_event.set()

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
            'league': flight.league,
            'reasons': flight.detection_reasons,
            'timestamp': flight.timestamp.isoformat(),
            'origin_city': flight.origin_city,
            'dest_city': flight.dest_city,
            'origin_lat': flight.origin_lat,
            'origin_lon': flight.origin_lon,
            'dest_lat': flight.dest_lat,
            'dest_lon': flight.dest_lon,
            'route_progress': flight.route_progress,
            'window_id': flight.window_id,
            'landed': flight.landed,
        }


def test_single_scan(league: str = "NBA", use_type_filter: bool = True):
    """Run a single scan cycle for testing (no threading)"""
    logger.debug("=" * 70)
    logger.debug(f"LIVE FLIGHT TRACKER - SINGLE SCAN TEST ({league})")
    logger.debug("=" * 70)

    db = DatabaseManager()
    detector = TeamFlightDetector(db, league)

    logger.debug(f"\n📍 Loaded {len(detector.team_locations)} team locations")
    logger.debug(f"✈️  Charter types: {CHARTER_AIRCRAFT_TYPES}")

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
        logger.debug(f"📅 {len(upcoming_games)} {league} games in next 48h")
    except Exception as e:
        logger.warning(f"⚠️  Could not load games: {e}")
        upcoming_games = []

    detected_flights = []

    if use_type_filter:
        # Use adsb.lol type-based filtering
        adsb = AdsbLolClient()
        types_to_scan = ['B752', 'A21N']

        logger.debug(f"\n🌐 Fetching {types_to_scan} aircraft from adsb.lol...")

        for type_code in types_to_scan:
            aircraft = adsb.get_aircraft_by_type(type_code)
            us_aircraft = adsb.filter_us_aircraft([{**ac, 'aircraft_type': type_code} for ac in aircraft])
            logger.debug(f"   {type_code}: {len(aircraft)} global, {len(us_aircraft)} in US")

            for ac in us_aircraft:
                flight = detector.detect_from_adsb_lol(ac, upcoming_games)
                if flight:
                    detected_flights.append(flight)
    else:
        # Fallback to OpenSky
        try:
            with open(Path(__file__).resolve().parent / 'api_keys.json') as f:
                keys = json.load(f)
        except FileNotFoundError:
            keys = {}

        opensky = NBAFlightTracker(
            client_id=keys.get('clientIdOS'),
            client_secret=keys.get('clientSecretOS'),
            username=keys.get('open_sky_user'),
            password=keys.get('open_sky_pwd')
        )

        logger.debug(f"\n🌐 Fetching from OpenSky...")
        states_data = opensky.get_us_states()

        if states_data and 'states' in states_data:
            logger.debug(f"📡 {len(states_data['states'])} aircraft")
            for state in states_data['states']:
                flight = detector.detect_team_flight(state, upcoming_games)
                if flight:
                    detected_flights.append(flight)

    # Report results
    logger.debug(f"\n{'=' * 70}")
    logger.debug(f"RESULTS: {len(detected_flights)} potential team flights")
    logger.debug("=" * 70)

    if detected_flights:
        detected_flights.sort(key=lambda f: f.confidence, reverse=True)
        for i, flight in enumerate(detected_flights, 1):
            type_str = f"[{flight.aircraft_type}]" if flight.aircraft_type else ""
            logger.debug(f"\n[{i}] {flight.callsign or flight.icao24} {type_str} - {flight.confidence}%")
            logger.debug(f"    Team: {flight.team_id or 'Unknown'} | Reg: {flight.registration or 'N/A'}")
            logger.debug(f"    Position: ({flight.latitude:.2f}, {flight.longitude:.2f})")
            logger.debug(f"    Alt: {flight.altitude_ft:,.0f}ft | Speed: {flight.speed_kts:.0f}kts | Hdg: {flight.heading:.0f}°")
            logger.debug(f"    Reasons: {', '.join(flight.detection_reasons)}")
    else:
        logger.debug("\nNo team flights detected. Normal if no games scheduled.")

    return detected_flights


def replay_window(origin: Tuple, dest: Tuple, d: date, slices, cache_dir=None):
    """Reconstruct a COMPLETED travel window from adsb.lol ground truth and
    rank the candidate flights (the analysis harness, folded in from the old
    charter_replay.py). origin/dest = (name, lat, lon, radius_km). `slices` =
    UTC half-hour heatmap indices (0..47) bracketing the expected departure.

    Enumerates aircraft that touched BOTH metros from the day heatmap, then
    pulls each one's trace for the real dep/arr/callsign/operator. Prints a
    ranked table; returns the candidate dicts.
    """
    cache = Path(cache_dir or "/tmp/charter_replay_cache")
    cache.mkdir(exist_ok=True)
    client = AdsbLolClient()
    o_name, o_lat, o_lon, o_r = origin
    s_name, s_lat, s_lon, s_r = dest

    near_o, near_d, total = set(), set(), 0
    for idx in slices:
        fn = cache / f"{d.isoformat()}_{idx:02d}.bin"
        blob = fn.read_bytes() if fn.exists() else client.fetch_heatmap_slice(d, idx)
        if blob and not fn.exists():
            fn.write_bytes(blob)
        if not blob:
            continue
        hexes, lat, lon, _alt = parse_heatmap(blob)
        total += len(hexes)
        near_o.update(np.unique(hexes[_within_km_vec(lat, lon, o_lat, o_lon, o_r)]).tolist())
        near_d.update(np.unique(hexes[_within_km_vec(lat, lon, s_lat, s_lon, s_r)]).tolist())
        print(f"  slice {idx:02d}: near {o_name}={len(near_o)} near {s_name}={len(near_d)}")

    corridor = near_o & near_d
    print(f"\n{total:,} positions; {len(corridor)} aircraft touched both metros")
    results = []
    for hx in corridor:
        hexid = f"{hx:06x}"
        tr = client.fetch_trace(hexid)
        if not tr:
            continue
        leg = analyze_trace_leg(tr, o_lat, o_lon, o_r, s_lat, s_lon, s_r)
        if not leg or not (1.0 < leg['dur_h'] < 8.0):
            continue
        results.append({"hex": hexid, "reg": tr.get("r", ""), "type": tr.get("t", ""),
                        "op": tr.get("ownOp", ""), **leg})
    results.sort(key=lambda r: r["dep"])
    print(f"\n{'hex':7} {'reg':9} {'type':5} {'dep':16} {'dur':5} {'callsign':16} operator")
    print("-" * 96)
    for r in results:
        print(f"{r['hex']:7} {r['reg']:9} {r['type']:5} "
              f"{r['dep'].strftime('%m-%d %H:%M'):16} {r['dur_h']:4.1f}h "
              f"{','.join(r['calls'][:2]):16} {r['op'][:26]}")
    return results


# Named replay windows for the CLI (origin/dest = name,lat,lon,radius_km)
_REPLAY_WINDOWS = {
    "nyk": (("NYC", 40.75, -74.00, 90), ("SAT", 29.45, -98.50, 70),
            date.today(), range(28, 48)),
    "min": (("DTW", 42.30, -83.25, 60), ("MSP", 44.88, -93.22, 60),
            date(2026, 6, 12), range(4, 34)),
}


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        key = sys.argv[2] if len(sys.argv) > 2 else "nyk"
        o, dst, d, sl = _REPLAY_WINDOWS[key]
        replay_window(o, dst, d, list(sl))
        sys.exit(0)

    league = sys.argv[1].upper() if len(sys.argv) > 1 else "NBA"
    use_opensky = "--opensky" in sys.argv

    if league not in ["NBA", "NHL", "MLB"]:
        logger.debug(f"Invalid league: {league}. Use NBA, NHL, or MLB.")
        sys.exit(1)

    logger.debug(f"Mode: {'OpenSky (legacy)' if use_opensky else 'adsb.lol (type-filtered)'}")
    test_single_scan(league, use_type_filter=not use_opensky)


# ===========================================================================
# Travel intelligence UI panel (merged from flight_tracker_panel.py)
# ===========================================================================

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
