"""
PropWindowUtils.py — central player statistics/data utility for the props
window. Merges the former mlb_prop_stats.py (data layer) and
prop_player_detail.py (Player Detail panel), plus the matchup context layer.

Layout of this module:

  1. MLB DATA LAYER (Qt-free)
     - MARKET_STATS: every batter_*/pitcher_* Odds API market key mapped to a
       game-log stat extractor
     - MLBPropStats: roster/name resolution, per-player game logs, and
       PropStatSummary (Szn/L5/L10 averages + hit-rate vs a line) from the
       public MLB StatsAPI (statsapi.mlb.com — no key)
     - MatchupContext: today's game for a team — opponent, venue, park
       factors (MLBAnalytics/parkFactors.csv), probable opposing pitcher with
       season rates, opposing team batting (for pitcher props), and stadium
       weather (weatherman.WeatherService + STADIUM_DATA coords)

  2. PLAYER DETAIL PANEL (Qt)
     - PlayerDetailPanel: headshot header, stat badges, stat/line switcher,
       matchup strip, last-15-games bar chart vs the prop line, Savant
       percentile bars (reuses PercentileBar from player_overlay_widget)

All network fetches are aiohttp coroutines taking the caller's ClientSession
(mirroring propQuery.PropClient) so the qasync window drives them directly;
the sync weather call runs in an executor. Everything is cached in-memory
per process; the roster is disk-cached with a 1-day TTL.
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import aiohttp

STATS_BASE = "https://statsapi.mlb.com/api/v1"
SAVE_DIR = Path(__file__).resolve().parent / "savedata"
SAVE_DIR.mkdir(exist_ok=True)
# Per-player pitch-by-pitch detail (same query savant_bbe_fetch.py uses).
# type=details returns EVERY pitch the player saw/threw this season, so both
# whiff rates and contact quality can be computed per pitch type.
SAVANT_SEARCH_URL = (
    "https://baseballsavant.mlb.com/statcast_search/csv"
    "?hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones=&hfPull="
    "&hfC=&hfSea={year}%7C&hfSit=&player_type={player_type}"
    "&hfOuts=&hfOpponent=&pitcher_throws=&batter_stands=&hfSA=&min_pitches=0"
    "&min_results=0&group_by=name&sort_col=pitches&player_event_sort=api_p_release_speed"
    "&sort_order=desc&min_abs=0&type=details&player_id={player_id}"
)
SAVANT_XSTATS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type={player_type}&year={year}&position=&team=&min=25&csv=true"
)
BULLPEN_USAGE_URL = "https://insidethepen.com/bullpen-usage.html"
# SP deep card boards (league-wide CSVs, cached; joined by MLBAM id)
SAVANT_ARSENAL_STATS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
    "?type=pitcher&year={year}&min=5&csv=true"
)
SAVANT_ARM_ANGLES_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitcher-arm-angles"
    "?game_type=Regular&season={year}&csv=true"
)
SAVANT_ARSENALS_URL = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-arsenals"
    "?year={year}&min=50&type={kind}&csv=true"
)

# FanGraphs internal leaders API. Cloudflare 403s every non-browser client
# from this network (requests, curl_cffi impersonation, even their HTML), but
# a real headless Firefox passes untouched — so Selenium is used purely as
# transport: load robots.txt for a same-origin context, run fetch() in-page,
# return the JSON. One call returns the FULL stat row per player (Stuff+,
# per-pitch sp_s_XX, FIP/xFIP/SIERA, gmLI, ...) keyed by xMLBAMID.
FG_API_PATH = ("/api/leaders/major-league/data?age=&pos=all&stats={stats}"
               "&lg=all&qual={qual}&season={season}&season1={season}"
               "&ind=0&type=8&month=0&pageitems={pageitems}&pagenum=1")
FG_CACHE_TTL = 6 * 3600

# Savant pitch_name -> FanGraphs per-pitch Stuff+ column suffix
FG_PITCH_CODES = {
    "4-Seam Fastball": "FF", "Sinker": "SI", "Cutter": "FC",
    "Slider": "SL", "Sweeper": "SL",     # FG folds sweepers into SL
    "Curveball": "CU", "Knuckle Curve": "KC", "Changeup": "CH",
    "Split-Finger": "FS", "Forkball": "FO",
}


# ---------------------------------------------------------------------------
# insidethepen authenticated session (Advanced Pitcher Traits sit behind a
# free-account content gate). Credentials live in Creds.py:
#   INSIDETHEPEN_EMAIL = "..."
#   INSIDETHEPEN_PASSWORD = "..."
# Cookies persist to savedata/ so login happens rarely. Without creds the
# fetch still returns the ungated parts (site role, snapshot, BP power rank).
# ---------------------------------------------------------------------------

try:
    import Creds as _Creds
    ITP_EMAIL = getattr(_Creds, "INSIDETHEPEN_EMAIL", None)
    ITP_PASSWORD = getattr(_Creds, "INSIDETHEPEN_PASSWORD", None)
except ImportError:
    ITP_EMAIL = ITP_PASSWORD = None

# NOTE: bare domain, NOT www — www 301-redirects, which turns the login POST
# into a GET (credentials dropped) and strands the session cookie
ITP_BASE = "https://insidethepen.com"
ITP_COOKIES_FILE = SAVE_DIR / "itp_cookies.json"
_ITP_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:144.0) "
           "Gecko/20100101 Firefox/144.0")
_itp_session = None
_itp_lock = __import__("threading").Lock()

# Substring-matched against the page text ("Pitches versus LH batters" on
# the live site vs "Pitchers versus..." in the old saved CSVs — the shorter
# forms match both)
_ITP_TRAIT_LABELS = [
    "Games Pitched this Season", "Games Started this Season",
    "versus LH batters", "versus RH batters",
    "Avg Inning when called", "Avg Run Diff when called",
    "over 30 pitches", "before the 8th",
    "back to back days",
]


def _get_itp_session():
    """Shared requests session with persisted cookies (thread-safe)."""
    global _itp_session
    import requests as _rq
    with _itp_lock:
        if _itp_session is None:
            s = _rq.Session()
            s.headers["User-Agent"] = _ITP_UA
            if ITP_COOKIES_FILE.exists():
                try:
                    s.cookies.update(json.loads(ITP_COOKIES_FILE.read_text()))
                except Exception:
                    pass
            _itp_session = s
        return _itp_session


_itp_logged_in = False


def _itp_login() -> bool:
    """Log in with the Creds account; persists cookies. Returns success.
    Serialized so concurrent reliever fetches trigger exactly one login."""
    global _itp_logged_in
    if not (ITP_EMAIL and ITP_PASSWORD):
        return False
    s = _get_itp_session()   # acquires/releases the lock itself
    with _itp_lock:
        if _itp_logged_in:
            return True
        return _itp_login_inner(s)


def _itp_login_inner(s) -> bool:
    global _itp_logged_in
    from bs4 import BeautifulSoup
    try:
        r = s.get(f"{ITP_BASE}/login.html", timeout=20)
        soup = BeautifulSoup(r.content, "lxml")
        token_input = soup.find("input", {"name": "csrf_token"})
        token = token_input.get("value") if token_input else ""
        r2 = s.post(f"{ITP_BASE}/login.html", timeout=20, data={
            "csrf_token": token, "email": ITP_EMAIL,
            "pass2": ITP_PASSWORD, "stayin": "1"})
        ok = "logout" in r2.text.lower() or "login.html" not in r2.url
        if ok:
            global _itp_logged_in
            _itp_logged_in = True
            try:
                ITP_COOKIES_FILE.write_text(json.dumps(dict(s.cookies)))
            except Exception:
                pass
            print("PropWindowUtils: insidethepen login OK")
        else:
            print("PropWindowUtils: insidethepen login FAILED (check creds)")
        return ok
    except Exception as e:
        print(f"PropWindowUtils: insidethepen login error: {e}")
        return False


_itp_warned = False


def fetch_reliever_page_sync(pid: int, name_slug_href: Optional[str] = None) -> dict:
    """Fetch one reliever's insidethepen page: site role + snapshot (ungated)
    and Advanced Pitcher Traits (needs the free-account login). Blocking —
    call from an executor. Returns {} fields that couldn't be parsed."""
    import re as _re
    from bs4 import BeautifulSoup
    s = _get_itp_session()
    url = (f"{ITP_BASE}{name_slug_href.lstrip('.')}" if name_slug_href
           else f"{ITP_BASE}/pitcher/x-{pid}.html")
    out: dict = {}
    try:
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            return out
        text = r.text
        # Traits gated? one login attempt, then refetch
        if "Games Pitched this Season" not in text:
            if ITP_EMAIL:
                if _itp_login():
                    r = s.get(url, timeout=20)
                    text = r.text
            else:
                global _itp_warned
                if not _itp_warned:
                    _itp_warned = True
                    print("PropWindowUtils: insidethepen traits are gated — "
                          "add INSIDETHEPEN_EMAIL / INSIDETHEPEN_PASSWORD to "
                          "Creds.py to fill the vs/Inn/Diff/B2B columns")
        soup = BeautifulSoup(r.content, "lxml")
        flat = soup.get_text("\n", strip=True)

        m = _re.search(r"Primary Role\(s\):\s*\n?([^\n]+)", flat)
        if m:
            out["role_site"] = m.group(1).strip()
        m = _re.search(r"IP \(last 7 games\):\s*\n?([\d.]+)", flat)
        if m:
            out["ip7"] = m.group(1)
        m = _re.search(r"ERA \(last 7 games\):\s*\n?([\d.]+)", flat)
        if m:
            out["era7"] = m.group(1)
        m = _re.search(r"BP Power Rank:\s*\n?#?(\d+)", flat)
        if m:
            out["power_rank"] = int(m.group(1))

        traits = {}
        for label in _ITP_TRAIT_LABELS:
            m = _re.search(_re.escape(label) + r":?\s*\n?([^\n]+)", flat)
            if m:
                traits[label] = m.group(1).strip()
        if traits:
            out["traits"] = traits
    except Exception as e:
        print(f"PropWindowUtils: reliever page fetch failed ({pid}): {e}")
    return out


def fetch_fg_leaders_sync(stats: str = "pit", qual: str = "1",
                          season: Optional[int] = None,
                          pageitems: int = 3000) -> List[dict]:
    """Fetch a FanGraphs leaders board via headless Firefox (see note above).
    Disk-cached under savedata/ with a 6h TTL. Blocking — call from an
    executor. Returns [] on any failure."""
    season = season or datetime.now().year
    cache = SAVE_DIR / f"fg_{stats}_{season}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < FG_CACHE_TTL:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    rows: List[dict] = []
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        opts = Options()
        opts.add_argument("-headless")
        driver = webdriver.Firefox(options=opts)
        driver.set_page_load_timeout(45)
        driver.get("https://www.fangraphs.com/robots.txt")
        path = FG_API_PATH.format(stats=stats, qual=qual, season=season,
                                  pageitems=pageitems)
        driver.execute_script(
            "window.__fgout=null;"
            f"fetch('{path}').then(r=>r.text())"
            ".then(t=>{window.__fgout=t}).catch(e=>{window.__fgout='ERR:'+e});")
        out = None
        for _ in range(45):
            time.sleep(1)
            out = driver.execute_script("return window.__fgout")
            if out:
                break
        if out and not str(out).startswith("ERR:"):
            rows = json.loads(out).get("data", [])
            try:
                cache.write_text(json.dumps(rows))
            except Exception:
                pass
        else:
            print(f"PropWindowUtils: FG api fetch failed: {str(out)[:120]}")
    except Exception as e:
        print(f"PropWindowUtils: FG selenium fetch failed: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return rows
SAVANT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://baseballsavant.mlb.com/",
    "Accept-Language": "en-US,en;q=0.9",
}
PARK_FACTORS_CSV = Path(__file__).resolve().parent.parent / "MLBAnalytics" / "parkFactors.csv"
ROSTER_TTL = 24 * 3600          # disk cache lifetime
LOG_TTL = 15 * 60               # in-memory game-log / schedule lifetime
WEATHER_TTL = 15 * 60
PITCH_SPLITS_TTL = 3600         # Savant pitch-level detail lifetime
MAX_CONCURRENT = 8

# StatsAPI pitchArsenal descriptions -> Savant pitch_name (identity otherwise)
_ARSENAL_NAME_MAP = {
    "Four-seam FB": "4-Seam Fastball",
    "Two-seam FB": "2-Seam Fastball",
    "Splitter": "Split-Finger",
}

# Statcast swing/whiff description sets (pitch-level splits)
_SWING_DESCS = {"swinging_strike", "swinging_strike_blocked", "foul",
                "foul_tip", "hit_into_play", "foul_bunt", "missed_bunt",
                "bunt_foul_tip"}
_WHIFF_DESCS = {"swinging_strike", "swinging_strike_blocked", "missed_bunt"}

# Pitch families + velocity bands for the velo-split view ("hits 4FB fine at
# ≤96 but struggles above"). Band edges chosen around league-typical ranges.
_FB_TYPES = {"4-Seam Fastball", "2-Seam Fastball", "Fastball", "Sinker",
             "Cutter"}
_BRK_TYPES = {"Slider", "Sweeper", "Curveball", "Knuckle Curve", "Slurve",
              "Slow Curve", "Screwball", "Eephus"}
_OFF_TYPES = {"Changeup", "Split-Finger", "Forkball", "Knuckleball"}
VELO_BAND_ORDER = ["FB ≤92", "FB 93-94", "FB 95-96", "FB 97+",
                   "Brk ≤79", "Brk 80-84", "Brk 85+",
                   "Off ≤84", "Off 85+"]


def _velo_band(pitch_name: str, velo: Optional[float]) -> Optional[str]:
    if velo is None:
        return None
    if pitch_name in _FB_TYPES:
        if velo < 93:
            return "FB ≤92"
        if velo < 95:
            return "FB 93-94"
        if velo < 97:
            return "FB 95-96"
        return "FB 97+"
    if pitch_name in _BRK_TYPES:
        if velo < 80:
            return "Brk ≤79"
        if velo < 85:
            return "Brk 80-84"
        return "Brk 85+"
    if pitch_name in _OFF_TYPES:
        return "Off ≤84" if velo < 85 else "Off 85+"
    return None


def _is_barrel(ev: float, la: float) -> bool:
    """Standard barrel-zone approximation: starts at 98 mph / 26-30°,
    widening ~1° down and ~2° up per mph, capped at the 8-50° window."""
    if ev < 98:
        return False
    lo = max(26 - (ev - 98), 8)
    hi = min(30 + 2 * (ev - 98), 50)
    return lo <= la <= hi


# ===========================================================================
# 1. MLB DATA LAYER
# ===========================================================================

# ---------------------------------------------------------------------------
# Market key -> stat extraction
# ---------------------------------------------------------------------------

def _f(stat: dict, key: str) -> float:
    v = stat.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _singles(s: dict) -> float:
    return _f(s, "hits") - _f(s, "doubles") - _f(s, "triples") - _f(s, "homeRuns")


def _hits_runs_rbis(s: dict) -> float:
    return _f(s, "hits") + _f(s, "runs") + _f(s, "rbi")


def _dk_fantasy(s: dict) -> float:
    # DraftKings MLB hitter scoring
    return (3 * _singles(s) + 5 * _f(s, "doubles") + 8 * _f(s, "triples")
            + 10 * _f(s, "homeRuns") + 2 * _f(s, "rbi") + 2 * _f(s, "runs")
            + 2 * _f(s, "baseOnBalls") + 2 * _f(s, "hitByPitch")
            + 5 * _f(s, "stolenBases"))


@dataclass(frozen=True)
class MarketStat:
    group: str                                # "hitting" | "pitching"
    extract: Callable[[dict], float]
    display: str                              # short label for UI
    yes_no: bool = False                      # market has no point; line=0.5


MARKET_STATS: Dict[str, MarketStat] = {
    "batter_home_runs":       MarketStat("hitting", lambda s: _f(s, "homeRuns"), "HR"),
    "batter_first_home_run":  MarketStat("hitting", lambda s: _f(s, "homeRuns"), "HR", yes_no=True),
    "batter_hits":            MarketStat("hitting", lambda s: _f(s, "hits"), "H"),
    "batter_total_bases":     MarketStat("hitting", lambda s: _f(s, "totalBases"), "TB"),
    "batter_rbis":            MarketStat("hitting", lambda s: _f(s, "rbi"), "RBI"),
    "batter_runs_scored":     MarketStat("hitting", lambda s: _f(s, "runs"), "R"),
    "batter_hits_runs_rbis":  MarketStat("hitting", _hits_runs_rbis, "H+R+RBI"),
    "batter_singles":         MarketStat("hitting", _singles, "1B"),
    "batter_doubles":         MarketStat("hitting", lambda s: _f(s, "doubles"), "2B"),
    "batter_triples":         MarketStat("hitting", lambda s: _f(s, "triples"), "3B"),
    "batter_walks":           MarketStat("hitting", lambda s: _f(s, "baseOnBalls"), "BB"),
    "batter_strikeouts":      MarketStat("hitting", lambda s: _f(s, "strikeOuts"), "K"),
    "batter_stolen_bases":    MarketStat("hitting", lambda s: _f(s, "stolenBases"), "SB"),
    "batter_fantasy_score":   MarketStat("hitting", _dk_fantasy, "FPTS"),
    "pitcher_strikeouts":     MarketStat("pitching", lambda s: _f(s, "strikeOuts"), "K"),
    "pitcher_record_a_win":   MarketStat("pitching", lambda s: _f(s, "wins"), "W", yes_no=True),
    "pitcher_hits_allowed":   MarketStat("pitching", lambda s: _f(s, "hits"), "H"),
    "pitcher_walks":          MarketStat("pitching", lambda s: _f(s, "baseOnBalls"), "BB"),
    "pitcher_earned_runs":    MarketStat("pitching", lambda s: _f(s, "earnedRuns"), "ER"),
    "pitcher_outs":           MarketStat("pitching", lambda s: _f(s, "outs"), "Outs"),
}


def market_stat_for(market_key: str) -> Optional[MarketStat]:
    """Resolve a market key (alternate variants included) to its stat."""
    base = market_key
    if base.endswith("_alternate"):
        base = base[: -len("_alternate")]
    return MARKET_STATS.get(base)


# ---------------------------------------------------------------------------
# Name normalization (Odds API "Kyle Schwarber" -> StatsAPI fullName)
# ---------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace(".", " ").replace("-", " ").replace("'", "")
    parts = [p for p in s.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class GameValue:
    date: str                # "2026-07-02"
    opponent: str            # team abbreviation, e.g. "LAA"
    is_home: bool
    value: float


@dataclass
class PropStatSummary:
    player_id: int
    player_name: str          # StatsAPI fullName
    team: str                 # team abbreviation
    position: str             # e.g. "1B", "SP"
    market_key: str
    stat_label: str
    line: Optional[float]
    games: List[GameValue] = field(default_factory=list)  # oldest -> newest
    season_avg: float = 0.0
    l5_avg: float = 0.0
    l10_avg: float = 0.0
    hit_rate: Optional[float] = None       # season, vs line
    hit_rate_l10: Optional[float] = None

    @property
    def games_played(self) -> int:
        return len(self.games)


@dataclass
class PitchSplit:
    """One pitch type's line from a player's pitch-level Statcast detail.
    For batters: performance AGAINST the pitch. For pitchers: what their
    pitch gives up / generates."""
    pitch: str                  # "4-Seam Fastball"
    count: int                  # pitches seen/thrown
    velo: Optional[float]       # avg release_speed
    whiff_pct: Optional[float]  # whiffs / swings
    bbe: int                    # batted ball events (hit_into_play)
    avg_ev: Optional[float]
    hardhit_pct: Optional[float]   # EV >= 95 among BBE
    barrel_pct: Optional[float]    # barrel-zone approximation among BBE
    hr: int
    xwobacon: Optional[float]      # avg estimated_woba_using_speedangle


@dataclass
class MatchupContext:
    """Today's game context for one team, as shown on the detail panel."""
    team: str                              # abbr the lookup was for
    opponent: str
    is_home: bool
    venue: str
    game_time: str                         # local "7:05 PM" style
    park_factor: Optional[int] = None      # overall runs factor (100 = avg)
    park_hr_factor: Optional[int] = None
    opp_pitcher_id: Optional[int] = None
    opp_pitcher_name: Optional[str] = None
    opp_pitcher_hand: Optional[str] = None
    opp_pitcher_stats: Optional[dict] = None   # era, whip, k9, hr9, bb9, ip
    opp_team_batting: Optional[dict] = None    # ops, avg, k_per_game, hr (pitcher props)
    weather: Optional[dict] = None             # temperature, wind_speed, wind_dir_compass, condition
    batter_vs_hand: Optional[str] = None       # batter's slash vs the SP's hand
    bvp: Optional[str] = None                  # batter's career line vs this SP
    opp_pitcher_stuff: Optional[dict] = None   # FG Stuff+/Loc+/Pitch+ (late-filled)
    opp_pitcher_arm: Optional[float] = None    # arm angle ° (late-filled)
    lineup_posted: Optional[bool] = None       # batter's team lineup posted?
    lineup_slot: Optional[int] = None          # 1-9 when in the posted lineup
    probable_sp: Optional[bool] = None         # pitcher props: is he the probable?


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _compass(deg) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    try:
        return dirs[int((float(deg) + 22.5) % 360 // 45)]
    except (TypeError, ValueError):
        return "?"


def load_park_factors() -> Dict[str, dict]:
    """parkFactors.csv keyed by venue name. {'park_factor': int, 'hr': int}"""
    factors = {}
    try:
        with PARK_FACTORS_CSV.open(newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    factors[row["Venue"]] = {
                        "park_factor": int(row["Park Factor"]),
                        "hr": int(row["HR"]),
                    }
                except (KeyError, ValueError):
                    continue
    except OSError as e:
        print(f"PropWindowUtils: park factors unavailable: {e}")
    return factors


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MLBPropStats:
    def __init__(self, season: Optional[int] = None):
        self.season = season or datetime.now().year
        self._roster: Dict[str, dict] = {}        # norm name -> player record
        self._teams: Dict[int, str] = {}          # team id -> abbreviation
        self._logs: Dict[tuple, tuple] = {}       # (pid, group) -> (ts, splits)
        self._schedule: Optional[tuple] = None    # (ts, games list)
        self._persons: Dict[int, dict] = {}       # pid -> person record (hand)
        self._season_pitching: Dict[int, dict] = {}
        self._season_stats: Dict[tuple, Optional[dict]] = {}  # (pid, group)
        self._hand_splits: Dict[int, dict] = {}   # batter pid -> {vl:, vr:}
        self._bvp: Dict[tuple, Optional[dict]] = {}  # (batter, pitcher)
        self._team_batting: Dict[int, dict] = {}
        self._weather: Dict[str, tuple] = {}      # venue -> (ts, dict|None)
        self._pitch_splits: Dict[tuple, tuple] = {}   # (pid, type) -> (ts, rows)
        self._arsenals: Dict[int, dict] = {}          # pid -> {pitch_name: {...}}
        self._xstats: Dict[str, tuple] = {}           # type -> (ts, {pid: row})
        self._bullpen: Optional[tuple] = None         # (ts, {abbr: [rows]})
        self._fg_pitching: Optional[Dict[int, dict]] = None  # xMLBAMID -> row
        self._fg_batting: Optional[Dict[int, dict]] = None   # xMLBAMID -> row
        self._fg_lock = asyncio.Lock()
        self._itp_pages: Dict[int, tuple] = {}       # pid -> (ts, page dict)
        self._arsenal_stats: Optional[tuple] = None  # (ts, {(pid, pt): row})
        self._arm_angles: Optional[tuple] = None     # (ts, {pid: angle})
        self._arsenal_phys: Optional[tuple] = None   # (ts, {pid: {pt: {...}}})
        self._park_factors: Optional[Dict[str, dict]] = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._roster_lock = asyncio.Lock()

    # ------------------------------------------------------------ fetching

    async def _get_json(self, session: aiohttp.ClientSession, url: str,
                        params: Optional[dict] = None) -> Optional[dict]:
        async with self._sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"PropWindowUtils: {resp.status} for {url}")
                        return None
                    return await resp.json()
            except Exception as e:
                print(f"PropWindowUtils: error fetching {url}: {e}")
                return None

    async def ensure_roster(self, session: aiohttp.ClientSession) -> bool:
        """Load the season player list + team abbreviations (disk-cached)."""
        async with self._roster_lock:
            if self._roster and self._teams:
                return True
            cache = SAVE_DIR / f"mlb_roster_{self.season}.json"
            data = None
            if cache.exists() and time.time() - cache.stat().st_mtime < ROSTER_TTL:
                try:
                    data = json.loads(cache.read_text())
                except Exception:
                    data = None
            if data is None:
                players = await self._get_json(
                    session, f"{STATS_BASE}/sports/1/players",
                    {"season": str(self.season)})
                teams = await self._get_json(
                    session, f"{STATS_BASE}/teams", {"sportId": "1"})
                if not players or not teams:
                    return False
                data = {"players": players.get("people", []),
                        "teams": teams.get("teams", [])}
                try:
                    cache.write_text(json.dumps(data))
                except Exception:
                    pass

            self._teams = {t["id"]: t.get("abbreviation", "?")
                           for t in data["teams"]}
            self._team_name_to_abbr = {t.get("name", ""): t.get("abbreviation", "?")
                                       for t in data["teams"]}
            for p in data["players"]:
                rec = {
                    "id": p["id"],
                    "name": p.get("fullName", ""),
                    "team_id": (p.get("currentTeam") or {}).get("id"),
                    "position": (p.get("primaryPosition") or {}).get("abbreviation", ""),
                }
                self._roster[norm_name(rec["name"])] = rec
            print(f"PropWindowUtils: roster loaded "
                  f"({len(self._roster)} players, {len(self._teams)} teams)")
            return True

    def resolve_player(self, name: str) -> Optional[dict]:
        """Look up a prop's player name in the roster. Falls back to a
        last-name + first-initial match for nickname/format mismatches."""
        key = norm_name(name)
        rec = self._roster.get(key)
        if rec:
            return rec
        parts = key.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            candidates = [r for k, r in self._roster.items()
                          if k.split()[-1] == last and k.split()[0][:1] == first[:1]]
            if len(candidates) == 1:
                return candidates[0]
        return None

    async def get_game_log(self, session: aiohttp.ClientSession,
                           player_id: int, group: str) -> Optional[List[dict]]:
        key = (player_id, group)
        cached = self._logs.get(key)
        if cached and time.time() - cached[0] < LOG_TTL:
            return cached[1]
        data = await self._get_json(
            session, f"{STATS_BASE}/people/{player_id}/stats",
            {"stats": "gameLog", "group": group, "season": str(self.season)})
        splits: List[dict] = []
        if data:
            for block in data.get("stats", []):
                splits.extend(block.get("splits", []))
        # Regular-season logs come oldest-first already; sort to be safe.
        splits.sort(key=lambda s: s.get("date", ""))
        self._logs[key] = (time.time(), splits)
        return splits

    # ----------------------------------------------------------- summaries

    async def summarize(self, session: aiohttp.ClientSession, player_name: str,
                        market_key: str, line: Optional[float]) -> Optional[PropStatSummary]:
        """Build the full stat summary for one prop row. Returns None when the
        player or market can't be resolved."""
        ms = market_stat_for(market_key)
        if ms is None:
            return None
        if not await self.ensure_roster(session):
            return None
        rec = self.resolve_player(player_name)
        if rec is None:
            print(f"PropWindowUtils: no roster match for '{player_name}'")
            return None
        splits = await self.get_game_log(session, rec["id"], ms.group)
        if not splits:
            return None

        eff_line = 0.5 if (ms.yes_no or line is None) else line
        games: List[GameValue] = []
        for s in splits:
            stat = s.get("stat", {})
            opp_id = (s.get("opponent") or {}).get("id")
            games.append(GameValue(
                date=s.get("date", ""),
                opponent=self._teams.get(opp_id, "?"),
                is_home=bool(s.get("isHome")),
                value=ms.extract(stat),
            ))

        values = [g.value for g in games]
        summary = PropStatSummary(
            player_id=rec["id"],
            player_name=rec["name"],
            team=self._teams.get(rec["team_id"], "?"),
            position=rec["position"],
            market_key=market_key,
            stat_label=ms.display,
            line=eff_line,
            games=games,
            season_avg=_avg(values),
            l5_avg=_avg(values[-5:]),
            l10_avg=_avg(values[-10:]),
        )
        if values:
            summary.hit_rate = sum(v > eff_line for v in values) / len(values)
            last10 = values[-10:]
            summary.hit_rate_l10 = sum(v > eff_line for v in last10) / len(last10)
        return summary

    async def summarize_many(self, session: aiohttp.ClientSession,
                             rows: List[tuple]) -> Dict[str, Optional[PropStatSummary]]:
        """rows: [(row_label, player_name, market_key, line), ...] ->
        {row_label: summary|None}. Fetches concurrently (bounded)."""
        if not await self.ensure_roster(session):
            return {label: None for label, *_ in rows}

        async def one(label, player, market, line):
            try:
                return label, await self.summarize(session, player, market, line)
            except Exception as e:
                print(f"PropWindowUtils: summarize failed for {player}: {e}")
                return label, None

        results = await asyncio.gather(*(one(*row) for row in rows))
        return dict(results)

    # ------------------------------------------------------------- matchup

    async def _get_schedule(self, session: aiohttp.ClientSession) -> List[dict]:
        """Today's games hydrated with probable pitchers (cached)."""
        if self._schedule and time.time() - self._schedule[0] < LOG_TTL:
            return self._schedule[1]
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self._get_json(session, f"{STATS_BASE}/schedule", {
            "sportId": "1", "date": today,
            "hydrate": "probablePitcher,lineups"})
        games = []
        if data:
            for d in data.get("dates", []):
                games.extend(d.get("games", []))
        self._schedule = (time.time(), games)
        return games

    async def get_lineup_maps(self, session: aiohttp.ClientSession) -> Dict[str, dict]:
        """Per-team lineup/probable state from today's schedule:
        {abbr: {'posted': bool, 'slots': {pid: 1-9}, 'probable': pid|None}}.
        Built from the cached schedule fetch (lineups hydrate)."""
        games = await self._get_schedule(session)
        maps: Dict[str, dict] = {}
        for g in games:
            lineups = g.get("lineups") or {}
            for side, key in (("home", "homePlayers"), ("away", "awayPlayers")):
                team = (g.get("teams", {}).get(side, {}).get("team") or {})
                abbr = self._teams.get(team.get("id"))
                if not abbr:
                    continue
                m = maps.setdefault(abbr, {"posted": False, "slots": {},
                                           "probable": None})
                players = lineups.get(key) or []
                if players and not m["posted"]:
                    m["posted"] = True
                    m["slots"] = {p.get("id"): i + 1
                                  for i, p in enumerate(players)}
                prob = g.get("teams", {}).get(side, {}).get("probablePitcher")
                if prob and m["probable"] is None:
                    m["probable"] = prob.get("id")
        return maps

    async def _get_person(self, session: aiohttp.ClientSession, pid: int) -> dict:
        if pid not in self._persons:
            data = await self._get_json(session, f"{STATS_BASE}/people/{pid}")
            people = (data or {}).get("people") or [{}]
            self._persons[pid] = people[0]
        return self._persons[pid]

    async def _get_season_pitching(self, session: aiohttp.ClientSession,
                                   pid: int) -> Optional[dict]:
        if pid not in self._season_pitching:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{pid}/stats",
                {"stats": "season", "group": "pitching",
                 "season": str(self.season)})
            stat = None
            try:
                stat = data["stats"][0]["splits"][0]["stat"]
            except (TypeError, KeyError, IndexError):
                pass
            self._season_pitching[pid] = stat
        s = self._season_pitching[pid]
        if not s:
            return None
        return {
            "era": s.get("era"), "whip": s.get("whip"),
            "ip": s.get("inningsPitched"),
            "k9": s.get("strikeoutsPer9Inn"),
            "bb9": s.get("walksPer9Inn"),
            "hr9": s.get("homeRunsPer9"),
        }

    async def _get_team_batting(self, session: aiohttp.ClientSession,
                                team_id: int) -> Optional[dict]:
        if team_id not in self._team_batting:
            data = await self._get_json(
                session, f"{STATS_BASE}/teams/{team_id}/stats",
                {"group": "hitting", "stats": "season",
                 "season": str(self.season)})
            stat = None
            try:
                stat = data["stats"][0]["splits"][0]["stat"]
            except (TypeError, KeyError, IndexError):
                pass
            self._team_batting[team_id] = stat
        s = self._team_batting[team_id]
        if not s:
            return None
        games = s.get("gamesPlayed") or 0
        return {
            "ops": s.get("ops"), "avg": s.get("avg"),
            "k_per_game": (s.get("strikeOuts", 0) / games) if games else None,
            "hr": s.get("homeRuns"),
        }

    async def _get_season_stat_block(self, session: aiohttp.ClientSession,
                                     pid: int, group: str) -> Optional[dict]:
        key = (pid, group)
        if key not in self._season_stats:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{pid}/stats",
                {"stats": "season", "group": group, "season": str(self.season)})
            stat = None
            try:
                stat = data["stats"][0]["splits"][0]["stat"]
            except (TypeError, KeyError, IndexError):
                pass
            self._season_stats[key] = stat
        return self._season_stats[key]

    async def get_traditional_stats(self, session: aiohttp.ClientSession,
                                    pid: int, group: str) -> List[tuple]:
        """Condensed traditional season line for the detail header:
        ordered [(label, value), ...]. Rate stats derived where StatsAPI
        serves counts (BB%, K%, ISO)."""
        s = await self._get_season_stat_block(session, pid, group)
        if not s:
            return []

        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        pairs = []
        if group == "hitting":
            pa = fnum(s.get("plateAppearances")) or 0
            avg, slg = fnum(s.get("avg")), fnum(s.get("slg"))
            pairs = [
                ("G", s.get("gamesPlayed")), ("PA", s.get("plateAppearances")),
                ("AVG", s.get("avg")), ("OBP", s.get("obp")),
                ("SLG", s.get("slg")), ("OPS", s.get("ops")),
                ("HR", s.get("homeRuns")), ("RBI", s.get("rbi")),
                ("R", s.get("runs")), ("SB", s.get("stolenBases")),
                ("BB%", f"{fnum(s.get('baseOnBalls')) / pa:.1%}" if pa else None),
                ("K%", f"{fnum(s.get('strikeOuts')) / pa:.1%}" if pa else None),
                ("ISO", f"{slg - avg:.3f}".lstrip("0")
                 if (slg is not None and avg is not None) else None),
                ("BABIP", s.get("babip")),
            ]
        else:
            pairs = [
                ("W-L", f"{s.get('wins', 0)}-{s.get('losses', 0)}"),
                ("ERA", s.get("era")), ("G", s.get("gamesPlayed")),
                ("GS", s.get("gamesStarted")), ("IP", s.get("inningsPitched")),
                ("SO", s.get("strikeOuts")), ("WHIP", s.get("whip")),
                ("K/9", s.get("strikeoutsPer9Inn")),
                ("BB/9", s.get("walksPer9Inn")),
                ("HR/9", s.get("homeRunsPer9")),
                ("AVGa", s.get("avg")),
            ]
        # Savant expected stats + luck delta (actual wOBA minus xwOBA;
        # negative = underperforming the contact quality)
        xs = await self.get_expected_stats(session, pid, group)
        if xs:
            fmt3 = lambda v: (f"{v:.3f}".lstrip("0") if v is not None else None)
            pairs += [("xBA", fmt3(xs["xba"])), ("xSLG", fmt3(xs["xslg"])),
                      ("xwOBA", fmt3(xs["xwoba"]))]
            if xs["luck"] is not None:
                pairs.append(("Luck", f"{xs['luck']:+.3f}".replace("0.", ".")))

        return [(k, str(v)) for k, v in pairs if v not in (None, "", "None")]

    async def get_hand_splits(self, session: aiohttp.ClientSession,
                              pid: int) -> dict:
        """Batter's season splits vs LHP/RHP: {'vl': stat, 'vr': stat}."""
        if pid not in self._hand_splits:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{pid}/stats",
                {"stats": "statSplits", "sitCodes": "vl,vr",
                 "group": "hitting", "season": str(self.season)})
            out = {}
            try:
                for split in data["stats"][0]["splits"]:
                    code = (split.get("split") or {}).get("code")
                    if code in ("vl", "vr"):
                        out[code] = split.get("stat", {})
            except (TypeError, KeyError, IndexError):
                pass
            self._hand_splits[pid] = out
        return self._hand_splits[pid]

    async def get_bvp(self, session: aiohttp.ClientSession, batter_id: int,
                      pitcher_id: int) -> Optional[dict]:
        """Batter's CAREER line vs one pitcher (vsPlayerTotal)."""
        key = (batter_id, pitcher_id)
        if key not in self._bvp:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{batter_id}/stats",
                {"stats": "vsPlayerTotal", "opposingPlayerId": str(pitcher_id),
                 "group": "hitting"})
            stat = None
            try:
                stat = data["stats"][0]["splits"][0]["stat"]
            except (TypeError, KeyError, IndexError):
                pass
            self._bvp[key] = stat
        return self._bvp[key]

    async def get_expected_stats(self, session: aiohttp.ClientSession,
                                 player_id: int, group: str) -> Optional[dict]:
        """Savant expected-statistics board row for one player: actual xBA/
        xSLG/xwOBA plus the luck delta (woba - est_woba). One CSV fetch per
        player type per hour covers the whole league."""
        ptype = "pitcher" if group == "pitching" else "batter"
        cached = self._xstats.get(ptype)
        if not cached or time.time() - cached[0] >= PITCH_SPLITS_TTL:
            url = SAVANT_XSTATS_URL.format(player_type=ptype, year=self.season)
            board: Dict[int, dict] = {}
            async with self._sem:
                try:
                    async with session.get(url, headers=SAVANT_HEADERS,
                                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            # BOM corrupts the quoted first column and shifts
                            # every field — strip it before csv parsing
                            text = (await resp.text()).lstrip("﻿")
                            import io as _io
                            for row in csv.DictReader(_io.StringIO(text)):
                                try:
                                    board[int(row["player_id"])] = row
                                except (KeyError, ValueError):
                                    continue
                except Exception as e:
                    print(f"PropWindowUtils: xstats fetch failed: {e}")
            self._xstats[ptype] = (time.time(), board)
        row = self._xstats[ptype][1].get(player_id)
        if not row:
            return None

        def fnum(k):
            try:
                return float(row[k])
            except (KeyError, TypeError, ValueError):
                return None

        return {"xba": fnum("est_ba"), "xslg": fnum("est_slg"),
                "xwoba": fnum("est_woba"),
                "luck": fnum("est_woba_minus_woba_diff")}

    async def get_bullpen_usage(self, session: aiohttp.ClientSession,
                                team_abbr: str) -> List[dict]:
        """Opposing-pen fatigue from insidethepen bullpen-usage: per reliever
        {name, era, np_by_day: [(date_label, pitches)...] newest first,
        np_yday, np_l3, l7, appearances_l3, status}. One page fetch (cached
        30 min) covers all 30 teams."""
        if not self._bullpen or time.time() - self._bullpen[0] >= 1800:
            teams: Dict[str, List[dict]] = {}
            html = ""
            async with self._sem:
                try:
                    async with session.get(
                            BULLPEN_USAGE_URL, headers=SAVANT_HEADERS,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            html = await resp.text()
                except Exception as e:
                    print(f"PropWindowUtils: bullpen fetch failed: {e}")
            if html:
                teams = self._parse_bullpen_page(html)
            self._bullpen = (time.time(), teams)
        return self._bullpen[1].get(team_abbr, [])

    def _parse_bullpen_page(self, html: str) -> Dict[str, List[dict]]:
        from bs4 import BeautifulSoup
        import re as _re
        soup = BeautifulSoup(html, "lxml")
        name_map = getattr(self, "_team_name_to_abbr", {})
        out: Dict[str, List[dict]] = {}
        for h5 in soup.find_all("h5"):
            text = h5.get_text(strip=True)
            if not text.endswith("Bullpen Usage"):
                continue
            team_name = text[: -len("Bullpen Usage")].strip()
            abbr = name_map.get(team_name)
            if not abbr:
                continue
            table = h5.find_next("table")
            if table is None:
                continue
            head_row = table.find("tr")
            headers = [th.get_text(" ", strip=True)
                       for th in head_row.find_all(["th", "td"])]
            day_labels = headers[4:]      # Player, IP, NP-S, ERA, then days
            rows = []
            for tr in table.find_all("tr")[1:]:
                # Player name is a <th scope=row>; stat cells are <td>s
                name_th = tr.find("th")
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if name_th is None or len(tds) < 4:
                    continue
                name = name_th.get_text(" ", strip=True)
                if not name:
                    continue
                # href carries the MLBAM id: /pitcher/Paul-Sewald-623149.html
                pid = None
                href = None
                a = name_th.find("a")
                if a and a.get("href"):
                    href = a["href"]
                    m = _re.search(r"-(\d+)\.html", href)
                    if m:
                        pid = int(m.group(1))
                era = tds[2]
                np_by_day = []
                for label, cellval in zip(day_labels, tds[3:]):
                    m = _re.search(r"(\d+)-\d+\s*$", cellval)
                    np_by_day.append((label, int(m.group(1)) if m else 0))
                nps = [n for _, n in np_by_day]
                np_yday = nps[0] if nps else 0
                l3 = sum(nps[:3])
                appearances_l3 = sum(1 for n in nps[:3] if n > 0)
                if np_yday >= 25 or (len(nps) > 1 and np_yday and nps[1]):
                    status = "TAXED"
                elif np_yday:
                    status = "USED YDAY"
                elif l3 == 0:
                    status = "FRESH"
                else:
                    status = ""
                rows.append({
                    "name": name, "era": era, "pid": pid, "href": href,
                    "np_by_day": np_by_day, "np_yday": np_yday,
                    "np_l3": l3, "np_l7": sum(nps),
                    "appearances_l3": appearances_l3, "status": status,
                })
            # Heaviest recent usage first
            rows.sort(key=lambda r: (-r["np_l3"], -r["np_l7"]))
            out[abbr] = rows
        return out

    async def get_fg_pitching(self, pid: int) -> Optional[dict]:
        """FanGraphs pitching row for one MLBAM id: Stuff+/Location+/
        Pitching+ overall and per-pitch, plus FIP/SIERA/gmLI. First call
        launches the headless-Firefox API fetch in an executor (~10s, then
        disk-cached 6h); later calls are instant."""
        async with self._fg_lock:
            if self._fg_pitching is None:
                loop = asyncio.get_running_loop()
                rows = await loop.run_in_executor(
                    None, fetch_fg_leaders_sync, "pit", "1", self.season)
                board = {}
                for row in rows:
                    mlbam = row.get("xMLBAMID")
                    if mlbam:
                        board[int(mlbam)] = row
                self._fg_pitching = board
                print(f"PropWindowUtils: FG pitching board loaded "
                      f"({len(board)} pitchers)")
        row = self._fg_pitching.get(pid)
        if not row:
            return None
        per_pitch = {}
        for code in ("FF", "SI", "FC", "SL", "CU", "KC", "CH", "FS", "FO"):
            v = row.get(f"sp_s_{code}")
            if v is not None:
                per_pitch[code] = v
        return {
            "stuff": row.get("sp_stuff"), "location": row.get("sp_location"),
            "pitching": row.get("sp_pitching"), "per_pitch": per_pitch,
            "fip": row.get("FIP"), "xfip": row.get("xFIP"),
            "siera": row.get("SIERA"), "war": row.get("WAR"),
            "gmli": row.get("gmLI"), "csw": row.get("C+SwStr%"),
            "sv": row.get("SV"), "hld": row.get("HLD"),
            "throws": row.get("Throws"), "kbb": row.get("K-BB%"),
        }

    async def get_fg_batting(self, pid: int) -> Optional[dict]:
        """FanGraphs batting row for one MLBAM id: value stats (wOBA, wRC+,
        WAR) plus the full swing-tracking suite (bat speed, attack angle/
        direction, ideal-angle rate, squared-up, blast, swing length).
        Same headless-browser transport as the pitching board, cached 6h."""
        async with self._fg_lock:
            if self._fg_batting is None:
                loop = asyncio.get_running_loop()
                rows = await loop.run_in_executor(
                    None, fetch_fg_leaders_sync, "bat", "1", self.season)
                board = {}
                for row in rows:
                    mlbam = row.get("xMLBAMID")
                    if mlbam:
                        board[int(mlbam)] = row
                self._fg_batting = board
                print(f"PropWindowUtils: FG batting board loaded "
                      f"({len(board)} batters)")
        row = self._fg_batting.get(pid)
        if not row:
            return None
        return {
            "woba": row.get("wOBA"), "wrcplus": row.get("wRC+"),
            "war": row.get("WAR"),
            "bat_speed": row.get("AvgBatSpeed"),
            "attack_angle": row.get("AttackAngle"),
            "attack_dir": row.get("AttackDirection"),
            "ideal_aa": row.get("IdealAttackAngle%"),
            "fast_swing": row.get("FastSwing%"),
            "squared_up": row.get("SquaredUpContact%"),
            "blast": row.get("BlastContact%"),
            "swing_length": row.get("SwingLength"),
            "swords": row.get("Swords"),
        }

    async def get_spray_points(self, session: aiohttp.ClientSession,
                               player_id: int,
                               player_type: str = "batter") -> List[tuple]:
        """Season spray points from the cached pitch detail:
        [(x_ft, y_ft, category), ...] where +x = toward RF, +y = toward CF
        and category in HR/XBH/1B/OUT. For pitchers: contact allowed."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        points = []
        for r in rows:
            hx, hy = r.get("hc_x"), r.get("hc_y")
            if hx is None or hy is None or r["desc"] != "hit_into_play":
                continue
            x = (hx - 125.42) * 2.51
            y = (198.27 - hy) * 2.51
            ev = r.get("event") or ""
            if ev == "home_run":
                cat = "HR"
            elif ev in ("double", "triple"):
                cat = "XBH"
            elif ev == "single":
                cat = "1B"
            else:
                cat = "OUT"
            points.append((x, y, cat))
        return points

    def _park_factor_for(self, venue: str) -> Optional[dict]:
        if self._park_factors is None:
            self._park_factors = load_park_factors()
        return self._park_factors.get(venue)

    def _fetch_weather_sync(self, venue: str) -> Optional[dict]:
        """Stadium weather via weatherman (OpenWeather). Sync — run in an
        executor. Returns a small display dict or None."""
        try:
            from weatherman import STADIUM_DATA, WeatherService
            park = STADIUM_DATA.get(venue)
            if not park:
                return None
            svc = WeatherService()
            raw = svc.get_weather_by_location(park["lat"], park["lon"])
            w = svc.extract_weather_data(raw)
            return {
                "temperature": w.get("temperature"),
                "wind_speed": w.get("wind_speed"),
                "wind_dir_compass": _compass(w.get("wind_direction")),
                "condition": w.get("condition"),
            }
        except Exception as e:
            print(f"PropWindowUtils: weather unavailable for {venue}: {e}")
            return None

    async def _get_weather(self, session_unused, venue: str) -> Optional[dict]:
        cached = self._weather.get(venue)
        if cached and time.time() - cached[0] < WEATHER_TTL:
            return cached[1]
        loop = asyncio.get_running_loop()
        w = await loop.run_in_executor(None, self._fetch_weather_sync, venue)
        self._weather[venue] = (time.time(), w)
        return w

    # -------------------------------------------------- pitch-level splits

    async def _get_pitch_detail(self, session: aiohttp.ClientSession,
                                player_id: int, player_type: str) -> List[dict]:
        """Raw pitch-by-pitch rows (needed fields only) from the player's
        full-season Savant detail — one ~1-3s CSV fetch, cached 1h. Both the
        by-pitch and by-velocity aggregations run off this cache."""
        key = (player_id, player_type)
        cached = self._pitch_splits.get(key)
        if cached and time.time() - cached[0] < PITCH_SPLITS_TTL:
            return cached[1]

        url = SAVANT_SEARCH_URL.format(year=self.season, player_id=player_id,
                                       player_type=player_type)
        text = ""
        async with self._sem:
            try:
                async with session.get(url, headers=SAVANT_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                    else:
                        print(f"PropWindowUtils: savant search {resp.status} "
                              f"for player {player_id}")
            except Exception as e:
                print(f"PropWindowUtils: savant search failed: {e}")

        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        rows = []
        if text.strip():
            import io as _io
            for row in csv.DictReader(_io.StringIO(text)):
                pitch = row.get("pitch_name") or ""
                if not pitch:
                    continue
                rows.append({
                    "pitch": pitch,
                    "velo": fnum(row.get("release_speed")),
                    "desc": row.get("description") or "",
                    "ev": fnum(row.get("launch_speed")),
                    "la": fnum(row.get("launch_angle")),
                    "hr": (row.get("events") or "") == "home_run",
                    "xwoba": fnum(row.get("estimated_woba_using_speedangle")),
                    "event": row.get("events") or "",
                    "hc_x": fnum(row.get("hc_x")),
                    "hc_y": fnum(row.get("hc_y")),
                })
        self._pitch_splits[key] = (time.time(), rows)
        return rows

    async def get_pitch_splits(self, session: aiohttp.ClientSession,
                               player_id: int,
                               player_type: str = "batter") -> List[PitchSplit]:
        """Per-pitch-type splits, sorted by pitch count."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        splits = self._aggregate_splits(rows, lambda r: r["pitch"])
        splits.sort(key=lambda s: s.count, reverse=True)
        return splits

    async def get_velo_splits(self, session: aiohttp.ClientSession,
                              player_id: int,
                              player_type: str = "batter") -> List[PitchSplit]:
        """Performance by velocity band within pitch family (FB/Brk/Off) —
        e.g. a hitter who handles fastballs ≤96 but struggles at 97+."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        splits = self._aggregate_splits(
            rows, lambda r: _velo_band(r["pitch"], r["velo"]))
        order = {band: i for i, band in enumerate(VELO_BAND_ORDER)}
        splits.sort(key=lambda s: order.get(s.pitch, 99))
        return splits

    @staticmethod
    def _aggregate_splits(rows: List[dict], label_fn) -> List[PitchSplit]:
        buckets: Dict[str, dict] = {}
        for row in rows:
            label = label_fn(row)
            if not label:
                continue
            b = buckets.setdefault(label, {
                "count": 0, "velo": [], "swings": 0, "whiffs": 0,
                "ev": [], "hard": 0, "barrel": 0, "bbe": 0, "hr": 0,
                "xwoba": [],
            })
            b["count"] += 1
            if row["velo"] is not None:
                b["velo"].append(row["velo"])
            desc = row["desc"]
            if desc in _SWING_DESCS:
                b["swings"] += 1
                if desc in _WHIFF_DESCS:
                    b["whiffs"] += 1
            if desc == "hit_into_play":
                b["bbe"] += 1
                ev, la = row["ev"], row["la"]
                if ev is not None:
                    b["ev"].append(ev)
                    if ev >= 95:
                        b["hard"] += 1
                    if la is not None and _is_barrel(ev, la):
                        b["barrel"] += 1
                if row["hr"]:
                    b["hr"] += 1
                if row["xwoba"] is not None:
                    b["xwoba"].append(row["xwoba"])

        splits = []
        for label, b in buckets.items():
            splits.append(PitchSplit(
                pitch=label,
                count=b["count"],
                velo=_avg(b["velo"]) if b["velo"] else None,
                whiff_pct=(b["whiffs"] / b["swings"]) if b["swings"] else None,
                bbe=b["bbe"],
                avg_ev=_avg(b["ev"]) if b["ev"] else None,
                hardhit_pct=(b["hard"] / len(b["ev"])) if b["ev"] else None,
                barrel_pct=(b["barrel"] / len(b["ev"])) if b["ev"] else None,
                hr=b["hr"],
                xwobacon=_avg(b["xwoba"]) if b["xwoba"] else None,
            ))
        return splits

    async def get_bullpen_report(self, session: aiohttp.ClientSession,
                                 team_abbr: str) -> List[dict]:
        """Full bullpen intelligence: insidethepen fatigue rows enriched with
        StatsAPI game-log usage traits (back-to-back behavior, 30+ pitch
        outings, avg pitches, games finished) and the FanGraphs board
        (Stuff+/Loc+, gmLI leverage, SIERA, K-BB%, SV/HLD, throws). Role is
        inferred from saves/holds/leverage. Pitched-yesterday relievers who
        never work back-to-back get flagged UNAVAIL."""
        rows = await self.get_bullpen_usage(session, team_abbr)

        async def enrich(rec):
            out = dict(rec)
            pid = rec.get("pid")
            if not pid:
                return out
            try:
                splits = await self.get_game_log(session, pid, "pitching")
            except Exception:
                splits = None
            if splits:
                dates = []
                for s in splits:
                    try:
                        dates.append(datetime.fromisoformat(s.get("date", "")).date())
                    except ValueError:
                        pass
                out["outings"] = len(splits)
                out["b2b_count"] = sum(
                    1 for a, b in zip(dates, dates[1:]) if (b - a).days == 1)
                nps = [(s.get("stat", {}).get("numberOfPitches") or 0)
                       for s in splits]
                out["thirty_plus"] = sum(1 for n in nps if n >= 30)
                real_nps = [n for n in nps if n]
                out["avg_np"] = _avg(real_nps) if real_nps else None
                out["gf"] = sum((s.get("stat", {}).get("gamesFinished") or 0)
                                for s in splits)
            try:
                fg = await self.get_fg_pitching(pid)
            except Exception:
                fg = None
            if fg:
                out["fg"] = fg

            # insidethepen player page: site role, snapshot, gated traits
            cached = self._itp_pages.get(pid)
            if cached and time.time() - cached[0] < FG_CACHE_TTL:
                page = cached[1]
            else:
                loop = asyncio.get_running_loop()
                page = await loop.run_in_executor(
                    None, fetch_reliever_page_sync, pid, rec.get("href"))
                self._itp_pages[pid] = (time.time(), page)
            out.update(page)
            traits = page.get("traits") or {}
            out["b2b_site"] = traits.get("back to back days")
            out["avg_inning"] = traits.get("Avg Inning when called")
            out["avg_diff"] = traits.get("Avg Run Diff when called")
            out["over30"] = traits.get("over 30 pitches")
            out["pre8"] = traits.get("before the 8th")
            vs_l = (traits.get("versus LH batters") or "")[:1]
            vs_r = (traits.get("versus RH batters") or "")[:1]
            if vs_l or vs_r:
                # Y/Y -> B; one-sided or "Rarely" marked with lowercase
                if vs_l == "Y" and vs_r == "Y":
                    out["vs"] = "B"
                elif vs_r == "Y":
                    out["vs"] = "R" + ("ˡ" if vs_l == "R" else "")
                elif vs_l == "Y":
                    out["vs"] = "L" + ("ʳ" if vs_r == "R" else "")
                else:
                    out["vs"] = f"{vs_l}/{vs_r}"

            # Availability: site trait is authoritative when present; else
            # fall back to demonstrated season behavior from game logs
            if out.get("np_yday"):
                if out.get("b2b_site") == "No":
                    out["status"] = "UNAVAIL"
                elif out.get("b2b_site") == "Rarely":
                    out["status"] = "DOUBTFUL"
                elif (out.get("outings", 0) >= 10
                        and out.get("b2b_count", 99) == 0):
                    out["status"] = "UNAVAIL"

            # Role: site's label first, else saves/holds/leverage
            role_site = (out.get("role_site") or "").lower()
            if "closer" in role_site:
                out["role"] = "CL"
            elif "setup" in role_site:
                out["role"] = "SU"
            elif "long" in role_site:
                out["role"] = "LG"
            elif "middle" in role_site:
                out["role"] = "MID"
            else:
                sv = (fg or {}).get("sv") or 0
                hld = (fg or {}).get("hld") or 0
                gmli = (fg or {}).get("gmli") or 0
                if sv >= 8:
                    out["role"] = "CL"
                elif hld >= 8 or gmli >= 1.3:
                    out["role"] = "SU"
                elif gmli and gmli <= 0.7:
                    out["role"] = "LOW"
                elif fg:
                    out["role"] = "MID"
                else:
                    out["role"] = ""
            return out

        return list(await asyncio.gather(*(enrich(r) for r in rows)))

    async def _fetch_savant_csv(self, session: aiohttp.ClientSession,
                                url: str) -> List[dict]:
        """Shared Savant CSV fetch (BOM-stripped)."""
        async with self._sem:
            try:
                async with session.get(url, headers=SAVANT_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        print(f"PropWindowUtils: savant csv {resp.status} {url[:80]}")
                        return []
                    text = (await resp.text()).lstrip("﻿")
            except Exception as e:
                print(f"PropWindowUtils: savant csv failed: {e}")
                return []
        import io as _io
        return list(csv.DictReader(_io.StringIO(text)))

    async def get_sp_deep_card(self, session: aiohttp.ClientSession,
                               pid: int) -> Optional[dict]:
        """Full arsenal card for one pitcher: per-pitch usage, velo, spin,
        Stuff+, run value and results-against (Savant pitch-arsenal-stats +
        pitch-arsenals + FG board), plus arm angle. Three league-wide CSVs,
        each fetched once per hour."""
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        now = time.time()
        if not self._arsenal_stats or now - self._arsenal_stats[0] >= PITCH_SPLITS_TTL:
            board = {}
            for row in await self._fetch_savant_csv(
                    session, SAVANT_ARSENAL_STATS_URL.format(year=self.season)):
                try:
                    board[(int(row["player_id"]), row["pitch_type"])] = row
                except (KeyError, ValueError):
                    continue
            self._arsenal_stats = (now, board)
        if not self._arm_angles or now - self._arm_angles[0] >= PITCH_SPLITS_TTL:
            angles = {}
            for row in await self._fetch_savant_csv(
                    session, SAVANT_ARM_ANGLES_URL.format(year=self.season)):
                try:
                    angles[int(row["pitcher"])] = fnum(row["ball_angle"])
                except (KeyError, ValueError):
                    continue
            self._arm_angles = (now, angles)
        if not self._arsenal_phys or now - self._arsenal_phys[0] >= PITCH_SPLITS_TTL:
            phys: Dict[int, dict] = {}
            for kind, key in (("avg_speed", "velo"), ("avg_spin", "spin")):
                for row in await self._fetch_savant_csv(
                        session, SAVANT_ARSENALS_URL.format(year=self.season,
                                                            kind=kind)):
                    try:
                        p = int(row["pitcher"])
                    except (KeyError, ValueError):
                        continue
                    rec = phys.setdefault(p, {})
                    for col, v in row.items():
                        if col.endswith(f"_{kind}") and v:
                            pt = col.split("_")[0].upper()
                            rec.setdefault(pt, {})[key] = fnum(v)
            self._arsenal_phys = (now, phys)

        stats = self._arsenal_stats[1]
        pitches = [row for (p, _pt), row in stats.items() if p == pid]
        if not pitches:
            return None
        try:
            fg = await self.get_fg_pitching(pid)
        except Exception:
            fg = None
        phys = self._arsenal_phys[1].get(pid, {})

        rows = []
        per_pitch = (fg or {}).get("per_pitch") or {}
        # Savant/FG pitch-code mismatches: sweepers under SL, curve vs
        # knuckle-curve labels, splitter vs forkball
        fg_alias = {"ST": "SL", "CU": "KC", "KC": "CU", "FS": "FO", "FO": "FS"}
        for row in pitches:
            pt = row["pitch_type"]
            rows.append({
                "pitch_type": pt,
                "pitch": row.get("pitch_name", pt),
                "usage": fnum(row.get("pitch_usage")),
                "velo": (phys.get(pt) or {}).get("velo"),
                "spin": (phys.get(pt) or {}).get("spin"),
                "stuff": per_pitch.get(pt) or per_pitch.get(fg_alias.get(pt, "")),
                "rv100": fnum(row.get("run_value_per_100")),
                "woba": fnum(row.get("woba")),
                "xwoba": fnum(row.get("est_woba")),
                "whiff": fnum(row.get("whiff_percent")),
                "k": fnum(row.get("k_percent")),
                "slg": fnum(row.get("slg")),
                "hh": fnum(row.get("hard_hit_percent")),
                "pa": fnum(row.get("pa")),
            })
        rows.sort(key=lambda r: -(r["usage"] or 0))
        return {
            "pid": pid,
            "arm_angle": self._arm_angles[1].get(pid),
            "rows": rows,
            "fg": fg,
        }

    async def get_pitch_arsenal(self, session: aiohttp.ClientSession,
                                pid: int) -> Dict[str, dict]:
        """A pitcher's arsenal from StatsAPI pitchArsenal (instant JSON):
        {savant_pitch_name: {'usage': 0.23, 'speed': 92.2}}. Pitches under
        2% usage are dropped as noise."""
        if pid not in self._arsenals:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{pid}/stats",
                {"stats": "pitchArsenal", "season": str(self.season)})
            arsenal = {}
            try:
                for split in data["stats"][0]["splits"]:
                    st = split.get("stat", {})
                    desc = (st.get("type") or {}).get("description", "")
                    usage = st.get("percentage")
                    if not desc or usage is None or usage < 0.02:
                        continue
                    name = _ARSENAL_NAME_MAP.get(desc, desc)
                    arsenal[name] = {"usage": usage,
                                     "speed": st.get("averageSpeed")}
            except (TypeError, KeyError, IndexError):
                pass
            self._arsenals[pid] = arsenal
        return self._arsenals[pid]

    async def get_matchup(self, session: aiohttp.ClientSession, team_abbr: str,
                          include_opp_batting: bool = False,
                          batter_id: Optional[int] = None,
                          pitcher_id: Optional[int] = None) -> Optional[MatchupContext]:
        """Today's game context for a team abbreviation. Returns None when the
        team has no game today. include_opp_batting adds the opposing team's
        season batting line (for pitcher props). batter_id (batter props) adds
        the batter's split vs the SP's hand and their career BvP line."""
        if not await self.ensure_roster(session):
            return None
        abbr_to_id = {abbr: tid for tid, abbr in self._teams.items()}
        team_id = abbr_to_id.get(team_abbr)
        if team_id is None:
            return None

        games = await self._get_schedule(session)
        game = None
        for g in games:
            teams = g.get("teams", {})
            ids = {(teams.get(side, {}).get("team") or {}).get("id"): side
                   for side in ("home", "away")}
            if team_id in ids:
                game = g
                break
        if game is None:
            return None

        teams = game["teams"]
        is_home = (teams["home"]["team"].get("id") == team_id)
        own_side = "home" if is_home else "away"
        opp_side = "away" if is_home else "home"
        opp_team = teams[opp_side]["team"]
        venue = (game.get("venue") or {}).get("name", "?")

        try:
            gt = datetime.fromisoformat(
                game.get("gameDate", "").replace("Z", "+00:00")).astimezone()
            game_time = gt.strftime("%-I:%M %p")
        except ValueError:
            game_time = ""

        ctx = MatchupContext(
            team=team_abbr,
            opponent=self._teams.get(opp_team.get("id"), "?"),
            is_home=is_home,
            venue=venue,
            game_time=game_time,
        )

        pf = self._park_factor_for(venue)
        if pf:
            ctx.park_factor = pf["park_factor"]
            ctx.park_hr_factor = pf["hr"]

        prob = teams[opp_side].get("probablePitcher")
        if prob:
            ctx.opp_pitcher_id = prob.get("id")
            ctx.opp_pitcher_name = prob.get("fullName")
            person = await self._get_person(session, ctx.opp_pitcher_id)
            ctx.opp_pitcher_hand = (person.get("pitchHand") or {}).get("code")
            ctx.opp_pitcher_stats = await self._get_season_pitching(
                session, ctx.opp_pitcher_id)

            if batter_id:
                if ctx.opp_pitcher_hand in ("L", "R"):
                    code = "vl" if ctx.opp_pitcher_hand == "L" else "vr"
                    hs = (await self.get_hand_splits(session, batter_id)).get(code)
                    if hs:
                        ctx.batter_vs_hand = (
                            f"vs {ctx.opp_pitcher_hand}HP: "
                            f"{hs.get('avg')}/{hs.get('obp')}/{hs.get('slg')} "
                            f"· {hs.get('homeRuns')} HR "
                            f"({hs.get('plateAppearances')} PA)")
                bvp = await self.get_bvp(session, batter_id, ctx.opp_pitcher_id)
                if bvp and bvp.get("atBats"):
                    ctx.bvp = (f"BvP {bvp.get('hits')}-{bvp.get('atBats')}"
                               f" · {bvp.get('homeRuns')} HR"
                               f" · {bvp.get('strikeOuts')} K")

        if include_opp_batting and opp_team.get("id"):
            ctx.opp_team_batting = await self._get_team_batting(
                session, opp_team["id"])

        # Lineup confirmation / probable status
        maps = await self.get_lineup_maps(session)
        m = maps.get(team_abbr)
        if m:
            if batter_id:
                ctx.lineup_posted = m["posted"]
                if m["posted"]:
                    ctx.lineup_slot = m["slots"].get(batter_id)
            if pitcher_id:
                ctx.probable_sp = (m["probable"] == pitcher_id)

        ctx.weather = await self._get_weather(session, venue)
        return ctx


# ===========================================================================
# 2. PLAYER DETAIL PANEL (Qt)
# ===========================================================================

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QUrl, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap, QPainter, QPainterPath
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QGridLayout, QComboBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QScrollArea,
)

_STATS_TABLE_QSS = """
    QTableWidget {
        background-color: #151a21;
        alternate-background-color: #1a2029;
        color: #D5DBDB;
        font-size: 8pt;
        gridline-color: #2C3E50;
        border: 1px solid #34495E;
    }
    QTableWidget::item { padding: 0px 2px; }
    QHeaderView::section {
        background-color: #1E2A38;
        color: #95A5A6;
        font-size: 8pt;
        padding: 1px 2px;
        border: none;
    }
"""

from player_overlay_widget import PercentileBar, percentile_colour
from PyQt6.QtGui import QBrush, QLinearGradient


class CompactPercentileBar(PercentileBar):
    """Denser PercentileBar: 15px tall, short label, tighter number gutter —
    fits a 3-column grid under the chart."""

    LABEL_W = 52
    NUM_W = 22

    def __init__(self, label: str, percentile: float = 50.0, parent=None):
        super().__init__(label, percentile, parent)
        self.setFixedHeight(15)
        self.setMinimumWidth(140)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_x = self.LABEL_W + 3
        bar_w = w - bar_x - self.NUM_W - 4
        bar_h = 6
        bar_y = (h - bar_h) // 2

        p.setPen(QColor(170, 175, 180))
        p.setFont(QFont("Segoe UI", 7))
        p.drawText(0, 0, self.LABEL_W, h,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.label)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(50, 50, 55)))
        p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 3, 3)

        fill_w = max(3, int(bar_w * self.percentile / 100))
        colour = percentile_colour(self.percentile)
        grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
        grad.setColorAt(0, colour.darker(140))
        grad.setColorAt(1, colour)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(bar_x, bar_y, fill_w, bar_h, 3, 3)

        p.setPen(QColor(210, 210, 210))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(bar_x + bar_w + 3, 0, self.NUM_W, h,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"{self.percentile:.0f}")

HEADSHOT_DIR = Path(__file__).resolve().parent / "headshots"
HEADSHOT_URL = ("https://img.mlbstatic.com/mlb-photos/image/upload/"
                "w_120,q_auto:best/v1/people/{pid}/headshot/67/current")

# Stat-switcher options: every non-Yes/No market, split by player type.
# (Yes/No variants duplicate a counting stat at an implicit 0.5 line.)
BATTER_STAT_OPTIONS = [(k, ms.display) for k, ms in MARKET_STATS.items()
                       if k.startswith("batter") and not ms.yes_no]
PITCHER_STAT_OPTIONS = [(k, ms.display) for k, ms in MARKET_STATS.items()
                        if k.startswith("pitcher") and not ms.yes_no]

# Percentile columns to show, per player type: (df column, short bar label)
HITTER_PCT_COLS = [
    ("xwoba", "xwOBA"), ("xslg", "xSLG"), ("xiso", "xISO"),
    ("brl_percent", "Brl%"), ("hard_hit_percent", "HH%"),
    ("exit_velocity", "EV"), ("bat_speed", "BatSpd"),
    ("k_percent", "K%"), ("bb_percent", "BB%"),
    ("chase_percent", "Chase"), ("sprint_speed", "Sprint"),
    ("max_ev", "MaxEV"),
]
PITCHER_PCT_COLS = [
    ("xwoba", "xwOBA"), ("xera", "xERA"), ("k_percent", "K%"),
    ("bb_percent", "BB%"), ("whiff_percent", "Whiff"),
    ("chase_percent", "Chase"), ("brl_percent", "Brl%"),
    ("hard_hit_percent", "HH%"), ("fb_velocity", "FB Vel"),
    ("fb_spin", "FB Spin"), ("exit_velocity", "EV"), ("xslg", "xSLG"),
]

# Display abbreviations for pitch names (full name kept as tooltip)
PITCH_ABBREV = {
    "4-Seam Fastball": "FF", "2-Seam Fastball": "FT", "Fastball": "FA",
    "Sinker": "SI", "Cutter": "FC", "Slider": "SL", "Sweeper": "ST",
    "Curveball": "CU", "Knuckle Curve": "KC", "Changeup": "CH",
    "Split-Finger": "FS", "Forkball": "FO", "Slurve": "SV",
    "Slow Curve": "CS", "Knuckleball": "KN", "Eephus": "EP",
    "Screwball": "SC",
}

CHART_GAMES = 15
COLOR_OVER = (46, 204, 113)      # green — cleared the line
COLOR_UNDER = (192, 57, 43)      # red — missed
COLOR_LINE = (241, 196, 15)      # prop line


class PlayerDetailPanel(QWidget):
    """Side-tab panel: one player's granular stats vs their prop line."""

    # Emitted when the user switches stat or line for the shown player:
    # (market_key, line). The window re-summarizes and calls show_summary.
    stat_requested = pyqtSignal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct_hitters = None      # pandas DataFrames, set lazily
        self._pct_pitchers = None
        self._summary: Optional[PropStatSummary] = None
        self._updating = False        # guard: programmatic control updates
        self._splits = None
        self._velo_splits = None
        self._splits_type = None
        self._arsenal = None
        self._arsenal_pitcher = None
        self._arsenal_stuff = None
        self._sp_card = None
        self._sp_card_name = ""
        self._sp_card_hand = None
        self._spray_points = None
        self._last_ctx = None
        self._nam = QNetworkAccessManager(self)
        self._nam.finished.connect(self._on_headshot_reply)
        self._pending_pid: Optional[int] = None
        HEADSHOT_DIR.mkdir(exist_ok=True)
        self._build_ui()

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        self.setStyleSheet("""
            #statBadge {
                background-color: #1E2A38;
                border: 1px solid #34495E;
                border-radius: 5px;
            }
            #badgeTitle { color: #7F8C8D; font-size: 6pt; }
            #badgeValue { color: white; font-size: 9pt; font-weight: bold; }
            #playerName { color: white; font-size: 13pt; font-weight: bold; }
            #playerMeta { color: #95A5A6; font-size: 9pt; }
            #marketLabel { color: #dc9437; font-size: 9pt; font-weight: bold; }
            #placeholder { color: #7F8C8D; font-size: 11pt; }
            #pctMissing { color: #7F8C8D; font-size: 9pt; }
            #matchupStrip {
                background-color: #1E2A38;
                border: 1px solid #34495E;
                border-radius: 5px;
            }
            #matchupLine { color: #BDC3C7; font-size: 9pt; }
            #swingLine { color: #82C4E0; font-size: 9pt; }
            #matchupPitcher { color: #E67E22; font-size: 9pt; font-weight: bold; }
            #splitsTitle { color: #95A5A6; font-size: 9pt; font-weight: bold; }
            #tradLabel { color: #7F8C8D; font-size: 7pt; }
            #tradValue { color: #ECF0F1; font-size: 9pt; font-weight: bold; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 2)
        root.setSpacing(6)

        self._placeholder = QLabel("Click a prop row to load player detail")
        self._placeholder.setObjectName("placeholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._placeholder)

        self._content = QWidget()
        self._content.hide()
        root.addWidget(self._content, stretch=1)

        content_lay = QVBoxLayout(self._content)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(6)

        # -- header: headshot + identity + stat/line controls, badges right
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        self._headshot = QLabel()
        self._headshot.setFixedSize(56, 56)
        self._headshot.setScaledContents(True)
        head_row.addWidget(self._headshot)

        ident = QVBoxLayout()
        ident.setSpacing(0)
        name_meta = QHBoxLayout()
        name_meta.setSpacing(8)
        self._name_label = QLabel()
        self._name_label.setObjectName("playerName")
        self._meta_label = QLabel()
        self._meta_label.setObjectName("playerMeta")
        name_meta.addWidget(self._name_label)
        name_meta.addWidget(self._meta_label,
                            alignment=Qt.AlignmentFlag.AlignBottom)
        name_meta.addStretch()
        ident.addLayout(name_meta)
        self._market_label = QLabel()
        self._market_label.setObjectName("marketLabel")
        ident.addWidget(self._market_label)

        # stat/line switcher lives inside the identity column now
        switch_row = QHBoxLayout()
        switch_row.setSpacing(4)
        switch_row.addWidget(QLabel("Stat:"))
        self._stat_combo = QComboBox()
        self._stat_combo.currentIndexChanged.connect(self._on_stat_controls_changed)
        switch_row.addWidget(self._stat_combo)
        switch_row.addWidget(QLabel("Line:"))
        self._line_spin = QDoubleSpinBox()
        self._line_spin.setRange(0.0, 500.0)
        self._line_spin.setSingleStep(0.5)
        self._line_spin.setDecimals(1)
        self._line_spin.valueChanged.connect(self._on_stat_controls_changed)
        switch_row.addWidget(self._line_spin)
        # bottom-area view toggle lives up here to save a row below
        self._splits_view_combo = QComboBox()
        self._splits_view_combo.addItem("Stats", "stats")
        self._splits_view_combo.addItem("Spray chart", "spray")
        self._splits_view_combo.currentIndexChanged.connect(
            self._on_bottom_view_toggle)
        switch_row.addWidget(self._splits_view_combo)
        switch_row.addStretch()
        ident.addLayout(switch_row)
        ident.addStretch()
        head_row.addLayout(ident)

        # condensed traditional season line (G/PA/AVG/... or W-L/ERA/...)
        trad_holder = QWidget()
        self._trad_grid = QGridLayout(trad_holder)
        self._trad_grid.setContentsMargins(12, 2, 0, 0)
        self._trad_grid.setHorizontalSpacing(10)
        self._trad_grid.setVerticalSpacing(0)
        self._trad_grid.setAlignment(Qt.AlignmentFlag.AlignTop
                                     | Qt.AlignmentFlag.AlignLeft)
        head_row.addWidget(trad_holder)
        head_row.addStretch()
        content_lay.addLayout(head_row)

        # -- matchup strip: one compact line — game/park/weather + opp SP
        self._matchup_frame = QFrame()
        self._matchup_frame.setObjectName("matchupStrip")
        matchup_lay = QVBoxLayout(self._matchup_frame)
        matchup_lay.setContentsMargins(6, 2, 6, 2)
        matchup_lay.setSpacing(0)
        self._matchup_line = QLabel("")
        self._matchup_line.setObjectName("matchupLine")
        self._matchup_line.setTextFormat(Qt.TextFormat.RichText)
        self._matchup_line.setWordWrap(True)
        matchup_lay.addWidget(self._matchup_line)
        # swing-tracking line (batters): bat speed, attack angle, etc.
        self._swing_line = QLabel("")
        self._swing_line.setObjectName("swingLine")
        self._swing_line.setTextFormat(Qt.TextFormat.RichText)
        self._swing_line.setWordWrap(True)   # unwrapped rich text would set
        self._swing_line.hide()              # a huge minimum window width
        matchup_lay.addWidget(self._swing_line)
        self._matchup_frame.hide()
        content_lay.addWidget(self._matchup_frame)

        # -- game-log bar chart with the percentile stack on its right
        #    flank (the chart has width to spare; the tables below don't)
        pg.setConfigOptions(antialias=True)
        self._plot = pg.PlotWidget(background="#151a21")
        self._plot.showGrid(x=False, y=True, alpha=0.25)
        self._plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        # Reserve room for the two-line "@OPP\n07-04" tick labels
        self._plot.getPlotItem().getAxis("bottom").setHeight(42)
        self._plot.setMinimumHeight(180)

        # Right flank of the chart: percentile stack with the (fixed-shape,
        # 9-row) velo-band table docked underneath
        chart_row = QHBoxLayout()
        chart_row.setSpacing(8)
        chart_row.addWidget(self._plot, stretch=1)
        flank = QWidget()
        flank.setFixedWidth(400)
        flank_lay = QVBoxLayout(flank)
        flank_lay.setContentsMargins(0, 2, 0, 0)
        flank_lay.setSpacing(6)
        pct_holder = QWidget()
        self._pct_grid = QGridLayout(pct_holder)
        self._pct_grid.setContentsMargins(0, 0, 0, 0)
        self._pct_grid.setHorizontalSpacing(0)
        self._pct_grid.setVerticalSpacing(0)
        self._pct_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        flank_lay.addWidget(pct_holder)
        self._tbl_velo = self._make_stats_table()
        flank_lay.addWidget(self._tbl_velo,
                            alignment=Qt.AlignmentFlag.AlignTop
                            | Qt.AlignmentFlag.AlignLeft)
        flank_lay.addStretch()
        chart_row.addWidget(flank)
        content_lay.addLayout(chart_row, stretch=1)

        # -- bottom row: the analysis tables get the full panel width
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)

        # -- right of the percentiles: ALL analysis tables at once in a 2x2
        #    grid (Pitch Splits | SP Card / Velo Bands | Home-Road). Titles
        #    live inside each table's first column header (no label rows);
        #    the spray chart sits on a toggled second page.
        self._tbl_pitch = self._make_stats_table()
        self._tbl_sp = self._make_stats_table()

        stats_page = QWidget()
        grid = QGridLayout(stats_page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        # The two arsenal-collision tables side by side (velo bands live up
        # on the chart's right flank)
        grid.addWidget(self._tbl_pitch, 0, 0,
                       alignment=Qt.AlignmentFlag.AlignTop
                       | Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(self._tbl_sp, 0, 1,
                       alignment=Qt.AlignmentFlag.AlignTop
                       | Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)   # slack collects at the bottom

        stats_scroll = QScrollArea()
        stats_scroll.setWidgetResizable(True)
        stats_scroll.setFrameShape(QFrame.Shape.NoFrame)
        stats_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        stats_scroll.setWidget(stats_page)

        # spray page: bare plot — legend rendered as the plot's own title
        self._spray_plot = pg.PlotWidget(background="#151a21")
        self._spray_plot.setAspectLocked(True)
        self._spray_plot.hideAxis("bottom")
        self._spray_plot.hideAxis("left")
        self._spray_plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self._spray_plot.setMenuEnabled(False)
        self._spray_plot.hideButtons()

        from PyQt6.QtWidgets import QStackedWidget
        self._splits_stack = QStackedWidget()
        self._splits_stack.addWidget(stats_scroll)
        self._splits_stack.addWidget(self._spray_plot)
        bottom_row.addWidget(self._splits_stack, stretch=1)
        content_lay.addLayout(bottom_row)

    # ------------------------------------------------------------- data in

    def set_percentile_data(self, hitters_df, pitchers_df):
        """Provide Savant percentile leaderboards (may arrive after show)."""
        self._pct_hitters = hitters_df
        self._pct_pitchers = pitchers_df
        if self._summary is not None:
            self._update_percentiles(self._summary)

    def current_player_name(self) -> Optional[str]:
        return self._summary.player_name if self._summary else None

    def _sync_stat_controls(self, summary: PropStatSummary):
        """Point the stat combo/line spin at the shown summary without
        re-triggering a fetch."""
        self._updating = True
        try:
            is_pitcher = summary.market_key.startswith("pitcher")
            options = PITCHER_STAT_OPTIONS if is_pitcher else BATTER_STAT_OPTIONS
            # Rebuild only when the player type flipped
            current_keys = [self._stat_combo.itemData(i)
                            for i in range(self._stat_combo.count())]
            if current_keys != [k for k, _ in options]:
                self._stat_combo.clear()
                for key, label in options:
                    self._stat_combo.addItem(label, key)
            base_key = summary.market_key
            if base_key.endswith("_alternate"):
                base_key = base_key[: -len("_alternate")]
            idx = self._stat_combo.findData(base_key)
            if idx >= 0:
                self._stat_combo.setCurrentIndex(idx)
            if summary.line is not None:
                self._line_spin.setValue(summary.line)
        finally:
            self._updating = False

    def _on_stat_controls_changed(self, *_):
        if self._updating or self._summary is None:
            return
        market_key = self._stat_combo.currentData()
        if market_key:
            self.stat_requested.emit(market_key, self._line_spin.value())

    def show_summary(self, summary: PropStatSummary):
        """Populate the panel from a PropStatSummary."""
        player_changed = (self._summary is None
                          or self._summary.player_id != summary.player_id)
        self._summary = summary
        self._placeholder.hide()
        self._content.show()
        self._sync_stat_controls(summary)
        if player_changed:
            self.set_matchup_loading()
            self.set_pitch_splits_loading()
            self._clear_traditional()

        self._name_label.setText(summary.player_name)
        self._meta_label.setText(
            f"{summary.team} · {summary.position} · {summary.games_played} games")
        line_txt = "" if summary.line is None else f"  line {summary.line:g}"
        self._market_label.setText(
            f"{summary.market_key}  ({summary.stat_label}){line_txt}")

        self._load_headshot(summary.player_id)
        self._update_chart(summary)
        self._update_percentiles(summary)

    # ---------------------------------------------- stats-grid table infra

    def _make_stats_table(self) -> QTableWidget:
        t = QTableWidget()
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(17)
        t.setAlternatingRowColors(True)
        t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(18)
        t.setStyleSheet(_STATS_TABLE_QSS)
        return t

    @staticmethod
    def _fit_table(t: QTableWidget):
        """Fix BOTH dimensions to content so the widget frame hugs its
        columns/rows — no dead space inside the border, no inner scrollbars
        (the whole panel scrolls instead when space runs out)."""
        t.resizeColumnsToContents()
        h = t.horizontalHeader().height() + 2 * t.frameWidth()
        h += sum(t.rowHeight(r) for r in range(t.rowCount()))
        t.setFixedHeight(max(h, 42))
        w = 2 * t.frameWidth() + 2
        w += sum(t.columnWidth(c) for c in range(t.columnCount()))
        t.setFixedWidth(w)

    def _on_bottom_view_toggle(self, *_):
        spray = (self._splits_view_combo.currentData() == "spray")
        self._splits_stack.setCurrentIndex(1 if spray else 0)
        if spray:
            self._render_spray()

    # ---------------------------------------------------- traditional line

    def _clear_traditional(self):
        while self._trad_grid.count():
            item = self._trad_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    _TRAD_PAIRS_PER_ROW = 12

    def show_traditional(self, pairs: List[tuple]):
        """Condensed season stat line beside the headshot: [(label, value),
        ...] rendered as label/value mini-rows, banded so the full set never
        forces the panel wider than the screen."""
        self._clear_traditional()
        for i, (label, value) in enumerate(pairs):
            band, col = divmod(i, self._TRAD_PAIRS_PER_ROW)
            lab = QLabel(label)
            lab.setObjectName("tradLabel")
            lab.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            val = QLabel(value)
            val.setObjectName("tradValue")
            val.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._trad_grid.addWidget(lab, band * 2, col)
            self._trad_grid.addWidget(val, band * 2 + 1, col)

    # ------------------------------------------------------------- matchup

    def set_matchup_loading(self):
        self._matchup_frame.show()
        self._matchup_line.setText("Matchup loading…")

    def show_matchup(self, ctx: Optional[MatchupContext]):
        """Render today's game context on one compact line (None = no game)."""
        self._last_ctx = ctx
        self._matchup_frame.show()
        if ctx is None:
            self._matchup_line.setText("No game today")
            return

        vs = "vs" if ctx.is_home else "@"
        parts = [f"Today {vs} <b>{ctx.opponent}</b>"
                 + (f" {ctx.game_time}" if ctx.game_time else "")
                 + f" · {ctx.venue}"]

        # Lineup / probable-SP confirmation chip
        chip = None
        if ctx.probable_sp is not None:
            chip = (("#2ECC71", "Probable SP ✓") if ctx.probable_sp
                    else ("#E74C3C", "NOT the probable SP"))
        elif ctx.lineup_posted is not None:
            if not ctx.lineup_posted:
                chip = ("#95A5A6", "Lineup TBD")
            elif ctx.lineup_slot:
                chip = ("#2ECC71", f"Batting {_ordinal(ctx.lineup_slot)} ✓")
            else:
                chip = ("#E74C3C", "NOT IN LINEUP")
        if chip:
            parts.append(f"<span style='color:{chip[0]}; "
                         f"font-weight:bold;'>{chip[1]}</span>")
        if ctx.park_factor is not None:
            parts.append(f"Park {ctx.park_factor}/HR {ctx.park_hr_factor}")
        if ctx.weather:
            w = ctx.weather
            parts.append(f"{w['temperature']:.0f}°F {w['wind_speed']:.0f}mph "
                         f"{w['wind_dir_compass']} {w['condition']}")

        # opp_team_batting is only requested for pitcher props — when present
        # it's the relevant matchup half (the opposing SP is not).
        opp = ""
        if ctx.opp_team_batting:
            b = ctx.opp_team_batting
            bits = []
            if b.get("avg"):
                bits.append(f"AVG {b['avg']}")
            if b.get("ops"):
                bits.append(f"OPS {b['ops']}")
            if b.get("k_per_game") is not None:
                bits.append(f"{b['k_per_game']:.1f} K/gm")
            if b.get("hr") is not None:
                bits.append(f"{b['hr']} HR")
            opp = f"Opp lineup: " + " · ".join(bits)
        elif ctx.opp_pitcher_name:
            hand_bits = []
            if ctx.opp_pitcher_hand:
                hand_bits.append(f"{ctx.opp_pitcher_hand}HP")
            if ctx.opp_pitcher_arm is not None:
                hand_bits.append(f"arm {ctx.opp_pitcher_arm:.0f}°")
            hand = f" ({', '.join(hand_bits)})" if hand_bits else ""
            opp = f"Opp SP: {ctx.opp_pitcher_name}{hand}"
            st = ctx.opp_pitcher_stats
            if st:
                bits = [f"{lbl} {st[k]}" for k, lbl in
                        (("era", "ERA"), ("whip", "WHIP"), ("k9", "K/9"),
                         ("hr9", "HR/9")) if st.get(k)]
                if bits:
                    opp += " — " + " · ".join(bits)
            sp = ctx.opp_pitcher_stuff
            if sp:
                fg_bits = [f"{lbl} {sp[k]:.0f}" for k, lbl in
                           (("stuff", "Stf+"), ("location", "Loc+"),
                            ("pitching", "Pit+")) if sp.get(k) is not None]
                if sp.get("siera") is not None:
                    fg_bits.append(f"SIERA {sp['siera']:.2f}")
                if fg_bits:
                    opp += " · " + " · ".join(fg_bits)
        else:
            opp = "Opp SP: TBD"

        line = " &nbsp;·&nbsp; ".join(parts)
        if opp:
            line += (" &nbsp;&nbsp;<span style='color:#E67E22; "
                     f"font-weight:bold;'>{opp}</span>")
        # Batter-side context (blue): split vs the SP's hand + career BvP
        batter_bits = [b for b in (ctx.batter_vs_hand, ctx.bvp) if b]
        if batter_bits:
            line += (" &nbsp;&nbsp;<span style='color:#5DADE2; "
                     "font-weight:bold;'>" + " · ".join(batter_bits)
                     + "</span>")
        self._matchup_line.setText(line)

    # --------------------------------------------------------- pitch splits

    def set_pitch_splits_loading(self):
        for tbl, name in ((self._tbl_pitch, "Pitch Splits"),
                          (self._tbl_sp, "SP Card"),
                          (self._tbl_velo, "Velo Bands")):
            tbl.setRowCount(0)
            tbl.setColumnCount(1)
            tbl.setHorizontalHeaderLabels([f"{name} — loading…"])
            tbl.setToolTip("")
            self._fit_table(tbl)
        self._spray_plot.getPlotItem().setTitle("Spray — loading…",
                                                color="#95A5A6", size="8pt")
        self._splits = None
        self._velo_splits = None
        self._splits_type = None
        self._arsenal = None          # opposing SP arsenal (batter view)
        self._arsenal_pitcher = None
        self._arsenal_stuff = None
        self._sp_card = None
        self._sp_card_name = ""
        self._spray_points = None
        self._swing_line.hide()

    def set_spray(self, points: List[tuple]):
        """Season spray points [(x_ft, y_ft, cat)] for the shown player."""
        self._spray_points = points
        if self._splits_view_combo.currentData() == "spray":
            self._render_spray()

    def show_swing(self, fgb: Optional[dict]):
        """Swing-tracking line under the matchup strip (batters)."""
        if not fgb or fgb.get("bat_speed") is None:
            self._swing_line.hide()
            return

        def as_pct(v):
            return "" if v is None else f"{(v * 100 if v <= 1 else v):.0f}%"

        bits = [f"<b>Swing:</b> {fgb['bat_speed']:.1f} mph bat"]
        if fgb.get("attack_angle") is not None:
            aa = f"attack {fgb['attack_angle']:.0f}°"
            if fgb.get("ideal_aa") is not None:
                aa += f" (ideal {as_pct(fgb['ideal_aa'])})"
            bits.append(aa)
        if fgb.get("attack_dir") is not None:
            d = fgb["attack_dir"]
            bits.append(f"dir {abs(d):.0f}° {'pull' if d >= 0 else 'oppo'}")
        if fgb.get("fast_swing") is not None:
            bits.append(f"fast-swing {as_pct(fgb['fast_swing'])}")
        if fgb.get("squared_up") is not None:
            bits.append(f"sq-up {as_pct(fgb['squared_up'])}")
        if fgb.get("blast") is not None:
            bits.append(f"blast {as_pct(fgb['blast'])}")
        if fgb.get("swing_length") is not None:
            bits.append(f"length {fgb['swing_length']:.1f} ft")
        self._swing_line.setText(" · ".join(bits))
        self._swing_line.show()

    def set_sp_card(self, card: Optional[dict], pitcher_name: str,
                    hand: Optional[str] = None):
        """SP deep card (get_sp_deep_card result) — shown in the 'Opp SP'
        view. For pitcher props this is the player's own arsenal."""
        self._sp_card = card
        self._sp_card_name = pitcher_name
        self._sp_card_hand = hand
        self._render_sp_card()

    def show_pitch_splits(self, splits: List[PitchSplit], player_type: str,
                          velo_splits: Optional[List[PitchSplit]] = None):
        self._splits = splits
        self._velo_splits = velo_splits
        self._splits_type = player_type
        self._render_pitch_table()
        self._render_velo_table()

    def set_opposing_arsenal(self, pitcher_name: str, arsenal: dict,
                             stuff: Optional[dict] = None):
        """Highlight split rows matching the opposing SP's arsenal
        ({savant_pitch_name: {'usage': .., 'speed': ..}}). `stuff` is the
        FG row from get_fg_pitching — per-pitch Stf+ gets appended to the
        highlighted rows."""
        self._arsenal = arsenal or None
        self._arsenal_pitcher = pitcher_name
        self._arsenal_stuff = stuff
        if self._splits is not None:
            self._render_pitch_table()

    _SPLIT_HEADERS = ["Pitch", "#", "Velo", "Whiff", "BBE", "EV", "HH%",
                      "Brl%", "HR", "xwOBA"]

    @staticmethod
    def _cell(text, align_right=True):
        item = QTableWidgetItem(text)
        if align_right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _render_sp_card(self):
        """The SP deep card: per-pitch usage/velo/spin/Stuff+/run value and
        results against. Colored batter-centric: green = attackable pitch,
        red = threat."""
        table = self._tbl_sp
        cell = self._cell
        table.setRowCount(0)
        card = self._sp_card
        if card is None:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["SP Card — waiting for matchup…"])
            self._fit_table(table)
            return
        hand = f" · {self._sp_card_hand}" if self._sp_card_hand else ""
        arm = (f" · {card['arm_angle']:.0f}°"
               if card.get("arm_angle") is not None else "")
        # Identity lives in the tooltip — a long col-0 header would blow the
        # column width out (and the matchup strip already names the SP)
        headers = ["SP Mix", "Use%", "Velo", "Spin", "Stf+", "RV",
                   "wOBA", "xwOBA", "Whiff", "HH%"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setToolTip(
            f"{self._sp_card_name}{hand}{arm} — arsenal & results against; "
            "green = attackable, red = threat (RV/100 is pitcher-positive)")

        green, red = QColor(46, 204, 113), QColor(231, 76, 60)
        num = lambda v, d=1: "" if v is None else f"{v:.{d}f}"
        w3 = lambda v: "" if v is None else f"{v:.3f}".lstrip("0")
        for r, p in enumerate(card["rows"]):
            table.insertRow(r)
            name_cell = cell(PITCH_ABBREV.get(p["pitch"], p["pitch"]),
                             align_right=False)
            name_cell.setToolTip(p["pitch"])
            table.setItem(r, 0, name_cell)
            table.setItem(r, 1, cell(num(p["usage"])))
            table.setItem(r, 2, cell(num(p["velo"])))
            table.setItem(r, 3, cell(num(p["spin"], 0)))
            table.setItem(r, 4, cell(num(p["stuff"], 0)))
            rv = cell(num(p["rv100"]))
            if p["rv100"] is not None:
                # Savant RV/100 is pitcher-positive; batter view inverts it
                if p["rv100"] >= 1.5:
                    rv.setForeground(red)
                elif p["rv100"] <= -1.0:
                    rv.setForeground(green)
            table.setItem(r, 5, rv)
            woba = cell(w3(p["woba"]))
            if p["woba"] is not None:
                if p["woba"] >= 0.360:
                    woba.setForeground(green)
                elif p["woba"] <= 0.280:
                    woba.setForeground(red)
            table.setItem(r, 6, woba)
            xw = cell(w3(p["xwoba"]))
            if p["xwoba"] is not None:
                if p["xwoba"] >= 0.360:
                    xw.setForeground(green)
                elif p["xwoba"] <= 0.280:
                    xw.setForeground(red)
            table.setItem(r, 7, xw)
            whiff = cell(num(p["whiff"]))
            if (p["whiff"] or 0) >= 35:
                whiff.setForeground(red)
            table.setItem(r, 8, whiff)
            table.setItem(r, 9, cell(num(p["hh"])))
        table.resizeRowsToContents()
        self._fit_table(table)

    def _render_spray(self):
        """Season spray chart over today's park walls. +x = RF, +y = CF;
        wall arc from weatherman polar equations for the matchup venue."""
        plot = self._spray_plot.getPlotItem()
        plot.clear()
        points = self._spray_points
        venue = self._last_ctx.venue if self._last_ctx else None
        if points is None:
            plot.setTitle("Spray — loading…", color="#95A5A6", size="8pt")
            return
        if not points:
            plot.setTitle("Spray — no batted balls", color="#95A5A6",
                          size="8pt")
            return

        cats = {
            "HR":  (points, (241, 196, 15), 7),
            "XBH": (points, (52, 152, 219), 5),
            "1B":  (points, (46, 204, 113), 4),
            "OUT": (points, (127, 140, 141), 3),
        }
        counts = {}
        for cat, (_, color, size) in cats.items():
            xs = [p[0] for p in points if p[2] == cat]
            ys = [p[1] for p in points if p[2] == cat]
            counts[cat] = len(xs)
            if xs:
                plot.addItem(pg.ScatterPlotItem(
                    x=xs, y=ys, size=size,
                    brush=pg.mkBrush(*color, 190),
                    pen=pg.mkPen(0, 0, 0, 80)))

        # Park wall + foul lines
        wall_note = ""
        try:
            import math
            from weatherman import STADIUM_DATA, get_stadium_wall_distance
            if venue and venue in STADIUM_DATA:
                wx, wy = [], []
                for a in range(0, 91, 2):
                    rr = get_stadium_wall_distance(venue, a)
                    ang = math.radians(45 - a)
                    wx.append(rr * math.sin(ang))
                    wy.append(rr * math.cos(ang))
                plot.plot(wx, wy, pen=pg.mkPen(200, 200, 210, 200, width=2))
                wall_note = f" · wall: {venue}"
        except Exception as e:
            print(f"PropWindowUtils: spray wall draw failed: {e}")
        import math
        for sign in (1, -1):
            L = 340
            plot.plot([0, sign * L * math.sin(math.radians(45))],
                      [0, L * math.cos(math.radians(45))],
                      pen=pg.mkPen(120, 120, 130, 150,
                                   style=Qt.PenStyle.DashLine))
        # home plate marker
        plot.addItem(pg.ScatterPlotItem(
            x=[0], y=[0], size=9, symbol="d",
            brush=pg.mkBrush(230, 230, 235, 230), pen=pg.mkPen(None)))

        plot.setTitle(
            f"<span style='color:#F1C40F'>{counts['HR']} HR</span> · "
            f"<span style='color:#3498DB'>{counts['XBH']} XBH</span> · "
            f"<span style='color:#2ECC71'>{counts['1B']} 1B</span> · "
            f"<span style='color:#7F8C8D'>{counts['OUT']} outs</span>"
            f"<span style='color:#95A5A6'>{wall_note}</span>", size="8pt")

    def _fill_split_rows(self, table, splits, arsenal=None, stuff=None,
                         col0="Pitch"):
        """Shared row filler for the pitch and velo split tables. `col0`
        doubles as the table title (no separate label row)."""
        cell = self._cell
        table.setRowCount(0)
        table.setColumnCount(len(self._SPLIT_HEADERS))
        table.setHorizontalHeaderLabels([col0] + self._SPLIT_HEADERS[1:])
        highlight_bg = QColor(60, 45, 18)          # dark amber row tint
        highlight_fg = QColor(230, 126, 34)
        pct = lambda v: "" if v is None else f"{v:.0%}"
        num = lambda v, d=1: "" if v is None else f"{v:.{d}f}"
        for r, s in enumerate(splits):
            table.insertRow(r)
            in_arsenal = bool(arsenal) and s.pitch in arsenal
            pitch_text = PITCH_ABBREV.get(s.pitch, s.pitch)
            if in_arsenal:
                pitch_text += f"  ({arsenal[s.pitch]['usage']:.0%}"
                if stuff and stuff.get("per_pitch"):
                    code = FG_PITCH_CODES.get(s.pitch)
                    stf = stuff["per_pitch"].get(code)
                    if stf is not None:
                        pitch_text += f", Stf+ {stf:.0f}"
                pitch_text += ")"
            name_cell = cell(pitch_text, align_right=False)
            name_cell.setToolTip(s.pitch)
            row_cells = [
                name_cell,
                cell(str(s.count)),
                cell(num(s.velo)),
                cell(pct(s.whiff_pct)),
                cell(str(s.bbe)),
                cell(num(s.avg_ev)),
                cell(pct(s.hardhit_pct)),
                cell(pct(s.barrel_pct)),
                cell(str(s.hr)),
            ]
            xw = cell(num(s.xwobacon, 3))
            # Loud contact pops green, weak contact red (league xwOBAcon ~.370)
            if s.xwobacon is not None and s.bbe >= 5:
                if s.xwobacon >= 0.450:
                    xw.setForeground(QColor(46, 204, 113))
                elif s.xwobacon <= 0.300:
                    xw.setForeground(QColor(231, 76, 60))
            row_cells.append(xw)
            for c, item in enumerate(row_cells):
                if in_arsenal:
                    item.setBackground(highlight_bg)
                    if c == 0:
                        item.setForeground(highlight_fg)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                table.setItem(r, c, item)
        table.resizeRowsToContents()
        self._fit_table(table)

    def _render_pitch_table(self):
        splits = self._splits
        if splits is None:
            return
        player_type = self._splits_type
        arsenal = self._arsenal if player_type == "batter" else None
        if not splits:
            self._tbl_pitch.setRowCount(0)
            self._tbl_pitch.setColumnCount(1)
            self._tbl_pitch.setHorizontalHeaderLabels(
                ["Pitch Splits — no Statcast data"])
            self._fit_table(self._tbl_pitch)
            return
        if arsenal:
            tooltip = (f"Performance vs each pitch type; rows in "
                       f"{self._arsenal_pitcher}'s arsenal highlighted with "
                       "his usage % and per-pitch Stuff+")
        elif player_type == "batter":
            tooltip = "Contact quality vs each pitch type this season"
        else:
            tooltip = "Arsenal & contact allowed per pitch this season"
        self._tbl_pitch.setToolTip(tooltip)
        # Arsenal pitches sort to the top, by SP usage
        if arsenal:
            splits = sorted(splits, key=lambda s: (
                -(arsenal[s.pitch]["usage"] if s.pitch in arsenal else -1),
                -s.count))
        self._fill_split_rows(self._tbl_pitch, splits, arsenal,
                              self._arsenal_stuff, col0="Pitch Splits")

    def _render_velo_table(self):
        splits = self._velo_splits
        if splits is None:
            return
        if not splits:
            self._tbl_velo.setRowCount(0)
            self._tbl_velo.setColumnCount(1)
            self._tbl_velo.setHorizontalHeaderLabels(
                ["Velo Bands — no Statcast data"])
            self._fit_table(self._tbl_velo)
            return
        self._tbl_velo.setToolTip(
            "Performance by pitch speed band (FB/Brk/Off families)"
            if self._splits_type == "batter"
            else "Results allowed by pitch speed band")
        self._fill_split_rows(self._tbl_velo, splits, col0="Velo Band")

    # --------------------------------------------------------------- chart

    def _update_chart(self, summary: PropStatSummary):
        plot = self._plot.getPlotItem()
        plot.clear()

        games = summary.games[-CHART_GAMES:]
        if not games:
            return
        line = summary.line if summary.line is not None else 0.5

        xs = list(range(len(games)))
        heights, brushes = [], []
        for g in games:
            heights.append(g.value)
            c = COLOR_OVER if g.value > line else COLOR_UNDER
            brushes.append(pg.mkBrush(*c, 210))
        # Zero-value games still get a visible nub so misses don't vanish.
        drawn = [h if h > 0 else max(line, 1) * 0.04 for h in heights]

        bars = pg.BarGraphItem(x=xs, height=drawn, width=0.72, brushes=brushes,
                               pen=pg.mkPen(0, 0, 0, 120))
        plot.addItem(bars)

        prop_line = pg.InfiniteLine(
            pos=line, angle=0,
            pen=pg.mkPen(*COLOR_LINE, width=2, style=Qt.PenStyle.DashLine),
            label=f"{line:g}",
            labelOpts={"position": 0.02, "color": COLOR_LINE, "movable": False})
        plot.addItem(prop_line)

        # value labels above each bar
        for x, g in zip(xs, games):
            t = pg.TextItem(f"{g.value:g}", color=(220, 220, 220), anchor=(0.5, 1))
            t.setPos(x, max(g.value, drawn[x]))
            plot.addItem(t)

        axis = plot.getAxis("bottom")
        ticks = [(x, f"{'vs' if g.is_home else '@'}{g.opponent}\n{g.date[5:]}")
                 for x, g in zip(xs, games)]
        axis.setTicks([ticks])
        axis.setStyle(tickFont=QFont("Segoe UI", 7))

        ymax = max(max(heights), line) * 1.28 + 0.1
        plot.setYRange(0, ymax, padding=0)
        plot.setXRange(-0.6, len(games) - 0.4, padding=0)

    # --------------------------------------------------------- percentiles

    def _clear_pct_grid(self):
        while self._pct_grid.count():
            item = self._pct_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _pct_message(self, text: str):
        label = QLabel(text)
        label.setObjectName("pctMissing")
        self._pct_grid.addWidget(label, 0, 0, 1, 2)

    def _update_percentiles(self, summary: PropStatSummary):
        self._clear_pct_grid()

        is_pitcher = summary.market_key.startswith("pitcher")
        df = self._pct_pitchers if is_pitcher else self._pct_hitters
        cols = PITCHER_PCT_COLS if is_pitcher else HITTER_PCT_COLS
        if df is None:
            self._pct_message("Percentiles loading…")
            return

        row = df[df["player_id"] == summary.player_id]
        if row.empty:
            self._pct_message("No Savant percentile data (min PA/BF not met)")
            return
        row = row.iloc[0]

        bars = []
        for col, label in cols:
            if col not in row.index:
                continue
            try:
                pct = float(row[col])
            except (TypeError, ValueError):
                continue
            if pct != pct:      # NaN
                continue
            bars.append(CompactPercentileBar(label, pct))

        # Single tight stack — the splits table gets the horizontal space
        for i, bar in enumerate(bars):
            self._pct_grid.addWidget(bar, i, 0)

    # ----------------------------------------------------------- headshot

    def _load_headshot(self, player_id: int):
        self._pending_pid = player_id
        cached = HEADSHOT_DIR / f"{player_id}.png"
        if cached.exists():
            self._set_headshot(QPixmap(str(cached)))
            return
        self._headshot.setPixmap(QPixmap())
        req = QNetworkRequest(QUrl(HEADSHOT_URL.format(pid=player_id)))
        req.setAttribute(QNetworkRequest.Attribute.User, player_id)
        self._nam.get(req)

    def _on_headshot_reply(self, reply: QNetworkReply):
        pid = reply.request().attribute(QNetworkRequest.Attribute.User)
        data = reply.readAll()
        reply.deleteLater()
        if not data or pid != self._pending_pid:
            return
        px = QPixmap()
        if px.loadFromData(bytes(data)):
            try:
                px.save(str(HEADSHOT_DIR / f"{pid}.png"))
            except Exception:
                pass
            self._set_headshot(px)

    def _set_headshot(self, px: QPixmap):
        rounded = QPixmap(px.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, px.width(), px.height()), 10, 10)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, px)
        painter.end()
        self._headshot.setPixmap(rounded)


# ===========================================================================
# BULLPEN PANEL (Qt) — dedicated section below the odds grid
# ===========================================================================

class BullpenPanel(QWidget):
    """Standalone bullpen-fatigue section: team selector + per-reliever
    last-7-days pitch counts with TAXED/USED/FRESH flags. The props window
    syncs the team to the selected player's matchup; the combo allows
    browsing any pen. Data via MLBPropStats.get_bullpen_usage (one page
    fetch covers all 30 teams, cached 30 min)."""

    def __init__(self, stats: Optional[MLBPropStats] = None, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._current_team: Optional[str] = None
        self._build_ui()

    def set_stats_backend(self, stats: MLBPropStats):
        self._stats = stats

    def _build_ui(self):
        self.setStyleSheet("""
            #bullpenTitle { color: #dc9437; font-size: 9pt; font-weight: bold; }
            #bullpenContext { color: #7F8C8D; font-size: 8pt; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("BULLPEN FATIGUE")
        title.setObjectName("bullpenTitle")
        top.addWidget(title)
        self._team_combo = QComboBox()
        self._team_combo.currentTextChanged.connect(self._on_team_changed)
        top.addWidget(self._team_combo)
        self._context_label = QLabel("")
        self._context_label.setObjectName("bullpenContext")
        top.addWidget(self._context_label)
        top.addStretch()
        root.addLayout(top)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(17)
        self._table.setAlternatingRowColors(True)
        headers = ["Reliever", "T", "Role", "vs", "Inn", "Diff",
                   "ERA", "ERA7", "SIERA", "K-BB%", "Stf+", "Loc+", "gmLI",
                   "SV", "HLD", "GF", "NP", "30+", "B2B",
                   "Yd", "-2", "-3", "L3", "L7", "Status"]
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(18)
        self._table.setStyleSheet(_STATS_TABLE_QSS)
        # NOTE: no alignment arg — that would make the table adopt its tiny
        # default sizeHint instead of filling; the content-width maximum set
        # in _render() handles the right-edge cap.
        root.addWidget(self._table, stretch=1)

    # ------------------------------------------------------------- control

    def set_teams(self, abbrs: List[str]):
        """Populate the team combo once the roster/team list is known."""
        current = self._team_combo.currentText()
        self._team_combo.blockSignals(True)
        self._team_combo.clear()
        self._team_combo.addItems(sorted(abbrs))
        if current:
            idx = self._team_combo.findText(current)
            if idx >= 0:
                self._team_combo.setCurrentIndex(idx)
        self._team_combo.blockSignals(False)

    def show_team(self, abbr: str, context: str = ""):
        """Programmatic team switch (e.g. synced to the detail player)."""
        self._context_base = context
        self._context_label.setText(context)
        idx = self._team_combo.findText(abbr)
        if idx >= 0 and self._team_combo.currentIndex() != idx:
            self._team_combo.setCurrentIndex(idx)   # triggers load
        else:
            self._load_team(abbr)

    def _on_team_changed(self, abbr: str):
        if abbr:
            self._load_team(abbr)

    def _load_team(self, abbr: str):
        if self._stats is None or not abbr:
            return
        self._current_team = abbr
        asyncio.create_task(self._fetch(abbr))

    async def _fetch(self, abbr: str):
        try:
            async with aiohttp.ClientSession() as session:
                rows = await self._stats.get_bullpen_report(session, abbr)
        except Exception as e:
            print(f"BullpenPanel: fetch failed for {abbr}: {e}")
            return
        if self._current_team == abbr:
            self._render(rows)

    # -------------------------------------------------------------- render

    _GREEN = QColor(46, 204, 113)
    _RED = QColor(231, 76, 60)
    _ORANGE = QColor(230, 126, 34)

    def _render(self, rows: List[dict]):
        # Pen hierarchy first (leverage), fatigue visible per-row
        rows = sorted(rows, key=lambda r: -((r.get("fg") or {}).get("gmli") or 0))
        table = self._table
        table.setRowCount(0)

        def cell(text, align_right=True):
            item = QTableWidgetItem(text)
            if align_right:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            return item

        def bold(item, color=None):
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            if color:
                item.setForeground(color)
            return item

        pct = lambda v: "" if v is None else f"{v * 100:.0f}%"
        num = lambda v, d=2: "" if v is None else f"{v:.{d}f}"
        power_rank = next((rec.get("power_rank") for rec in rows
                           if rec.get("power_rank")), None)
        base = getattr(self, "_context_base", self._context_label.text())
        self._context_label.setText(
            base + (f"   ·   BP Power Rank #{power_rank}" if power_rank else ""))

        for r, rec in enumerate(rows):
            table.insertRow(r)
            fg = rec.get("fg") or {}
            nps = [n for _, n in rec["np_by_day"]] + [0, 0, 0]

            name = cell(rec["name"], align_right=False)
            if rec.get("role") == "CL":
                bold(name)
            table.setItem(r, 0, name)
            table.setItem(r, 1, cell(fg.get("throws") or "", align_right=False))
            role = cell(rec.get("role", ""), align_right=False)
            if rec.get("role") in ("CL", "SU"):
                bold(role, self._ORANGE)
            table.setItem(r, 2, role)
            table.setItem(r, 3, cell(rec.get("vs") or "", align_right=False))
            table.setItem(r, 4, cell(rec.get("avg_inning") or ""))
            table.setItem(r, 5, cell(rec.get("avg_diff") or ""))
            table.setItem(r, 6, cell(rec["era"]))
            table.setItem(r, 7, cell(rec.get("era7") or ""))
            table.setItem(r, 8, cell(num(fg.get("siera"))))
            table.setItem(r, 9, cell(pct(fg.get("kbb"))))
            stf = cell(num(fg.get("stuff"), 0))
            if fg.get("stuff") is not None:
                if fg["stuff"] >= 105:
                    stf.setForeground(self._GREEN)
                elif fg["stuff"] <= 95:
                    stf.setForeground(self._RED)
            table.setItem(r, 10, stf)
            table.setItem(r, 11, cell(num(fg.get("location"), 0)))
            gmli = cell(num(fg.get("gmli")))
            if (fg.get("gmli") or 0) >= 1.3:
                bold(gmli, self._ORANGE)
            table.setItem(r, 12, gmli)
            table.setItem(r, 13, cell(
                str(int(fg["sv"])) if fg.get("sv") else ""))
            table.setItem(r, 14, cell(
                str(int(fg["hld"])) if fg.get("hld") else ""))
            table.setItem(r, 15, cell(str(rec.get("gf") or "")))
            table.setItem(r, 16, cell(
                num(rec.get("avg_np"), 0) if rec.get("avg_np") else ""))
            table.setItem(r, 17, cell(str(rec.get("thirty_plus") or "")))
            # B2B: the site's own judgment when logged in, else season count
            b2b_site = rec.get("b2b_site")
            b2b = rec.get("b2b_count")
            b2b_cell = cell(b2b_site if b2b_site else
                            ("" if b2b is None else
                             (f"{b2b}×" if b2b else "never")))
            if b2b_site == "No" or (b2b_site is None and b2b == 0):
                b2b_cell.setForeground(self._RED)
            table.setItem(r, 18, b2b_cell)
            for c, v in enumerate((nps[0], nps[1], nps[2],
                                   rec["np_l3"], rec["np_l7"]), 19):
                table.setItem(r, c, cell(str(v) if v else ""))
            status = cell(rec["status"], align_right=False)
            if rec["status"] in ("TAXED", "UNAVAIL"):
                bold(status, self._RED)
            elif rec["status"] == "DOUBTFUL":
                bold(status, self._ORANGE)
            elif rec["status"] == "FRESH":
                status.setForeground(self._GREEN)
            table.setItem(r, 24, status)
        table.resizeRowsToContents()
        # Hug the columns horizontally — leftover pane width stays as plain
        # background instead of empty table frame
        table.resizeColumnsToContents()
        w = 2 * table.frameWidth() + 2
        w += sum(table.columnWidth(c) for c in range(table.columnCount()))
        table.setMaximumWidth(w)


# ===========================================================================
# CLI smoke test (data layer)
# ===========================================================================

async def _main():
    import sys
    player = sys.argv[1] if len(sys.argv) > 1 else "Kyle Schwarber"
    market = sys.argv[2] if len(sys.argv) > 2 else "batter_home_runs"
    line = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    stats = MLBPropStats()
    async with aiohttp.ClientSession() as session:
        s = await stats.summarize(session, player, market, line)
        if not s:
            print("no summary")
            return
        print(f"{s.player_name} ({s.team} {s.position}) — {s.stat_label} vs {s.line}")
        print(f"  games={s.games_played} szn={s.season_avg:.2f} "
              f"L5={s.l5_avg:.2f} L10={s.l10_avg:.2f} "
              f"hit%={s.hit_rate:.0%} L10 hit%={s.hit_rate_l10:.0%}")
        for g in s.games[-10:]:
            print(f"  {g.date} {'vs' if g.is_home else '@'} {g.opponent}: {g.value:g}")
        ctx = await stats.get_matchup(
            session, s.team, include_opp_batting=market.startswith("pitcher"))
        if ctx is None:
            print("  no game today")
        else:
            print(f"  matchup: {'vs' if ctx.is_home else '@'} {ctx.opponent} "
                  f"{ctx.game_time} at {ctx.venue} "
                  f"(park {ctx.park_factor}/HR {ctx.park_hr_factor})")
            print(f"  opp SP: {ctx.opp_pitcher_name} ({ctx.opp_pitcher_hand}) "
                  f"{ctx.opp_pitcher_stats}")
            print(f"  weather: {ctx.weather}")
            if ctx.opp_team_batting:
                print(f"  opp batting: {ctx.opp_team_batting}")
        ptype = "pitcher" if market.startswith("pitcher") else "batter"
        print("  — velo bands —")
        for sp in await stats.get_velo_splits(session, s.player_id, ptype):
            print(f"  {sp.pitch:<10} n={sp.count:<4} "
                  f"whiff={'' if sp.whiff_pct is None else f'{sp.whiff_pct:.0%}':<4} "
                  f"bbe={sp.bbe:<3} ev={'' if sp.avg_ev is None else f'{sp.avg_ev:.1f}':<5} "
                  f"hh={'' if sp.hardhit_pct is None else f'{sp.hardhit_pct:.0%}':<4} "
                  f"hr={sp.hr} xwobacon={'' if sp.xwobacon is None else f'{sp.xwobacon:.3f}'}")
        print("  — by pitch —")
        for sp in await stats.get_pitch_splits(session, s.player_id, ptype):
            print(f"  {sp.pitch:<18} n={sp.count:<4} velo={sp.velo or 0:5.1f} "
                  f"whiff={'' if sp.whiff_pct is None else f'{sp.whiff_pct:.0%}':<4} "
                  f"bbe={sp.bbe:<3} ev={'' if sp.avg_ev is None else f'{sp.avg_ev:.1f}':<5} "
                  f"hh={'' if sp.hardhit_pct is None else f'{sp.hardhit_pct:.0%}':<4} "
                  f"brl={'' if sp.barrel_pct is None else f'{sp.barrel_pct:.0%}':<4} "
                  f"hr={sp.hr} xwobacon={'' if sp.xwobacon is None else f'{sp.xwobacon:.3f}'}")


if __name__ == "__main__":
    asyncio.run(_main())
