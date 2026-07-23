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
import hashlib
import json
import os
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

# --- dev cache: EFFORTPROPS_DEVCACHE=1 persists every network response to
# disk and replays it on later runs, so the FULL app can be relaunched
# offline/instantly for UI-layout iteration. No TTL — delete the dir to
# refresh. Never enable in normal use (stale data by design).
DEV_CACHE = os.environ.get("EFFORTPROPS_DEVCACHE") == "1"
DEV_CACHE_DIR = SAVE_DIR / "devcache"


def dev_cache_get(key: str) -> Optional[str]:
    if not DEV_CACHE:
        return None
    p = DEV_CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".txt")
    try:
        return p.read_text() if p.exists() else None
    except OSError:
        return None


def dev_cache_put(key: str, text: str):
    if not DEV_CACHE or not text:
        return
    try:
        DEV_CACHE_DIR.mkdir(exist_ok=True)
        (DEV_CACHE_DIR
         / (hashlib.sha1(key.encode()).hexdigest() + ".txt")).write_text(text)
    except OSError as e:
        print(f"PropWindowUtils: dev cache write failed: {e}")
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
    # Dev-cache runs never expire the board — no headless FF mid-layout-test
    if cache.exists() and (DEV_CACHE
                           or time.time() - cache.stat().st_mtime < FG_CACHE_TTL):
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
        self._situ_splits: Dict[tuple, dict] = {}  # (pid, group) -> {code: stat}
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
        cache_key = url + json.dumps(params or {}, sort_keys=True)
        cached = dev_cache_get(cache_key)
        if cached is not None:
            return json.loads(cached)
        async with self._sem:
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"PropWindowUtils: {resp.status} for {url}")
                        return None
                    data = await resp.json()
                    dev_cache_put(cache_key, json.dumps(data))
                    return data
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

    # Situational split codes shown in the detail panel, in display order.
    # vl/vr mean "vs LHP/RHP" for batters and "vs LHB/RHB" for pitchers.
    SITU_CODES = ["h", "a", "d", "n", "vl", "vr", "risp"]

    async def get_situational_splits(self, session: aiohttp.ClientSession,
                                     pid: int, group: str) -> Dict[str, dict]:
        """Season situational splits for the detail panel:
        {code: stat_block} for Home/Road, Day/Night, vs L/R."""
        key = (pid, group)
        if key not in self._situ_splits:
            data = await self._get_json(
                session, f"{STATS_BASE}/people/{pid}/stats",
                {"stats": "statSplits", "sitCodes": ",".join(self.SITU_CODES),
                 "group": group, "season": str(self.season)})
            out = {}
            try:
                for split in data["stats"][0]["splits"]:
                    code = (split.get("split") or {}).get("code")
                    if code in self.SITU_CODES:
                        out[code] = split.get("stat", {})
            except (TypeError, KeyError, IndexError):
                pass
            self._situ_splits[key] = out
        return self._situ_splits[key]

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
            text = dev_cache_get(url)
            if text is None:
                async with self._sem:
                    try:
                        async with session.get(url, headers=SAVANT_HEADERS,
                                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status == 200:
                                # BOM corrupts the quoted first column and
                                # shifts every field — strip before parsing
                                text = (await resp.text()).lstrip("﻿")
                                dev_cache_put(url, text)
                    except Exception as e:
                        print(f"PropWindowUtils: xstats fetch failed: {e}")
            if text:
                import io as _io
                for row in csv.DictReader(_io.StringIO(text)):
                    try:
                        board[int(row["player_id"])] = row
                    except (KeyError, ValueError):
                        continue
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
            html = dev_cache_get(BULLPEN_USAGE_URL) or ""
            if not html:
                async with self._sem:
                    try:
                        async with session.get(
                                BULLPEN_USAGE_URL, headers=SAVANT_HEADERS,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status == 200:
                                html = await resp.text()
                    except Exception as e:
                        print(f"PropWindowUtils: bullpen fetch failed: {e}")
                dev_cache_put(BULLPEN_USAGE_URL, html)
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
            "wpa": row.get("WPA"),
            "defense": row.get("Defense"),   # FG Def (fielding + positional)
            "bsr": row.get("BaseRunning"),   # FG BsR (base running runs)
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
        dc = dev_cache_get(f"weather_{venue}")
        if dc is not None:
            w = json.loads(dc)
        else:
            loop = asyncio.get_running_loop()
            w = await loop.run_in_executor(None, self._fetch_weather_sync, venue)
            dev_cache_put(f"weather_{venue}", json.dumps(w))
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
        text = dev_cache_get(url) or ""
        if not text:
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
            dev_cache_put(url, text)

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
                    "spin": fnum(row.get("release_spin_rate")),
                    "desc": row.get("description") or "",
                    "ev": fnum(row.get("launch_speed")),
                    "la": fnum(row.get("launch_angle")),
                    "hr": (row.get("events") or "") == "home_run",
                    "xwoba": fnum(row.get("estimated_woba_using_speedangle")),
                    "event": row.get("events") or "",
                    "hc_x": fnum(row.get("hc_x")),
                    "hc_y": fnum(row.get("hc_y")),
                    "date": row.get("game_date") or "",
                    "bat_speed": fnum(row.get("bat_speed")),
                    # SP-form extras (times-through-order + real wOBA weights)
                    "tto": fnum(row.get("n_thruorder_pitcher")),
                    "stand": row.get("stand") or "",
                    "woba_v": fnum(row.get("woba_value")),
                    "woba_d": fnum(row.get("woba_denom")),
                    # Movement + plate location (SP movement/zone plots)
                    "pfx_x": fnum(row.get("pfx_x")),
                    "pfx_z": fnum(row.get("pfx_z")),
                    "plate_x": fnum(row.get("plate_x")),
                    "plate_z": fnum(row.get("plate_z")),
                    "sz_top": fnum(row.get("sz_top")),
                    "sz_bot": fnum(row.get("sz_bot")),
                    # Hawk-Eye kinematics (pitch-flight sim, phase 3)
                    "release_pos_x": fnum(row.get("release_pos_x")),
                    "release_pos_y": fnum(row.get("release_pos_y")),
                    "release_pos_z": fnum(row.get("release_pos_z")),
                    "vx0": fnum(row.get("vx0")),
                    "vy0": fnum(row.get("vy0")),
                    "vz0": fnum(row.get("vz0")),
                    "ax": fnum(row.get("ax")),
                    "ay": fnum(row.get("ay")),
                    "az": fnum(row.get("az")),
                    "ext": fnum(row.get("release_extension")),
                })
        self._pitch_splits[key] = (time.time(), rows)
        return rows

    async def get_per_game_statcast(self, session: aiohttp.ClientSession,
                                    player_id: int,
                                    player_type: str = "batter") -> List[dict]:
        """Per-game Statcast from the cached pitch detail, oldest first.

        Each row carries game-level aggregates for the trend lines AND the
        raw per-event detail for the hover box:
          aggregates: ev (avg), hh/brl (rate), xw (avg), whiff (rate),
                      bat (avg swing speed)
          counts:     bbe, hardhits, barrels, swings, whiffs
          raw:        bip = [{ev, la, barrel, hr, event, bat}, ...] one per
                      batted ball, bat_list = every swing's bat speed
        Aggregates are None where the game had no qualifying events."""
        rows = await self._get_pitch_detail(session, player_id, player_type)
        by_date: Dict[str, dict] = {}
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            b = by_date.setdefault(d, {
                "swings": 0, "whiffs": 0, "ev": [], "hard": 0, "brl": 0,
                "xw": [], "bat": [], "bip": [], "la": []})
            desc = r["desc"]
            if desc in _SWING_DESCS:
                b["swings"] += 1
                if desc in _WHIFF_DESCS:
                    b["whiffs"] += 1
            if r.get("bat_speed") is not None:
                b["bat"].append(r["bat_speed"])
            if desc == "hit_into_play":
                ev, la = r["ev"], r["la"]
                barrel = (ev is not None and la is not None
                          and _is_barrel(ev, la))
                b["bip"].append({
                    "ev": ev, "la": la, "barrel": barrel,
                    "hr": bool(r.get("hr")), "event": r.get("event") or "",
                    "bat": r.get("bat_speed")})
                if ev is not None:
                    b["ev"].append(ev)
                    if ev >= 95:
                        b["hard"] += 1
                    if barrel:
                        b["brl"] += 1
                if la is not None:
                    b["la"].append(la)
                if r["xwoba"] is not None:
                    b["xw"].append(r["xwoba"])
        out = []
        for d in sorted(by_date):
            b = by_date[d]
            out.append({
                "date": d,
                "ev": _avg(b["ev"]) if b["ev"] else None,
                "maxev": max(b["ev"]) if b["ev"] else None,
                "la": _avg(b["la"]) if b["la"] else None,
                "hh": (b["hard"] / len(b["ev"])) if b["ev"] else None,
                "brl": (b["brl"] / len(b["ev"])) if b["ev"] else None,
                "xw": _avg(b["xw"]) if b["xw"] else None,
                "whiff": (b["whiffs"] / b["swings"]) if b["swings"] else None,
                "bat": _avg(b["bat"]) if b["bat"] else None,
                "bbe": len(b["ev"]), "hardhits": b["hard"],
                "barrels": b["brl"], "swings": b["swings"],
                "whiffs": b["whiffs"], "bip": b["bip"],
                "bat_list": b["bat"],
            })
        return out

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

    async def get_sp_form(self, session: aiohttp.ClientSession,
                          pid: int) -> Optional[dict]:
        """Starter recent-form from the pitching game log (cached):
        {'apps': [{date, opp, is_home, started, outs, ip, h, er, k, bb,
                   hr, np, bf}, ...] oldest first,
         'leash': {starts, ip_per_start, np_per_start, pct15, pct18, pct21,
                   days_rest}} — leash computed over starts only."""
        splits = await self.get_game_log(session, pid, "pitching")
        if not splits:
            return None
        apps: List[dict] = []
        for s in splits:
            st = s.get("stat", {})
            outs = st.get("outs")
            if outs is None:
                try:
                    whole, frac = (st.get("inningsPitched") or "0.0").split(".")
                    outs = int(whole) * 3 + int(frac)
                except (ValueError, AttributeError):
                    outs = 0
            opp_id = (s.get("opponent") or {}).get("id")
            apps.append({
                "date": s.get("date", ""),
                "opp": self._teams.get(opp_id, "?"),
                "is_home": bool(s.get("isHome")),
                "started": bool(st.get("gamesStarted")),
                "outs": outs,
                "ip": st.get("inningsPitched") or "",
                "h": st.get("hits"), "er": st.get("earnedRuns"),
                "k": st.get("strikeOuts"), "bb": st.get("baseOnBalls"),
                "hr": st.get("homeRuns"),
                "np": st.get("numberOfPitches"),
                "bf": st.get("battersFaced"),
            })
        starts = [a for a in apps if a["started"]] or apps
        n = len(starts)
        leash = {
            "starts": n,
            "ip_per_start": sum(a["outs"] for a in starts) / n / 3,
            "np_per_start": _avg([a["np"] for a in starts if a["np"]]),
            "pct15": sum(a["outs"] >= 15 for a in starts) / n,
            "pct18": sum(a["outs"] >= 18 for a in starts) / n,
            "pct21": sum(a["outs"] >= 21 for a in starts) / n,
            "days_rest": None,
        }
        try:
            last = datetime.strptime(apps[-1]["date"], "%Y-%m-%d")
            leash["days_rest"] = (datetime.now() - last).days
        except ValueError:
            pass
        return {"apps": apps, "leash": leash}

    _FB_FAMILY = ("4-Seam Fastball", "Sinker")
    _CSW_DESCS = _WHIFF_DESCS | {"called_strike"}

    async def get_sp_statcast_form(self, session: aiohttp.ClientSession,
                                   pid: int) -> dict:
        """Per-start Statcast joins for the SP form table, all off the cached
        pitch detail: {'velo': {date: fb_velo}, 'velo_season': float|None,
        'csw': {date: csw_rate}, 'tto': [{tto, pa, woba, xw, k_pct, hr},
        ...]} — fastball velo prefers 4-Seam/Sinker, falls back to the
        pitcher's hardest pitch type."""
        rows = await self._get_pitch_detail(session, pid, "pitcher")
        fb = [r for r in rows if r["pitch"] in self._FB_FAMILY
              and r["velo"] is not None]
        if not fb:
            by_pitch: Dict[str, List[float]] = {}
            for r in rows:
                if r["velo"] is not None:
                    by_pitch.setdefault(r["pitch"], []).append(r["velo"])
            if by_pitch:
                hardest = max(by_pitch, key=lambda p: _avg(by_pitch[p]))
                fb = [r for r in rows if r["pitch"] == hardest
                      and r["velo"] is not None]
        # Per-start fastball velo + count (the FBv overlay + its "over N FB"
        # tooltip read the fastball family)
        velo_by_date: Dict[str, List[float]] = {}
        fbn_by_date: Dict[str, int] = {}
        for r in fb:
            d = r["date"]
            velo_by_date.setdefault(d, []).append(r["velo"])
            fbn_by_date[d] = fbn_by_date.get(d, 0) + 1
        # Per-start spin PER PITCH TYPE — each becomes its own selectable
        # overlay (FF spin, SL spin, CH spin, …) plus a per-start pitch count
        # for the tooltip ("2336 rpm over 42 FF")
        spin_pitch: Dict[str, Dict[str, List[float]]] = {}
        for r in rows:
            if r.get("spin") is None:
                continue
            pt = PITCH_ABBREV.get(r["pitch"], r["pitch"])
            spin_pitch.setdefault(pt, {}).setdefault(r["date"], []).append(
                r["spin"])
        csw_by_date: Dict[str, List[int]] = {}
        whiff_by_date: Dict[str, List[int]] = {}   # [whiffs, swings]
        xw_by_date: Dict[str, List[float]] = {}    # xwOBA on contact allowed
        bip_by_date: Dict[str, List] = {}          # [evs, hard, brl] allowed
        for r in rows:
            n_csw, n_p = csw_by_date.setdefault(r["date"], [0, 0])
            csw_by_date[r["date"]] = [n_csw + (r["desc"] in self._CSW_DESCS),
                                      n_p + 1]
            if r["desc"] in _SWING_DESCS:
                w = whiff_by_date.setdefault(r["date"], [0, 0])
                w[1] += 1
                if r["desc"] in _WHIFF_DESCS:
                    w[0] += 1
            if r["xwoba"] is not None:
                xw_by_date.setdefault(r["date"], []).append(r["xwoba"])
            if r["ev"] is not None:
                bb = bip_by_date.setdefault(r["date"], [[], 0, 0])
                bb[0].append(r["ev"])
                bb[1] += r["ev"] >= 95
                if r["la"] is not None:
                    bb[2] += _is_barrel(r["ev"], r["la"])
        # Damage splits: times-through-order + platoon (vs LHB/RHB), same
        # per-PA aggregation off the event-ending rows
        def _bin():
            return {"pa": 0, "k": 0, "bb": 0, "hr": 0, "wv": 0.0, "wd": 0.0,
                    "xw": [], "ev": [], "hard": 0, "brl": 0, "bip": 0}

        tto_bins: Dict[int, dict] = {}
        hand_bins: Dict[str, dict] = {}
        for r in rows:
            if not r["event"]:
                continue
            bins = [tto_bins.setdefault(min(int(r["tto"] or 1), 3), _bin())]
            if r.get("stand") in ("L", "R"):
                bins.append(hand_bins.setdefault(r["stand"], _bin()))
            for b in bins:
                b["pa"] += 1
                b["k"] += r["event"] == "strikeout"
                b["bb"] += r["event"] == "walk"
                b["hr"] += bool(r["hr"])
                if r["woba_v"] is not None and r["woba_d"]:
                    b["wv"] += r["woba_v"]
                    b["wd"] += r["woba_d"]
                if r["xwoba"] is not None:
                    b["xw"].append(r["xwoba"])
                if r["ev"] is not None:
                    b["bip"] += 1
                    b["ev"].append(r["ev"])
                    b["hard"] += r["ev"] >= 95
                    if r["la"] is not None:
                        b["brl"] += _is_barrel(r["ev"], r["la"])

        def _row(label, b):
            return {
                "label": label, "pa": b["pa"],
                "woba": (b["wv"] / b["wd"]) if b["wd"] else None,
                "xw": _avg(b["xw"]) if b["xw"] else None,
                "k_pct": b["k"] / b["pa"], "bb_pct": b["bb"] / b["pa"],
                "hr": b["hr"],
                "ev": _avg(b["ev"]) if b["ev"] else None,
                "hh": (b["hard"] / b["bip"]) if b["bip"] else None,
                "brl": (b["brl"] / b["bip"]) if b["bip"] else None,
            }

        tto = [_row(f"{t}{'+' if t == 3 else ''}", tto_bins[t])
               for t in sorted(tto_bins)]
        tto += [_row(f"vs {h}", hand_bins[h]) for h in ("L", "R")
                if h in hand_bins]
        all_velo = [v for vs in velo_by_date.values() for v in vs]
        return {
            "velo": {d: _avg(vs) for d, vs in velo_by_date.items()},
            "velo_season": _avg(all_velo) if all_velo else None,
            "fbn": dict(fbn_by_date),   # fastball count per start (tooltip)
            # {pitch_abbrev: {date: avg_spin}} and matching per-start counts
            "spin_by_pitch": {pt: {d: _avg(vs) for d, vs in dd.items()}
                              for pt, dd in spin_pitch.items()},
            "spinn_by_pitch": {pt: {d: len(vs) for d, vs in dd.items()}
                               for pt, dd in spin_pitch.items()},
            "csw": {d: (c / p if p else None)
                    for d, (c, p) in csw_by_date.items()},
            "whiff": {d: (w / s if s else None)
                      for d, (w, s) in whiff_by_date.items()},
            "xw": {d: _avg(vs) for d, vs in xw_by_date.items()},
            "ev": {d: _avg(evs) for d, (evs, _h, _b) in bip_by_date.items()},
            "hh": {d: h / len(evs)
                   for d, (evs, h, _b) in bip_by_date.items()},
            "brl": {d: b / len(evs)
                    for d, (evs, _h, b) in bip_by_date.items()},
            "tto": tto,
        }

    async def get_sp_movement(self, session: aiohttp.ClientSession,
                              pid: int) -> dict:
        """Per-pitch-type movement + plate location for the SP shape plots,
        off the cached pitch detail: {'pitches': [{pitch, n, velo,
        mean_hb, mean_ivb, mv: [(hb_in, ivb_in), ...], loc: [(px_ft,
        pz_ft), ...]}, ...] usage-sorted, 'sz_top': ft, 'sz_bot': ft}.
        hb/ivb are pfx_x/pfx_z in inches (catcher's view); samples are
        thinned to ~120 points per pitch."""
        rows = await self._get_pitch_detail(session, pid, "pitcher")
        KIN = ("release_pos_x", "release_pos_y", "release_pos_z",
               "vx0", "vy0", "vz0", "ax", "ay", "az")
        by_pitch: Dict[str, dict] = {}
        sz_t: List[float] = []
        sz_b: List[float] = []
        for r in rows:
            if r.get("sz_top"):
                sz_t.append(r["sz_top"])
            if r.get("sz_bot"):
                sz_b.append(r["sz_bot"])
            if r.get("pfx_x") is None or r.get("pfx_z") is None:
                continue
            b = by_pitch.setdefault(r["pitch"], {"mv": [], "loc": [],
                                                 "velo": [], "kin": []})
            b["mv"].append((r["pfx_x"] * 12, r["pfx_z"] * 12))
            if r["velo"] is not None:
                b["velo"].append(r["velo"])
            if r.get("plate_x") is not None and r.get("plate_z") is not None:
                b["loc"].append((r["plate_x"], r["plate_z"]))
            if all(r.get(k) is not None for k in KIN):
                b["kin"].append([r[k] for k in KIN])
        pitches = []
        for name, b in sorted(by_pitch.items(), key=lambda kv: -len(kv[1]["mv"])):
            mv, loc = b["mv"], b["loc"]
            step = max(1, len(mv) // 120)
            # Mean Hawk-Eye kinematics = the pitcher's representative pitch
            # of this type, ready for savant_pitch_trajectory()
            kin = None
            if b["kin"]:
                kin = {k: _avg([row[i] for row in b["kin"]])
                       for i, k in enumerate(KIN)}
            pitches.append({
                "pitch": name, "n": len(mv),
                "velo": _avg(b["velo"]) if b["velo"] else None,
                "mean_hb": _avg([m[0] for m in mv]),
                "mean_ivb": _avg([m[1] for m in mv]),
                "mv": mv[::step], "loc": loc[::max(1, len(loc) // 120)],
                "kin": kin,
            })
        return {"pitches": pitches,
                "sz_top": _avg(sz_t) if sz_t else 3.4,
                "sz_bot": _avg(sz_b) if sz_b else 1.6}

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
                dc = dev_cache_get(f"itp_page_{pid}")
                if dc is not None:
                    page = json.loads(dc)
                else:
                    loop = asyncio.get_running_loop()
                    page = await loop.run_in_executor(
                        None, fetch_reliever_page_sync, pid, rec.get("href"))
                    dev_cache_put(f"itp_page_{pid}", json.dumps(page))
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
        text = dev_cache_get(url)
        if text is None:
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
            dev_cache_put(url, text)
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
                    try:
                        ab = int(bvp["atBats"])
                        avg = f" (.{round(int(bvp.get('hits', 0)) / ab * 1000):03d})" if ab else ""
                    except (TypeError, ValueError, ZeroDivisionError):
                        avg = ""
                    xbh = sum(int(bvp.get(k) or 0)
                              for k in ("doubles", "triples", "homeRuns"))
                    bits = [f"BvP {bvp.get('hits')}-{bvp.get('atBats')}{avg}"]
                    if xbh:
                        bits.append(f"{xbh} XBH")
                    bits.append(f"{bvp.get('homeRuns')} HR")
                    if bvp.get("baseOnBalls"):
                        bits.append(f"{bvp['baseOnBalls']} BB")
                    bits.append(f"{bvp.get('strikeOuts')} K")
                    ctx.bvp = " · ".join(bits)

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
from PyQt6.QtCore import (Qt, QUrl, QRect, QRectF, QEvent, QPoint, QPointF,
                          QSize, pyqtSignal, QTimer)
from PyQt6.QtGui import (QColor, QFont, QPixmap, QPainter, QPainterPath,
                         QIcon, QAction)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QGridLayout, QComboBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QScrollArea, QPushButton,
    QMenu, QToolButton,
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

class _StayOpenMenu(QMenu):
    """QMenu whose checkable actions toggle in place instead of closing the
    menu — lets the user flip several overlays on/off in one visit."""

    def mouseReleaseEvent(self, e):
        act = self.actionAt(e.position().toPoint())
        if act is not None and act.isCheckable():
            act.setChecked(not act.isChecked())
            e.accept()
            return
        super().mouseReleaseEvent(e)


class StatMenuButton(QToolButton):
    """Compact 'Stats ▾' button that floats in a plot corner and opens a
    stay-open menu of checkable, color-swatched overlay entries. The
    QActions in .acts are drop-in isChecked()/setChecked()/toggled
    replacements for the old chip-row QPushButtons."""

    def __init__(self, overlays, parent=None):
        super().__init__(parent)
        self.setText("Stats ▾")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QToolButton { color: #BDC3C7; background: transparent;"
            " border: none; font-size: 8pt; font-weight: bold;"
            " padding: 0px 4px; }"
            "QToolButton::menu-indicator { image: none; }")
        menu = _StayOpenMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #1E2A38; border: 1px solid #34495E;"
            " color: #D5DBDB; font-size: 8pt; }"
            "QMenu::item { padding: 2px 14px 2px 4px; }"
            "QMenu::item:selected { background: #2C3E50; }")
        self.acts: Dict[str, QAction] = {}
        for key, label, color in overlays:
            act = QAction(label, menu)
            act.setCheckable(True)
            swatch = QPixmap(10, 10)
            swatch.fill(QColor(color))
            act.setIcon(QIcon(swatch))
            menu.addAction(act)
            self.acts[key] = act
        self.setMenu(menu)


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

# Savant-style pitch-type colors (keyed by abbrev code), tuned for the dark
# background — used by the SP movement/zone plots
PITCH_COLORS = {
    "FF": "#E4455A", "FT": "#F0743E", "FA": "#E4455A", "SI": "#F79420",
    "FC": "#B8703F", "SL": "#F5E626", "ST": "#C4D941", "SV": "#9C6ADE",
    "CU": "#2FBBE8", "KC": "#7B5BE6", "CS": "#2FBBE8", "CH": "#35C75A",
    "FS": "#4FB3AE", "FO": "#4FB3AE", "SC": "#35C75A", "KN": "#95A5A6",
    "EP": "#95A5A6",
}
_PITCH_COLOR_DEFAULT = "#95A5A6"

# Approx MLB league-average release spin (RPM) by pitch type — reference for
# the arsenal card's spin delta (a pitcher's spin vs league for that pitch).
LEAGUE_SPIN = {
    "FF": 2310, "FT": 2170, "FA": 2270, "SI": 2150, "FC": 2410,
    "SL": 2430, "ST": 2520, "SV": 2500, "CU": 2570, "KC": 2650,
    "CS": 2400, "CH": 1780, "FS": 1420, "FO": 1450, "SC": 1900,
    "KN": 1400, "EP": 1500,
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

    # Trend-plot overlay series: key into get_per_game_statcast rows
    _OVERLAYS = [
        ("xw",    "xwOBAcon", "#9B59B6"),
        ("ev",    "EV",       "#E67E22"),
        ("maxev", "MaxEV",    "#2ECC71"),
        ("la",    "LA",       "#FF6FB5"),
        ("hh",    "HH%",      "#E74C3C"),
        ("brl",   "Brl%",     "#F1C40F"),
        ("whiff", "Whiff%",   "#1ABC9C"),
        ("bat",   "BatSpd",   "#5DADE2"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct_hitters = None      # pandas DataFrames, set lazily
        self._pct_pitchers = None
        self._summary: Optional[PropStatSummary] = None
        self._updating = False        # guard: programmatic control updates
        self._splits = None
        self._velo_splits = None
        self._splits_type = None
        self._situ_data = None
        self._show_all_pitches = False   # matchup-table expander state
        self._pitch_toggle_row = -1      # in-table expander row index
        self._pg_statcast = None         # per-game statcast (trend overlays)
        self._trend_games = []           # games behind the current trend x-axis
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
            #tradLabel { color: #7F8C8D; font-size: 6pt; }
            #tradValue { color: #ECF0F1; font-size: 8pt; font-weight: bold; }
            #bannerReadout { color: #95A5A6; font-size: 9pt; }
            #trendHover { color: #95A5A6; font-size: 8pt; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 6, 2, 2)
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

        # -- header: LEFT block = [headshot | name+stats] on top with the
        #    selector row filling the full width below it; matchup strip right
        head_row = QHBoxLayout()
        head_row.setSpacing(8)

        header_left = QVBoxLayout()
        header_left.setSpacing(2)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self._headshot = QLabel()
        self._headshot.setFixedSize(56, 56)
        self._headshot.setScaledContents(True)
        top_row.addWidget(self._headshot,
                          alignment=Qt.AlignmentFlag.AlignTop)

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
        # condensed traditional season line (G/PA/AVG/... or W-L/ERA/...)
        # sits directly under the name, beside the headshot
        trad_holder = QWidget()
        self._trad_grid = QGridLayout(trad_holder)
        self._trad_grid.setContentsMargins(0, 2, 0, 0)
        self._trad_grid.setHorizontalSpacing(10)
        self._trad_grid.setVerticalSpacing(0)
        self._trad_grid.setAlignment(Qt.AlignmentFlag.AlignTop
                                     | Qt.AlignmentFlag.AlignLeft)
        ident.addWidget(trad_holder)
        ident.addStretch()
        top_row.addLayout(ident)
        top_row.addStretch()
        header_left.addLayout(top_row)

        # stat/line/window/filter selectors — ONE row spanning the full width
        # below the headshot + stat grid. (The old Trends/Spray-chart combo is
        # gone; the spray-chart toggle now lives in the plot's Stats ▾ menu.)
        sel_row = QHBoxLayout()
        sel_row.setContentsMargins(0, 1, 0, 0)
        sel_row.setSpacing(4)
        sel_row.addWidget(QLabel("Stat:"))
        self._stat_combo = QComboBox()
        # keep the combo compact even when a market name is long — it elides
        # rather than stretching the row
        self._stat_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._stat_combo.setMinimumContentsLength(7)
        self._stat_combo.currentIndexChanged.connect(self._on_stat_controls_changed)
        sel_row.addWidget(self._stat_combo)
        sel_row.addWidget(QLabel("Line:"))
        self._line_spin = QDoubleSpinBox()
        self._line_spin.setRange(0.0, 500.0)
        self._line_spin.setSingleStep(0.5)
        self._line_spin.setDecimals(1)
        self._line_spin.valueChanged.connect(self._on_stat_controls_changed)
        sel_row.addWidget(self._line_spin)
        # chart window / venue filter (client-side re-slice of the cached log)
        self._chart_window_combo = QComboBox()
        self._chart_window_combo.addItem("L15", 15)
        self._chart_window_combo.addItem("L30", 30)
        self._chart_window_combo.addItem("Season", 0)
        self._chart_window_combo.currentIndexChanged.connect(
            self._on_chart_view_changed)
        sel_row.addWidget(self._chart_window_combo)
        self._chart_filter_combo = QComboBox()
        self._chart_filter_combo.addItem("All", "all")
        self._chart_filter_combo.addItem("Home", "home")
        self._chart_filter_combo.addItem("Road", "road")
        self._chart_filter_combo.currentIndexChanged.connect(
            self._on_chart_view_changed)
        sel_row.addWidget(self._chart_filter_combo)
        sel_row.addStretch()
        header_left.addLayout(sel_row)
        head_row.addLayout(header_left)

        # -- matchup strip: game/park/weather + opp SP + swing chips. Rides
        #    in the header's right (vacated) space, starting right after the
        #    stat grid; the selector row sits on its own line below.
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
        # Swing-tracking chip row (label-over-value badges, replaces the old
        # printed text line). Ignored horizontal policy so a long chip row
        # never sets the panel's minimum width.
        self._swing_row = QWidget()
        self._swing_row.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        # Grid (4 chips/row) rather than one long row — wraps to two lines so
        # the badges fit the narrower header slot instead of clipping.
        self._swing_lay = QGridLayout(self._swing_row)
        self._swing_lay.setContentsMargins(0, 3, 0, 1)
        self._swing_lay.setHorizontalSpacing(12)
        self._swing_lay.setVerticalSpacing(2)
        self._swing_row.hide()
        matchup_lay.addWidget(self._swing_row)
        self._matchup_frame.hide()
        head_row.addWidget(self._matchup_frame, stretch=1,
                           alignment=Qt.AlignmentFlag.AlignTop)
        content_lay.addLayout(head_row)

        # -- compact hit-rate banner (replaces the old tall bar chart):
        #    over-rate readout on the left, mini over/under bars beside it
        pg.setConfigOptions(antialias=True)
        self._banner = pg.PlotWidget(background="#151a21")
        self._banner.hideAxis("bottom")
        self._banner.hideAxis("left")
        self._banner.getPlotItem().getViewBox().setMouseEnabled(x=False,
                                                                y=False)
        self._banner.setMenuEnabled(False)
        self._banner.hideButtons()
        self._banner.setFixedHeight(56)
        # Small minimum so the left column can shrink and never force the
        # flank tables off-screen on a narrow panel
        self._banner.setMinimumWidth(140)
        # The Over x/x · line readout rides inside the banner's top-left
        # corner (over the oldest bars) instead of a separate side label
        self._banner_text = pg.TextItem(anchor=(0, 0),
                                        fill=pg.mkBrush(21, 26, 33, 190))
        self._banner_text.setZValue(50)

        # Right flank of the chart: percentile stack with the analysis
        # tables stacked underneath — keeps the left column free for the
        # banner + trend plot
        chart_row = QHBoxLayout()
        chart_row.setSpacing(4)
        # Left column: banner strip on top, trend plot / spray chart below —
        # everything left of the flank tables
        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        left_col.addWidget(self._banner)
        chart_row.addLayout(left_col, stretch=1)
        # No minimum width: the flank hugs its widest table so the plot
        # (stretch=1) claims every spare pixel to its left
        flank = QWidget()
        flank_lay = QVBoxLayout(flank)
        flank_lay.setContentsMargins(0, 2, 0, 0)
        flank_lay.setSpacing(6)
        pct_holder = QWidget()
        self._pct_grid = QGridLayout(pct_holder)
        self._pct_grid.setContentsMargins(0, 0, 0, 0)
        self._pct_grid.setHorizontalSpacing(0)
        self._pct_grid.setVerticalSpacing(0)
        self._pct_grid.setAlignment(Qt.AlignmentFlag.AlignBottom)
        # Percentile stack and the situational-splits table side by side,
        # bottoms level; the percentiles stretch into whatever width the
        # split table leaves over
        self._tbl_situ = self._make_stats_table()
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(pct_holder, stretch=1)
        top_row.addWidget(self._tbl_situ,
                          alignment=Qt.AlignmentFlag.AlignBottom
                          | Qt.AlignmentFlag.AlignRight)
        flank_lay.addLayout(top_row)
        self._tbl_velo = self._make_stats_table()
        self._tbl_pitch = self._make_stats_table()   # merged SP-mix + splits
        for tbl in (self._tbl_velo, self._tbl_pitch):
            flank_lay.addWidget(tbl,
                                alignment=Qt.AlignmentFlag.AlignTop
                                | Qt.AlignmentFlag.AlignRight)
        # Pitch-type expander lives INSIDE the matchup table as its last
        # (spanned) row — clicking it toggles the out-of-arsenal pitches
        self._tbl_pitch.cellClicked.connect(self._on_pitch_cell_clicked)
        flank_lay.addStretch()
        chart_row.addWidget(flank)
        content_lay.addLayout(chart_row, stretch=1)

        # -- trend page: the big analytical plot. Rolling average of the
        #    prop stat (left axis) with toggleable rolling Statcast overlays
        #    on a second right-hand ViewBox.
        trend_page = QWidget()
        trend_lay = QVBoxLayout(trend_page)
        trend_lay.setContentsMargins(0, 0, 0, 0)
        trend_lay.setSpacing(2)

        self._trend_plot = pg.PlotWidget(background="#151a21")
        self._trend_plot.showGrid(x=False, y=False)
        self._trend_plot.getPlotItem().getViewBox().setMouseEnabled(
            x=False, y=False)
        self._trend_plot.setMenuEnabled(False)
        self._trend_plot.hideButtons()
        self._trend_plot.setMinimumWidth(160)
        # Condensed: bottom of the plot lands level with the Velo Band
        # table's bottom instead of running the full panel height
        self._trend_plot.setMaximumHeight(365)
        # Taller bottom axis: 2-line date ticks up top, a clear band beneath
        # for the overlay legend that floats there (_trend_bottom_legend)
        self._trend_plot.getPlotItem().getAxis("bottom").setHeight(58)

        # Roll selector + stat chips ride INSIDE the plot (top-right, same
        # pattern as the SP form plot's chip bar) — floating child widget
        # repositioned via eventFilter on every plot resize
        self._trend_chip_bar = QWidget(self._trend_plot)
        self._trend_chip_bar.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True)
        self._trend_chip_bar.setStyleSheet(
            "background: rgba(21, 26, 33, 175); border-radius: 3px;")
        chips_row = QHBoxLayout(self._trend_chip_bar)
        chips_row.setContentsMargins(4, 0, 2, 0)
        chips_row.setSpacing(2)
        # Just the 'Stats ▾' dropdown up here — the active-overlay legend now
        # rides UNDER the x-axis (_trend_bottom_legend), where there's room
        self._trend_stat_menu = StatMenuButton(self._OVERLAYS)
        self._overlay_btns = self._trend_stat_menu.acts
        for act in self._overlay_btns.values():
            act.toggled.connect(self._on_chart_view_changed)
        chips_row.addWidget(self._trend_stat_menu)
        self._overlay_btns["xw"].setChecked(True)
        # Spray-chart toggle: one shared checkable action added to BOTH plots'
        # Stats ▾ menus, so it can be flipped from the trend view or the spray
        # view (a QAction can belong to multiple menus, sharing one state).
        self._spray_action = QAction("Spray chart", self)
        self._spray_action.setCheckable(True)
        self._spray_action.toggled.connect(self._on_bottom_view_toggle)
        trend_menu = self._trend_stat_menu.menu()
        trend_menu.addSeparator()
        trend_menu.addAction(self._spray_action)
        self._trend_plot.installEventFilter(self)
        # The prop-stat bars live on the MAIN viewbox but need no axis (the
        # value sits inside each bar). Both side axes are repurposed for the
        # advanced overlays: two extra viewboxes, one per side axis, so two
        # overlays can each keep a real-unit scale (true dual axis).
        p = self._trend_plot.getPlotItem()
        p.getViewBox().setDefaultPadding(0)
        # Bars sit on the main viewbox (low z); overlay viewboxes are lifted
        # above it so their lines always render ON TOP of the bars.
        p.getViewBox().setZValue(0)
        self._left_vb = pg.ViewBox()
        self._left_vb.setMouseEnabled(x=False, y=False)
        self._left_vb.setZValue(20)
        p.scene().addItem(self._left_vb)
        p.getAxis("left").linkToView(self._left_vb)
        self._left_vb.setXLink(p.getViewBox())
        self._right_vb = pg.ViewBox()
        self._right_vb.setMouseEnabled(x=False, y=False)
        self._right_vb.setZValue(20)
        p.scene().addItem(self._right_vb)
        p.getAxis("right").linkToView(self._right_vb)
        self._right_vb.setXLink(p.getViewBox())
        p.getViewBox().sigResized.connect(self._sync_overlay_geom)
        p.showAxis("left", False)
        p.showAxis("right", False)
        # Crosshair scrub: vertical line + floating readout inside the plot
        # (NOT a layout widget — so hovering never triggers a relayout)
        self._trend_vline = pg.InfiniteLine(
            angle=90, pen=pg.mkPen(150, 150, 155, 130))
        self._trend_vline.hide()
        # Opaque fill + TOP-LEVEL scene item: the overlay viewboxes sit at
        # scene z=20 (above the whole plotItem), so the readout must live
        # directly in the scene to render above the lines it describes
        self._trend_htext = pg.TextItem(
            anchor=(0, 0), color=(225, 225, 230),
            border=pg.mkPen("#34495E"),
            fill=pg.mkBrush(18, 24, 31))
        self._trend_htext.hide()
        self._trend_htext.setZValue(100)
        self._trend_plot.scene().addItem(self._trend_htext)
        self._trend_plot.scene().sigMouseMoved.connect(self._on_trend_mouse)
        trend_lay.addWidget(self._trend_plot, stretch=1)
        # Active-overlay legend + scale/rolling note: a floating child INSIDE
        # the plot, in the empty band under the x-axis date ticks (moved off
        # the top-left corner where it crowded the bars). Positioned in
        # _place_trend_chip_bar on every plot resize.
        self._trend_bottom_legend = QLabel("", self._trend_plot)
        self._trend_bottom_legend.setObjectName("trendHover")
        self._trend_bottom_legend.setTextFormat(Qt.TextFormat.RichText)
        self._trend_bottom_legend.setStyleSheet("background: transparent;")
        self._trend_bottom_legend.hide()
        # Height-capped plot pins to the top; spare page height pools below
        trend_lay.addStretch(1)

        # spray page: bare plot — legend rendered as the plot's own title.
        # Height-capped + trailing stretch like the trend page, so toggling
        # spray on occupies the SAME vertical band (not the full window).
        self._spray_plot = pg.PlotWidget(background="#151a21")
        self._spray_plot.setAspectLocked(True)
        self._spray_plot.hideAxis("bottom")
        self._spray_plot.hideAxis("left")
        self._spray_plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
        self._spray_plot.setMenuEnabled(False)
        self._spray_plot.hideButtons()
        self._spray_plot.setMaximumHeight(365)
        # Floating Stats ▾ (top-right) carrying the shared spray action, so the
        # user can toggle back to Trends from the spray view; repositioned via
        # eventFilter on spray-plot resize.
        self._spray_menu_btn = QToolButton(self._spray_plot)
        self._spray_menu_btn.setText("Stats ▾")
        self._spray_menu_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._spray_menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._spray_menu_btn.setStyleSheet(
            "QToolButton { color: #BDC3C7; background: rgba(21,26,33,175);"
            " border-radius: 3px; font-size: 8pt; font-weight: bold;"
            " padding: 1px 4px; }"
            "QToolButton::menu-indicator { image: none; }")
        _spray_menu = _StayOpenMenu(self._spray_menu_btn)
        _spray_menu.setStyleSheet(
            "QMenu { background: #1E2A38; border: 1px solid #34495E;"
            " color: #D5DBDB; font-size: 8pt; }"
            "QMenu::item { padding: 2px 14px 2px 4px; }"
            "QMenu::item:selected { background: #2C3E50; }")
        _spray_menu.addAction(self._spray_action)
        self._spray_menu_btn.setMenu(_spray_menu)
        self._spray_plot.installEventFilter(self)

        spray_page = QWidget()
        spray_lay = QVBoxLayout(spray_page)
        spray_lay.setContentsMargins(0, 0, 0, 0)
        spray_lay.setSpacing(2)
        spray_lay.addWidget(self._spray_plot, stretch=1)
        spray_lay.addStretch(1)

        from PyQt6.QtWidgets import QStackedWidget
        self._splits_stack = QStackedWidget()
        self._splits_stack.addWidget(trend_page)
        self._splits_stack.addWidget(spray_page)
        left_col.addWidget(self._splits_stack, stretch=1)

    def eventFilter(self, obj, ev):
        if obj is self._trend_plot:
            if ev.type() == QEvent.Type.Resize:
                self._place_trend_chip_bar()
            elif ev.type() == QEvent.Type.Leave:
                self._trend_vline.hide()
                self._trend_htext.hide()
        elif obj is self._spray_plot and ev.type() == QEvent.Type.Resize:
            self._place_spray_menu_btn()
        return super().eventFilter(obj, ev)

    def _place_spray_menu_btn(self):
        btn = self._spray_menu_btn
        btn.adjustSize()
        btn.move(max(0, self._spray_plot.width() - btn.width() - 6), 3)
        btn.raise_()

    def _place_trend_chip_bar(self):
        bar = self._trend_chip_bar
        bar.adjustSize()
        # Top-right, inside the plot frame but clear of the right axis
        bar.move(max(0, self._trend_plot.width() - bar.width() - 44), 3)
        bar.raise_()
        # Overlay legend rides in the band under the x-axis ticks, bottom-left
        leg = self._trend_bottom_legend
        leg.adjustSize()
        leg.move(6, max(0, self._trend_plot.height() - leg.height() - 2))
        leg.raise_()

    def _set_trend_note(self, html: str):
        # Legend/scale note floats in the band under the x-axis ticks
        lbl = self._trend_bottom_legend
        lbl.setText(html)
        lbl.adjustSize()
        lbl.setVisible(bool(html))
        self._place_trend_chip_bar()

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
        """Fix the HEIGHT to content and record the natural (full-content)
        width. The actual width is set by _sync_flank_widths, which caps it
        to the space available so wide tables scroll internally instead of
        running off the right of the screen."""
        # Reset any stretch mode from a previous width-sync so the natural
        # content width measures true
        t.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        t.resizeColumnsToContents()
        h = t.horizontalHeader().height() + 2 * t.frameWidth()
        h += sum(t.rowHeight(r) for r in range(t.rowCount()))
        # Scrollbar room is added by _sync_flank_widths ONLY when it caps
        # the width — reserving it unconditionally left a ghost empty row
        # at the bottom of every non-scrolling table
        t._natural_h = max(h, 42)
        t.setFixedHeight(t._natural_h)
        w = 2 * t.frameWidth() + 2
        w += sum(t.columnWidth(c) for c in range(t.columnCount()))
        t._natural_w = w
        t.setFixedWidth(w)

    # Width the percentile stack gets beside the split table (bars scale
    # to fit, no distortion — paintEvent works off the live width).
    # 134: at 150 the splits table came up ~15px short and grew a needless
    # internal h-scrollbar
    _PCT_STACK_W = 134
    _PLOT_RESERVE = 272   # min width kept for the plot beside the flank
    _FLANK_MARGIN = 20

    def _available_width(self) -> int:
        """Width the panel actually has to lay out in. When wrapped in a
        QScrollArea (as in the props window) the parent is the scroll
        viewport, whose width is the real budget — self.width() would be the
        already-overgrown content width and useless for capping."""
        p = self.parentWidget()
        w = p.width() if p is not None else self.width()
        return w if w > 50 else 0

    def _sync_flank_widths(self):
        """Give the stacked flank tables (Velo/Matchup) one shared width so
        their left edges line up flush with the plot — but cap that width to
        what fits beside a usable plot. Over-wide tables then scroll
        internally instead of clipping off the right of the screen."""
        velo_n = getattr(self._tbl_velo, "_natural_w", 0)
        match_n = getattr(self._tbl_pitch, "_natural_w", 0)
        situ_n = getattr(self._tbl_situ, "_natural_w", 0)
        top_w = (situ_n + self._PCT_STACK_W + 8) if situ_n else 0
        desired = max(velo_n, match_n, top_w)
        if not desired:
            return
        avail = self._available_width()
        if avail:
            cap = max(260, avail - self._PLOT_RESERVE - self._FLANK_MARGIN)
            target = min(desired, cap)
        else:
            target = desired
        self._flank_target = target

        def apply(t, width):
            t.setFixedWidth(width)
            # Height gets the h-scrollbar allowance only when this width
            # actually truncates the content (the bar will show)
            nat_h = getattr(t, "_natural_h", 0)
            if nat_h:
                sb = (t.horizontalScrollBar().sizeHint().height()
                      if width < getattr(t, "_natural_w", 0) else 0)
                t.setFixedHeight(nat_h + sb)

        for t, nat in ((self._tbl_velo, velo_n), (self._tbl_pitch, match_n)):
            if not nat:
                continue
            apply(t, target)
            t.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Stretch)
        # Situational shares the top row with the percentile stack; keep the
        # stack at least _PCT_STACK_W, let the table scroll if space is tight
        if situ_n:
            apply(self._tbl_situ,
                  min(situ_n, max(160, target - self._PCT_STACK_W - 8)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-cap the flank when the panel (splitter/monitor) changes width.
        # Cheap (setFixedWidth only); the chart re-thins its own ticks on the
        # next combo change, so no heavy re-render on every drag pixel.
        if getattr(self, "_tbl_velo", None) is not None:
            self._sync_flank_widths()

    def _on_bottom_view_toggle(self, *_):
        spray = self._spray_action.isChecked()
        self._splits_stack.setCurrentIndex(1 if spray else 0)
        if spray:
            self._render_spray()

    # ---------------------------------------------------- traditional line

    def _clear_traditional(self):
        while self._trad_grid.count():
            item = self._trad_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # 7/row (was 12): at 12 the header row's minimum width alone outgrew the
    # scroll viewport once the async season line landed, h-scrolling the
    # whole panel and clipping every flank table at the right edge; 7 keeps
    # the detail min ~782 so the widened SP splits table fits beside it
    _TRAD_PAIRS_PER_ROW = 7

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

    # -------------------------------------------------- situational splits

    _SITU_LABELS_BAT = {"h": "Home", "a": "Road", "d": "Day", "n": "Night",
                        "vl": "vs LHP", "vr": "vs RHP", "risp": "RISP"}
    _SITU_LABELS_PIT = {"h": "Home", "a": "Road", "d": "Day", "n": "Night",
                        "vl": "vs LHB", "vr": "vs RHB", "risp": "RISP"}

    def show_situational(self, splits: Dict[str, dict], group: str):
        """Season situational splits (Home/Road, Day/Night, vs L/R) from
        MLBPropStats.get_situational_splits."""
        self._situ_data = (splits, group)
        self._render_situ()

    def _render_situ(self):
        table = self._tbl_situ
        table.setRowCount(0)
        if not self._situ_data:
            return
        splits, group = self._situ_data
        if not splits:
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(["Situational — no split data"])
            self._fit_table(table)
            return

        cell = self._cell
        hitting = (group == "hitting")
        labels = self._SITU_LABELS_BAT if hitting else self._SITU_LABELS_PIT

        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        if hitting:
            headers = ["Split", "PA", "AVG", "OBP", "SLG", "OPS",
                       "HR", "XBH", "SB", "BB%", "K%"]
        else:
            headers = ["Split", "BF", "IP", "ERA", "WHIP",
                       "K", "BB", "HR", "AVGa", "OPSa"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setToolTip("Season splits: venue, time of day, platoon")

        # Which rows describe today's game (from the matchup context)
        today_codes = set()
        ctx = self._last_ctx
        if ctx is not None:
            today_codes.add("h" if ctx.is_home else "a")
            if hitting and ctx.opp_pitcher_hand in ("L", "R"):
                today_codes.add("vl" if ctx.opp_pitcher_hand == "L" else "vr")

        highlight_bg = QColor(24, 42, 58)     # subtle blue row tint
        r = 0
        for code in MLBPropStats.SITU_CODES:
            s = splits.get(code)
            if not s:
                continue
            table.insertRow(r)
            if hitting:
                pa = fnum(s.get("plateAppearances")) or 0
                xbh = sum(int(s.get(k) or 0)
                          for k in ("doubles", "triples", "homeRuns"))
                vals = [str(s.get("plateAppearances") or ""),
                        s.get("avg") or "", s.get("obp") or "",
                        s.get("slg") or "", s.get("ops") or "",
                        str(s.get("homeRuns") or 0), str(xbh),
                        str(s.get("stolenBases") or 0),
                        f"{(fnum(s.get('baseOnBalls')) or 0) / pa:.0%}" if pa else "",
                        f"{(fnum(s.get('strikeOuts')) or 0) / pa:.0%}" if pa else ""]
            else:
                vals = [str(s.get("battersFaced") or ""),
                        s.get("inningsPitched") or "",
                        s.get("era") or "", s.get("whip") or "",
                        str(s.get("strikeOuts") or 0),
                        str(s.get("baseOnBalls") or 0),
                        str(s.get("homeRuns") or 0),
                        s.get("avg") or "", s.get("ops") or ""]
            name_cell = cell(labels.get(code, code), align_right=False)
            row_cells = [name_cell] + [cell(v) for v in vals]
            is_today = code in today_codes
            for c, item in enumerate(row_cells):
                if is_today:
                    item.setBackground(highlight_bg)
                    if c == 0:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                table.setItem(r, c, item)
            r += 1
        table.resizeRowsToContents()
        self._fit_table(table)
        self._sync_flank_widths()

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
        # Re-render the situational table: today-row highlighting depends on
        # this context and the two fetches race each other
        if self._situ_data:
            self._render_situ()

    # --------------------------------------------------------- pitch splits

    def set_pitch_splits_loading(self):
        self._pitch_toggle_row = -1
        self._tbl_pitch.clearSpans()
        for tbl, name in ((self._tbl_pitch, "Matchup"),
                          (self._tbl_velo, "Velo Bands"),
                          (self._tbl_situ, "Situational")):
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
        self._situ_data = None
        self._pg_statcast = None
        self._arsenal = None          # opposing SP arsenal (batter view)
        self._arsenal_pitcher = None
        self._arsenal_stuff = None
        self._sp_card = None
        self._sp_card_name = ""
        self._spray_points = None
        self._swing_row.hide()

    def set_spray(self, points: List[tuple]):
        """Season spray points [(x_ft, y_ft, cat)] for the shown player."""
        self._spray_points = points
        if self._spray_action.isChecked():
            self._render_spray()

    def show_swing(self, fgb: Optional[dict]):
        """Swing-tracking chip row under the matchup strip (batters):
        label-over-value badges with hot/cold coloring."""
        lay = self._swing_lay
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not fgb or fgb.get("bat_speed") is None:
            self._swing_row.hide()
            return

        def as_num(v):
            return None if v is None else (v * 100 if v <= 1 else v)

        chips = []

        def chip(label, value, unit="", color="#82C4E0"):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            t = QLabel(label)
            t.setStyleSheet("color: #7F8C8D; font-size: 6pt;"
                            " background: transparent;")
            val = QLabel(
                f"<span style='color:{color}; font-size:9pt;"
                f" font-weight:bold;'>{value}</span>"
                + (f"<span style='color:#7F8C8D; font-size:7pt;'>"
                   f" {unit}</span>" if unit else ""))
            val.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(t)
            v.addWidget(val)
            chips.append(w)

        def heat(v, hi, lo):
            return ("#2ECC71" if v >= hi else
                    "#E74C3C" if v <= lo else "#82C4E0")

        bs = fgb["bat_speed"]
        chip("BAT SPEED", f"{bs:.1f}", "mph", heat(bs, 74, 69))
        if fgb.get("attack_angle") is not None:
            chip("ATTACK", f"{fgb['attack_angle']:.0f}°")
        ideal = as_num(fgb.get("ideal_aa"))
        if ideal is not None:
            chip("IDEAL AA", f"{ideal:.0f}%", "", heat(ideal, 60, 40))
        if fgb.get("attack_dir") is not None:
            d = fgb["attack_dir"]
            chip("DIRECTION", f"{abs(d):.0f}° {'pull' if d >= 0 else 'oppo'}")
        fast = as_num(fgb.get("fast_swing"))
        if fast is not None:
            chip("FAST SWING", f"{fast:.0f}%", "", heat(fast, 40, 15))
        squ = as_num(fgb.get("squared_up"))
        if squ is not None:
            chip("SQUARED-UP", f"{squ:.0f}%", "", heat(squ, 36, 25))
        blast = as_num(fgb.get("blast"))
        if blast is not None:
            chip("BLAST", f"{blast:.0f}%", "", heat(blast, 15, 8))
        if fgb.get("swing_length") is not None:
            chip("LENGTH", f"{fgb['swing_length']:.1f}", "ft")
        # 4 chips per row; a trailing stretch column keeps them left-packed
        for i, w in enumerate(chips):
            lay.addWidget(w, i // 4, i % 4)
        lay.setColumnStretch(4, 1)
        self._swing_row.show()

    def set_sp_card(self, card: Optional[dict], pitcher_name: str,
                    hand: Optional[str] = None):
        """SP deep card (get_sp_deep_card result) — shown in the 'Opp SP'
        view. For pitcher props this is the player's own arsenal."""
        self._sp_card = card
        self._sp_card_name = pitcher_name
        self._sp_card_hand = hand
        self._render_matchup()

    def show_pitch_splits(self, splits: List[PitchSplit], player_type: str,
                          velo_splits: Optional[List[PitchSplit]] = None):
        self._splits = splits
        self._velo_splits = velo_splits
        self._splits_type = player_type
        self._render_matchup()
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
        self._render_matchup()

    _SPLIT_HEADERS = ["Pitch", "#", "Velo", "Whiff", "BBE", "EV", "HH%",
                      "Brl%", "HR", "xwOBA"]

    @staticmethod
    def _cell(text, align_right=True):
        item = QTableWidgetItem(text)
        if align_right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _on_toggle_pitches(self):
        self._show_all_pitches = not self._show_all_pitches
        self._render_matchup()

    def _on_pitch_cell_clicked(self, row, _col):
        if row == self._pitch_toggle_row and row >= 0:
            self._on_toggle_pitches()

    def _render_matchup(self):
        """Merged SP-mix + pitch-splits table. Spine = the SP's arsenal
        (deep card when loaded, early StatsAPI arsenal before that), one row
        per pitch: orange columns are the SP's side (usage/velo/Stuff+/RV/
        xwOBA/whiff allowed), blue columns the batter's season results vs
        that pitch type. Pitches outside the arsenal hide behind the
        expander. Pitcher props: spine is the player's own arsenal. No SP
        context at all -> plain batter splits table."""
        table = self._tbl_pitch
        splits = self._splits
        card = self._sp_card
        is_pitcher = (self._splits_type == "pitcher")
        arsenal = self._arsenal if not is_pitcher else None
        if splits is None and card is None and not arsenal:
            return
        splits = splits or []
        by_name = {s.pitch: s for s in splits}

        # SP-side spine rows, graceful degradation card -> early arsenal
        spine = []
        if card and card.get("rows"):
            for p in card["rows"]:
                spine.append({"pitch": p["pitch"], "use": p["usage"],
                              "velo": p["velo"], "spin": p["spin"],
                              "stuff": p["stuff"], "rv": p["rv100"],
                              "xw": p["xwoba"], "whiff": p["whiff"],
                              "woba": p["woba"]})
        elif arsenal:
            per_pitch = (self._arsenal_stuff or {}).get("per_pitch") or {}
            for name, a in sorted(arsenal.items(),
                                  key=lambda kv: -kv[1]["usage"]):
                spine.append({"pitch": name, "use": a["usage"] * 100,
                              "velo": a.get("speed"), "spin": None,
                              "stuff": per_pitch.get(FG_PITCH_CODES.get(name)),
                              "rv": None, "xw": None, "whiff": None,
                              "woba": None})

        if not spine:
            # No SP/arsenal context (no game today, matchup still loading)
            self._pitch_toggle_row = -1
            table.clearSpans()
            if not splits:
                table.setRowCount(0)
                table.setColumnCount(1)
                table.setHorizontalHeaderLabels(
                    ["Pitch Splits — no Statcast data"])
                self._fit_table(table)
                self._sync_flank_widths()
                return
            table.setToolTip(
                "Contact quality vs each pitch type this season"
                if not is_pitcher
                else "Arsenal & contact allowed per pitch this season")
            self._fill_split_rows(table, splits, col0="Pitch Splits")
            return

        spine_names = {p["pitch"] for p in spine}
        extras = sorted((s for s in splits if s.pitch not in spine_names),
                        key=lambda s: -s.count)
        shown_extras = extras if self._show_all_pitches else []

        col0 = "Arsenal" if is_pitcher else "Matchup"
        headers = [col0, "Use%", "Velo", "Stf+", "RV", "xw", "Whf",
                   "#", "Whf", "HH%", "Brl%", "HR", "xw"]
        table.setRowCount(0)
        table.clearSpans()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        sp_hdr = QColor(230, 126, 34)
        bat_hdr = QColor(93, 173, 226)
        for c in range(1, 7):
            table.horizontalHeaderItem(c).setForeground(sp_hdr)
        for c in range(7, 13):
            table.horizontalHeaderItem(c).setForeground(bat_hdr)
        sp_name = self._sp_card_name or self._arsenal_pitcher or "SP"
        me = self._summary.player_name if self._summary else "player"
        if is_pitcher:
            table.setToolTip(
                f"{me}'s arsenal — orange: pitch quality (usage/velo/Stuff+/"
                "RV/xwOBA/whiff), blue: contact allowed on that pitch")
        else:
            table.setToolTip(
                f"Orange: {sp_name}'s pitch (usage · velo · Stuff+ · RV/100 "
                f"· xwOBA allowed · whiff%)  |  Blue: {me} vs that pitch "
                "type this season (# seen · whiff · HH% · Brl% · HR · "
                "xwOBAcon). Green = edge for the batter, red = threat.")

        cell = self._cell
        green, red = QColor(46, 204, 113), QColor(231, 76, 60)
        orange = QColor(230, 126, 34)
        grey = QColor(127, 140, 141)
        num = lambda v, d=1: "" if v is None else f"{v:.{d}f}"
        w3 = lambda v: "" if v is None else f"{v:.3f}".lstrip("0")
        pct = lambda v: "" if v is None else f"{v:.0%}"

        def batter_cells(s):
            if s is None:
                return [cell("") for _ in range(6)]
            xw = cell(w3(s.xwobacon))
            if s.xwobacon is not None and s.bbe >= 5:
                if s.xwobacon >= 0.450:
                    xw.setForeground(green)
                elif s.xwobacon <= 0.300:
                    xw.setForeground(red)
            return [cell(str(s.count)), cell(pct(s.whiff_pct)),
                    cell(pct(s.hardhit_pct)), cell(pct(s.barrel_pct)),
                    cell(str(s.hr)), xw]

        r = 0
        for p in spine:
            table.insertRow(r)
            name_cell = cell(PITCH_ABBREV.get(p["pitch"], p["pitch"]),
                             align_right=False)
            tip = p["pitch"]
            if p["spin"] is not None:
                tip += f" · {p['spin']:.0f} rpm"
            if p["woba"] is not None:
                tip += f" · wOBA {w3(p['woba'])} allowed"
            name_cell.setToolTip(tip)
            name_cell.setForeground(orange)
            font = name_cell.font()
            font.setBold(True)
            name_cell.setFont(font)
            rv = cell(num(p["rv"]))
            if p["rv"] is not None:
                # Savant RV/100 is pitcher-positive; batter view inverts it
                if p["rv"] >= 1.5:
                    rv.setForeground(red)
                elif p["rv"] <= -1.0:
                    rv.setForeground(green)
            spxw = cell(w3(p["xw"]))
            if p["xw"] is not None:
                if p["xw"] >= 0.360:
                    spxw.setForeground(green)
                elif p["xw"] <= 0.280:
                    spxw.setForeground(red)
            spwhf = cell(num(p["whiff"]))
            if (p["whiff"] or 0) >= 35:
                spwhf.setForeground(red)
            row_cells = [name_cell, cell(num(p["use"])), cell(num(p["velo"])),
                         cell(num(p["stuff"], 0)), rv, spxw, spwhf]
            row_cells += batter_cells(by_name.get(p["pitch"]))
            for c, item in enumerate(row_cells):
                table.setItem(r, c, item)
            r += 1
        for s in shown_extras:
            table.insertRow(r)
            name_cell = cell(PITCH_ABBREV.get(s.pitch, s.pitch),
                             align_right=False)
            name_cell.setToolTip(f"{s.pitch} — not in {sp_name}'s arsenal")
            name_cell.setForeground(grey)
            row_cells = ([name_cell] + [cell("") for _ in range(6)]
                         + batter_cells(s))
            for c, item in enumerate(row_cells):
                table.setItem(r, c, item)
            r += 1

        # Expander as the table's own last row, spanned across all columns
        if extras:
            table.insertRow(r)
            toggle = cell(
                f"  ▾ hide {len(extras)} other pitch types"
                if self._show_all_pitches
                else f"  ▸ {len(extras)} more pitch types",
                align_right=False)
            toggle.setForeground(grey)
            toggle.setToolTip("Batter pitch types outside the SP's arsenal")
            table.setItem(r, 0, toggle)
            table.setSpan(r, 0, 1, len(headers))
            self._pitch_toggle_row = r
        else:
            self._pitch_toggle_row = -1
        table.resizeRowsToContents()
        self._fit_table(table)
        self._sync_flank_widths()

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
                wall_note = f"<br>wall: {venue}"
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
        self._sync_flank_widths()

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

    def _on_chart_view_changed(self, *_):
        if self._summary is not None:
            self._update_chart(self._summary)

    def _filtered_games(self, summary):
        games = summary.games
        venue = self._chart_filter_combo.currentData()
        if venue == "home":
            games = [g for g in games if g.is_home]
        elif venue == "road":
            games = [g for g in games if not g.is_home]
        return games

    @staticmethod
    def _rolling(vals, w):
        """Trailing rolling mean, None-tolerant (skips missing games)."""
        out = []
        for i in range(len(vals)):
            window = [v for v in vals[max(0, i - w + 1):i + 1]
                      if v is not None]
            out.append(sum(window) / len(window) if window else None)
        return out

    def set_per_game_statcast(self, rows: List[dict]):
        """Per-game Statcast aggregates (get_per_game_statcast) for the
        trend overlays."""
        self._pg_statcast = {r["date"]: r for r in rows}
        if self._summary is not None:
            self._render_trend(self._summary)

    def _update_chart(self, summary: PropStatSummary):
        self._render_banner(summary)
        self._render_trend(summary)

    # -------------------------------------------------------------- banner

    def _render_banner(self, summary: PropStatSummary):
        plot = self._banner.getPlotItem()
        plot.clear()
        games = self._filtered_games(summary)
        window = self._chart_window_combo.currentData() or 0
        if window:
            games = games[-window:]
        venue = self._chart_filter_combo.currentData()
        line = summary.line if summary.line is not None else 0.5
        if not games:
            self._banner_text.setHtml(
                "<span style='font-size:8pt;color:#7F8C8D'>"
                "No games in window</span>")
            plot.addItem(self._banner_text)
            self._banner_text.setPos(0, 1)
            return
        n = len(games)
        overs = sum(1 for g in games if g.value > line)

        xs = list(range(n))
        heights = [g.value for g in games]
        brushes = [pg.mkBrush(*(COLOR_OVER if h > line else COLOR_UNDER), 210)
                   for h in heights]
        drawn = [h if h > 0 else max(line, 1) * 0.06 for h in heights]
        plot.addItem(pg.BarGraphItem(x=xs, height=drawn, width=0.78,
                                     brushes=brushes,
                                     pen=pg.mkPen(0, 0, 0, 120)))
        plot.addItem(pg.InfiniteLine(
            pos=line, angle=0,
            pen=pg.mkPen(*COLOR_LINE, width=1,
                         style=Qt.PenStyle.DashLine)))
        ymax = max(max(heights), line) * 1.15 + 0.05
        plot.setYRange(0, ymax, padding=0)
        plot.setXRange(-0.6, n - 0.4, padding=0)

        # Readout badge in the top-left corner, over the oldest bars
        self._banner_text.setHtml(
            "<div style='font-size:8pt; line-height:112%;'>"
            f"<span style='color:#2ECC71; font-weight:bold;'>"
            f"Over {overs}/{n}</span> · {overs / n:.0%}"
            + ("" if venue == "all" else f" · {venue}")
            + f"<br><span style='color:#F1C40F'>line {line:g}</span></div>")
        plot.addItem(self._banner_text)
        self._banner_text.setPos(-0.55, ymax)

    # ---------------------------------------------------------- trend plot

    # Per-overlay value formatter for hover + single-overlay axis ticks
    _OVERLAY_FMT = {
        "xw":  lambda v: f"{v:.3f}".lstrip("0"),
        "ev":  lambda v: f"{v:.1f}",
        "maxev": lambda v: f"{v:.1f}",
        "la":  lambda v: f"{v:.0f}°",
        "bat": lambda v: f"{v:.1f}",
        "hh":  lambda v: f"{v:.0%}",
        "brl": lambda v: f"{v:.0%}",
        "whiff": lambda v: f"{v:.0%}",
    }

    # Each metric's realistic domain, used to scale overlays onto a shared
    # 0-1 axis when several are shown at once. Fixed domains (not per-window
    # min/max) keep the vertical position meaningful and stop a barely-moving
    # series from being stretched to look dramatic.
    _OVERLAY_DOMAIN = {
        "xw":  (0.150, 0.550),   # rolling xwOBAcon
        "ev":  (82.0, 96.0),     # rolling avg exit velo (mph)
        "maxev": (95.0, 116.0),  # rolling max exit velo (mph)
        "la":  (-5.0, 35.0),     # rolling avg launch angle (deg)
        "hh":  (0.20, 0.65),     # hard-hit rate
        "brl": (0.0, 0.25),      # barrel rate
        "whiff": (0.10, 0.45),   # whiff rate
        "bat": (66.0, 80.0),     # rolling avg bat speed (mph)
    }

    def _visible_start(self, n):
        window = self._chart_window_combo.currentData() or 0
        return max(0, n - window) if window else 0

    def _sync_overlay_geom(self):
        """Keep the two overlay viewboxes glued to the main plot area."""
        rect = self._trend_plot.getPlotItem().getViewBox().sceneBoundingRect()
        self._left_vb.setGeometry(rect)
        self._right_vb.setGeometry(rect)

    def _put_real_axis(self, vb, axis, roll_o, color, x0, xs, nanify):
        """Draw one overlay as a real-unit line on `vb` and scale `axis` to
        its visible range with a colored, valued axis."""
        vb.addItem(pg.PlotCurveItem(
            x=xs, y=nanify(roll_o),
            pen=pg.mkPen(color, width=2.2), connect="finite"))
        mm = self._minmax_visible(roll_o, x0)
        if mm:
            lo, hi = mm
            pad = (hi - lo) * 0.18 or (abs(hi) * 0.08) or 0.05
            vb.setYRange(lo - pad, hi + pad, padding=0)
        axis.setStyle(showValues=True, tickFont=QFont("Segoe UI", 7))
        axis.setPen(pg.mkPen(color))
        axis.setTextPen(pg.mkPen(color))

    @staticmethod
    def _minmax_visible(series, x0):
        """(lo, hi) over finite values from x0 onward, or None."""
        finite = [v for v in series[x0:] if v is not None]
        if not finite:
            return None
        return min(finite), max(finite)

    def _render_trend(self, summary: PropStatSummary):
        plot = self._trend_plot.getPlotItem()
        plot.clear()
        self._left_vb.clear()
        self._right_vb.clear()
        self._trend_vline.hide()
        self._trend_htext.hide()
        games = self._filtered_games(summary)
        self._trend_games = games
        l_axis = plot.getAxis("left")
        r_axis = plot.getAxis("right")
        if not games:
            plot.showAxis("left", False)
            plot.showAxis("right", False)
            self._set_trend_note("")
            return
        line = summary.line if summary.line is not None else 0.5
        n = len(games)
        xs = list(range(n))
        # Rolling window follows the visible-window selector (L15 → 5g,
        # L30 → 10g, Season → 15g) so the overlay line always resolves
        # within the games actually on screen
        w = {15: 5, 30: 10}.get(
            self._chart_window_combo.currentData() or 0, 15)
        w_label = f"rolling {w}g"
        x0 = self._visible_start(n)
        nanify = lambda v: [float("nan") if x is None else x for x in v]

        # --- the selected prop stat as per-game bars (combo chart). No left
        #     axis — the value sits inside each bar. Colored over/under the
        #     line; the advanced overlays draw as lines on top.
        vals = [g.value for g in games]
        # Muted fill + a slightly stronger same-color edge so the bars read
        # as background context; the overlay lines (higher z) stay crisp.
        bar_brushes = [pg.mkBrush(*(COLOR_OVER if v > line else COLOR_UNDER),
                                  70) for v in vals]
        bar_pens = [pg.mkPen(*(COLOR_OVER if v > line else COLOR_UNDER), 150,
                             width=1) for v in vals]
        drawn = [v if v > 0 else max(line, 1) * 0.05 for v in vals]
        bars = pg.BarGraphItem(x=xs, height=drawn, width=0.66,
                               brushes=bar_brushes, pens=bar_pens)
        bars.setZValue(-10)
        plot.addItem(bars)
        # Value labels INSIDE each bar, tucked under the top edge
        shown = n - x0
        if shown <= 18:
            for x, v in zip(xs, vals):
                if x < x0:
                    continue
                if v > 0:
                    t = pg.TextItem(anchor=(0.5, 0))
                    t.setHtml("<span style='font-size:8pt;color:#ECF0F1;"
                              f"font-weight:bold;'>{v:g}</span>")
                    t.setPos(x, v)
                else:
                    t = pg.TextItem(anchor=(0.5, 1))
                    t.setHtml("<span style='font-size:8pt;color:#7F8C8D;'>"
                              "0</span>")
                    t.setPos(x, drawn[x])
                plot.addItem(t)

        # --- overlays: rolling per-game Statcast series
        pgsc = self._pg_statcast or {}
        active = []
        for key, label, color in self._OVERLAYS:
            if not self._overlay_btns[key].isChecked():
                continue
            series = [(pgsc.get(g.date) or {}).get(key) for g in games]
            roll_o = self._rolling(series, w)
            if any(v is not None for v in roll_o):
                active.append((key, label, color, roll_o))

        if not active:
            plot.showAxis("left", False)
            plot.showAxis("right", False)
            self._set_trend_note("")
        elif len(active) == 1:
            # One overlay: real units on the right axis
            key, label, color, roll_o = active[0]
            self._put_real_axis(self._right_vb, r_axis, roll_o, color,
                                x0, xs, nanify)
            plot.showAxis("left", False)
            plot.showAxis("right", True)
            self._set_trend_note(
                f"<span style='color:{color}'>{label} · {w_label} "
                "(right)</span>")
        elif len(active) == 2:
            # Two overlays: true dual axis — first on the left, second on
            # the right, each keeping its own real-unit scale
            (ka, la, ca, ra), (kb, lb, cb, rb) = active
            self._put_real_axis(self._left_vb, l_axis, ra, ca,
                                x0, xs, nanify)
            self._put_real_axis(self._right_vb, r_axis, rb, cb,
                                x0, xs, nanify)
            plot.showAxis("left", True)
            plot.showAxis("right", True)
            self._set_trend_note(
                f"<span style='color:{ca}'>{la} (left)</span> · "
                f"<span style='color:{cb}'>{lb} (right)</span> · "
                f"<span style='color:#7F8C8D'>{w_label}</span>")
        else:
            # 3+ overlays: can't give each a real axis — scale each onto a
            # shared 0-1 band by its fixed realistic domain (flat stays flat).
            for key, label, color, roll_o in active:
                lo, hi = self._OVERLAY_DOMAIN.get(key, (None, None))
                if lo is None:
                    mm = self._minmax_visible(roll_o, x0)
                    if not mm:
                        continue
                    lo, hi = mm
                rng = (hi - lo) or 1.0
                norm = [None if v is None
                        else max(0.0, min(1.0, (v - lo) / rng))
                        for v in roll_o]
                self._right_vb.addItem(pg.PlotCurveItem(
                    x=xs, y=nanify(norm),
                    pen=pg.mkPen(color, width=2.0), connect="finite"))
            self._right_vb.setYRange(-0.03, 1.03, padding=0)
            plot.showAxis("left", False)
            plot.showAxis("right", True)
            r_axis.setStyle(showValues=False)
            r_axis.setPen(pg.mkPen("#4A5A68"))
            names = " · ".join(
                f"<span style='color:{color}'>{label}</span>"
                for _k, label, color, _r in active)
            self._set_trend_note(
                names + f" · <span style='color:#7F8C8D'>{w_label} · "
                "normalized</span>")

        # keep the overlay viewboxes aligned to the (possibly axis-shifted)
        # plot area, then x-zoom to the window
        self._sync_overlay_geom()
        plot.setXRange(x0 - 0.5, n - 0.5, padding=0)
        vis_vals = vals[x0:]
        ymax = max(max(vis_vals, default=line), line) * 1.22 + 0.1
        plot.setYRange(0, ymax, padding=0)

        axis = plot.getAxis("bottom")
        avail = max(self._trend_plot.width(), 200)
        step = max(1, -(-shown // max(5, avail // 42)))
        ticks = [(x, f"{'vs' if g.is_home else '@'}{g.opponent}\n{g.date[5:]}"
                     if (x - x0) % step == 0 else "")
                 for x, g in zip(xs, games)]
        axis.setTicks([ticks])
        axis.setStyle(tickFont=QFont("Segoe UI", 7))

        # crosshair line survives plot.clear() only if re-added (the hover
        # readout is a top-level scene item — untouched by clear())
        plot.addItem(self._trend_vline)

    def _on_trend_mouse(self, pos):
        """Crosshair scrub: snap a vertical line to the nearest game and float
        an in-plot readout box (prop value + every enabled overlay's REAL
        value). Pure item moves — never touches the layout, so no resize."""
        games = self._trend_games
        if not games or self._summary is None:
            self._trend_vline.hide()
            self._trend_htext.hide()
            return
        plot = self._trend_plot.getPlotItem()
        vb = plot.getViewBox()
        if not self._trend_plot.sceneBoundingRect().contains(pos):
            self._trend_vline.hide()
            self._trend_htext.hide()
            return
        n = len(games)
        x0 = self._visible_start(n)
        mp = vb.mapSceneToView(pos)
        idx = int(round(mp.x()))
        if idx < x0 or idx >= n:
            self._trend_vline.hide()
            self._trend_htext.hide()
            return
        g = games[idx]
        self._trend_vline.setPos(idx)
        self._trend_vline.show()

        rec = (self._pg_statcast or {}).get(g.date) or {}
        lines = [f"<b>{'vs' if g.is_home else '@'}{g.opponent} "
                 f"{g.date[5:]}</b>",
                 f"<span style='color:#2ECC71'>{g.value:g} "
                 f"{self._summary.stat_label}</span>"]
        for key, label, color in self._OVERLAYS:
            if not self._overlay_btns[key].isChecked():
                continue
            txt = self._hover_overlay_detail(key, rec)
            if txt:
                lines.append(f"<span style='color:{color}'>{txt}</span>")
        self._trend_htext.setHtml(
            "<div style='font-size:8pt; line-height:120%;'>"
            + "<br>".join(lines) + "</div>")
        # anchor flips to keep the box on-screen near the right edge;
        # readout starts BELOW the floating chip bar (QWidget children paint
        # over scene items and would obscure it)
        clear_px = self._trend_chip_bar.y() + self._trend_chip_bar.height()
        y_scene = self._trend_plot.mapToScene(QPoint(0, clear_px + 2)).y()
        x_scene = vb.mapViewToScene(QPointF(float(idx), 0.0)).x()
        right_half = idx > x0 + (n - x0) * 0.55
        self._set_htext_anchor((1, 0) if right_half else (0, 0))
        self._trend_htext.setPos(x_scene, y_scene)
        self._trend_htext.show()

    @staticmethod
    def _hover_overlay_detail(key, rec):
        """Per-overlay hover text. For the contact-quality overlays this is
        the raw per-event detail for that game (individual BIP EVs, per-swing
        bat speeds, barrel/hard-hit COUNTS) — not the game-level average that
        drives the plot line."""
        bip = rec.get("bip") or []
        if key == "ev":
            evs = sorted((b["ev"] for b in bip if b["ev"] is not None),
                         reverse=True)
            if not evs:
                return None
            # HR gets ↗, non-HR barrel gets •
            marks = {round(b["ev"], 1): ("↗" if b["hr"] else
                                         ("•" if b["barrel"] else ""))
                     for b in bip if b["ev"] is not None}
            shown = " ".join(f"{e:.0f}{marks.get(round(e, 1), '')}"
                             for e in evs[:9])
            more = f" +{len(evs) - 9}" if len(evs) > 9 else ""
            return f"EV: {shown}{more}"
        if key == "bat":
            bats = sorted((b["bat"] for b in bip if b["bat"] is not None),
                          reverse=True)
            if not bats:
                bl = rec.get("bat_list") or []
                if not bl:
                    return None
                return f"BatSpd: {_avg(bl):.1f} avg ({len(bl)} sw)"
            shown = " ".join(f"{s:.0f}" for s in bats[:9])
            return f"BatSpd(BIP): {shown}"
        if key == "brl":
            bbe = rec.get("bbe") or 0
            if not bbe:
                return None
            b = rec.get("barrels", 0)
            return f"Barrels: {b}/{bbe}"
        if key == "hh":
            bbe = rec.get("bbe") or 0
            if not bbe:
                return None
            return f"HardHit: {rec.get('hardhits', 0)}/{bbe}"
        if key == "maxev":
            v = rec.get("maxev")
            return None if v is None else f"MaxEV: {v:.1f}"
        if key == "la":
            las = sorted((b["la"] for b in bip if b.get("la") is not None),
                         reverse=True)
            if not las:
                return None
            shown = " ".join(f"{v:.0f}°" for v in las[:9])
            more = f" +{len(las) - 9}" if len(las) > 9 else ""
            return f"LA: {shown}{more}"
        if key == "whiff":
            sw = rec.get("swings") or 0
            if not sw:
                return None
            return f"Whiff: {rec.get('whiffs', 0)}/{sw}"
        if key == "xw":
            v = rec.get("xw")
            return None if v is None else f"xwOBAcon: {v:.3f}".replace("0.", ".")
        return None

    def _set_htext_anchor(self, anchor):
        """Set the floating readout's anchor (pyqtgraph API varies by
        version)."""
        try:
            self._trend_htext.setAnchor(anchor)
        except AttributeError:
            self._trend_htext.anchor = pg.Point(anchor)
            self._trend_htext.updateTextPos()

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
        """)
        # The panel is JUST the table: the team selector + power rank live
        # inside the horizontal header (overlaid on the blank "Reliever"
        # section), so every pixel of panel height goes to reliever rows.
        # Zero left margin — the table hugs the window edge so its last
        # columns fit without scrolling.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(0)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(17)
        self._table.setAlternatingRowColors(True)
        # Status rides next to the name (col 1) as a short code so it's
        # always visible without scrolling to the far right.
        headers = ["", "St", "T", "Role", "vs", "Inn", "Diff",
                   "ERA", "ERA7", "SIERA", "K-BB%", "Stf+", "Loc+", "gmLI",
                   "SV", "HLD", "GF", "NP", "30+", "B2B",
                   "Yd", "-2", "-3", "L3", "L7"]
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(18)
        self._table.setStyleSheet(_STATS_TABLE_QSS)
        # NOTE: no alignment arg — that would make the table adopt its tiny
        # default sizeHint instead of filling; the content caps set in
        # _render() handle the right/bottom edges, and the zero spacer
        # soaks up the leftover panel height (no blank table rows)
        root.addWidget(self._table, stretch=1)
        root.addStretch(0)

        # In-header controls: combo + "PEN #n" rank riding on section 0.
        # The section is blank (header label "") so nothing shows through;
        # geometry re-tracks on column resize / horizontal scroll.
        hdr.setFixedHeight(22)
        self._hdr_box = QWidget(hdr)
        hdr_lay = QHBoxLayout(self._hdr_box)
        hdr_lay.setContentsMargins(2, 1, 2, 1)
        hdr_lay.setSpacing(6)
        self._team_combo = QComboBox()
        self._team_combo.setFixedHeight(18)
        self._team_combo.currentTextChanged.connect(self._on_team_changed)
        hdr_lay.addWidget(self._team_combo)
        self._rank_label = QLabel("PEN")
        self._rank_label.setObjectName("bullpenTitle")
        hdr_lay.addWidget(self._rank_label)
        hdr_lay.addStretch()
        hdr.sectionResized.connect(lambda *_: self._place_hdr_box())
        self._table.horizontalScrollBar().valueChanged.connect(
            lambda *_: self._place_hdr_box())
        self._place_hdr_box()

    def _place_hdr_box(self):
        hdr = self._table.horizontalHeader()
        self._hdr_box.setGeometry(hdr.sectionViewportPosition(0), 0,
                                  hdr.sectionSize(0), hdr.height())

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
        self._team_combo.setToolTip(context)
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

    _STATUS_SHORT = {"TAXED": "TAX", "UNAVAIL": "UNA", "DOUBTFUL": "DBT",
                     "USED YDAY": "YD", "FRESH": "FR"}

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
        self._rank_label.setText(f"PEN #{power_rank}" if power_rank else "PEN")
        self._rank_label.setToolTip("BP Power Rank" if power_rank else "")

        for r, rec in enumerate(rows):
            table.insertRow(r)
            fg = rec.get("fg") or {}
            nps = [n for _, n in rec["np_by_day"]] + [0, 0, 0]

            name = cell(rec["name"], align_right=False)
            if rec.get("role") == "CL":
                bold(name)

            # Short status code beside the name (full text on hover)
            st = rec.get("status") or ""
            status = cell(self._STATUS_SHORT.get(st, st), align_right=False)
            if st:
                status.setToolTip(st)
            if st in ("TAXED", "UNAVAIL"):
                bold(status, self._RED)
            elif st == "DOUBTFUL":
                bold(status, self._ORANGE)
            elif st == "FRESH":
                status.setForeground(self._GREEN)

            role = cell(rec.get("role", ""), align_right=False)
            if rec.get("role") in ("CL", "SU"):
                bold(role, self._ORANGE)

            stf = cell(num(fg.get("stuff"), 0))
            if fg.get("stuff") is not None:
                if fg["stuff"] >= 105:
                    stf.setForeground(self._GREEN)
                elif fg["stuff"] <= 95:
                    stf.setForeground(self._RED)

            gmli = cell(num(fg.get("gmli")))
            if (fg.get("gmli") or 0) >= 1.3:
                bold(gmli, self._ORANGE)

            # B2B: the site's own judgment when logged in, else season count
            b2b_site = rec.get("b2b_site")
            b2b = rec.get("b2b_count")
            b2b_cell = cell(("Rare" if b2b_site == "Rarely" else b2b_site)
                            if b2b_site else
                            ("" if b2b is None else
                             (f"{b2b}×" if b2b else "never")))
            if b2b_site == "No" or (b2b_site is None and b2b == 0):
                b2b_cell.setForeground(self._RED)

            # Column order matches `headers` (status at index 1)
            row_cells = [
                name, status,
                cell(fg.get("throws") or "", align_right=False),
                role,
                cell(rec.get("vs") or "", align_right=False),
                cell(rec.get("avg_inning") or ""),
                cell(rec.get("avg_diff") or ""),
                cell(rec["era"]),
                cell(rec.get("era7") or ""),
                cell(num(fg.get("siera"))),
                cell(pct(fg.get("kbb"))),
                stf,
                cell(num(fg.get("location"), 0)),
                gmli,
                cell(str(int(fg["sv"])) if fg.get("sv") else ""),
                cell(str(int(fg["hld"])) if fg.get("hld") else ""),
                cell(str(rec.get("gf") or "")),
                cell(num(rec.get("avg_np"), 0) if rec.get("avg_np") else ""),
                cell(str(rec.get("thirty_plus") or "")),
                b2b_cell,
                cell(str(nps[0]) if nps[0] else ""),
                cell(str(nps[1]) if nps[1] else ""),
                cell(str(nps[2]) if nps[2] else ""),
                cell(str(rec["np_l3"]) if rec["np_l3"] else ""),
                cell(str(rec["np_l7"]) if rec["np_l7"] else ""),
            ]
            for c, item in enumerate(row_cells):
                table.setItem(r, c, item)
        table.resizeRowsToContents()
        # Hug the columns horizontally — leftover pane width stays as plain
        # background instead of empty table frame
        table.resizeColumnsToContents()
        # Col 0 must also clear the in-header combo + rank label; fixed mode
        # so the manual width sticks (ResizeToContents ignores setColumnWidth)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(
            0, max(table.sizeHintForColumn(0) + 8,
                   self._hdr_box.sizeHint().width() + 4))
        w = 2 * table.frameWidth() + 2
        w += sum(table.columnWidth(c) for c in range(table.columnCount()))
        table.setMaximumWidth(w)
        # Bottom edge hugs the last reliever too. Capping the PANEL as well
        # stops the splitter from growing this section past its data — the
        # surplus height goes to the SP form section instead. Keep room for
        # the h-scrollbar (the pen table is usually wider than the pane).
        h = 2 * table.frameWidth() + hdr.height()
        h += sum(table.rowHeight(r) for r in range(table.rowCount()))
        h += table.horizontalScrollBar().sizeHint().height()
        table.setMaximumHeight(h)
        # Fixed panel height = data height: the splitter can neither grow
        # this section past its rows nor squeeze relievers behind a
        # scrollbar — surplus goes to the SP form section
        self.setMinimumHeight(h + 4)   # + root layout margins
        self.setMaximumHeight(h + 4)
        self._place_hdr_box()


class PitcherFormPanel(QWidget):
    """Starter recent-form section that sits under the bullpen table: the
    game-by-game log (with per-start FB velo + CSW% joined from the Savant
    pitch-detail cache), a leash strip (IP/NP per start, outs distributions,
    days rest), and times-through-order damage. Shows the OPPOSING SP for
    batter props and the player himself for pitcher props — same sync logic
    as the bullpen panel. Data via MLBPropStats.get_sp_form (StatsAPI game
    log, instant) then get_sp_statcast_form (one cached CSV fetch)."""

    _N_APPS = 4   # appearances shown collapsed, newest first

    def __init__(self, stats: Optional[MLBPropStats] = None, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._pid: Optional[int] = None
        self._form: Optional[dict] = None
        self._sc: Optional[dict] = None
        self._name = ""
        self._context = ""
        self._mv: Optional[dict] = None
        self._card: Optional[dict] = None   # SP deep card (arsenal quick-card)
        self._show_all_starts = False
        self._starts_toggle_row = -1
        self._pct_df = None            # Savant pitcher percentile leaderboard
        self._build_ui()

    def set_stats_backend(self, stats: MLBPropStats):
        self._stats = stats

    def _build_ui(self):
        self.setStyleSheet("""
            #spFormInfo { color: #BDC3C7; font-size: 8pt; }
            #spName { color: white; font-size: 12pt; font-weight: bold; }
            #spTradLabel { color: #7F8C8D; font-size: 6pt; }
            #spTradValue { color: #ECF0F1; font-size: 8pt; font-weight: bold; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)   # flush left, like the pen table
        root.setSpacing(2)

        # Header: headshot + name/context/leash + season line — mirrors the
        # batter header in the Player Detail panel
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        self._headshot = QLabel()
        self._headshot.setFixedSize(44, 44)
        self._headshot.setScaledContents(True)
        head_row.addWidget(self._headshot,
                           alignment=Qt.AlignmentFlag.AlignTop)
        ident = QVBoxLayout()
        ident.setSpacing(0)
        # Name row: season stat line rides directly beside the name instead
        # of hugging the panel's right edge
        name_row = QHBoxLayout()
        name_row.setSpacing(0)
        self._name_label = QLabel("")
        self._name_label.setObjectName("spName")
        name_row.addWidget(self._name_label,
                           alignment=Qt.AlignmentFlag.AlignTop)
        trad_holder = QWidget()
        self._trad_grid = QGridLayout(trad_holder)
        self._trad_grid.setContentsMargins(12, 0, 4, 0)
        self._trad_grid.setHorizontalSpacing(10)
        self._trad_grid.setVerticalSpacing(0)
        self._trad_grid.setAlignment(Qt.AlignmentFlag.AlignTop
                                     | Qt.AlignmentFlag.AlignLeft)
        name_row.addWidget(trad_holder)
        name_row.addStretch()
        ident.addLayout(name_row)
        # Context + leash readout. wordWrap so its length never sets the
        # panel's minimum width.
        self._info_label = QLabel("")
        self._info_label.setObjectName("spFormInfo")
        self._info_label.setTextFormat(Qt.TextFormat.RichText)
        self._info_label.setWordWrap(True)
        ident.addWidget(self._info_label)
        head_row.addLayout(ident, stretch=1)
        root.addLayout(head_row)

        # Column layout: plot on top, starts log (last 4 + in-table
        # expander) and TTO side by side beneath it. The column hugs the
        # tables' width; everything right of it is free for the coming
        # movement/zone additions.
        self._starts_table = self._make_table(
            ["Date", "Opp", "IP", "H", "ER", "BB", "K", "HR", "NP", "BF",
             "FBv", "CSW"])
        self._starts_table.cellClicked.connect(self._on_starts_cell_clicked)
        self._tto_table = self._make_table(
            ["Split", "PA", "wOBA", "xwOBA", "K%", "BB%",
             "EV", "HH%", "Brl%", "HR"])
        self._tto_table.setToolTip(
            "Damage splits: times through the order (1/2/3+) and platoon "
            "(vs LHB / vs RHB)")
        # No internal scrollbars: heights are capped to content, and when the
        # splitter squeezes the panel a v-scrollbar would steal column width
        # (clipping CSW behind an h-scrollbar) — older rows clip instead
        for t in (self._starts_table, self._tto_table):
            t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            t.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._plot = pg.PlotWidget(background="#151a21")
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setMinimumWidth(180)
        self._plot.setFixedHeight(240)   # level with the movement square

        # Stat-selector chips ride INSIDE the plot (top-right corner, over
        # the bars' headroom) as a floating legend — repositioned via
        # eventFilter on every plot resize
        self._chip_bar = QWidget(self._plot)
        self._chip_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,
                                    True)
        self._chip_bar.setStyleSheet(
            "background: rgba(21, 26, 33, 175); border-radius: 3px;")
        chip_lay = QHBoxLayout(self._chip_bar)
        chip_lay.setContentsMargins(2, 0, 2, 0)
        chip_lay.setSpacing(0)
        # Colored legend of the ACTIVE overlays + a 'Stats ▾' dropdown
        # replace the old always-visible chip row
        self._chip_legend = QLabel()
        self._chip_legend.setStyleSheet(
            "background: transparent; font-size: 8pt; font-weight: bold;")
        self._chip_legend.setTextFormat(Qt.TextFormat.RichText)
        chip_lay.addWidget(self._chip_legend)
        self._chip_stat_menu = StatMenuButton(self._OVERLAYS)
        self._chip_btns = self._chip_stat_menu.acts
        chip_lay.addWidget(self._chip_stat_menu)
        self._chip_btns["velo"].setChecked(True)
        self._chip_btns["xw"].setChecked(True)
        # Connect AFTER the defaults: setChecked fires toggled, and the
        # plot's overlay viewboxes don't exist yet at this point
        for act in self._chip_btns.values():
            act.toggled.connect(self._render_plot)
        # Per-pitch spin overlays are appended to this menu per pitcher, once
        # the arsenal is known (_rebuild_spin_menu)
        self._spin_actions: Dict[str, QAction] = {}
        self._spin_sep = None
        self._plot.installEventFilter(self)

        p = self._plot.getPlotItem()
        p.getViewBox().setMouseEnabled(x=False, y=False)
        p.getViewBox().setDefaultPadding(0.02)
        self._vb_rate = pg.ViewBox()
        self._vb_velo = pg.ViewBox()
        # Spin (RPM) needs a third scale distinct from mph velo; it borrows
        # the right axis when no mph overlay is active, else draws unlabeled
        self._vb_spin = pg.ViewBox()
        for vb in (self._vb_rate, self._vb_velo, self._vb_spin):
            vb.setMouseEnabled(x=False, y=False)
            vb.setZValue(20)
            p.scene().addItem(vb)
            vb.setXLink(p.getViewBox())
        p.getAxis("left").linkToView(self._vb_rate)
        p.getAxis("right").linkToView(self._vb_velo)
        p.getViewBox().sigResized.connect(self._sync_plot_vbs)
        # Crosshair scrub: per-start readout floats inside the plot
        self._plot_vline = pg.InfiniteLine(
            angle=90, pen=pg.mkPen(150, 150, 155, 130))
        self._plot_vline.hide()
        # Opaque + top-level scene item so the overlay lines (viewboxes at
        # scene z=20) never draw over the readout — same as the trend plot
        self._plot_htext = pg.TextItem(
            anchor=(0, 0), color=(225, 225, 230),
            border=pg.mkPen("#34495E"),
            fill=pg.mkBrush(18, 24, 31))
        self._plot_htext.hide()
        self._plot_htext.setZValue(100)
        self._plot.scene().addItem(self._plot_htext)
        self._plot.scene().sigMouseMoved.connect(self._on_plot_mouse)

        # Savant percentile stack (pitcher skill bars) rides alongside the
        # starts/TTO tables — data arrives via set_percentile_data
        pct_holder = QWidget()
        self._pct_grid = QGridLayout(pct_holder)
        self._pct_grid.setContentsMargins(0, 0, 0, 0)
        self._pct_grid.setHorizontalSpacing(0)
        self._pct_grid.setVerticalSpacing(0)
        self._pct_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        pct_holder.setMinimumWidth(150)

        # Movement scatter (HB vs iVB, inches, catcher's view) colored per
        # pitch type (PITCH_COLORS) — shares the TOP ROW with the form plot
        # so it never widens the panel past the tables row below
        def shape_plot(aspect=True):
            w = pg.PlotWidget(background="#151a21")
            w.setMenuEnabled(False)
            w.hideButtons()
            w.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
            if aspect:
                w.setAspectLocked(True)
            return w

        self._mv_plot = shape_plot()
        self._mv_plot.setToolTip(
            "Pitch movement (catcher's view): horizontal vs induced "
            "vertical break in inches; big dots = per-pitch average — "
            "CLICK a dot to fly the pitch in 3D")
        # Square, sized with the form plot's height band — an aspect-locked
        # plot given a tall sliver renders the clusters microscopic
        self._mv_plot.setFixedSize(240, 240)

        # Row 1: form plot (takes the slack) + movement square.
        # Row 2: starts log | damage splits | percentile stack.
        # Everything hugs the top; the vertical stretch keeps the splitter's
        # spare height empty below the content instead of inside it.
        # Arsenal quick-card: per-pitch Stuff+ / velo / spin fills the gap
        # between the percentile stack and the tables row
        self._ars_table = self._make_table(
            ["Pitch", "Stf+", "Velo", "Spin", "vLg"])
        self._ars_table.setToolTip(
            "Arsenal: FanGraphs Stuff+ with Savant avg velo/spin per pitch. "
            "Pitch cell shows usage%; vLg = spin vs league avg for that pitch")
        self._ars_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._ars_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        pct_col = QVBoxLayout()
        pct_col.setSpacing(4)
        pct_col.addWidget(pct_holder)
        pct_col.addWidget(self._ars_table)
        pct_col.addStretch(1)

        # AlignTop on BOTH plots keeps them level: an unaligned height-capped
        # widget gets vertically centered when its row is taller than it
        plots_row = QHBoxLayout()
        plots_row.setSpacing(4)
        plots_row.addWidget(self._plot, stretch=1,
                            alignment=Qt.AlignmentFlag.AlignTop)
        plots_row.addWidget(self._mv_plot,
                            alignment=Qt.AlignmentFlag.AlignTop)

        tables_row = QHBoxLayout()
        tables_row.setSpacing(4)
        tables_row.addWidget(self._starts_table,
                             alignment=Qt.AlignmentFlag.AlignTop)
        tables_row.addWidget(self._tto_table,
                             alignment=Qt.AlignmentFlag.AlignTop)
        tables_row.addStretch(1)

        # Tables ride in the same column as the plots, so they tuck directly
        # under the plot band beside the (taller) pct/arsenal column instead
        # of starting a new full-width row below it
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addLayout(plots_row)
        right_col.addLayout(tables_row)
        right_col.addStretch(1)

        content = QHBoxLayout()
        content.setSpacing(4)
        content.addLayout(pct_col)
        content.addLayout(right_col, stretch=1)
        root.addLayout(content, stretch=1)

    @staticmethod
    def _make_table(headers: List[str]) -> QTableWidget:
        t = QTableWidget()
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(17)
        t.setAlternatingRowColors(True)
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(18)
        t.setStyleSheet(_STATS_TABLE_QSS)
        return t

    # ------------------------------------------------------------- control

    def set_percentile_data(self, pitchers_df):
        """Savant pitcher percentile leaderboard (may arrive after show)."""
        self._pct_df = pitchers_df
        self._render_pct()

    def _render_pct(self):
        while self._pct_grid.count():
            item = self._pct_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self._pct_df is None or not self._pid:
            return
        row = self._pct_df[self._pct_df["player_id"] == self._pid]
        if row.empty:
            lbl = QLabel("No Savant %iles")
            lbl.setStyleSheet("color: #7F8C8D; font-size: 8pt;")
            self._pct_grid.addWidget(lbl, 0, 0)
            return
        row = row.iloc[0]
        i = 0
        for col, label in PITCHER_PCT_COLS:
            if col not in row.index:
                continue
            try:
                pct = float(row[col])
            except (TypeError, ValueError):
                continue
            if pct != pct:      # NaN
                continue
            self._pct_grid.addWidget(CompactPercentileBar(label, pct), i, 0)
            i += 1

    def show_pitcher(self, pid: int, name: str, hand: Optional[str] = None,
                     context: str = ""):
        """Point the panel at a pitcher (no-op when already showing him)."""
        if self._stats is None or not pid:
            return
        self._name = name + (f" ({hand})" if hand else "")
        self._context = context
        self._update_info()
        if pid == self._pid:
            return
        self._pid = pid
        self._render_pct()
        self._form = self._sc = self._mv = self._card = None
        self._ars_table.setRowCount(0)
        self._plot_vline.hide()
        self._plot_htext.hide()
        self._headshot.setPixmap(QPixmap())
        self._render_trad([])
        asyncio.create_task(self._fetch(pid))

    def _update_info(self):
        leash = (self._form or {}).get("leash")
        self._name_label.setText(f"SP {self._name}")
        parts = []
        if self._context:
            parts.append(f"<span style='color:#7F8C8D'>{self._context}</span>")
        if leash:
            rest = leash.get("days_rest")
            np_s = leash.get("np_per_start")
            parts.append(
                "<span style='color:#82C4E0'>"
                f"Leash {leash['ip_per_start']:.1f} IP/GS"
                + (f" · {np_s:.0f} NP" if np_s else "")
                + f" · ≥5IP {leash['pct15']:.0%}"
                + f" · ≥6IP {leash['pct18']:.0%}"
                + f" · ≥7IP {leash['pct21']:.0%}"
                + (f" · Rest {rest}d" if rest is not None else "")
                + "</span>")
        self._info_label.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts))

    def _render_trad(self, pairs):
        """Season line mini-grid (label over value), like the batter header."""
        while self._trad_grid.count():
            item = self._trad_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for col, (label, value) in enumerate(pairs[:12]):
            lbl = QLabel(label)
            lbl.setObjectName("spTradLabel")
            val = QLabel(value)
            val.setObjectName("spTradValue")
            for w in (lbl, val):
                w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._trad_grid.addWidget(lbl, 0, col)
            self._trad_grid.addWidget(val, 1, col)

    async def _fetch(self, pid: int):
        # Stage 1: game log (fast) renders immediately; stage 2 fills the
        # velo/CSW columns and the TTO table from the pitch-detail cache
        try:
            async with aiohttp.ClientSession() as session:
                form = await self._stats.get_sp_form(session, pid)
                if self._pid != pid:
                    return
                self._form = form
                self._render()
                # Header fill: mugshot (shared cache) + season line
                shot = HEADSHOT_DIR / f"{pid}.png"
                if not shot.exists():
                    try:
                        async with session.get(
                                HEADSHOT_URL.format(pid=pid),
                                timeout=aiohttp.ClientTimeout(total=10)
                                ) as resp:
                            if resp.status == 200:
                                HEADSHOT_DIR.mkdir(exist_ok=True)
                                shot.write_bytes(await resp.read())
                    except Exception:
                        pass
                if self._pid == pid and shot.exists():
                    self._headshot.setPixmap(QPixmap(str(shot)))
                try:
                    pairs = await self._stats.get_traditional_stats(
                        session, pid, "pitching")
                    if self._pid == pid and pairs:
                        self._render_trad(pairs)
                except Exception as e:
                    print(f"PitcherFormPanel: season line failed: {e}")
                sc = await self._stats.get_sp_statcast_form(session, pid)
                mv = await self._stats.get_sp_movement(session, pid)
                card = await self._stats.get_sp_deep_card(session, pid)
        except Exception as e:
            print(f"PitcherFormPanel: fetch failed for {pid}: {e}")
            return
        if self._pid == pid:
            self._sc = sc
            self._mv = mv
            self._card = card
            self._render()
            self._render_shapes()
            self._render_arsenal()

    # -------------------------------------------------------------- render

    _GREEN = QColor(46, 204, 113)
    _RED = QColor(231, 76, 60)
    _ORANGE = QColor(230, 126, 34)

    # (key, chip label, color) — "k" draws the per-start bars on the main
    # viewbox; velo plots on the right axis in mph; the rest share the left
    # axis (rates + xwOBA live in the same 0-0.6 band)
    _OVERLAYS = [("k", "K", "#2ECC71"),
                 ("velo", "FBv", "#E67E22"), ("csw", "CSW%", "#82C4E0"),
                 ("whiff", "Whiff%", "#AF7AC5"), ("xw", "xwOBA", "#E74C3C"),
                 ("ev", "EV", "#F1C40F"), ("hh", "HH%", "#1ABC9C"),
                 ("brl", "Brl%", "#FF6FB5")]

    def eventFilter(self, obj, ev):
        if obj is self._plot:
            if ev.type() == QEvent.Type.Resize:
                self._place_chip_bar()
            elif ev.type() == QEvent.Type.Leave:
                self._plot_vline.hide()
                self._plot_htext.hide()
        return super().eventFilter(obj, ev)

    def _place_chip_bar(self):
        bar = self._chip_bar
        bar.adjustSize()
        # Top-right, inside the plot frame but clear of the right velo axis
        bar.move(max(0, self._plot.width() - bar.width() - 42), 3)
        bar.raise_()

    def _sync_plot_vbs(self):
        rect = self._plot.getPlotItem().getViewBox().sceneBoundingRect()
        self._vb_rate.setGeometry(rect)
        self._vb_velo.setGeometry(rect)
        self._vb_spin.setGeometry(rect)

    def _on_plot_mouse(self, pos):
        """Crosshair scrub over the form plot: snap to the nearest start and
        float a readout (line + real values for every enabled overlay)."""
        apps = (self._form or {}).get("apps") or []
        if not apps or not self._plot.sceneBoundingRect().contains(pos):
            self._plot_vline.hide()
            self._plot_htext.hide()
            return
        vb = self._plot.getPlotItem().getViewBox()
        idx = int(round(vb.mapSceneToView(pos).x()))
        if idx < 0 or idx >= len(apps):
            self._plot_vline.hide()
            self._plot_htext.hide()
            return
        a = apps[idx]
        sc = self._sc or {}
        lines = [f"<b>{'vs' if a['is_home'] else '@'}{a['opp']} "
                 f"{a['date'][5:]}</b>",
                 f"{a['ip']} IP · {a['h'] or 0} H · {a['er'] or 0} ER · "
                 f"{a['bb'] or 0} BB · "
                 f"<span style='color:#2ECC71'>{a['k'] or 0} K</span> · "
                 f"{a['np'] or 0} NP"]
        fmt = {"velo": lambda v: f"FBv: {v:.1f} mph",
               "csw": lambda v: f"CSW: {v:.0%}",
               "whiff": lambda v: f"Whiff: {v:.0%}",
               "xw": lambda v: f"xwOBA: {v:.3f}".replace("0.", "."),
               "ev": lambda v: f"EV: {v:.1f}",
               "hh": lambda v: f"HH: {v:.0%}",
               "brl": lambda v: f"Brl: {v:.0%}"}
        # FB velo is averaged over the fastballs thrown that start; show the
        # count so a small-sample blip reads as such
        fbn = (sc.get("fbn") or {}).get(a["date"])
        for key, _label, color in self._OVERLAYS:
            if key == "k" or not self._chip_btns[key].isChecked():
                continue
            v = (sc.get(key) or {}).get(a["date"])
            if v is None:
                continue
            txt = fmt[key](v)
            if key == "velo" and fbn:
                txt += f" over {fbn} FB"
            lines.append(f"<span style='color:{color}'>{txt}</span>")
        # Per-pitch spin lines: "2336 rpm over 42 FF"
        sc_spin = sc.get("spin_by_pitch") or {}
        sc_spinn = sc.get("spinn_by_pitch") or {}
        for pt, act in self._spin_actions.items():
            if not act.isChecked():
                continue
            v = (sc_spin.get(pt) or {}).get(a["date"])
            if v is None:
                continue
            n = (sc_spinn.get(pt) or {}).get(a["date"])
            color = PITCH_COLORS.get(pt, _PITCH_COLOR_DEFAULT)
            txt = f"{pt} spin: {v:.0f} rpm"
            if n:
                txt += f" over {n} {pt}"
            lines.append(f"<span style='color:{color}'>{txt}</span>")
        self._plot_htext.setHtml(
            "<div style='font-size:8pt; line-height:120%;'>"
            + "<br>".join(lines) + "</div>")
        # Start below the floating chip bar (widgets paint over scene items);
        # anchor flips to keep the box on-screen near the right edge
        clear_px = self._chip_bar.y() + self._chip_bar.height()
        y_scene = self._plot.mapToScene(QPoint(0, clear_px + 2)).y()
        x_scene = vb.mapViewToScene(QPointF(float(idx), 0.0)).x()
        try:
            self._plot_htext.setAnchor(
                (1, 0) if idx > len(apps) * 0.55 else (0, 0))
        except AttributeError:
            self._plot_htext.anchor = pg.Point(
                (1, 0) if idx > len(apps) * 0.55 else (0, 0))
            self._plot_htext.updateTextPos()
        self._plot_vline.setPos(idx)
        self._plot_vline.show()
        self._plot_htext.setPos(x_scene, y_scene)
        self._plot_htext.show()

    def _rebuild_spin_menu(self, pitch_types: List[str]):
        """Rebuild the per-pitch spin overlay entries in the Stats ▾ menu for
        the current pitcher's arsenal (usage-sorted), each colored by pitch
        type. Preserves the checked state of pitches carried over."""
        menu = self._chip_stat_menu.menu()
        prev = {pt for pt, a in self._spin_actions.items() if a.isChecked()}
        for act in self._spin_actions.values():
            menu.removeAction(act)
        self._spin_actions.clear()
        if self._spin_sep is not None:
            menu.removeAction(self._spin_sep)
            self._spin_sep = None
        if not pitch_types:
            return
        self._spin_sep = menu.addSeparator()
        for pt in pitch_types:
            color = PITCH_COLORS.get(pt, _PITCH_COLOR_DEFAULT)
            act = QAction(f"{pt} spin", menu)
            act.setCheckable(True)
            sw = QPixmap(10, 10)
            sw.fill(QColor(color))
            act.setIcon(QIcon(sw))
            if pt in prev:
                act.setChecked(True)
            act.toggled.connect(self._render_plot)
            menu.addAction(act)
            self._spin_actions[pt] = act

    def _render_arsenal(self):
        """Per-pitch Stuff+ / velo / spin quick-card from the SP deep card."""
        table = self._ars_table
        rows = (self._card or {}).get("rows") or []
        # Refresh the spin-overlay menu to this pitcher's arsenal (usage order)
        self._rebuild_spin_menu([r.get("pitch_type") for r in rows
                                 if r.get("pitch_type")])
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            pt = row.get("pitch_type", "?")
            use = row.get("usage")
            # Condensed first cell: pitch code + usage% (frees a column for
            # the spin-vs-league delta)
            name_txt = pt if use is None else f"{pt} {use:.0f}%"
            name_item = QTableWidgetItem(name_txt)
            name_item.setForeground(
                QColor(PITCH_COLORS.get(pt, _PITCH_COLOR_DEFAULT)))
            f = name_item.font()
            f.setBold(True)
            name_item.setFont(f)
            name_item.setToolTip(row.get("pitch", ""))

            def cell(text):
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
                return it

            stuff = row.get("stuff")
            velo = row.get("velo")
            spin = row.get("spin")
            stuff_item = cell("" if stuff is None else f"{stuff:.0f}")
            if stuff is not None:
                if stuff >= 105:
                    stuff_item.setForeground(self._GREEN)
                elif stuff <= 95:
                    stuff_item.setForeground(self._RED)
            # Spin delta vs the league average for this pitch type
            lg = LEAGUE_SPIN.get(pt)
            if spin is None or lg is None:
                dspin_item = cell("")
            else:
                d = spin - lg
                dspin_item = cell(f"{d:+.0f}")
                if d >= 100:
                    dspin_item.setForeground(self._GREEN)
                elif d <= -100:
                    dspin_item.setForeground(self._RED)
            cells = [name_item,
                     stuff_item,
                     cell("" if velo is None else f"{velo:.1f}"),
                     cell("" if spin is None else f"{spin:.0f}"),
                     dspin_item]
            for c, it in enumerate(cells):
                table.setItem(r, c, it)
        self._fit(table, cap_height=True)
        # spin menu was just rebuilt for this arsenal — re-render so any
        # carried-over spin selections draw
        self._render_plot()

    def _update_chip_legend(self):
        parts = [f"<span style='color:{c}'>{lbl}</span>"
                 for k, lbl, c in self._OVERLAYS
                 if self._chip_btns[k].isChecked()]
        parts += [f"<span style='color:"
                  f"{PITCH_COLORS.get(pt, _PITCH_COLOR_DEFAULT)}'>{pt} spin"
                  "</span>"
                  for pt, a in self._spin_actions.items() if a.isChecked()]
        self._chip_legend.setText("&nbsp;·&nbsp;".join(parts))
        self._chip_legend.setVisible(bool(parts))
        self._place_chip_bar()

    def _render_plot(self, *_):
        import numpy as np
        self._update_chip_legend()
        p = self._plot.getPlotItem()
        p.clear()
        self._vb_rate.clear()
        self._vb_velo.clear()
        # clear() drops the crosshair line — re-add it (hidden). The hover
        # readout is a top-level scene item, untouched by clear().
        p.addItem(self._plot_vline, ignoreBounds=True)
        self._plot_vline.hide()
        self._plot_htext.hide()
        apps = (self._form or {}).get("apps") or []
        if not apps:
            p.showAxis("left", False)
            p.showAxis("right", False)
            return
        sc = self._sc or {}
        x = np.arange(len(apps))
        # Bars are optional now — pin the shared x-range explicitly so the
        # overlay lines don't collapse the axis when K is off
        p.getViewBox().setXRange(-0.5, len(apps) - 0.5, padding=0.02)

        # K bars on the main viewbox (own scale, no axis — headroom keeps
        # the tallest bar clear of the value labels)
        if self._chip_btns["k"].isChecked():
            ks = np.array([float(a["k"] or 0) for a in apps])
            p.addItem(pg.BarGraphItem(x=x, height=ks, width=0.66,
                                      brush=(46, 204, 113, 90), pen=None))
            for i, k in enumerate(ks):
                if k > 0:
                    t = pg.TextItem(f"{k:g}", color=(140, 200, 160),
                                    anchor=(0.5, 1.0))
                    t.setPos(i, k)
                    p.addItem(t)
            p.getViewBox().setYRange(0, max(ks.max(), 1) * 1.3, padding=0)

        # Bottom axis: MM-DD per start, thinned when crowded
        step = max(1, (len(apps) + 5) // 6)
        ticks = [(i, apps[i]["date"][5:]) for i in range(0, len(apps), step)]
        p.getAxis("bottom").setTicks([ticks])

        # Overlay lines from the per-date Statcast joins
        def series(key):
            vals = [(sc.get(key) or {}).get(a["date"]) for a in apps]
            return np.array([np.nan if v is None else float(v)
                             for v in vals])

        def add_line(vb, key, color):
            y = series(key)
            if not np.isfinite(y).any():
                return False
            vb.addItem(pg.PlotCurveItem(
                x=x, y=y, connect="finite",
                pen=pg.mkPen(color, width=2)))
            fin = y[np.isfinite(y)]
            lo, hi = float(fin.min()), float(fin.max())
            pad = max((hi - lo) * 0.15, 0.01)
            vb._auto_range = (lo - pad, hi + pad)
            return True

        colors = dict((k, c) for k, _, c in self._OVERLAYS)
        rate_keys = [k for k in ("csw", "whiff", "xw", "hh", "brl")
                     if self._chip_btns[k].isChecked()]
        rate_on = False
        lo, hi = np.inf, -np.inf
        for key in rate_keys:
            if add_line(self._vb_rate, key, colors[key]):
                rate_on = True
                lo = min(lo, self._vb_rate._auto_range[0])
                hi = max(hi, self._vb_rate._auto_range[1])
        if rate_on:
            self._vb_rate.setYRange(lo, hi, padding=0)
        # mph axis (right) is shared by FB velo and avg EV allowed
        self._vb_spin.clear()
        velo_on = False
        lo_v, hi_v = np.inf, -np.inf
        for key in ("velo", "ev"):
            if (self._chip_btns[key].isChecked()
                    and add_line(self._vb_velo, key, colors[key])):
                velo_on = True
                lo_v = min(lo_v, self._vb_velo._auto_range[0])
                hi_v = max(hi_v, self._vb_velo._auto_range[1])
        if velo_on:
            self._vb_velo.setYRange(lo_v, hi_v, padding=0)
        # Per-pitch spin (RPM) on the shared spin viewbox — every selected
        # pitch draws its own line in its pitch color. The RPM axis spans all
        # active spin series so e.g. a 1800-rpm change and a 2800-rpm curve
        # both read true; borrows the right axis when no mph overlay is on.
        sc_spin = sc.get("spin_by_pitch") or {}
        spin_lo, spin_hi = np.inf, -np.inf
        for pt, act in self._spin_actions.items():
            if not act.isChecked():
                continue
            d = sc_spin.get(pt) or {}
            y = np.array([np.nan if d.get(a["date"]) is None
                          else float(d[a["date"]]) for a in apps])
            if not np.isfinite(y).any():
                continue
            self._vb_spin.addItem(pg.PlotCurveItem(
                x=x, y=y, connect="finite",
                pen=pg.mkPen(PITCH_COLORS.get(pt, _PITCH_COLOR_DEFAULT),
                             width=2)))
            fin = y[np.isfinite(y)]
            spin_lo = min(spin_lo, float(fin.min()))
            spin_hi = max(spin_hi, float(fin.max()))
        spin_on = spin_lo != np.inf
        if spin_on:
            pad = max((spin_hi - spin_lo) * 0.15, 30)
            self._vb_spin.setYRange(spin_lo - pad, spin_hi + pad, padding=0)
        right_axis = p.getAxis("right")
        if velo_on:
            right_axis.linkToView(self._vb_velo)
        elif spin_on:
            right_axis.linkToView(self._vb_spin)
        p.showAxis("left", rate_on)
        p.showAxis("right", velo_on or spin_on)
        self._sync_plot_vbs()

    def _render(self):
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

        sc = self._sc or {}
        velo_season = sc.get("velo_season")

        table = self._starts_table
        table.setRowCount(0)
        table.clearSpans()
        apps = (self._form or {}).get("apps") or []
        shown = apps if self._show_all_starts else apps[-self._N_APPS:]
        has_toggle = len(apps) > self._N_APPS
        r = -1
        for r, a in enumerate(reversed(shown)):
            table.insertRow(r)
            opp = ("vs " if a["is_home"] else "@ ") + a["opp"]
            if not a["started"]:
                opp += " ·R"
            velo = (sc.get("velo") or {}).get(a["date"])
            velo_cell = cell("" if velo is None else f"{velo:.1f}")
            if velo is not None and velo_season is not None:
                if velo - velo_season <= -0.8:
                    bold(velo_cell, self._RED)
                elif velo - velo_season >= 0.5:
                    velo_cell.setForeground(self._GREEN)
            csw = (sc.get("csw") or {}).get(a["date"])
            csw_cell = cell("" if csw is None else f"{csw * 100:.0f}%")
            if csw is not None:
                if csw >= 0.32:
                    csw_cell.setForeground(self._GREEN)
                elif csw <= 0.24:
                    csw_cell.setForeground(self._RED)
            er_cell = cell("" if a["er"] is None else str(a["er"]))
            if (a["er"] or 0) >= 5:
                bold(er_cell, self._RED)
            row_cells = [
                cell(a["date"][5:], align_right=False),
                cell(opp, align_right=False),
                cell(a["ip"]), cell(str(a["h"] if a["h"] is not None else "")),
                er_cell,
                cell(str(a["bb"] if a["bb"] is not None else "")),
                cell(str(a["k"] if a["k"] is not None else "")),
                cell(str(a["hr"] if a["hr"] is not None else "")),
                cell(str(a["np"] if a["np"] is not None else "")),
                cell(str(a["bf"] if a["bf"] is not None else "")),
                velo_cell, csw_cell,
            ]
            for c, item in enumerate(row_cells):
                table.setItem(r, c, item)
        # In-table expander (same pattern as the matchup pitch toggle).
        # Collapsed -> appended after the 4 shown; expanded -> inserted at
        # row 0 (as the last of 20 rows it would clip off the panel bottom
        # and be unclickable)
        if has_toggle:
            tr = 0 if self._show_all_starts else r + 1
            table.insertRow(tr)
            toggle = cell(
                f"  ▾ last {self._N_APPS} only" if self._show_all_starts
                else f"  ▸ {len(apps) - len(shown)} earlier starts",
                align_right=False)
            toggle.setForeground(QColor(127, 140, 141))
            table.setItem(tr, 0, toggle)
            table.setSpan(tr, 0, 1, table.columnCount())
            self._starts_toggle_row = tr
        else:
            self._starts_toggle_row = -1
        self._fit(table, cap_height=True)
        self._update_info()

        table = self._tto_table
        table.setRowCount(0)
        for r, b in enumerate(sc.get("tto") or []):
            table.insertRow(r)
            woba = b["woba"]
            woba_cell = cell("" if woba is None else f"{woba:.3f}".lstrip("0"))
            if woba is not None and woba >= 0.340:
                bold(woba_cell, self._ORANGE)
            xw = b["xw"]
            hh, brl, ev = b.get("hh"), b.get("brl"), b.get("ev")
            hh_cell = cell("" if hh is None else f"{hh * 100:.0f}%")
            if hh is not None and hh >= 0.45:
                bold(hh_cell, self._RED)
            brl_cell = cell("" if brl is None else f"{brl * 100:.0f}%")
            if brl is not None and brl >= 0.10:
                bold(brl_cell, self._RED)
            row_cells = [
                cell(b["label"], align_right=False),
                cell(str(b["pa"])), woba_cell,
                cell("" if xw is None else f"{xw:.3f}".lstrip("0")),
                cell(f"{b['k_pct'] * 100:.0f}%"),
                cell("" if b.get("bb_pct") is None
                     else f"{b['bb_pct'] * 100:.0f}%"),
                cell("" if ev is None else f"{ev:.0f}"),
                hh_cell,
                brl_cell,
                cell(str(b["hr"])),
            ]
            for c, item in enumerate(row_cells):
                table.setItem(r, c, item)
        self._fit(table, cap_height=True)
        self._render_plot()

    def _on_starts_cell_clicked(self, row, _col):
        if row == self._starts_toggle_row and row >= 0:
            self._show_all_starts = not self._show_all_starts
            self._render()

    # -------------------------------------------------- pitch-shape plots

    def _on_mean_clicked(self, _item, points, *_):
        if points:
            self._open_flight_viewer(points[0].data())

    def _open_flight_viewer(self, start_pitch: str = ""):
        """Pop out the 3D arsenal flight viewer (Phase 3)."""
        if not (self._mv or {}).get("pitches"):
            return
        try:
            from sp_flight_viewer import SPFlightWindow
        except Exception as e:
            print(f"PitcherFormPanel: flight viewer unavailable: {e}")
            return
        self._flight_win = SPFlightWindow(
            self._name, self._mv["pitches"],
            self._mv.get("sz_top", 3.4), self._mv.get("sz_bot", 1.6),
            PITCH_COLORS, PITCH_ABBREV, start_pitch=start_pitch)
        self._flight_win.show()

    def _render_shapes(self):
        mv_p = self._mv_plot.getPlotItem()
        mv_p.clear()
        pitches = (self._mv or {}).get("pitches") or []
        if not pitches:
            return

        grid_pen = pg.mkPen(60, 70, 82, width=1)
        # Movement plot: zero-cross lines + per-pitch scatter and mean dots
        mv_p.addItem(pg.InfiniteLine(angle=0, pen=grid_pen))
        mv_p.addItem(pg.InfiniteLine(angle=90, pen=grid_pen))
        # Standard ±25" break-chart frame — stray outliers clip rather than
        # zooming the clusters out
        lim = 25.0
        for pt in pitches:
            code = PITCH_ABBREV.get(pt["pitch"], pt["pitch"])
            color = QColor(PITCH_COLORS.get(code, _PITCH_COLOR_DEFAULT))
            if pt["mv"]:
                xs = [m[0] for m in pt["mv"]]
                ys = [m[1] for m in pt["mv"]]
                c = QColor(color)
                c.setAlpha(70)
                mv_p.addItem(pg.ScatterPlotItem(
                    x=xs, y=ys, size=4, brush=pg.mkBrush(c), pen=None))
            # Mean marker + label double as the legend; clicking one pops
            # the 3D flight viewer on that pitch
            mean = pg.ScatterPlotItem(
                x=[pt["mean_hb"]], y=[pt["mean_ivb"]], size=11,
                brush=pg.mkBrush(color), pen=pg.mkPen(20, 25, 32, width=2),
                data=[pt["pitch"]])
            mean.sigClicked.connect(self._on_mean_clicked)
            mv_p.addItem(mean)
            label = pg.TextItem(code, color=color, anchor=(0.5, 1.15))
            label.setPos(pt["mean_hb"], pt["mean_ivb"])
            mv_p.addItem(label)
        mv_p.getViewBox().setRange(xRange=(-lim, lim), yRange=(-lim, lim),
                                   padding=0)

    @staticmethod
    def _fit(table: QTableWidget, cap_height: bool = False):
        table.resizeRowsToContents()
        table.resizeColumnsToContents()
        w = 2 * table.frameWidth() + 2
        w += sum(table.columnWidth(c) for c in range(table.columnCount()))
        table.setMaximumWidth(w)
        table.setMinimumWidth(w)   # scrollbars are off — don't let the
                                   # plot's stretch squeeze columns away
        if cap_height:
            # Top-aligned table: hug the rows so no empty frame trails below
            h = 2 * table.frameWidth() + table.horizontalHeader().height()
            h += sum(table.rowHeight(r) for r in range(table.rowCount()))
            table.setMaximumHeight(h)


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


# ===========================================================================
# 5. EFFORTMLB WINDOW — standalone MLB viewer (no odds table)
#    Top: game banner (today's slate, one card per game, click to select).
#    Left: lineup rail (both teams' SP + batting order — click to load).
#    Middle: pitcher half — SP form panel + bullpen panel, full height.
#    Right: batter half — Player Detail / Advanced Stats tabs.
# ===========================================================================

from PyQt6.QtWidgets import (QMainWindow, QSplitter, QTabWidget, QListWidget,
                             QListWidgetItem, QStyledItemDelegate, QStyle)

# Default market/line a rail click summarizes with — the detail panel's
# stat/line controls re-summarize from there
RAIL_BATTER_MARKET = ("batter_total_bases", 1.5)
RAIL_PITCHER_MARKET = ("pitcher_strikeouts", 4.5)


class GameCard(QFrame):
    """One game's slice of the top banner: teams + time/score + probables."""

    clicked = pyqtSignal(int)

    def __init__(self, idx: int, game: dict, teams: Dict[int, str],
                 parent=None):
        super().__init__(parent)
        self.idx = idx
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        away = game.get("teams", {}).get("away", {})
        home = game.get("teams", {}).get("home", {})
        a_abbr = teams.get((away.get("team") or {}).get("id"), "?")
        h_abbr = teams.get((home.get("team") or {}).get("id"), "?")

        status = (game.get("status") or {}).get("abstractGameState", "")
        when = ""
        try:
            dt = datetime.fromisoformat(
                game.get("gameDate", "").replace("Z", "+00:00")).astimezone()
            when = dt.strftime("%H:%M")
        except ValueError:
            pass
        if status == "Live":
            mid = (f"<span style='color:#E74C3C'>{away.get('score', 0)}-"
                   f"{home.get('score', 0)} LIVE</span>")
        elif status == "Final":
            mid = (f"<span style='color:#7F8C8D'>{away.get('score', 0)}-"
                   f"{home.get('score', 0)} F</span>")
        else:
            mid = f"<span style='color:#95A5A6'>{when}</span>"

        def sp_last(side):
            name = ((side.get("probablePitcher") or {}).get("fullName") or "")
            return name.split()[-1] if name else "TBD"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 1, 4, 1)
        lay.setSpacing(0)
        top = QLabel(f"<b>{a_abbr}</b> @ <b>{h_abbr}</b>  {mid}")
        top.setTextFormat(Qt.TextFormat.RichText)
        top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bot = QLabel(f"<span style='color:#dc9437'>{sp_last(away)} · "
                     f"{sp_last(home)}</span>")
        bot.setTextFormat(Qt.TextFormat.RichText)
        bot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for lbl in (top, bot):
            lbl.setStyleSheet("font-size: 8pt; background: transparent;"
                              " border: none;")
            lay.addWidget(lbl)

    def mousePressEvent(self, ev):
        self.clicked.emit(self.idx)
        super().mousePressEvent(ev)

    def set_selected(self, on: bool):
        self.setProperty("selected", on)
        self.style().unpolish(self)
        self.style().polish(self)


class GameBanner(QWidget):
    """Full-width strip of today's games; each card shares the width
    equally. Replaces the old prop-type dropdown / fetch button / progress
    bar row."""

    game_selected = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setStyleSheet("""
            GameCard { background: #151a21; border: 1px solid #2C3E50;
                       border-radius: 4px; }
            GameCard[selected="true"] { background: #1E2A38;
                                        border: 1px solid #dc9437; }
        """)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(2, 2, 2, 2)
        self._lay.setSpacing(3)
        self._cards: List[GameCard] = []
        self._games: List[dict] = []
        self._placeholder = QLabel("Loading today's slate…")
        self._placeholder.setStyleSheet("color: #7F8C8D;")
        self._lay.addWidget(self._placeholder,
                            alignment=Qt.AlignmentFlag.AlignCenter)

    def set_games(self, games: List[dict], teams: Dict[int, str]):
        while self._lay.count():
            item = self._lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards, self._games = [], list(games)
        if not games:
            lbl = QLabel("No MLB games today")
            lbl.setStyleSheet("color: #7F8C8D;")
            self._lay.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignCenter)
            return
        for i, g in enumerate(games):
            card = GameCard(i, g, teams)
            card.clicked.connect(self.select)
            self._cards.append(card)
            self._lay.addWidget(card, stretch=1)

    def select(self, idx: int):
        if not (0 <= idx < len(self._games)):
            return
        for i, c in enumerate(self._cards):
            c.set_selected(i == idx)
        self.game_selected.emit(self._games[idx])


PID_ROLE = Qt.ItemDataRole.UserRole + 1
BATTER_ROLE = Qt.ItemDataRole.UserRole + 2      # True on batter rows
STATS_ROLE = Qt.ItemDataRole.UserRole + 3       # FG value dict or None


class LineupCardDelegate(QStyledItemDelegate):
    """Paints batter rows as a mugshot + name with a 2x2 grid of value stats
    (wRC+ / Def / WPA / BsR) beneath, color-coded good/bad. Header, pitcher,
    and roster-note rows fall back to the default list rendering."""

    ICON = 30
    PAD = 4
    NAME_H = 15
    CELL_H = 13
    _GREY = QColor("#7F8C8D")

    def _is_card(self, index):
        return bool(index.data(BATTER_ROLE))

    def sizeHint(self, option, index):
        if self._is_card(index):
            return QSize(120, self.PAD + self.NAME_H + 2 * self.CELL_H + 3)
        return super().sizeHint(option, index)

    @staticmethod
    def _abbrev_name(full: str) -> str:
        """'Aaron Judge' -> 'A. Judge'; leaves a single token untouched."""
        parts = full.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}. {' '.join(parts[1:])}"
        return full

    @staticmethod
    def _heat(v, hi, lo):
        if v is None:
            return QColor("#95A5A6")
        if v >= hi:
            return QColor("#2ECC71")
        if v <= lo:
            return QColor("#E74C3C")
        return QColor("#BDC3C7")

    def paint(self, painter, option, index):
        if not self._is_card(index):
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(rect, QColor("#1E2A38"))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, QColor("#182029"))

        # mugshot
        ix = rect.left() + self.PAD
        iy = rect.top() + self.PAD
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, QRect(ix, iy, self.ICON, self.ICON))
        tx = ix + self.ICON + 6
        avail = rect.right() - tx - 4

        # name: keep the "{slot} {pos}" prefix but abbreviate the player to
        # "F. Last" (e.g. A. Judge) so it fits the narrow rail
        name = (index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n")[0]
        data = index.data(Qt.ItemDataRole.UserRole)
        full = data[0] if data else ""
        if full and name.endswith(full):
            name = name[:-len(full)] + self._abbrev_name(full)
        f = painter.font()
        f.setPointSize(8)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor("#dc9437") if selected else QColor("#E6E9EA"))
        ny = rect.top() + self.PAD
        painter.drawText(QRect(tx, ny, avail, self.NAME_H),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft), name)

        # 2x2 stat grid
        stats = index.data(STATS_ROLE) or {}
        colw = avail // 2
        cells = [
            ("wRC+", stats.get("wrcplus"),
             lambda v: f"{v:.0f}", self._heat(stats.get("wrcplus"), 110, 90)),
            ("Def", stats.get("defense"),
             lambda v: f"{v:+.1f}", self._heat(stats.get("defense"), 0.1, -0.1)),
            ("WPA", stats.get("wpa"),
             lambda v: f"{v:+.2f}", self._heat(stats.get("wpa"), 0.01, -0.01)),
            ("BsR", stats.get("bsr"),
             lambda v: f"{v:+.1f}", self._heat(stats.get("bsr"), 0.1, -0.1)),
        ]
        gy = ny + self.NAME_H
        for i, (label, val, fmt, color) in enumerate(cells):
            cx = tx + (i % 2) * colw
            cy = gy + (i // 2) * self.CELL_H
            lf = painter.font()
            lf.setPointSize(7)
            lf.setBold(False)
            painter.setFont(lf)
            painter.setPen(self._GREY)
            painter.drawText(QRect(cx, cy, colw, self.CELL_H),
                             int(Qt.AlignmentFlag.AlignVCenter
                                 | Qt.AlignmentFlag.AlignLeft), label)
            lw = painter.fontMetrics().horizontalAdvance(label) + 3
            vf = painter.font()
            vf.setPointSize(8)
            vf.setBold(True)
            painter.setFont(vf)
            painter.setPen(color)
            txt = "—" if val is None else fmt(val)
            painter.drawText(QRect(cx + lw, cy, colw - lw, self.CELL_H),
                             int(Qt.AlignmentFlag.AlignVCenter
                                 | Qt.AlignmentFlag.AlignLeft), txt)
        painter.restore()


class LineupRail(QListWidget):
    """Narrow nav column: both teams' probable SP + batting order for the
    selected game. Clicking a row loads that player into the detail panel."""

    player_selected = pyqtSignal(str, bool)   # (full name, is_pitcher)

    PID_ROLE = PID_ROLE
    BATTER_ROLE = BATTER_ROLE
    STATS_ROLE = STATS_ROLE

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(158)
        self.setMaximumWidth(240)
        self.setIconSize(QSize(30, 30))
        self.setMouseTracking(True)   # delegate hover highlight
        self.setStyleSheet("""
            QListWidget { background: #151a21; color: #D5DBDB;
                          font-size: 8pt; border: 1px solid #2C3E50;
                          outline: none; }
            QListWidget::item { padding: 1px 4px; }
            QListWidget::item:selected { background: #1E2A38;
                                         color: #dc9437; }
        """)
        self.setItemDelegate(LineupCardDelegate(self))
        self.itemClicked.connect(self._on_item)

    def _on_item(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.player_selected.emit(*data)

    def _header(self, text: str):
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        it.setForeground(QColor("#dc9437"))
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        self.addItem(it)

    def _player_row(self, text: str, name: str, is_pitcher: bool,
                    pid: Optional[int] = None):
        it = QListWidgetItem(text)
        it.setData(Qt.ItemDataRole.UserRole, (name, is_pitcher))
        if pid:
            it.setData(self.PID_ROLE, pid)
        # Batter rows render via LineupCardDelegate (mugshot + 2x2 stat grid);
        # the FG values land later in _decorate_rail under STATS_ROLE
        it.setData(self.BATTER_ROLE, not is_pitcher)
        self.addItem(it)

    def set_game(self, game: dict, stats: MLBPropStats):
        self.clear()
        for side, key in (("away", "awayPlayers"), ("home", "homePlayers")):
            team = game.get("teams", {}).get(side, {})
            tid = (team.get("team") or {}).get("id")
            abbr = stats._teams.get(tid, "?")
            self._header(f"{abbr} ({side})")
            sp = team.get("probablePitcher") or {}
            if sp.get("fullName"):
                self._player_row(f"SP  {sp['fullName']}", sp["fullName"],
                                 True, sp.get("id"))
            players = (game.get("lineups") or {}).get(key) or []
            if players:
                for slot, p in enumerate(players, 1):
                    pos = (p.get("primaryPosition") or {}).get(
                        "abbreviation", "")
                    self._player_row(f"{slot}  {pos:<3} {p.get('fullName')}",
                                     p.get("fullName", ""), False,
                                     p.get("id"))
            else:
                # Lineup not posted yet — fall back to the team's roster bats
                note = QListWidgetItem("   lineup TBD — roster:")
                note.setFlags(Qt.ItemFlag.NoItemFlags)
                note.setForeground(QColor("#7F8C8D"))
                self.addItem(note)
                bats = sorted(
                    (r for r in stats._roster.values()
                     if r["team_id"] == tid and r["position"] not in ("P",)),
                    key=lambda r: r["name"])
                for r in bats:
                    self._player_row(f"   {r['position']:<3} {r['name']}",
                                     r["name"], False, r["id"])


class MLBWindow(QMainWindow):
    """Standalone MLB viewer: game banner on top; lineup rail | pitcher half
    (SP form + bullpen) | batter half (Player Detail / Advanced Stats)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EffortMLB")
        self.stats = MLBPropStats()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(3)

        self.banner = GameBanner()
        self.banner.game_selected.connect(self._on_game_selected)
        root.addWidget(self.banner)

        self.rail = LineupRail()
        self.rail.player_selected.connect(self._on_player_selected)

        # Pitcher half: SP form on top, bullpen below — full window height
        self.pitcher_form_panel = PitcherFormPanel(self.stats)
        self.bullpen_panel = BullpenPanel(self.stats)
        pitcher_split = QSplitter(Qt.Orientation.Vertical)
        pitcher_split.addWidget(self.pitcher_form_panel)
        pitcher_split.addWidget(self.bullpen_panel)
        pitcher_split.setStretchFactor(0, 2)
        pitcher_split.setStretchFactor(1, 1)

        # Batter half: detail panel + advanced stats tabs
        self.player_detail_panel = PlayerDetailPanel()
        self.player_detail_panel.stat_requested.connect(
            self._on_detail_stat_requested)
        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._detail_scroll.setWidget(self.player_detail_panel)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self._detail_scroll, "Player Detail")
        # Advanced Stats tab is added in _init_async — its widget needs a
        # running event loop at construction
        self.advanced_stats_widget = None

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(self.rail)
        self.main_splitter.addWidget(pitcher_split)
        self.main_splitter.addWidget(self.detail_tabs)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 1)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setCollapsible(2, False)
        # Pen table needs ~882px to show every column without scrolling;
        # detail panel min is ~820 — split the 1900px budget accordingly
        self.main_splitter.setSizes([178, 890, 838])
        root.addWidget(self.main_splitter, stretch=1)

        # Async init once the qasync loop is running
        QTimer.singleShot(0, lambda: asyncio.create_task(self._init_async()))
        self._cap_window_to_screen()

    # -------------------------------------------------------- window sizing

    def _cap_window_to_screen(self):
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.setMaximumSize(geo.width(), geo.height())
        if (self.width() > geo.width()) or (self.height() > geo.height()):
            self.resize(min(self.width(), geo.width()),
                        min(self.height(), geo.height()))

    def moveEvent(self, a0):
        self._cap_window_to_screen()
        super().moveEvent(a0)

    # ------------------------------------------------------------ async init

    async def _init_async(self):
        try:
            from TrackingStatsWidget import AdvancedStatsWidget
            self.advanced_stats_widget = AdvancedStatsWidget()
            self.advanced_stats_widget.set_sport("baseball_mlb")
            self.detail_tabs.addTab(self.advanced_stats_widget,
                                    "Advanced Stats")
        except Exception as e:
            print(f"EffortMLB: Advanced Stats tab unavailable: {e}")
        try:
            async with aiohttp.ClientSession() as session:
                if not await self.stats.ensure_roster(session):
                    print("EffortMLB: roster load failed")
                    return
                games = await self.stats._get_schedule(session)
        except Exception as e:
            print(f"EffortMLB: init failed: {e}")
            return
        self.banner.set_games(games, self.stats._teams)
        if games:
            self.banner.select(0)
        asyncio.create_task(self._load_percentile_data())
        asyncio.create_task(self._populate_bullpen_teams())

    async def _load_percentile_data(self):
        try:
            from MLBpercentilerankings import (fetch_leaderboard_data,
                                               PITCHER_URL, HITTER_URL)
            loop = asyncio.get_event_loop()
            hitters = await loop.run_in_executor(
                None, fetch_leaderboard_data, HITTER_URL)
            pitchers = await loop.run_in_executor(
                None, fetch_leaderboard_data, PITCHER_URL)
            self.player_detail_panel.set_percentile_data(hitters, pitchers)
            self.pitcher_form_panel.set_percentile_data(pitchers)
        except Exception as e:
            print(f"EffortMLB: percentile data load failed: {e}")

    async def _populate_bullpen_teams(self):
        try:
            async with aiohttp.ClientSession() as session:
                if await self.stats.ensure_roster(session):
                    self.bullpen_panel.set_teams(
                        list(self.stats._teams.values()))
        except Exception as e:
            print(f"EffortMLB: bullpen team list load failed: {e}")

    # ------------------------------------------------------- game selection

    def _on_game_selected(self, game: dict):
        self.rail.set_game(game, self.stats)
        self._rail_gen = getattr(self, "_rail_gen", 0) + 1
        asyncio.create_task(self._decorate_rail(self._rail_gen))
        # Seed the pitcher half with the away probable until a click refines
        away = game.get("teams", {}).get("away", {})
        home = game.get("teams", {}).get("home", {})
        sp = away.get("probablePitcher") or {}
        h_abbr = self.stats._teams.get(
            (home.get("team") or {}).get("id"), "?")
        a_abbr = self.stats._teams.get(
            (away.get("team") or {}).get("id"), "?")
        if sp.get("id"):
            self.pitcher_form_panel.show_pitcher(
                sp["id"], sp.get("fullName", "?"),
                context=f"{a_abbr} probable — @ {h_abbr}")
        self.bullpen_panel.show_team(a_abbr, f"{a_abbr} pen")

    async def _decorate_rail(self, gen: int):
        """Fill the lineup rail with mugshots and a wRC+/Def/WPA line per
        batter (FG batting board, cached). Aborts silently when the rail is
        rebuilt mid-flight (game switch)."""
        HEADSHOT_DIR.mkdir(exist_ok=True)
        try:
            async with aiohttp.ClientSession() as session:
                for i in range(self.rail.count()):
                    if gen != self._rail_gen:
                        return
                    it = self.rail.item(i)
                    pid = it.data(LineupRail.PID_ROLE)
                    if not pid:
                        continue
                    path = HEADSHOT_DIR / f"{pid}.png"
                    if not path.exists():
                        try:
                            async with session.get(
                                    HEADSHOT_URL.format(pid=pid),
                                    timeout=aiohttp.ClientTimeout(total=10)
                                    ) as resp:
                                if resp.status == 200:
                                    path.write_bytes(await resp.read())
                        except Exception:
                            pass
                    data = it.data(Qt.ItemDataRole.UserRole)
                    fgb = None
                    if data and not data[1]:      # batter → FG value line
                        try:
                            fgb = await self.stats.get_fg_batting(pid)
                        except Exception:
                            fgb = None
                    if gen != self._rail_gen:
                        return
                    if path.exists():
                        it.setIcon(QIcon(str(path)))
                    if fgb:
                        # Store the 4 value stats for the delegate's 2x2 grid
                        it.setData(LineupRail.STATS_ROLE, {
                            "wrcplus": fgb.get("wrcplus"),
                            "defense": fgb.get("defense"),
                            "wpa": fgb.get("wpa"),
                            "bsr": fgb.get("bsr"),
                        })
        except RuntimeError:
            return   # rail items deleted mid-decoration (game switched)

    # ------------------------------------------------------ player selection

    def _on_player_selected(self, name: str, is_pitcher: bool):
        market, line = (RAIL_PITCHER_MARKET if is_pitcher
                        else RAIL_BATTER_MARKET)
        asyncio.create_task(self._load_detail_summary(name, market, line))

    def _on_detail_stat_requested(self, market_key, line):
        player = self.player_detail_panel.current_player_name()
        if player:
            asyncio.create_task(
                self._load_detail_summary(player, market_key, line))

    async def _load_detail_summary(self, player, market_key, line):
        try:
            async with aiohttp.ClientSession() as session:
                summary = await self.stats.summarize(
                    session, player, market_key, line)
        except Exception as e:
            print(f"EffortMLB: summarize failed for {player}: {e}")
            return
        if summary is not None:
            self._show_player_detail(summary)

    # ---------------------------------------------- detail panel orchestration
    # (same load fan-out the props window ran on prop-row click)

    def _show_player_detail(self, summary):
        self.player_detail_panel.show_summary(summary)
        self.detail_tabs.setCurrentWidget(self._detail_scroll)
        asyncio.create_task(self._load_matchup(summary))
        asyncio.create_task(self._load_pitch_splits(summary))
        asyncio.create_task(self._load_traditional_stats(summary))
        asyncio.create_task(self._load_situational_splits(summary))

    async def _load_situational_splits(self, summary):
        group = ("pitching" if summary.market_key.startswith("pitcher")
                 else "hitting")
        try:
            async with aiohttp.ClientSession() as session:
                splits = await self.stats.get_situational_splits(
                    session, summary.player_id, group)
        except Exception as e:
            print(f"EffortMLB: situational splits failed: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_situational(splits, group)

    async def _load_traditional_stats(self, summary):
        group = ("pitching" if summary.market_key.startswith("pitcher")
                 else "hitting")
        try:
            async with aiohttp.ClientSession() as session:
                pairs = await self.stats.get_traditional_stats(
                    session, summary.player_id, group)
        except Exception as e:
            print(f"EffortMLB: traditional stats failed: {e}")
            return
        if (pairs and self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_traditional(pairs)
        if group == "hitting":
            try:
                fgb = await self.stats.get_fg_batting(summary.player_id)
            except Exception as e:
                print(f"EffortMLB: FG batting failed: {e}")
                return
            if (fgb and self.player_detail_panel.current_player_name()
                    == summary.player_name):
                extra = []
                if fgb.get("woba") is not None:
                    extra.append(("wOBA", f"{fgb['woba']:.3f}".lstrip("0")))
                if fgb.get("wrcplus") is not None:
                    extra.append(("wRC+", f"{fgb['wrcplus']:.0f}"))
                if fgb.get("war") is not None:
                    extra.append(("WAR", f"{fgb['war']:.1f}"))
                self.player_detail_panel.show_traditional(pairs + extra)
                self.player_detail_panel.show_swing(fgb)

    async def _load_pitch_splits(self, summary):
        player_type = ("pitcher" if summary.market_key.startswith("pitcher")
                       else "batter")
        try:
            async with aiohttp.ClientSession() as session:
                splits = await self.stats.get_pitch_splits(
                    session, summary.player_id, player_type)
                velo_splits = await self.stats.get_velo_splits(
                    session, summary.player_id, player_type)
        except Exception as e:
            print(f"EffortMLB: pitch splits failed: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_pitch_splits(
                splits, player_type, velo_splits)
            try:
                async with aiohttp.ClientSession() as session:
                    pergame = await self.stats.get_per_game_statcast(
                        session, summary.player_id, player_type)
                    spray = await self.stats.get_spray_points(
                        session, summary.player_id, player_type)
            except Exception as e:
                print(f"EffortMLB: per-game/spray failed: {e}")
                pergame, spray = [], []
            if (self.player_detail_panel.current_player_name()
                    == summary.player_name):
                self.player_detail_panel.set_per_game_statcast(pergame)
                self.player_detail_panel.set_spray(spray)

    async def _load_matchup(self, summary):
        is_pitcher_prop = summary.market_key.startswith("pitcher")
        try:
            async with aiohttp.ClientSession() as session:
                ctx = await self.stats.get_matchup(
                    session, summary.team,
                    include_opp_batting=is_pitcher_prop,
                    batter_id=None if is_pitcher_prop else summary.player_id,
                    pitcher_id=summary.player_id if is_pitcher_prop else None)
        except Exception as e:
            print(f"EffortMLB: matchup failed for {summary.team}: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                == summary.player_name):
            self.player_detail_panel.show_matchup(ctx)
        # Pen sync: opposing pen for batters, own pen for pitchers
        pen_team = (summary.team if (is_pitcher_prop or ctx is None)
                    else ctx.opponent)
        context = (f"{summary.player_name} — "
                   + ("opponent pen" if (not is_pitcher_prop and ctx)
                      else "own pen"))
        self.bullpen_panel.show_team(pen_team, context)

        if is_pitcher_prop:
            card_pid, card_name, card_hand = (
                summary.player_id, summary.player_name, None)
        elif ctx is not None and ctx.opp_pitcher_id:
            card_pid, card_name, card_hand = (
                ctx.opp_pitcher_id, ctx.opp_pitcher_name,
                ctx.opp_pitcher_hand)
        else:
            return
        self.pitcher_form_panel.show_pitcher(
            card_pid, card_name, card_hand,
            context=("own form" if is_pitcher_prop
                     else f"opp SP — {summary.player_name}"))
        try:
            async with aiohttp.ClientSession() as session:
                arsenal = None
                if not is_pitcher_prop:
                    arsenal = await self.stats.get_pitch_arsenal(
                        session, card_pid)
                    if (arsenal
                            and self.player_detail_panel.current_player_name()
                            == summary.player_name):
                        self.player_detail_panel.set_opposing_arsenal(
                            card_name, arsenal)
                card = await self.stats.get_sp_deep_card(session, card_pid)
        except Exception as e:
            print(f"EffortMLB: SP card failed for {card_name}: {e}")
            return
        if (self.player_detail_panel.current_player_name()
                != summary.player_name):
            return
        self.player_detail_panel.set_sp_card(card, card_name, card_hand)
        sp_stuff = (card or {}).get("fg")
        if not is_pitcher_prop and ctx is not None:
            if card is not None:
                ctx.opp_pitcher_arm = card.get("arm_angle")
            if sp_stuff:
                ctx.opp_pitcher_stuff = sp_stuff
            self.player_detail_panel.show_matchup(ctx)
            if arsenal and sp_stuff:
                self.player_detail_panel.set_opposing_arsenal(
                    card_name, arsenal, sp_stuff)


def launch():
    """Run the EffortMLB window standalone (qasync event loop)."""
    import sys
    import qasync
    from PyQt6.QtWidgets import QApplication
    # Running as a script loads this module as __main__; alias it so the
    # PropWindowUtils shim / lazy importers reuse it instead of loading a
    # second copy of the module
    sys.modules.setdefault("EffortMLB", sys.modules[__name__])
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MLBWindow()
    window.resize(1900, 1040)
    window.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        sys.argv.remove("--probe")
        asyncio.run(_main())    # legacy CLI data probe
    else:
        launch()
