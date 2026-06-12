"""Schedule-fatigue scoring engine.

Computes per-team travel fatigue for upcoming games from the cached schedule
database (games + venues), producing a 0-100 fatigue score per team per game
and a home/away differential — the "schedule edge" signal intended for
consumption by EffortOdds.

Deliberately Qt-free: only stdlib + database_manager, so it can be imported
by any host (panel, scripts, EffortOdds workers) without pulling in OpenGL.
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fallback coordinates for venue cities whose DB rows lack lat/lon
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "Phoenix": (33.4484, -112.0740), "Atlanta": (33.7490, -84.3880),
    "Baltimore": (39.2904, -76.6122), "Boston": (42.3601, -71.0589),
    "Chicago": (41.8781, -87.6298), "Cincinnati": (39.1031, -84.5120),
    "Cleveland": (41.4993, -81.6944), "Denver": (39.7392, -104.9903),
    "Detroit": (42.3314, -83.0458), "Houston": (29.7604, -95.3698),
    "Kansas City": (39.0997, -94.5786), "Los Angeles": (34.0522, -118.2437),
    "Miami": (25.7617, -80.1918), "Milwaukee": (43.0389, -87.9065),
    "Minneapolis": (44.9778, -93.2650), "New York": (40.7128, -74.0060),
    "Oakland": (37.8044, -122.2712), "Philadelphia": (39.9526, -75.1652),
    "Pittsburgh": (40.4406, -79.9959), "San Diego": (32.7157, -117.1611),
    "San Francisco": (37.7749, -122.4194), "Seattle": (47.6062, -122.3321),
    "St. Louis": (38.6270, -90.1994), "Tampa": (27.9506, -82.4572),
    "Dallas": (32.7767, -96.7970), "Washington": (38.9072, -77.0369),
    "Indianapolis": (39.7684, -86.1581), "Charlotte": (35.2271, -80.8431),
    "Orlando": (28.5383, -81.3792), "Portland": (45.5152, -122.6784),
    "Sacramento": (38.5816, -121.4944), "Salt Lake City": (40.7608, -111.8910),
    "Oklahoma City": (35.4676, -97.5164), "Memphis": (35.1495, -90.0490),
    "New Orleans": (29.9511, -90.0715), "San Antonio": (29.4241, -98.4936),
    "Buffalo": (42.8864, -78.8784), "Sunrise": (26.1354, -80.2373),
    "Raleigh": (35.7796, -78.6382), "Columbus": (39.9612, -82.9988),
    "Newark": (40.7357, -74.1724), "Nashville": (36.1627, -86.7816),
    "Anaheim": (33.8366, -117.9143), "Las Vegas": (36.1699, -115.1398),
    "San Jose": (37.3382, -121.8863),
    "Toronto": (43.6532, -79.3832), "Montreal": (45.5017, -73.5673),
    "Vancouver": (49.2827, -123.1207), "Calgary": (51.0447, -114.0719),
    "Edmonton": (53.5461, -113.4938), "Ottawa": (45.4215, -75.6972),
    "Winnipeg": (49.8951, -97.1384),
}

# Standard-time UTC offsets; cities not listed fall back to round(lon / 15)
CITY_TZ: Dict[str, int] = {
    "New York": -5, "Boston": -5, "Philadelphia": -5, "Washington": -5,
    "Miami": -5, "Atlanta": -5, "Detroit": -5, "Cleveland": -5,
    "Baltimore": -5, "Tampa": -5, "Pittsburgh": -5, "Charlotte": -5,
    "Orlando": -5, "Indianapolis": -5, "Buffalo": -5, "Sunrise": -5,
    "Raleigh": -5, "Columbus": -5, "Newark": -5, "Cincinnati": -5,
    "Chicago": -6, "Milwaukee": -6, "Minneapolis": -6, "Dallas": -6,
    "Houston": -6, "San Antonio": -6, "New Orleans": -6, "Memphis": -6,
    "Kansas City": -6, "St. Louis": -6, "Oklahoma City": -6, "Nashville": -6,
    "Denver": -7, "Phoenix": -7, "Salt Lake City": -7,
    "Los Angeles": -8, "San Francisco": -8, "Seattle": -8, "Portland": -8,
    "Las Vegas": -8, "Sacramento": -8, "San Diego": -8, "Oakland": -8,
    "Anaheim": -8, "San Jose": -8,
    "Toronto": -5, "Montreal": -5, "Ottawa": -5,
    "Winnipeg": -6, "Calgary": -7, "Edmonton": -7, "Vancouver": -8,
}

# Venues above ~3000 ft where arriving from sea level is itself a stressor
HIGH_ALTITUDE_CITIES = {"Denver", "Salt Lake City", "Calgary", "Mexico City"}

# Per-league scoring weights. MLB plays daily, so density terms (back-to-back,
# 3-in-4) are normal there and carry no weight; NBA/NHL punish them heavily.
LEAGUE_WEIGHTS = {
    "NBA": dict(b2b=20.0, three_in_four=14.0, games_7d_baseline=3.5,
                density=5.0, miles_div=60.0, tz=6.0, altitude=8.0, rest_relief=6.0),
    "NHL": dict(b2b=18.0, three_in_four=12.0, games_7d_baseline=3.5,
                density=5.0, miles_div=60.0, tz=6.0, altitude=7.0, rest_relief=6.0),
    "MLB": dict(b2b=0.0, three_in_four=0.0, games_7d_baseline=6.5,
                density=3.0, miles_div=55.0, tz=7.0, altitude=8.0, rest_relief=8.0),
}


@dataclass
class TeamFatigue:
    """Fatigue snapshot for one team going into one game."""
    team_id: str
    team_name: str
    league: str
    game_id: str
    game_date: datetime
    rest_days: int = 99            # full off-days since previous game
    miles_7d: float = 0.0          # venue-to-venue great-circle miles
    miles_14d: float = 0.0
    tz_hops_7d: int = 0            # sum of |tz changes| between venues
    games_7d: int = 0              # games played in prior 7 days
    back_to_back: bool = False
    three_in_four: bool = False
    altitude_shift: bool = False   # low-altitude -> high-altitude arrival
    score: float = 0.0             # 0 (fresh) .. 100 (cooked)

    def factors(self) -> List[str]:
        """Human-readable list of what is driving the score."""
        out = []
        if self.back_to_back:
            out.append("back-to-back")
        if self.three_in_four:
            out.append("3 games in 4 nights")
        if self.miles_7d >= 2000:
            out.append(f"{self.miles_7d:,.0f} mi in 7d")
        if self.tz_hops_7d >= 3:
            out.append(f"{self.tz_hops_7d} tz hops in 7d")
        if self.altitude_shift:
            out.append("altitude arrival")
        if self.rest_days >= 3 and self.rest_days < 99:
            out.append(f"{self.rest_days}d rest")
        return out


@dataclass
class GameFatigueReport:
    """Fatigue comparison for one upcoming game."""
    game_id: str
    league: str
    season: str
    game_date: datetime
    venue_city: str
    home: TeamFatigue
    away: TeamFatigue

    @property
    def differential(self) -> float:
        """away.score - home.score: positive means the road team is more tired."""
        return self.away.score - self.home.score

    def summary(self) -> str:
        return (f"{self.away.team_id.upper()} @ {self.home.team_id.upper()} "
                f"{self.game_date.strftime('%b %d')}: "
                f"away {self.away.score:.0f} vs home {self.home.score:.0f} "
                f"(diff {self.differential:+.0f})")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


class FatigueEngine:
    """Scores upcoming games from the cached schedule DB."""

    def __init__(self, db):
        self.db = db  # DatabaseManager

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _venue_coords(venue) -> Optional[Tuple[float, float]]:
        if venue.latitude and venue.longitude:
            return (venue.latitude, venue.longitude)
        return CITY_COORDS.get(venue.city)

    @staticmethod
    def _tz_offset(city: str, lon: Optional[float]) -> int:
        if city in CITY_TZ:
            return CITY_TZ[city]
        if lon is not None:
            return round(lon / 15.0)
        return 0

    def _build_team_timelines(self, games) -> Dict[str, List]:
        """team_id -> chronological list of that team's games."""
        timelines: Dict[str, List] = {}
        seen: Dict[str, set] = {}
        for game in sorted(games, key=lambda g: g.date):
            for team in (game.home_team, game.away_team):
                tid = team.team_id.lower()
                if game.game_id in seen.setdefault(tid, set()):
                    continue
                seen[tid].add(game.game_id)
                timelines.setdefault(tid, []).append(game)
        return timelines

    # ------------------------------------------------------------ scoring

    def score_team_for_game(self, team, game, timeline, league: str) -> TeamFatigue:
        tf = TeamFatigue(
            team_id=team.team_id.lower(),
            team_name=team.display_name,
            league=league,
            game_id=game.game_id,
            game_date=game.date,
        )
        weights = LEAGUE_WEIGHTS.get(league, LEAGUE_WEIGHTS["NBA"])

        # Past games strictly before this one (timeline is chronological)
        past = [g for g in timeline if g.date < game.date and g.game_id != game.game_id]
        if not past:
            return tf

        last_game = past[-1]
        tf.rest_days = max((game.date.date() - last_game.date.date()).days - 1, 0)
        tf.back_to_back = (game.date.date() - last_game.date.date()).days == 1

        window_4d = game.date - timedelta(days=3)
        games_in_4 = sum(1 for g in past if g.date >= window_4d) + 1  # incl. this game
        tf.three_in_four = games_in_4 >= 3

        window_7d = game.date - timedelta(days=7)
        window_14d = game.date - timedelta(days=14)
        tf.games_7d = sum(1 for g in past if g.date >= window_7d)

        # Travel legs: consecutive venues over the past 14 days, plus the leg
        # into this game's venue
        legs = [g for g in past if g.date >= window_14d] + [game]
        prev_coords = None
        prev_tz = None
        prev_city = None
        for g in legs:
            coords = self._venue_coords(g.venue)
            tz = self._tz_offset(g.venue.city, coords[1] if coords else None)
            if prev_coords and coords and g.venue.city != prev_city:
                miles = haversine_miles(*prev_coords, *coords)
                tf.miles_14d += miles
                if g.date >= window_7d:
                    tf.miles_7d += miles
                    if prev_tz is not None:
                        tf.tz_hops_7d += abs(tz - prev_tz)
            if coords:
                prev_coords = coords
                prev_tz = tz
                prev_city = g.venue.city

        # Altitude arrival: this venue is high, the previous one wasn't
        tf.altitude_shift = (game.venue.city in HIGH_ALTITUDE_CITIES
                             and last_game.venue.city not in HIGH_ALTITUDE_CITIES)

        score = 0.0
        score += tf.miles_7d / weights["miles_div"]
        score += tf.tz_hops_7d * weights["tz"]
        if tf.back_to_back:
            score += weights["b2b"]
        if tf.three_in_four:
            score += weights["three_in_four"]
        density_excess = max(tf.games_7d - weights["games_7d_baseline"], 0)
        score += density_excess * weights["density"]
        if tf.altitude_shift:
            score += weights["altitude"]
        score -= min(tf.rest_days, 3) * weights["rest_relief"]

        tf.score = max(0.0, min(100.0, score))
        return tf

    def score_upcoming_games(self, league: str, season: str,
                             days_ahead: int = 14,
                             now: Optional[datetime] = None) -> List[GameFatigueReport]:
        """Score every game in [now, now+days_ahead] for the given league/season."""
        now = now or datetime.now()
        cutoff = now + timedelta(days=days_ahead)

        games = self.db.load_games(season, league)
        if not games:
            return []

        timelines = self._build_team_timelines(games)
        upcoming = [g for g in games if now <= g.date <= cutoff]

        reports = []
        for game in sorted(upcoming, key=lambda g: g.date):
            home_tl = timelines.get(game.home_team.team_id.lower(), [])
            away_tl = timelines.get(game.away_team.team_id.lower(), [])
            reports.append(GameFatigueReport(
                game_id=game.game_id,
                league=league,
                season=season,
                game_date=game.date,
                venue_city=game.venue.city,
                home=self.score_team_for_game(game.home_team, game, home_tl, league),
                away=self.score_team_for_game(game.away_team, game, away_tl, league),
            ))

        return reports
